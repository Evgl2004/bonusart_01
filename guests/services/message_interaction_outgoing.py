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
import secrets
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction

from guests.models import (
    DispatchTask,
    InteractionButtonSet,
    InteractionLinkLabelCode,
    MessageInteraction,
    MessageInteractionLinkDestination,
    MessageInteractionTrackedLink,
)
from guests.services.message_interaction_links import (
    PUBLIC_TOKEN_BYTES,
    PUBLIC_TOKEN_LENGTH,
    PUBLIC_TOKEN_PATTERN,
    MessageInteractionConfigurationError,
    build_public_redirect_url,
    validate_tracked_link_snapshot_url,
)


SERVICE_DATA_TYPE = "si"
SERVICE_DATA_VERSION = 2
MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
TELEGRAM_CALLBACK_DATA_LIMIT_BYTES = 64
MAX_INTEGRITY_ISOLATION_OPERATIONS = 64
MAX_TRACKED_LINK_TOKEN_ATTEMPTS = 3


logger = logging.getLogger(__name__)


class DispatchTaskAlreadyExists(Exception):
    """Задача с тем же непустым ключом идемпотентности уже существует."""


@dataclass(frozen=True)
class DispatchTaskCreationSpec:
    """Описание одной задачи для пакетного атомарного создания."""

    button_set: str
    interaction_enabled: bool
    dispatch_task_fields: dict[str, Any]
    tracked_link_destination: MessageInteractionLinkDestination | None = None


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
class _TrackedLinkSnapshotSpec:
    """Проверенные неизменяемые данные ссылки для конкретного сообщения."""

    label_code: str
    target_url: str


_NormalizedCreationSpec = tuple[
    int,
    DispatchTaskCreationSpec,
    str,
    _TrackedLinkSnapshotSpec | None,
]


@dataclass(frozen=True)
class NormalizedCallbackButton:
    """Платформенно-независимое описание обратной кнопки."""

    action: str
    text: str
    service_data: str


@dataclass(frozen=True)
class NormalizedLinkButton:
    """Платформенно-независимое описание обычной URL-кнопки."""

    text: str
    url: str


NormalizedInteractionButton = NormalizedCallbackButton | NormalizedLinkButton


BUTTON_LABELS: dict[str, str] = {
    "l": "👍 Нравится",
    "d": "👎 Не нравится",
    "c": "🎟 В купоны",
    "m": "☰ Меню",
}

