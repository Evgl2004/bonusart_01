"""Эксплуатационные операции для интерактивных сообщений.

Модуль объединяет безопасные операции, которые нужны перед включением и при
диагностике функции:

* аудит готовности без изменения данных;
* сухой расчёт и подтверждённая постановка одной пилотной задачи;
* поиск интерактивностей и событий без вывода персональных данных.

Внешние программные интерфейсы платформ здесь не вызываются. Подтверждённый
пилот создаёт обычную задачу ``DispatchTask``: её обрабатывает тот же
диспетчер и тот же отправитель, что и рабочие сообщения.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import Count, Max, Q
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    GuestBotBinding,
    InteractionButtonSet,
    MessageInteraction,
    MessageInteractionEvent,
)
from guests.services.message_interaction_outgoing import (
    DispatchTaskAlreadyExists,
    create_dispatch_task_with_optional_interaction,
    interactions_enabled_for_new_task,
)


logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = frozenset(
    {
        BotProfile.ProviderType.TELEGRAM,
        BotProfile.ProviderType.VK,
        BotProfile.ProviderType.MAX,
    }
)
PILOT_BUTTON_SETS = frozenset(
    {
        InteractionButtonSet.RATING_MENU,
        InteractionButtonSet.RATING_COUPONS,
    }
)
PILOT_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
PILOT_MESSAGE_MAX_LENGTH = 4000
PILOT_IDEMPOTENCY_PREFIX = "message-interaction-pilot:"


class MessageInteractionOperationError(ValueError):
    """Безопасная ошибка эксплуатационной операции."""


def build_message_interaction_readiness_report(
    *,
    require_enabled: bool = False,
) -> dict[str, Any]:
    """Формирует отчёт готовности без изменения базы данных.

    ``require_enabled`` переводит выключенные эксплуатационные переключатели
    из предупреждений в блокирующие ошибки. Это позволяет одной командой
    проверять и безопасное развёртывание с выключенной функцией, и готовность
    непосредственно перед пилотом.
    """

    checks: list[dict[str, Any]] = []
    formation_enabled = bool(getattr(settings, "MESSAGE_INTERACTIONS_ENABLED", False))
    callback_enabled = bool(
        getattr(settings, "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_ENABLED", False)
    )
    allowed_providers = _normalize_allowed_providers(
        getattr(settings, "MESSAGE_INTERACTIONS_ALLOWED_PROVIDERS", set())
    )
    unknown_providers = sorted(allowed_providers - SUPPORTED_PROVIDERS)
    supported_allowed_providers = sorted(allowed_providers & SUPPORTED_PROVIDERS)

    _add_check(
        checks,
        code="formation_enabled",
        status=(
            "ok"
            if formation_enabled
            else ("blocked" if require_enabled else "warning")
        ),
        message=(
            "Формирование новых сообщений с кнопками включено."
            if formation_enabled
            else "Формирование новых сообщений с кнопками выключено."
        ),
        details={"enabled": formation_enabled},
    )

    if unknown_providers:
        provider_status = "blocked"
        provider_message = "В перечне разрешённых платформ есть неизвестные значения."
    elif not supported_allowed_providers:
        provider_status = "blocked" if formation_enabled or require_enabled else "warning"
        provider_message = "Не разрешена ни одна поддерживаемая платформа."
    else:
        provider_status = "ok"
        provider_message = "Перечень разрешённых платформ корректен."
    _add_check(
        checks,
        code="allowed_providers",
        status=provider_status,
        message=provider_message,
        details={
            "providers": supported_allowed_providers,
            "unknown_providers": unknown_providers,
        },
    )

    _add_check(
        checks,
        code="callback_enabled",
        status=(
            "ok"
            if callback_enabled
            else ("blocked" if require_enabled else "warning")
        ),
        message=(
            "Приём пакетных событий vtelemax включён."
            if callback_enabled
            else "Приём пакетных событий vtelemax выключен."
        ),
        details={"enabled": callback_enabled},
    )

    callback_secret_present = bool(
        str(
            getattr(
                settings,
                "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_HMAC_SECRET",
                "",
            )
            or ""
        ).strip()
    )
    _add_check(
        checks,
        code="callback_secret",
        status=(
            "ok"
            if callback_secret_present
            else ("blocked" if callback_enabled or require_enabled else "warning")
        ),
        message=(
            "Секрет подписи входящих пакетов задан."
            if callback_secret_present
            else "Секрет подписи входящих пакетов не задан."
        ),
        details={"configured": callback_secret_present},
    )

    require_https = bool(
        getattr(
            settings,
            "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_REQUIRE_HTTPS",
            True,
        )
    )
    _add_check(
        checks,
        code="callback_https",
        status="ok" if require_https else ("blocked" if callback_enabled else "warning"),
        message=(
            "Защищённое соединение обязательно для входящей точки."
            if require_https
            else "Требование защищённого соединения для входящей точки выключено."
        ),
        details={"required": require_https},
    )

    _collect_schema_check(checks)
    _collect_provider_checks(
        checks,
        providers=supported_allowed_providers,
        strict=formation_enabled or require_enabled,
    )
    observations = _collect_readiness_observations()

    overall_status = _resolve_overall_status(checks)
    return {
        "summary": {
            "overall_status": overall_status,
            "checks_total": len(checks),
            "checks_ok": sum(item["status"] == "ok" for item in checks),
            "checks_warning": sum(item["status"] == "warning" for item in checks),
            "checks_blocked": sum(item["status"] == "blocked" for item in checks),
            "generated_at": timezone.now().isoformat(),
        },
        "checks": checks,
        "observations": observations,
    }


def run_message_interaction_pilot(
    *,
    guest_id: int,
    bot_code: str,
    button_set: str,
    message_text: str,
    run_id: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """Проверяет пилотную цель и при подтверждении создаёт одну задачу.

    Без ``confirm`` функция выполняет только запросы на чтение. При
    подтверждении требуется уникальный ``run_id``; он становится ключом
    идемпотентности и исключает повторную постановку того же пилота.
    """

    normalized_run_id = str(run_id or "").strip()
    plan, binding = _build_pilot_plan(
        guest_id=guest_id,
        bot_code=bot_code,
        button_set=button_set,
        message_text=message_text,
        run_id=normalized_run_id,
        confirm=confirm,
    )
    if not confirm:
        return {
            **plan,
            "dry_run": True,
            "confirmed": False,
            "created": False,
            "already_exists": False,
            "dispatch_task_id": None,
            "interaction_id": None,
        }

    if plan["blockers"]:
        raise MessageInteractionOperationError("; ".join(plan["blockers"]))
    if binding is None:  # Защитная ветка: блокировка выше обязана это исключать.
        raise MessageInteractionOperationError("Современная привязка гостя не найдена.")

    idempotency_key = f"{PILOT_IDEMPOTENCY_PREFIX}{normalized_run_id}"
    existing_task = DispatchTask.objects.filter(idempotency_key=idempotency_key).first()
    if existing_task is not None:
        return _build_existing_pilot_result(
            plan=plan,
            task=existing_task,
            binding=binding,
            button_set=button_set,
            message_text=message_text,
        )

    try:
        task = create_dispatch_task_with_optional_interaction(
            button_set=button_set,
            interaction_enabled=True,
            source_type=DispatchTask.SourceType.MANUAL,
            provider_type=binding.bot.provider_type,
            priority=DispatchTask.Priority.HIGH,
            status=DispatchTask.Status.PENDING,
            guest_id=binding.guest_id,
            bot_profile=binding.bot,
            guest_binding=binding,
            external_chat_id=binding.external_chat_id,
            message_text=message_text,
            payload={
                "message_interaction_pilot": True,
                "run_id": normalized_run_id,
                "button_set": button_set,
            },
            idempotency_key=idempotency_key,
        )
    except DispatchTaskAlreadyExists:
        task = DispatchTask.objects.get(idempotency_key=idempotency_key)
        return _build_existing_pilot_result(
            plan=plan,
            task=task,
            binding=binding,
            button_set=button_set,
            message_text=message_text,
        )

    interaction = task.message_interaction
    logger.info(
        "Создана пилотная задача интерактивного сообщения: task_id=%s "
        "interaction_id=%s provider=%s run_id=%s",
        task.id,
        interaction.id,
        task.provider_type,
        normalized_run_id,
    )
    return {
        **plan,
        "dry_run": False,
        "confirmed": True,
        "created": True,
        "already_exists": False,
        "dispatch_task_id": task.id,
        "interaction_id": interaction.id,
    }


def build_message_interaction_diagnostic_report(
    *,
    interaction_id: int | None = None,
    event_id: str | None = None,
    mailing_id: int | None = None,
    scenario_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Возвращает безопасный снимок интерактивностей по одному критерию."""

    selectors = {
        "interaction_id": interaction_id,
        "event_id": str(event_id or "").strip() or None,
        "mailing_id": mailing_id,
        "scenario_id": scenario_id,
    }
    selected = [(name, value) for name, value in selectors.items() if value is not None]
    if len(selected) != 1:
        raise MessageInteractionOperationError(
            "Нужно указать ровно один критерий: интерактивность, событие, рассылку или сценарий."
        )

    selector_name, selector_value = selected[0]
    if selector_name == "event_id":
        try:
            selector_value = uuid.UUID(str(selector_value))
        except (ValueError, AttributeError) as error:
            raise MessageInteractionOperationError(
                "Идентификатор события должен быть корректным UUID."
            ) from error
    elif isinstance(selector_value, bool) or int(selector_value) <= 0:
        raise MessageInteractionOperationError("Числовой критерий должен быть положительным.")

    safe_limit = min(max(int(limit), 1), 100)
    queryset = (
        MessageInteraction.objects.select_related(
            "dispatch_task",
            "dispatch_task__mailing_guest",
            "dispatch_task__notification_scenario",
        )
        .annotate(
            events_total=Count("events"),
            accepted_ratings_total=Count(
                "events",
                filter=Q(
                    events__result=MessageInteractionEvent.Result.ACCEPTED,
                    events__action__in=(
                        MessageInteractionEvent.Action.LIKE,
                        MessageInteractionEvent.Action.DISLIKE,
                    ),
                ),
            ),
            repeated_ratings_total=Count(
                "events",
                filter=Q(
                    events__result=MessageInteractionEvent.Result.RATING_ALREADY_RECORDED,
                ),
            ),
            coupon_actions_total=Count(
                "events",
                filter=Q(events__action=MessageInteractionEvent.Action.COUPONS),
            ),
            menu_actions_total=Count(
                "events",
                filter=Q(events__action=MessageInteractionEvent.Action.MENU),
            ),
            last_event_received_at=Max("events__received_at"),
        )
        .order_by("id")
    )
    if selector_name == "interaction_id":
        queryset = queryset.filter(id=int(selector_value))
    elif selector_name == "event_id":
        queryset = queryset.filter(events__event_id=selector_value)
    elif selector_name == "mailing_id":
        queryset = queryset.filter(dispatch_task__mailing_guest__mailing_id=int(selector_value))
    else:
        queryset = queryset.filter(dispatch_task__notification_scenario_id=int(selector_value))

    total = queryset.count()
    interactions = [_serialize_interaction(row) for row in queryset[:safe_limit]]
    selected_event = None
    if selector_name == "event_id":
        event = MessageInteractionEvent.objects.filter(event_id=selector_value).first()
        if event is not None:
            selected_event = {
                "event_id": str(event.event_id),
                "interaction_id": event.interaction_id,
                "action": event.action,
                "result": event.result,
                "occurred_at": event.occurred_at.isoformat(),
                "received_at": event.received_at.isoformat(),
                "provider_message_id_present": bool(event.provider_message_id),
            }

    return {
        "selector": {selector_name: str(selector_value)},
        "total": total,
        "limit": safe_limit,
        "truncated": total > safe_limit,
        "interactions": interactions,
        "selected_event": selected_event,
    }


