"""
Сервис моста из входящего webhook (notificationType=1) в журнал OLAP-синхронизации.

Назначение:
1. Принять webhook-событие о транзакции/балансе;
2. Подготовить детерминированный idempotency_key;
3. Идемпотентно создать запись в `OlapCheckSyncJournal`.

Важно:
- сервис не ломает основной поток обработки webhook;
- при дубле запись не создаётся повторно, а возвращается статус `created=False`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
from typing import Any

from django.db import IntegrityError
from django.utils import timezone

from guests.models import Guest, OlapCheckSyncJournal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OlapWebhookBridgeResult:
    """
    Результат постановки задачи в `OlapCheckSyncJournal`.
    """

    created: bool
    row_id: int | None
    reason: str


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_event_at(changed_on: Any) -> datetime:
    if changed_on:
        try:
            event_at = datetime.fromisoformat(str(changed_on))
        except ValueError:
            event_at = timezone.now()
    else:
        event_at = timezone.now()

    if timezone.is_naive(event_at):
        event_at = timezone.make_aware(event_at, timezone.get_current_timezone())
    return event_at


def _build_idempotency_key(*, webhook_id: Any, event: dict[str, Any]) -> str:
    canonical_payload = {
        "webhook_id": str(webhook_id or ""),
        "event_id": str(event.get("id") or ""),
        "transaction_id": str(event.get("transactionId") or ""),
        "order_number": _to_int(event.get("orderNumber")),
        "changed_on": str(event.get("changedOn") or ""),
        "terminal_group_id": str(event.get("terminalGroupId") or ""),
        "organization_id": str(event.get("organizationId") or ""),
    }
    canonical_json = json.dumps(canonical_payload, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return f"webhook_nt1:{digest}"


def enqueue_olap_sync_from_webhook(
    *,
    webhook: dict[str, Any],
    guest: Guest | None = None,
) -> OlapWebhookBridgeResult:
    """
    Ставит задачу дозагрузки чека в `OlapCheckSyncJournal` по webhook notificationType=1.

    Параметры:
    - webhook: исходный webhook-объект c `id` и `parsed_body`;
    - guest: опционально уже найденный гость (чтобы не делать повторный поиск).
    """

    event = webhook.get("parsed_body") or {}
    if not isinstance(event, dict):
        return OlapWebhookBridgeResult(created=False, row_id=None, reason="parsed_body не словарь")

    notification_type = _to_int(event.get("notificationType"))
    if notification_type != 1:
        return OlapWebhookBridgeResult(
            created=False,
            row_id=None,
            reason=f"notificationType={notification_type}, ожидаем 1",
        )

    order_number = _to_int(event.get("orderNumber"))
    if order_number is None:
        return OlapWebhookBridgeResult(
            created=False,
            row_id=None,
            reason="В webhook отсутствует orderNumber, задача OLAP не создана",
        )

    webhook_id = webhook.get("id")
    event_at = _parse_event_at(event.get("changedOn"))
    idempotency_key = _build_idempotency_key(webhook_id=webhook_id, event=event)
    source_webhook_id = str(webhook_id or "").strip() or None

    defaults = {
        "guest": guest,
        "source_webhook_id": source_webhook_id,
        "organization_id": (str(event.get("organizationId") or "").strip() or None),
        "terminal_group_id": (str(event.get("terminalGroupId") or "").strip() or None),
        "order_number": order_number,
        "order_external_id": (str(event.get("orderId") or "").strip() or None),
        "transaction_id": (str(event.get("transactionId") or "").strip() or None),
        "event_at": event_at,
        "business_date": event_at.date(),
        "department_id": (str(event.get("departmentId") or "").strip() or None),
        "department_code": (str(event.get("departmentCode") or "").strip() or None),
        "restoraunt_group_id": (
            str(event.get("restaurantGroupId") or event.get("restorauntGroupId") or "").strip() or None
        ),
    }

    try:
        row, created = OlapCheckSyncJournal.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults=defaults,
        )
    except IntegrityError:
        row = OlapCheckSyncJournal.objects.filter(idempotency_key=idempotency_key).first()
        return OlapWebhookBridgeResult(
            created=False,
            row_id=getattr(row, "id", None),
            reason="Конкурентный дубль idempotency_key",
        )

    if created:
        logger.info(
            "OLAP bridge: создана задача id=%s для webhook_id=%s order_number=%s",
            row.id,
            source_webhook_id,
            order_number,
        )
        return OlapWebhookBridgeResult(created=True, row_id=row.id, reason="Задача создана")

    logger.info(
        "OLAP bridge: дубль задачи, пропуск (id=%s, webhook_id=%s, order_number=%s)",
        row.id,
        source_webhook_id,
        order_number,
    )
    return OlapWebhookBridgeResult(created=False, row_id=row.id, reason="Дубль задачи")