BUTTON_SET_ROWS: dict[str, tuple[tuple[str, ...], ...]] = {
    InteractionButtonSet.RATING_MENU: (("l", "d"), ("m",)),
    InteractionButtonSet.RATING_COUPONS: (("l", "d"), ("c",)),
    InteractionButtonSet.RATING_MENU_LINK: (("l", "d"), ("link",), ("m",)),
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
    InteractionButtonSet.RATING_MENU_LINK: {
        "l": "ldm",
        "d": "dlm",
        "m": "mld",
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

LINK_LABELS = dict(InteractionLinkLabelCode.choices)


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
        InteractionButtonSet.RATING_MENU_LINK,
    }
    if normalized not in allowed:
        raise MessageInteractionConfigurationError(
            f"Неизвестный набор интерактивных кнопок: {normalized!r}."
        )
    return normalized


def _normalize_tracked_link_snapshot(
    *,
    button_set: str,
    interaction_enabled: bool,
    destination: MessageInteractionLinkDestination | None,
) -> _TrackedLinkSnapshotSpec | None:
    """Проверяет назначение и формирует неизменяемый снимок новой ссылки."""

    if button_set != InteractionButtonSet.RATING_MENU_LINK:
        if destination is not None:
            raise MessageInteractionConfigurationError(
                "Назначение ссылки передано для набора кнопок без ссылки."
            )
        return None

    # Исторический маршрут намеренно получает обычное сообщение без кнопок.
    if not interaction_enabled:
        return None
    if not bool(getattr(settings, "MESSAGE_TRACKED_LINKS_ENABLED", False)):
        raise MessageInteractionConfigurationError(
            "Формирование новых отслеживаемых ссылок выключено."
        )
    if destination is None:
        raise MessageInteractionConfigurationError(
            "Для набора «Оценка, ссылка и главное меню» не выбрано назначение ссылки."
        )
    if (
        not isinstance(destination, MessageInteractionLinkDestination)
        or destination.pk is None
    ):
        raise MessageInteractionConfigurationError(
            "Назначение ссылки должно быть сохранённой записью утверждённого справочника."
        )
    if not destination.is_active:
        raise MessageInteractionConfigurationError(
            "Выбранное назначение отслеживаемой ссылки неактивно."
        )

    label_code = str(destination.label_code or "").strip()
    if label_code not in LINK_LABELS:
        raise MessageInteractionConfigurationError(
            "У назначения ссылки указана неподдерживаемая подпись кнопки."
        )
    target_url = validate_tracked_link_snapshot_url(destination.target_url)
    return _TrackedLinkSnapshotSpec(
        label_code=label_code,
        target_url=target_url,
    )


def _generate_public_token() -> str:
    """Создаёт 192-битный токен Base64URL без заполнения."""

    token = secrets.token_urlsafe(PUBLIC_TOKEN_BYTES)
    if len(token) != PUBLIC_TOKEN_LENGTH or PUBLIC_TOKEN_PATTERN.fullmatch(token) is None:
        raise MessageInteractionConfigurationError(
            "Генератор вернул публичный токен неожиданного формата."
        )
    return token


def _create_tracked_link(
    *,
    interaction: MessageInteraction,
    snapshot: _TrackedLinkSnapshotSpec,
) -> MessageInteractionTrackedLink:
    """Создаёт снимок ссылки, ограниченно повторяя только конфликт токена."""

    last_error: IntegrityError | None = None
    for attempt in range(1, MAX_TRACKED_LINK_TOKEN_ATTEMPTS + 1):
        public_token = _generate_public_token()
        try:
            with transaction.atomic():
                return MessageInteractionTrackedLink.objects.create(
                    interaction=interaction,
                    public_token=public_token,
                    label_code=snapshot.label_code,
                    target_url=snapshot.target_url,
                )
        except IntegrityError as error:
            if not MessageInteractionTrackedLink.objects.filter(
                public_token=public_token
            ).exists():
                raise
            last_error = error
            logger.warning(
                "Конфликт публичного токена отслеживаемой ссылки: "
                "interaction_id=%s попытка=%s из=%s",
                interaction.id,
                attempt,
                MAX_TRACKED_LINK_TOKEN_ATTEMPTS,
            )
    if last_error is None:  # Защитная ветка при изменении числа попыток.
        raise MessageInteractionConfigurationError(
            "Не выполнена ни одна попытка создания отслеживаемой ссылки."
        )
    raise last_error


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
    tracked_link: MessageInteractionTrackedLink | None = None,
) -> tuple[tuple[NormalizedInteractionButton, ...], ...]:
    """Строит утверждённую раскладку без привязки к платформе."""

    normalized_set = _normalize_button_set(button_set)
    rows = BUTTON_SET_ROWS.get(normalized_set)
    if rows is None:
        raise MessageInteractionConfigurationError(
            "Интерактивность сообщения не может использовать набор «Без кнопок»."
        )

    link_button: NormalizedLinkButton | None = None
    if normalized_set == InteractionButtonSet.RATING_MENU_LINK:
        if tracked_link is None:
            raise MessageInteractionConfigurationError(
                "Для ссылочного набора отсутствует неизменяемый снимок ссылки."
            )
        if tracked_link.disabled_at is not None:
            raise MessageInteractionConfigurationError(
                "Отслеживаемая ссылка отключена и не может быть отправлена."
            )
        link_text = LINK_LABELS.get(str(tracked_link.label_code or "").strip())
        if not link_text:
            raise MessageInteractionConfigurationError(
                "Снимок отслеживаемой ссылки содержит неподдерживаемую подпись."
            )
        link_button = NormalizedLinkButton(
            text=link_text,
            url=build_public_redirect_url(tracked_link.public_token),
        )
    elif tracked_link is not None:
        raise MessageInteractionConfigurationError(
            "Снимок ссылки найден у набора кнопок без ссылки."
        )

    normalized_rows: list[tuple[NormalizedInteractionButton, ...]] = []
    for row in rows:
        normalized_row: list[NormalizedInteractionButton] = []
        for action in row:
            if action == "link":
                if link_button is None:  # Защита от несогласованной схемы рядов.
                    raise MessageInteractionConfigurationError(
                        "Ссылочная кнопка не была подготовлена."
                    )
                normalized_row.append(link_button)
                continue
            normalized_row.append(
                NormalizedCallbackButton(
                    action=action,
                    text=BUTTON_LABELS[action],
                    service_data=build_service_data(
                        interaction_id=interaction_id,
                        button_set=normalized_set,
                        action=action,
                    ),
                )
            )
        normalized_rows.append(tuple(normalized_row))
    return tuple(normalized_rows)


