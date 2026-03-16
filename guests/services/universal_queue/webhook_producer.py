import logging
import re
from typing import Any, Dict

from django.conf import settings

from guests.models import DispatchTask, Guest
from guests.services.universal_queue.notification_producer import enqueue_guest_notification_tasks

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
    Определяет, нужно ли ставить задачу на отправку по этому webhook.
    """
    notify_types_raw = getattr(settings, "UNIVERSAL_QUEUE_WEBHOOK_NOTIFY_TYPES", "")
    if notify_types_raw:
        allowed = {item.strip() for item in notify_types_raw.split(",") if item.strip()}
        return str(event.get("notificationType")) in allowed

    # Базовая эвристика: если есть текст — webhook считается коммуникационным.
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


def enqueue_high_priority_webhook_tasks(webhook: Dict[str, Any]) -> int:
    """
    Создаёт high-priority DispatchTask для входящего webhook.

    Важно:
    1. Механизм работает только при включённом feature-flag.
    2. Текущий контур обработки webhook не ломается при ошибках producer-а.
    3. Дедупликация выполняется через idempotency_key на основе `source_key`.
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

    webhook_id = webhook.get("id")
    priority = _resolve_priority()
    message_text = _build_message_text(event)
    primary_only = bool(getattr(settings, "UNIVERSAL_QUEUE_WEBHOOK_PRIMARY_ONLY", True))
    fallback_legacy = bool(getattr(settings, "UNIVERSAL_QUEUE_FALLBACK_OLD_TG_LINKS", True))

    return enqueue_guest_notification_tasks(
        guest=guest,
        message_text=message_text,
        source_type=DispatchTask.SourceType.WEBHOOK,
        source_key=str(webhook_id or ""),
        priority=priority,
        primary_only=primary_only,
        payload={
            "webhook_id": webhook_id,
            "notification_type": event.get("notificationType"),
            "event": event,
        },
        fallback_legacy=fallback_legacy,
    )
