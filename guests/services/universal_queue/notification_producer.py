"""
Унифицированный producer уведомлений для бизнес-событий.

В целевой архитектуре маршрутизация выполняется только через `GuestBotBinding`.
Fallback на `GuestChannelLink` удалён.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from django.utils import timezone

from guests.models import (
    DispatchTask,
    Guest,
    GuestBotBinding,
    InteractionButtonSet,
    NotificationEvent,
    NotificationScenario,
)
from guests.services.message_interaction_outgoing import (
    DispatchTaskCreationSpec,
    create_dispatch_tasks_with_optional_interactions,
    interactions_enabled_for_new_task,
)

logger = logging.getLogger(__name__)


def _normalize_priority(priority: str, default: str = DispatchTask.Priority.HIGH) -> str:
    """
    Нормализует приоритет задачи под допустимые значения модели DispatchTask.
    """
    value = str(priority or "").strip().lower()
    allowed = {
        DispatchTask.Priority.HIGH,
        DispatchTask.Priority.NORMAL,
        DispatchTask.Priority.BULK,
    }
    return value if value in allowed else default


def _normalize_allowed_bot_profile_ids(allowed_bot_profile_ids: Optional[Iterable[int]]) -> Optional[List[int]]:
    """
    Нормализует фильтр bot_profile_id для безопасной маршрутизации.

    Правила:
    1. `None` -> фильтр не задан;
    2. Невалидные/неположительные значения отбрасываются;
    3. Дубли удаляются с сохранением порядка.
    """
    if allowed_bot_profile_ids is None:
        return None

    normalized: List[int] = []
    for raw_id in allowed_bot_profile_ids:
        try:
            bot_profile_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if bot_profile_id <= 0:
            continue
        if bot_profile_id not in normalized:
            normalized.append(bot_profile_id)
    return normalized


def _normalize_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Безопасно нормализует payload для сохранения в JSONField.

    Если payload не является словарём, возвращаем диагностический безопасный
    контейнер, чтобы не падать в рантайме и не терять факт события.
    """
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)

    logger.warning(
        "Notification enqueue: payload имеет неподдерживаемый тип '%s', используется fallback-структура.",
        type(payload).__name__,
    )
    return {
        "payload_error": "invalid_payload_type",
        "payload_type": type(payload).__name__,
        "payload_preview": str(payload)[:500],
    }


def _collect_targets_from_bindings(
    guest: Guest,
    primary_only: bool,
    allowed_bot_profile_ids: Optional[Iterable[int]] = None,
) -> List[Dict[str, Any]]:
    """
    Собирает цели отправки из модели GuestBotBinding.
    """
    query = (
        GuestBotBinding.objects.select_related("bot")
        .filter(
            guest=guest,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
            bot__is_active=True,
        )
        .exclude(external_chat_id__isnull=True)
        .exclude(external_chat_id="")
        .order_by("-is_primary", "id")
    )
    if primary_only:
        query = query.filter(is_primary=True)
    normalized_bot_ids = _normalize_allowed_bot_profile_ids(allowed_bot_profile_ids)
    if allowed_bot_profile_ids is not None:
        if not normalized_bot_ids:
            logger.warning(
                "Notification enqueue: передан пустой/некорректный список allowed_bot_profile_ids, цели отправки не выбраны."
            )
            return []
        query = query.filter(bot_id__in=normalized_bot_ids)

    targets: List[Dict[str, Any]] = []
    for binding in query:
        provider_type = binding.bot.provider_type
        if provider_type not in ("telegram", "max", "vk"):
            continue
        targets.append(
            {
                "provider_type": provider_type,
                "external_chat_id": str(binding.external_chat_id).strip(),
                "guest_binding": binding,
                "bot_profile": binding.bot,
            }
        )
    return targets


def enqueue_guest_notification_tasks(
    guest: Guest,
    message_text: str,
    *,
    source_type: str = DispatchTask.SourceType.SYSTEM,
    source_key: str = "",
    priority: str = DispatchTask.Priority.HIGH,
    primary_only: bool = True,
    payload: Optional[Dict[str, Any]] = None,
    notification_scenario: Optional[NotificationScenario] = None,
    notification_event: Optional[NotificationEvent] = None,
    available_at: Optional[datetime] = None,
    allowed_bot_profile_ids: Optional[Iterable[int]] = None,
) -> int:
    """
    Универсальный producer задач уведомления для одного гостя.

    Использование:
    1. Веб-хуки (high priority).
    2. Бизнес-события без веб-хука.
    3. Ручные системные триггеры.
    """
    if guest is None:
        return 0

    safe_message = str(message_text or "").strip()
    if not safe_message:
        return 0

    safe_payload = _normalize_payload(payload)
    safe_priority = _normalize_priority(priority=priority, default=DispatchTask.Priority.HIGH)

    targets = _collect_targets_from_bindings(
        guest=guest,
        primary_only=primary_only,
        allowed_bot_profile_ids=allowed_bot_profile_ids,
    )
    if not targets:
        logger.info("Notification enqueue: нет доступных bot-привязок для guest_id=%s", guest.id)
        return 0

    now = timezone.now()
    task_available_at = available_at if available_at is not None else now
    safe_source_key = str(source_key or "").strip()
    button_set = (
        notification_scenario.button_set
        if notification_scenario is not None
        else InteractionButtonSet.NONE
    )
    tracked_link_destination = (
        notification_scenario.tracked_link_destination
        if notification_scenario is not None
        and button_set == InteractionButtonSet.RATING_MENU_LINK
        else None
    )
    specifications: list[DispatchTaskCreationSpec] = []

    for target in targets:
        provider_type = target["provider_type"]
        external_chat_id = str(target["external_chat_id"]).strip()

        idempotency_key = None
        if safe_source_key:
            idempotency_key = (
                f"{source_type}:{safe_source_key}:guest:{guest.id}:provider:{provider_type}:chat:{external_chat_id}"
            )

        specifications.append(
            DispatchTaskCreationSpec(
                button_set=button_set,
                interaction_enabled=interactions_enabled_for_new_task(provider_type),
                tracked_link_destination=tracked_link_destination,
                dispatch_task_fields={
                    "source_type": source_type,
                    "provider_type": provider_type,
                    "priority": safe_priority,
                    "status": DispatchTask.Status.PENDING,
                    "guest": guest,
                    "notification_scenario": notification_scenario,
                    "notification_event": notification_event,
                    "bot_profile": target["bot_profile"],
                    "guest_binding": target["guest_binding"],
                    "external_chat_id": external_chat_id,
                    "message_text": safe_message,
                    "payload": safe_payload,
                    "scheduled_at": task_available_at,
                    "available_at": task_available_at,
                    "idempotency_key": idempotency_key,
                },
            )
        )

    creation_result = create_dispatch_tasks_with_optional_interactions(specifications)
    for position in sorted(creation_result.duplicate_positions):
        duplicate_key = specifications[position].dispatch_task_fields.get("idempotency_key")
        logger.info("Постановка уведомления: дублирующая задача пропущена (%s)", duplicate_key)
    for position, error in creation_result.errors.items():
        logger.error(
            "Ошибка пакетной постановки уведомления: позиция=%s тип=%s",
            position,
            type(error).__name__,
        )
    return len(creation_result.created_tasks)