def _build_pilot_plan(
    *,
    guest_id: int,
    bot_code: str,
    button_set: str,
    message_text: str,
    run_id: str,
    confirm: bool,
) -> tuple[dict[str, Any], GuestBotBinding | None]:
    blockers: list[str] = []
    warnings: list[str] = []
    normalized_bot_code = str(bot_code or "").strip()
    normalized_message = str(message_text or "")

    if isinstance(guest_id, bool) or not isinstance(guest_id, int) or guest_id <= 0:
        blockers.append("Идентификатор гостя должен быть положительным целым числом.")
        guest = None
    else:
        guest = Guest.objects.filter(pk=guest_id).only("id").first()
        if guest is None:
            blockers.append("Гость с указанным идентификатором не найден.")

    if not normalized_bot_code:
        blockers.append("Код профиля бота обязателен.")
        bot = None
    else:
        bot = BotProfile.objects.filter(code=normalized_bot_code).first()
        if bot is None:
            blockers.append("Профиль бота с указанным кодом не найден.")
        elif not bot.is_active:
            blockers.append("Выбранный профиль бота выключен.")
        elif bot.provider_type not in SUPPORTED_PROVIDERS:
            blockers.append("Платформа выбранного бота не поддерживает интерактивные сообщения.")
        elif not bot.resolve_token():
            blockers.append("Для выбранного профиля бота не найден действующий токен.")

    if button_set not in PILOT_BUTTON_SETS:
        blockers.append("Для пилота разрешены только два утверждённых набора кнопок.")
    if not normalized_message.strip():
        blockers.append("Текст пилотного сообщения не может быть пустым.")
    elif len(normalized_message) > PILOT_MESSAGE_MAX_LENGTH:
        blockers.append(
            f"Текст пилотного сообщения длиннее {PILOT_MESSAGE_MAX_LENGTH} символов."
        )

    if confirm and not run_id:
        blockers.append("Для подтверждённого пилота обязателен уникальный идентификатор запуска.")
    if run_id and not PILOT_RUN_ID_PATTERN.fullmatch(run_id):
        blockers.append(
            "Идентификатор запуска должен содержать не более 80 латинских букв, цифр, "
            "точек, дефисов или подчёркиваний и начинаться с буквы либо цифры."
        )

    binding = None
    if guest is not None and bot is not None:
        binding = (
            GuestBotBinding.objects.select_related("bot")
            .filter(guest_id=guest.id, bot=bot)
            .first()
        )
        if binding is None:
            blockers.append("У гостя нет современной привязки к выбранному боту.")
        elif not binding.is_active:
            blockers.append("Современная привязка гостя к боту выключена.")
        elif not binding.is_opt_in or binding.is_stop_sending:
            blockers.append("Гость не разрешил отправку сообщений через выбранную привязку.")
        elif not str(binding.external_chat_id or "").strip():
            blockers.append("В современной привязке отсутствует идентификатор получателя.")

    provider_type = bot.provider_type if bot is not None else None
    if provider_type and not interactions_enabled_for_new_task(provider_type):
        blockers.append(
            "Формирование интерактивных сообщений выключено для выбранной платформы."
        )

    if not confirm:
        warnings.append("Сухой режим: задача и интерактивность не создаются.")

    return (
        {
            "guest_id": guest_id,
            "bot_code": normalized_bot_code,
            "provider": provider_type,
            "button_set": button_set,
            "run_id": run_id or None,
            "blockers": blockers,
            "warnings": warnings,
            "ready": not blockers,
        },
        binding,
    )