def build_telegram_reply_markup(
    *,
    interaction_id: int,
    button_set: str,
    tracked_link: MessageInteractionTrackedLink | None = None,
) -> dict[str, Any]:
    """Формирует встроенную клавиатуру Telegram."""

    rows = build_normalized_button_rows(
        interaction_id=interaction_id,
        button_set=button_set,
        tracked_link=tracked_link,
    )
    return {
        "inline_keyboard": [
            [
                _build_telegram_button(button)
                for button in row
            ]
            for row in rows
        ]
    }


def _build_telegram_button(button: NormalizedInteractionButton) -> dict[str, str]:
    """Преобразует одну нормализованную кнопку для Telegram."""

    if isinstance(button, NormalizedLinkButton):
        return {"text": button.text, "url": button.url, "style": "primary"}
    return {
        "text": button.text,
        "callback_data": button.service_data,
        "style": TELEGRAM_BUTTON_STYLES[button.action],
    }


def build_vk_keyboard(
    *,
    interaction_id: int,
    button_set: str,
    tracked_link: MessageInteractionTrackedLink | None = None,
) -> dict[str, Any]:
    """Формирует встроенную клавиатуру VK."""

    rows = build_normalized_button_rows(
        interaction_id=interaction_id,
        button_set=button_set,
        tracked_link=tracked_link,
    )
    return {
        "one_time": False,
        "inline": True,
        "buttons": [
            [
                _build_vk_button(button)
                for button in row
            ]
            for row in rows
        ],
    }


def _build_vk_button(button: NormalizedInteractionButton) -> dict[str, Any]:
    """Преобразует одну нормализованную кнопку для VK."""

    if isinstance(button, NormalizedLinkButton):
        return {
            "action": {
                "type": "open_link",
                "label": button.text,
                "link": button.url,
            },
            "color": "primary",
        }
    return {
        "action": {
            "type": "callback",
            "label": button.text,
            "payload": button.service_data,
        },
        "color": VK_BUTTON_COLORS[button.action],
    }


def build_max_attachments(
    *,
    interaction_id: int,
    button_set: str,
    tracked_link: MessageInteractionTrackedLink | None = None,
) -> list[dict[str, Any]]:
    """Формирует вложение со встроенной клавиатурой MAX."""

    rows = build_normalized_button_rows(
        interaction_id=interaction_id,
        button_set=button_set,
        tracked_link=tracked_link,
    )
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        _build_max_button(button)
                        for button in row
                    ]
                    for row in rows
                ]
            },
        }
    ]


def _build_max_button(button: NormalizedInteractionButton) -> dict[str, str]:
    """Преобразует одну нормализованную кнопку для MAX."""

    if isinstance(button, NormalizedLinkButton):
        return {"type": "link", "text": button.text, "url": button.url}
    return {
        "type": "callback",
        "text": button.text,
        "payload": button.service_data,
    }


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

    tracked_link: MessageInteractionTrackedLink | None = None
    if interaction.button_set == InteractionButtonSet.RATING_MENU_LINK:
        try:
            tracked_link = interaction.tracked_link
        except (AttributeError, MessageInteractionTrackedLink.DoesNotExist) as error:
            raise MessageInteractionConfigurationError(
                "Для ссылочного набора сообщения не найден снимок ссылки."
            ) from error

    provider = str(provider_type or "").strip().lower()
    if provider == "telegram":
        return {
            "reply_markup": build_telegram_reply_markup(
                interaction_id=interaction.id,
                button_set=interaction.button_set,
                tracked_link=tracked_link,
            )
        }
    if provider == "vk":
        keyboard = build_vk_keyboard(
            interaction_id=interaction.id,
            button_set=interaction.button_set,
            tracked_link=tracked_link,
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
                tracked_link=tracked_link,
            )
        }
    raise MessageInteractionConfigurationError(
        f"Интерактивные сообщения не поддерживают провайдера {provider!r}."
    )


