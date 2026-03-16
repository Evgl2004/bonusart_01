import logging
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from guests.models import DispatchTask, Guest, GuestBotBinding, GuestChannelLink, MailingChannel

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


def _collect_targets_from_bindings(guest: Guest, primary_only: bool) -> List[Dict[str, Any]]:
    """
    Собирает цели отправки из новой модели GuestBotBinding.
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
                "legacy_channel_id": None,
            }
        )
    return targets


def _collect_targets_from_legacy_links(guest: Guest) -> List[Dict[str, Any]]:
    """
    Fallback на legacy GuestChannelLink (только Telegram).
    """
    query = (
        GuestChannelLink.objects.select_related("channel")
        .filter(
            guest=guest,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
            channel__is_active=True,
            channel__channel_kind__in=[
                MailingChannel.ChannelKind.PHONE_TELEGRAM,
                MailingChannel.ChannelKind.PHONE_TELEGRAM_BOT,
            ],
        )
        .exclude(external_chat_id__isnull=True)
        .exclude(external_chat_id="")
        .order_by("id")
    )

    targets: List[Dict[str, Any]] = []
    for link in query:
        targets.append(
            {
                "provider_type": "telegram",
                "external_chat_id": str(link.external_chat_id).strip(),
                "guest_binding": None,
                "bot_profile": None,
                "legacy_channel_id": link.channel_id,
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
    fallback_legacy: Optional[bool] = None,
) -> int:
    """
    Универсальный producer задач уведомления для одного гостя.

    Использование:
    1. Веб-хуки (high priority).
    2. Бизнес-события без веб-хука (balance changes, сервисные уведомления).
    3. Ручные системные триггеры.
    """
    if guest is None:
        return 0

    safe_message = str(message_text or "").strip()
    if not safe_message:
        return 0

    safe_payload: Dict[str, Any] = dict(payload or {})
    safe_priority = _normalize_priority(priority=priority, default=DispatchTask.Priority.HIGH)
    use_legacy_fallback = (
        bool(getattr(settings, "UNIVERSAL_QUEUE_FALLBACK_OLD_TG_LINKS", True))
        if fallback_legacy is None
        else bool(fallback_legacy)
    )

    targets = _collect_targets_from_bindings(guest=guest, primary_only=primary_only)
    if not targets and use_legacy_fallback:
        targets = _collect_targets_from_legacy_links(guest=guest)

    if not targets:
        logger.info("Notification enqueue: нет доступных каналов для guest_id=%s", guest.id)
        return 0

    created_count = 0
    now = timezone.now()
    safe_source_key = str(source_key or "").strip()

    for target in targets:
        provider_type = target["provider_type"]
        external_chat_id = str(target["external_chat_id"]).strip()
        legacy_channel_id = target.get("legacy_channel_id")

        idempotency_key = None
        if safe_source_key:
            idempotency_key = (
                f"{source_type}:{safe_source_key}:guest:{guest.id}:provider:{provider_type}:chat:{external_chat_id}"
            )

        task_payload = dict(safe_payload)
        task_payload["legacy_channel_id"] = legacy_channel_id

        try:
            DispatchTask.objects.create(
                source_type=source_type,
                provider_type=provider_type,
                priority=safe_priority,
                status=DispatchTask.Status.PENDING,
                guest=guest,
                bot_profile=target["bot_profile"],
                guest_binding=target["guest_binding"],
                external_chat_id=external_chat_id,
                message_text=safe_message,
                payload=task_payload,
                scheduled_at=now,
                available_at=now,
                idempotency_key=idempotency_key,
            )
            created_count += 1
        except IntegrityError:
            logger.info("Notification enqueue: дубль задачи, пропуск (%s)", idempotency_key)

    return created_count