def _build_existing_pilot_result(
    *,
    plan: dict[str, Any],
    task: DispatchTask,
    binding: GuestBotBinding,
    button_set: str,
    message_text: str,
) -> dict[str, Any]:
    try:
        interaction = task.message_interaction
    except MessageInteraction.DoesNotExist as error:
        raise MessageInteractionOperationError(
            "Идентификатор запуска уже занят задачей без интерактивности."
        ) from error

    same_parameters = (
        task.guest_id == binding.guest_id
        and task.bot_profile_id == binding.bot_id
        and task.guest_binding_id == binding.id
        and task.provider_type == binding.bot.provider_type
        and task.message_text == message_text
        and interaction.button_set == button_set
    )
    if not same_parameters:
        raise MessageInteractionOperationError(
            "Идентификатор запуска уже использован с другими параметрами."
        )

    logger.info(
        "Повторный пилот не создан из-за ключа идемпотентности: task_id=%s run_id=%s",
        task.id,
        plan["run_id"],
    )
    return {
        **plan,
        "dry_run": False,
        "confirmed": True,
        "created": False,
        "already_exists": True,
        "dispatch_task_id": task.id,
        "interaction_id": interaction.id,
    }


def _collect_schema_check(checks: list[dict[str, Any]]) -> None:
    expected_tables = {"dispatch_tasks", "message_interactions", "message_interaction_events"}
    try:
        existing_tables = set(connection.introspection.table_names())
    except Exception as error:  # noqa: BLE001 - аудит обязан вернуть управляемый результат.
        _add_check(
            checks,
            code="database_schema",
            status="blocked",
            message="Не удалось проверить структуру базы данных.",
            details={"error_type": type(error).__name__},
        )
        return

    missing_tables = sorted(expected_tables - existing_tables)
    _add_check(
        checks,
        code="database_schema",
        status="blocked" if missing_tables else "ok",
        message=(
            "Таблицы интерактивных сообщений присутствуют."
            if not missing_tables
            else "В базе отсутствуют обязательные таблицы интерактивных сообщений."
        ),
        details={"missing_tables": missing_tables},
    )