def create_dispatch_task_with_optional_interaction(
    *,
    button_set: str,
    interaction_enabled: bool,
    tracked_link_destination: MessageInteractionLinkDestination | None = None,
    **dispatch_task_fields: Any,
) -> DispatchTask:
    """Атомарно создаёт задачу и, при необходимости, интерактивность.

    ``interaction_enabled=False`` применяется к историческим маршрутам:
    настройка набора остаётся у исходной рассылки, но конкретная историческая
    задача создаётся без кнопок и без строки ``MessageInteraction``.
    """

    normalized_set = _normalize_button_set(button_set)
    link_snapshot = _normalize_tracked_link_snapshot(
        button_set=normalized_set,
        interaction_enabled=interaction_enabled,
        destination=tracked_link_destination,
    )
    try:
        with transaction.atomic():
            task = DispatchTask.objects.create(**dispatch_task_fields)
            if interaction_enabled and normalized_set != InteractionButtonSet.NONE:
                interaction = MessageInteraction.objects.create(
                    dispatch_task=task,
                    button_set=normalized_set,
                )
                if link_snapshot is not None:
                    _create_tracked_link(
                        interaction=interaction,
                        snapshot=link_snapshot,
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
    normalized_specs: list[_NormalizedCreationSpec] = []
    for position, specification in enumerate(specifications):
        try:
            normalized_set = _normalize_button_set(specification.button_set)
            link_snapshot = _normalize_tracked_link_snapshot(
                button_set=normalized_set,
                interaction_enabled=specification.interaction_enabled,
                destination=specification.tracked_link_destination,
            )
        except Exception as error:  # noqa: BLE001 - нужен поэлементный результат.
            result.errors[position] = error
            continue
        normalized_specs.append(
            (position, specification, normalized_set, link_snapshot)
        )

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
    chunk: list[_NormalizedCreationSpec],
    result: BulkDispatchTaskCreationResult,
    batch_size: int,
) -> None:
    """Создаёт одну порцию либо безопасно распределяет ошибку по позициям."""

    idempotency_keys = {
        str(specification.dispatch_task_fields.get("idempotency_key") or "").strip()
        for _, specification, _, _ in chunk
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
        for position, _, _, _ in chunk:
            result.errors[position] = error
        return

    candidates: list[_NormalizedCreationSpec] = []
    for item in chunk:
        position, specification, _, _ = item
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
    candidates: list[_NormalizedCreationSpec],
    result: BulkDispatchTaskCreationResult,
    batch_size: int,
    isolation_budget: _IntegrityIsolationBudget | None = None,
) -> None:
    """Пакетно создаёт кандидатов и при конфликте изолирует минимальную часть."""

    try:
        with transaction.atomic():
            task_objects = [
                DispatchTask(**specification.dispatch_task_fields)
                for _, specification, _, _ in candidates
            ]
            created_tasks = DispatchTask.objects.bulk_create(
                task_objects,
                batch_size=batch_size,
            )
            if len(created_tasks) != len(candidates) or any(task.pk is None for task in created_tasks):
                raise MessageInteractionConfigurationError(
                    "База данных не вернула идентификаторы пакетно созданных задач."
                )

            interactions: list[MessageInteraction] = []
            link_snapshots: list[_TrackedLinkSnapshotSpec | None] = []
            for task, (_, specification, normalized_set, link_snapshot) in zip(
                created_tasks,
                candidates,
                strict=True,
            ):
                if (
                    not specification.interaction_enabled
                    or normalized_set == InteractionButtonSet.NONE
                ):
                    continue
                interactions.append(
                    MessageInteraction(
                        dispatch_task=task,
                        button_set=normalized_set,
                    )
                )
                link_snapshots.append(link_snapshot)
            if interactions:
                MessageInteraction.objects.bulk_create(
                    interactions,
                    batch_size=batch_size,
                )
            tracked_links = [
                MessageInteractionTrackedLink(
                    interaction=interaction,
                    public_token=_generate_public_token(),
                    label_code=link_snapshot.label_code,
                    target_url=link_snapshot.target_url,
                )
                for interaction, link_snapshot in zip(
                    interactions,
                    link_snapshots,
                    strict=True,
                )
                if link_snapshot is not None
            ]
            if tracked_links:
                MessageInteractionTrackedLink.objects.bulk_create(
                    tracked_links,
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
        for position, _, _, _ in candidates:
            result.errors[position] = error
        return

    for task, (position, _, _, _) in zip(created_tasks, candidates, strict=True):
        result.created_tasks[position] = task


def _isolate_integrity_conflict(
    *,
    candidates: list[_NormalizedCreationSpec],
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
            for position, _, _, _ in part:
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
    candidates: list[_NormalizedCreationSpec],
    result: BulkDispatchTaskCreationResult,
) -> None:
    """Разбирает только порцию, в которой возник конфликт целостности."""

    for position, specification, _, _ in candidates:
        try:
            task = create_dispatch_task_with_optional_interaction(
                button_set=specification.button_set,
                interaction_enabled=specification.interaction_enabled,
                tracked_link_destination=specification.tracked_link_destination,
                **specification.dispatch_task_fields,
            )
        except DispatchTaskAlreadyExists:
            result.duplicate_positions.add(position)
        except Exception as error:  # noqa: BLE001 - сохраняется изолированный результат позиции.
            result.errors[position] = error
        else:
            result.created_tasks[position] = task
