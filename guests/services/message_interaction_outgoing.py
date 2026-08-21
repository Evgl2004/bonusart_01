"""Формирование исходящих интерактивных сообщений SAGUR.

Модуль отвечает только за согласованный исходящий контракт:

1. создаёт задачу доставки и её интерактивность в одной транзакции;
2. формирует компактные служебные данные версии 2;
3. преобразует единое описание кнопок в структуры Telegram, VK и MAX.

Обработка входящих нажатий вынесена в отдельный контур и здесь не выполняется.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction

from guests.models import DispatchTask, InteractionButtonSet, MessageInteraction


SERVICE_DATA_TYPE = "si"
SERVICE_DATA_VERSION = 2
MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
TELEGRAM_CALLBACK_DATA_LIMIT_BYTES = 64
MAX_INTEGRITY_ISOLATION_OPERATIONS = 64


logger = logging.getLogger(__name__)


class MessageInteractionConfigurationError(ValueError):
    """Ошибка согласованной конфигурации исходящей интерактивности."""


class DispatchTaskAlreadyExists(Exception):
    """Задача с тем же непустым ключом идемпотентности уже существует."""


@dataclass(frozen=True)
class DispatchTaskCreationSpec:
    """Описание одной задачи для пакетного атомарного создания."""

    button_set: str
    interaction_enabled: bool
    dispatch_task_fields: dict[str, Any]


@dataclass
class BulkDispatchTaskCreationResult:
    """Поэлементный результат пакетного создания задач."""

    created_tasks: dict[int, DispatchTask]
    duplicate_positions: set[int]
    errors: dict[int, Exception]


@dataclass
class _IntegrityIsolationBudget:
    """Ограничивает число дополнительных операций после конфликта порции."""

    remaining: int = MAX_INTEGRITY_ISOLATION_OPERATIONS

    def consume(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


@dataclass(frozen=True)
class NormalizedInteractionButton:
    """Платформенно-независимое описание одной кнопки."""

    action: str
    text: str
    service_data: str


BUTTON_LABELS: dict[str, str] = {
    "l": "👍 Нравится",
    "d": "👎 Не нравится",
    "c": "🎟 В купоны",
    "m": "☰ Меню",
}

BUTTON_SET_ROWS: dict[str, tuple[tuple[str, ...], ...]] = {
    InteractionButtonSet.RATING_MENU: (("l", "d"), ("m",)),
    InteractionButtonSet.RATING_COUPONS: (("l", "d"), ("c",)),
}

# Первый символ обозначает фактически нажатую кнопку, оставшиеся символы —
# остальные действия исходного набора. Перечень закрыт контрактом с vtelemax.
COMPOSITE_ACTIONS: dict[str, dict[str, str]] = {
    InteractionButtonSet.RATING_MENU: {
        "l": "ldm",
        "d": "dlm",
        "m": "mld",
    },
    InteractionButtonSet.RATING_COUPONS: {
        "l": "ldc",
        "d": "dlc",
        "c": "cld",
    },
}

TELEGRAM_BUTTON_STYLES: dict[str, str] = {
    "l": "success",
    "d": "danger",
    "c": "primary",
    "m": "primary",
}

VK_BUTTON_COLORS: dict[str, str] = {
    "l": "positive",
    "d": "negative",
    "c": "primary",
    "m": "primary",
}


def interactions_enabled_for_new_task(provider_type: str) -> bool:
    """Проверяет эксплуатационный допуск кнопок для новой задачи.

    Переключатели не применяются к уже созданным интерактивностям и входящим
    нажатиям: их назначение — безопасно остановить только формирование новых
    кнопок без потери ранее отправленных событий.
    """

    if not bool(getattr(settings, "MESSAGE_INTERACTIONS_ENABLED", False)):
        return False
    raw_allowed_providers = getattr(
        settings,
        "MESSAGE_INTERACTIONS_ALLOWED_PROVIDERS",
        set(),
    )
    if isinstance(raw_allowed_providers, str):
        raw_allowed_providers = raw_allowed_providers.split(",")
    allowed_providers = {
        str(value or "").strip().lower()
        for value in raw_allowed_providers
        if str(value or "").strip()
    }
    return str(provider_type or "").strip().lower() in allowed_providers


def _normalize_button_set(button_set: str) -> str:
    """Проверяет набор кнопок и возвращает его строковый код."""

    normalized = str(button_set or "").strip()
    allowed = {
        InteractionButtonSet.NONE,
        InteractionButtonSet.RATING_MENU,
        InteractionButtonSet.RATING_COUPONS,
    }
    if normalized not in allowed:
        raise MessageInteractionConfigurationError(
            f"Неизвестный набор интерактивных кнопок: {normalized!r}."
        )
    return normalized


def build_service_data(*, interaction_id: int, button_set: str, action: str) -> str:
    """Формирует компактный служебный JSON версии 2 для одной кнопки.

    Размер проверяется по UTF-8, поскольку Telegram ограничивает именно число
    байтов в ``callback_data``, а не количество символов Python.
    """

    if isinstance(interaction_id, bool) or not isinstance(interaction_id, int):
        raise MessageInteractionConfigurationError(
            "Идентификатор интерактивности должен быть целым числом."
        )
    if not 1 <= interaction_id <= MAX_SIGNED_BIGINT:
        raise MessageInteractionConfigurationError(
            "Идентификатор интерактивности находится вне диапазона положительного bigint."
        )

    normalized_set = _normalize_button_set(button_set)
    if normalized_set == InteractionButtonSet.NONE:
        raise MessageInteractionConfigurationError(
            "Для набора «Без кнопок» служебные данные сформировать нельзя."
        )

    normalized_action = str(action or "").strip()
    composite_action = COMPOSITE_ACTIONS[normalized_set].get(normalized_action)
    if composite_action is None:
        raise MessageInteractionConfigurationError(
            f"Действие {normalized_action!r} не входит в набор {normalized_set!r}."
        )

    service_data = json.dumps(
        {
            "t": SERVICE_DATA_TYPE,
            "v": SERVICE_DATA_VERSION,
            "i": interaction_id,
            "a": composite_action,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    size_bytes = len(service_data.encode("utf-8"))
    if size_bytes > TELEGRAM_CALLBACK_DATA_LIMIT_BYTES:
        raise MessageInteractionConfigurationError(
            "Служебные данные кнопки превышают предел Telegram: "
            f"{size_bytes} байт вместо {TELEGRAM_CALLBACK_DATA_LIMIT_BYTES}."
        )
    return service_data


def build_normalized_button_rows(
    *,
    interaction_id: int,
    button_set: str,
) -> tuple[tuple[NormalizedInteractionButton, ...], ...]:
    """Строит утверждённую двухрядную раскладку без привязки к платформе."""

    normalized_set = _normalize_button_set(button_set)
    rows = BUTTON_SET_ROWS.get(normalized_set)
    if rows is None:
        raise MessageInteractionConfigurationError(
            "Интерактивность сообщения не может использовать набор «Без кнопок»."
        )

    return tuple(
        tuple(
            NormalizedInteractionButton(
                action=action,
                text=BUTTON_LABELS[action],
                service_data=build_service_data(
                    interaction_id=interaction_id,
                    button_set=normalized_set,
                    action=action,
                ),
            )
            for action in row
        )
        for row in rows
    )


def build_telegram_reply_markup(*, interaction_id: int, button_set: str) -> dict[str, Any]:
    """Формирует встроенную клавиатуру Telegram."""

    rows = build_normalized_button_rows(
        interaction_id=interaction_id,
        button_set=button_set,
    )
    return {
        "inline_keyboard": [
            [
                {
                    "text": button.text,
                    "callback_data": button.service_data,
                    "style": TELEGRAM_BUTTON_STYLES[button.action],
                }
                for button in row
            ]
            for row in rows
        ]
    }


def build_vk_keyboard(*, interaction_id: int, button_set: str) -> dict[str, Any]:
    """Формирует встроенную клавиатуру VK."""

    rows = build_normalized_button_rows(
        interaction_id=interaction_id,
        button_set=button_set,
    )
    return {
        "one_time": False,
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "callback",
                        "label": button.text,
                        "payload": button.service_data,
                    },
                    "color": VK_BUTTON_COLORS[button.action],
                }
                for button in row
            ]
            for row in rows
        ],
    }


def build_max_attachments(*, interaction_id: int, button_set: str) -> list[dict[str, Any]]:
    """Формирует вложение со встроенной клавиатурой MAX."""

    rows = build_normalized_button_rows(
        interaction_id=interaction_id,
        button_set=button_set,
    )
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {
                            "type": "callback",
                            "text": button.text,
                            "payload": button.service_data,
                        }
                        for button in row
                    ]
                    for row in rows
                ]
            },
        }
    ]


def build_provider_interaction_parameters(
    *,
    task: DispatchTask,
    provider_type: str,
) -> dict[str, Any]:
    """Возвращает параметры клавиатуры для задачи и конкретной платформы.

    Пустой словарь означает, что у задачи нет интерактивности. Ошибка
    конфигурации не подавляется: отправитель должен перевести задачу в ошибку,
    а не отправлять сообщение без обязательных кнопок.
    """

    try:
        interaction = task.message_interaction
    except (AttributeError, MessageInteraction.DoesNotExist):
        return {}

    provider = str(provider_type or "").strip().lower()
    if provider == "telegram":
        return {
            "reply_markup": build_telegram_reply_markup(
                interaction_id=interaction.id,
                button_set=interaction.button_set,
            )
        }
    if provider == "vk":
        keyboard = build_vk_keyboard(
            interaction_id=interaction.id,
            button_set=interaction.button_set,
        )
        return {
            "keyboard": json.dumps(
                keyboard,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        }
    if provider == "max":
        return {
            "attachments": build_max_attachments(
                interaction_id=interaction.id,
                button_set=interaction.button_set,
            )
        }
    raise MessageInteractionConfigurationError(
        f"Интерактивные сообщения не поддерживают провайдера {provider!r}."
    )


def create_dispatch_task_with_optional_interaction(
    *,
    button_set: str,
    interaction_enabled: bool,
    **dispatch_task_fields: Any,
) -> DispatchTask:
    """Атомарно создаёт задачу и, при необходимости, интерактивность.

    ``interaction_enabled=False`` применяется к историческим маршрутам:
    настройка набора остаётся у исходной рассылки, но конкретная историческая
    задача создаётся без кнопок и без строки ``MessageInteraction``.
    """

    normalized_set = _normalize_button_set(button_set)
    try:
        with transaction.atomic():
            task = DispatchTask.objects.create(**dispatch_task_fields)
            if interaction_enabled and normalized_set != InteractionButtonSet.NONE:
                MessageInteraction.objects.create(
                    dispatch_task=task,
                    button_set=normalized_set,
                )
    except IntegrityError as error:
        idempotency_key = dispatch_task_fields.get("idempotency_key")
        if idempotency_key and DispatchTask.objects.filter(idempotency_key=idempotency_key).exists():
            raise DispatchTaskAlreadyExists from error
        raise
    return task


def create_dispatch_tasks_with_optional_interactions(
    specifications: list[DispatchTaskCreationSpec],
    *,
    batch_size: int = 500,
) -> BulkDispatchTaskCreationResult:
    """Пакетно создаёт задачи и интерактивности с поэлементным результатом.

    Нормальный путь использует по одной пакетной вставке задач и связанных
    интерактивностей на порцию. Предварительно найденные ключи идемпотентности
    отмечаются как дубли. Если между проверкой и вставкой возник редкий
    конфликт целостности, порция делится пополам до изоляции конфликтующих
    элементов с жёстким пределом дополнительных операций. При иных ошибках
    повторные запросы не выполняются.
    """

    result = BulkDispatchTaskCreationResult(
        created_tasks={},
        duplicate_positions=set(),
        errors={},
    )
    if not specifications:
        return result

    safe_batch_size = min(max(int(batch_size), 1), 1000)
    normalized_specs: list[tuple[int, DispatchTaskCreationSpec, str]] = []
    for position, specification in enumerate(specifications):
        try:
            normalized_set = _normalize_button_set(specification.button_set)
        except Exception as error:  # noqa: BLE001 - нужен поэлементный результат.
            result.errors[position] = error
            continue
        normalized_specs.append((position, specification, normalized_set))

    for offset in range(0, len(normalized_specs), safe_batch_size):
        chunk = normalized_specs[offset : offset + safe_batch_size]
        _create_dispatch_task_chunk(
            chunk=chunk,
            result=result,
            batch_size=safe_batch_size,
        )
    return result


def _create_dispatch_task_chunk(
    *,
    chunk: list[tuple[int, DispatchTaskCreationSpec, str]],
    result: BulkDispatchTaskCreationResult,
    batch_size: int,
) -> None:
    """Создаёт одну порцию либо безопасно распределяет ошибку по позициям."""

    idempotency_keys = {
        str(specification.dispatch_task_fields.get("idempotency_key") or "").strip()
        for _, specification, _ in chunk
        if str(specification.dispatch_task_fields.get("idempotency_key") or "").strip()
    }
    try:
        existing_keys = set(
            DispatchTask.objects.filter(idempotency_key__in=idempotency_keys).values_list(
                "idempotency_key",
                flat=True,
            )
        )
    except Exception as error:  # noqa: BLE001 - команда выше получит поэлементную ошибку.
        for position, _, _ in chunk:
            result.errors[position] = error
        return

    candidates: list[tuple[int, DispatchTaskCreationSpec, str]] = []
    for item in chunk:
        position, specification, _ = item
        idempotency_key = str(
            specification.dispatch_task_fields.get("idempotency_key") or ""
        ).strip()
        if idempotency_key and idempotency_key in existing_keys:
            result.duplicate_positions.add(position)
        else:
            candidates.append(item)
    if not candidates:
        return

    _bulk_create_candidate_chunk(
        candidates=candidates,
        result=result,
        batch_size=batch_size,
    )


def _bulk_create_candidate_chunk(
    *,
    candidates: list[tuple[int, DispatchTaskCreationSpec, str]],
    result: BulkDispatchTaskCreationResult,
    batch_size: int,
    isolation_budget: _IntegrityIsolationBudget | None = None,
) -> None:
    """Пакетно создаёт кандидатов и при конфликте изолирует минимальную часть."""

    try:
        with transaction.atomic():
            task_objects = [
                DispatchTask(**specification.dispatch_task_fields)
                for _, specification, _ in candidates
            ]
            created_tasks = DispatchTask.objects.bulk_create(
                task_objects,
                batch_size=batch_size,
            )
            if len(created_tasks) != len(candidates) or any(task.pk is None for task in created_tasks):
                raise MessageInteractionConfigurationError(
                    "База данных не вернула идентификаторы пакетно созданных задач."
                )

            interactions = [
                MessageInteraction(
                    dispatch_task=task,
                    button_set=normalized_set,
                )
                for task, (_, specification, normalized_set) in zip(
                    created_tasks,
                    candidates,
                    strict=True,
                )
                if specification.interaction_enabled
                and normalized_set != InteractionButtonSet.NONE
            ]
            if interactions:
                MessageInteraction.objects.bulk_create(
                    interactions,
                    batch_size=batch_size,
                )
    except IntegrityError as error:
        if isolation_budget is None:
            isolation_budget = _IntegrityIsolationBudget()
            logger.warning(
                "Пакетное создание задач получило конфликт целостности: "
                "размер_порции=%s предел_изоляции=%s",
                len(candidates),
                isolation_budget.remaining,
            )
        _isolate_integrity_conflict(
            candidates=candidates,
            result=result,
            batch_size=batch_size,
            isolation_budget=isolation_budget,
            original_error=error,
        )
        return
    except Exception as error:  # noqa: BLE001 - поэлементный результат нужен вызывающей стороне.
        for position, _, _ in candidates:
            result.errors[position] = error
        return

    for task, (position, _, _) in zip(created_tasks, candidates, strict=True):
        result.created_tasks[position] = task


def _isolate_integrity_conflict(
    *,
    candidates: list[tuple[int, DispatchTaskCreationSpec, str]],
    result: BulkDispatchTaskCreationResult,
    batch_size: int,
    isolation_budget: _IntegrityIsolationBudget,
    original_error: IntegrityError,
) -> None:
    """Делит конфликтующую порцию, не допуская поштучного шторма запросов."""

    if len(candidates) == 1:
        if isolation_budget.consume():
            _create_dispatch_task_chunk_individually(candidates=candidates, result=result)
        else:
            result.errors[candidates[0][0]] = original_error
        return

    midpoint = len(candidates) // 2
    for part in (candidates[:midpoint], candidates[midpoint:]):
        if not isolation_budget.consume():
            for position, _, _ in part:
                result.errors[position] = original_error
            continue
        _bulk_create_candidate_chunk(
            candidates=part,
            result=result,
            batch_size=batch_size,
            isolation_budget=isolation_budget,
        )


def _create_dispatch_task_chunk_individually(
    *,
    candidates: list[tuple[int, DispatchTaskCreationSpec, str]],
    result: BulkDispatchTaskCreationResult,
) -> None:
    """Разбирает только порцию, в которой возник конфликт целостности."""

    for position, specification, _ in candidates:
        try:
            task = create_dispatch_task_with_optional_interaction(
                button_set=specification.button_set,
                interaction_enabled=specification.interaction_enabled,
                **specification.dispatch_task_fields,
            )
        except DispatchTaskAlreadyExists:
            result.duplicate_positions.add(position)
        except Exception as error:  # noqa: BLE001 - сохраняется изолированный результат позиции.
            result.errors[position] = error
        else:
            result.created_tasks[position] = task