def _collect_provider_checks(
    checks: list[dict[str, Any]],
    *,
    providers: list[str],
    strict: bool,
) -> None:
    for provider in providers:
        try:
            bots = list(
                BotProfile.objects.filter(provider_type=provider, is_active=True).order_by("id")
            )
            bots_with_token = sum(bool(bot.resolve_token()) for bot in bots)
            bindings_total = (
                GuestBotBinding.objects.filter(
                    bot__in=bots,
                    is_active=True,
                    is_opt_in=True,
                    is_stop_sending=False,
                )
                .exclude(external_chat_id="")
                .count()
            )
        except Exception as error:  # noqa: BLE001 - аудит не должен падать при неполной схеме.
            _add_check(
                checks,
                code=f"provider_{provider}",
                status="blocked",
                message="Не удалось проверить профиль бота и современные привязки.",
                details={"provider": provider, "error_type": type(error).__name__},
            )
            continue

        if not bots or bots_with_token == 0:
            status = "blocked" if strict else "warning"
            message = "Нет активного профиля бота с действующим токеном."
        elif bindings_total == 0:
            status = "warning"
            message = "Нет разрешённых современных привязок для платформы."
        else:
            status = "ok"
            message = "Профиль бота и современные разрешённые привязки найдены."
        _add_check(
            checks,
            code=f"provider_{provider}",
            status=status,
            message=message,
            details={
                "provider": provider,
                "active_bots": len(bots),
                "bots_with_token": bots_with_token,
                "permitted_bindings": bindings_total,
            },
        )


