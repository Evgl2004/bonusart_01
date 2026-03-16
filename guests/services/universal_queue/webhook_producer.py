import logging
import re
from typing import Any, Dict, List

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from guests.models import DispatchTask, Guest, GuestBotBinding, GuestChannelLink, MailingChannel

logger = logging.getLogger(__name__)

# Поддержка legacy-формата телефона в тексте веб-хука.
PHONE_RE = re.compile(r"Имя \(Guest\.Name\):\s*(\+\d+)")


def _extract_phone(event: Dict[str, Any]) -> str:
    """
    Извлекает номер телефона из payload веб-хука.
    """
    raw_phone = event.get("phone")
    if raw_phone:
        return str(raw_phone).strip()

    raw_text = event.get("text") or ""
    match = PHONE_RE.search(str(raw_text))
    if match:
        return match.group(1).strip()
    return ""


def _resolve_guest(event: Dict[str, Any]) -> Guest | None:
    """
    Ищет гостя по телефону, затем по customerId.
    """
    phone = _extract_phone(event)
    if phone:
        guest = Guest.objects.filter(phone=phone).first()
        if guest:
            return guest

    customer_id = event.get("customerId")
    if customer_id:
        return Guest.objects.filter(iiko_id=str(customer_id)).first()

    return None


def _should_enqueue(event: Dict[str, Any]) -> bool:
    """
    Определяет, нужно ли ставить задачу на отправку по данному webhook.
    """
    notify_types_raw = getattr(settings, "UNIVERSAL_QUEUE_WEBHOOK_NOTIFY_TYPES", "")
    if notify_types_raw:
        allowed = {item.strip() for item in notify_types_raw.split(",") if item.strip()}
        return str(event.get("notificationType")) in allowed

    # Базовая эвристика: если есть текст, считаем webhook коммуникационным.
    return bool(str(event.get("text") or "").strip())


def _resolve_priority() -> str:
    """
    Возвращает приоритет для webhook-задач с валидацией.
    """
    raw_priority = str(getattr(settings, "UNIVERSAL_QUEUE_WEBHOOK_PRIORITY", "high")).strip().lower()
    allowed = {
        DispatchTask.Priority.HIGH,
        DispatchTask.Priority.NORMAL,
        DispatchTask.Priority.BULK,
    }
    return raw_priority if raw_priority in allowed else DispatchTask.Priority.HIGH


def _build_message_text(event: Dict[str, Any]) -> str:
    """
    Формирует текст сообщения для отправки в чат.
    """
    raw_text = str(event.get("text") or "").strip()
    if raw_text:
        return raw_text

    for field_name in ("sum", "balance", "newBalance", "changeSum"):
        if field_name in event and event[field_name] is not None:
            return f"Изменение баланса: {event[field_name]}"

    notification_type = event.get("notificationType")
    return f"Системное уведомление (тип {notification_type})."


def _collect_targets_from_bindings(guest: Guest, primary_only: bool) -> List[Dict[str, Any]]:
    """
    Собирает целевые каналы из новой модели GuestBotBinding.
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
                "external_chat_id": binding.external_chat_id,
                "guest_binding": binding,
                "bot_profile": binding.bot,
            }
        )
    return targets


def _collect_targets_from_legacy_links(guest: Guest) -> List[Dict[str, Any]]:
    """
    Fallback на существующую схему GuestChannelLink (только Telegram).
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
                "external_chat_id": link.external_chat_id,
                "guest_binding": None,
                "bot_profile": None,
            }
        )
    return targets


def enqueue_high_priority_webhook_tasks(webhook: Dict[str, Any]) -> int:
    """
    Создаёт high-priority задачи DispatchTask на основе входящего webhook.

    Важно:
    1. Механизм работает только при включенном feature flag.
    2. Текущий контур обработки webhook не ломается при ошибке постановки задач.
    3. Для защиты от дублей используется idempotency_key.
    """
    if not getattr(settings, "UNIVERSAL_QUEUE_ENABLE_WEBHOOK_ENQUEUE", False):
        return 0

    event = webhook.get("parsed_body") or {}
    if not isinstance(event, dict):
        return 0

    if not _should_enqueue(event):
        return 0

    guest = _resolve_guest(event)
    if guest is None:
        logger.info("Webhook enqueue: гость не найден, задача не создана.")
        return 0

    priority = _resolve_priority()
    message_text = _build_message_text(event)
    webhook_id = webhook.get("id")
    primary_only = bool(getattr(settings, "UNIVERSAL_QUEUE_WEBHOOK_PRIMARY_ONLY", True))

    targets = _collect_targets_from_bindings(guest=guest, primary_only=primary_only)
    if not targets and bool(getattr(settings, "UNIVERSAL_QUEUE_FALLBACK_OLD_TG_LINKS", True)):
        targets = _collect_targets_from_legacy_links(guest=guest)

    if not targets:
        logger.info("Webhook enqueue: нет доступных каналов для guest_id=%s", guest.id)
        return 0

    created_count = 0
    now = timezone.now()
    for target in targets:
        provider_type = target["provider_type"]
        external_chat_id = str(target["external_chat_id"]).strip()
        idempotency_key = (
            f"webhook:{webhook_id}:guest:{guest.id}:provider:{provider_type}:chat:{external_chat_id}"
        )

        try:
            DispatchTask.objects.create(
                source_type=DispatchTask.SourceType.WEBHOOK,
                provider_type=provider_type,
                priority=priority,
                status=DispatchTask.Status.PENDING,
                guest=guest,
                bot_profile=target["bot_profile"],
                guest_binding=target["guest_binding"],
                external_chat_id=external_chat_id,
                message_text=message_text,
                payload={
                    "webhook_id": webhook_id,
                    "notification_type": event.get("notificationType"),
                    "event": event,
                },
                scheduled_at=now,
                available_at=now,
                idempotency_key=idempotency_key,
            )
            created_count += 1
        except IntegrityError:
            logger.info("Webhook enqueue: дубликат задачи, пропускаем (%s)", idempotency_key)

    return created_count
