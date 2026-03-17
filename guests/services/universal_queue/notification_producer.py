"""
Унифицированный producer уведомлений для бизнес-событий.

В целевой архитектуре маршрутизация выполняется только через `GuestBotBinding`.
Fallback на `GuestChannelLink` удалён.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from django.db import IntegrityError
from django.utils import timezone

from guests.models import DispatchTask, Guest, GuestBotBinding, NotificationEvent, NotificationScenario

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
    if allowed_bot_profile_ids is not None:
        bot_ids = [int(bot_id) for bot_id in allowed_bot_profile_ids]
        if bot_ids:
            query = query.filter(bot_id__in=bot_ids)

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

    safe_payload: Dict[str, Any] = dict(payload or {})
    safe_priority = _normalize_priority(priority=priority, default=DispatchTask.Priority.HIGH)

    targets = _collect_targets_from_bindings(
        guest=guest,
        primary_only=primary_only,
        allowed_bot_profile_ids=allowed_bot_profile_ids,
    )
    if not targets:
        logger.info("Notification enqueue: нет доступных bot-привязок для guest_id=%s", guest.id)
        return 0

    created_count = 0
    now = timezone.now()
    task_available_at = available_at if available_at is not None else now
    safe_source_key = str(source_key or "").strip()

    for target in targets:
        provider_type = target["provider_type"]
        external_chat_id = str(target["external_chat_id"]).strip()

        idempotency_key = None
        if safe_source_key:
            idempotency_key = (
                f"{source_type}:{safe_source_key}:guest:{guest.id}:provider:{provider_type}:chat:{external_chat_id}"
            )

        try:
            DispatchTask.objects.create(
                source_type=source_type,
                provider_type=provider_type,
                priority=safe_priority,
                status=DispatchTask.Status.PENDING,
                guest=guest,
                notification_scenario=notification_scenario,
                notification_event=notification_event,
                bot_profile=target["bot_profile"],
                guest_binding=target["guest_binding"],
                external_chat_id=external_chat_id,
                message_text=safe_message,
                payload=safe_payload,
                scheduled_at=task_available_at,
                available_at=task_available_at,
                idempotency_key=idempotency_key,
            )
            created_count += 1
        except IntegrityError:
            logger.info("Notification enqueue: дубль задачи, пропуск (%s)", idempotency_key)

    return created_count