def _collect_readiness_observations() -> dict[str, Any]:
    try:
        interactions_total = MessageInteraction.objects.count()
        events_total = MessageInteractionEvent.objects.count()
        last_event_at = MessageInteractionEvent.objects.aggregate(value=Max("received_at"))["value"]
        failed_interactive_tasks = DispatchTask.objects.filter(
            status=DispatchTask.Status.FAILED,
            message_interaction__isnull=False,
        ).count()
    except Exception as error:  # noqa: BLE001 - результат аудита должен оставаться безопасным.
        return {"available": False, "error_type": type(error).__name__}
    return {
        "available": True,
        "interactions_total": interactions_total,
        "events_total": events_total,
        "failed_interactive_tasks_total": failed_interactive_tasks,
        "last_event_received_at": last_event_at.isoformat() if last_event_at else None,
    }


def _serialize_interaction(interaction: MessageInteraction) -> dict[str, Any]:
    task = interaction.dispatch_task
    mailing_id = (
        task.mailing_guest.mailing_id
        if task.mailing_guest_id and task.mailing_guest is not None
        else None
    )
    return {
        "interaction_id": interaction.id,
        "dispatch_task_id": task.id,
        "button_set": interaction.button_set,
        "provider": task.provider_type,
        "task_status": task.status,
        "source_type": task.source_type,
        "guest_id": task.guest_id,
        "mailing_id": mailing_id,
        "scenario_id": task.notification_scenario_id,
        "created_at": interaction.created_at.isoformat(),
        "events_total": interaction.events_total,
        "accepted_ratings_total": interaction.accepted_ratings_total,
        "repeated_ratings_total": interaction.repeated_ratings_total,
        "coupon_actions_total": interaction.coupon_actions_total,
        "menu_actions_total": interaction.menu_actions_total,
        "last_event_received_at": (
            interaction.last_event_received_at.isoformat()
            if interaction.last_event_received_at
            else None
        ),
    }


def _normalize_allowed_providers(raw_value: Any) -> set[str]:
    if isinstance(raw_value, str):
        values = raw_value.split(",")
    else:
        try:
            values = list(raw_value or [])
        except TypeError:
            values = [raw_value]
    return {str(value or "").strip().lower() for value in values if str(value or "").strip()}


def _add_check(
    checks: list[dict[str, Any]],
    *,
    code: str,
    status: str,
    message: str,
    details: dict[str, Any],
) -> None:
    checks.append(
        {
            "code": code,
            "status": status,
            "message": message,
            "details": details,
        }
    )


def _resolve_overall_status(checks: list[dict[str, Any]]) -> str:
    if any(item["status"] == "blocked" for item in checks):
        return "blocked"
    if any(item["status"] == "warning" for item in checks):
        return "warning"
    return "ready"
