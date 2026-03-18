"""
Обработчик webhook-сценария `balance_changed`.

Модуль изолирует логику постановки уведомления об изменении баланса:
1. валидация, что webhook относится к категории баланса;
2. поиск/восстановление гостя;
3. создание `NotificationEvent -> DispatchTask` через общий контур.

Важно:
1. модуль не импортирует `webhooks.py`, чтобы избежать циклических зависимостей;
2. импорт iiko-клиента выполняется лениво только при необходимости.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, Optional

from django.utils import timezone

from guests.models import DispatchTask, Guest
from guests.services.notification_events import (
    ScenarioNotConfiguredError,
    enqueue_notification_event_from_scenario,
)
from guests.services.notification_registry import SCENARIO_CODE_BALANCE_CHANGED
from guests.services.universal_queue.notification_producer import enqueue_guest_notification_tasks

logger = logging.getLogger(__name__)


BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID = "BSamfrT83o4Cw5ZG1m4RU7N4CtW6WR2M"
PHONE_RE = re.compile(r"Имя \(Guest\.Name\):\s*(\+\d+)")


def _extract_balance_change_value(event: dict) -> Optional[str]:
    for field_name in ("changeSum", "newBalance", "balance", "sum"):
        value = event.get(field_name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _extract_category_external_id(webhook: dict, event: dict) -> str:
    candidates = (
        webhook.get("category_id_ext"),
        event.get("category_id_ext"),
        event.get("categoryExternalId"),
        event.get("categoryId"),
    )
    for value in candidates:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def is_balance_webhook(webhook: dict, event: dict) -> bool:
    """
    Проверяет, что webhook относится к сценарию «Баланс».
    """
    return _extract_category_external_id(webhook, event) == BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID


def _build_balance_notification_text(event: dict) -> str:
    text = str(event.get("text") or "").strip()
    if text:
        return text

    value = _extract_balance_change_value(event)
    if value is not None:
        return f"Изменение баланса: {value}"
    return "Произошло изменение баланса."


def _find_guest(event: dict) -> Optional[Guest]:
    phone = event.get("phone")
    if not phone and "text" in event:
        match = PHONE_RE.search(event["text"] or "")
        if match:
            phone = match.group(1)

    if phone:
        guest_by_phone = Guest.objects.filter(phone=phone).first()
        if guest_by_phone is not None:
            return guest_by_phone

    customer_id = event.get("customerId")
    if customer_id:
        return Guest.objects.filter(iiko_id=customer_id).first()

    return None


def _get_or_create_guest_from_iiko(phone: str) -> Optional[Guest]:
    """
    Пытается получить гостя из iiko и создать/обновить запись в БД.

    Импорт `iiko_client` выполняется лениво, чтобы модуль можно было
    безопасно импортировать в окружениях без настроенного iiko API.
    """
    if not phone:
        return None

    try:
        from guests.services.iiko_client import iiko_client
    except Exception as err:
        logger.warning("iiko_client недоступен при обработке balance webhook: %s", err)
        return None

    try:
        data = iiko_client.get_customer_by_phone(phone)
    except Exception as err:
        logger.error("Ошибка запроса к iiko API при обработке balance webhook: %s", err)
        return None

    if not data:
        return None

    customer = data.get("customer") or data
    iiko_id = customer.get("id")
    if not iiko_id:
        return None

    guest, created = Guest.objects.get_or_create(
        iiko_id=iiko_id,
        defaults={
            "phone": customer.get("phone"),
            "first_name": customer.get("name") or "",
            "last_name": customer.get("surname") or "",
            "email": customer.get("email") or "",
            "gender": customer.get("sex") or None,
            "birthdate": customer.get("birthdate") or None,
            "created_at": timezone.now(),
            "updated_at": timezone.now(),
        },
    )
    if not created:
        guest.updated_at = timezone.now()
        guest.save(update_fields=["updated_at"])
    return guest


def _build_balance_dedupe_key(webhook: dict, event: dict, guest_id: int) -> str:
    webhook_id = webhook.get("id")
    if webhook_id:
        return f"balance:webhook:{webhook_id}"

    stable_payload: Dict[str, Any] = {
        "guest_id": guest_id,
        "category_id_ext": _extract_category_external_id(webhook, event),
        "changed_on": str(event.get("changedOn") or ""),
        "notification_type": str(event.get("notificationType") or ""),
        "value": str(_extract_balance_change_value(event) or ""),
    }
    digest = hashlib.sha1(
        json.dumps(stable_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return f"balance:fallback:{digest}"


def enqueue_balance_notification_from_webhook(
    webhook: dict,
    *,
    is_enabled: bool = True,
    priority: str = DispatchTask.Priority.HIGH,
    primary_only: bool = True,
) -> int:
    """
    Явный бизнес-вызов постановки уведомления о балансе в universal queue.
    """
    if not is_enabled:
        return 0

    event = webhook.get("parsed_body") or {}
    if not isinstance(event, dict):
        return 0

    if not is_balance_webhook(webhook, event):
        return 0

    guest = _find_guest(event)
    if guest is None and event.get("phone"):
        guest = _get_or_create_guest_from_iiko(str(event["phone"]))
    if guest is None:
        logger.info("Balance webhook enqueue: гость не найден, задача не создана.")
        return 0

    message_text = _build_balance_notification_text(event)
    if not message_text:
        return 0

    webhook_id = webhook.get("id")
    dedupe_key = _build_balance_dedupe_key(webhook=webhook, event=event, guest_id=guest.id)
    payload = {
        "webhook_id": webhook_id,
        "notification_type": event.get("notificationType"),
        "kind": "balance_changed",
        "event": event,
    }

    try:
        return enqueue_notification_event_from_scenario(
            scenario_code=SCENARIO_CODE_BALANCE_CHANGED,
            guest=guest,
            dedupe_key=dedupe_key,
            source_ref=str(webhook_id or ""),
            event_source_type="webhook",
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload=payload,
            template_context={
                "message_text": message_text,
                "guest_id": guest.id,
                "balance_change_value": _extract_balance_change_value(event) or "",
            },
            fallback_message_text=message_text,
        )
    except ScenarioNotConfiguredError:
        logger.warning(
            "Balance scenario '%s' не найден/выключен. Используется fallback-продюсер.",
            SCENARIO_CODE_BALANCE_CHANGED,
        )
        return enqueue_guest_notification_tasks(
            guest=guest,
            message_text=message_text,
            source_type=DispatchTask.SourceType.WEBHOOK,
            source_key=f"balance:{webhook_id or ''}",
            priority=priority,
            primary_only=primary_only,
            payload=payload,
        )
