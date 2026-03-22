"""
Сервис моста из входящего webhook (notificationType=1) в журнал OLAP-синхронизации.

Назначение:
1. Принять webhook-событие о транзакции/балансе;
2. Подготовить детерминированный idempotency_key;
3. Идемпотентно создать запись в `OlapCheckSyncJournal`;
4. Восстановить `department_id` по `terminalGroupId`, если в webhook нет `departmentId`.
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

from guests.models import Guest, OlapCheckSyncJournal, TerminalDepartmentMap

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


def _normalize_text(value: Any) -> str | None:
    """
    Нормализует строковое поле: `None`/пустая строка -> `None`.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_event_at(changed_on: Any) -> datetime:
    """
    Парсит метку времени события webhook.
    """
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
    """
    Формирует детерминированный ключ задачи для защиты от дублей.
    """
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


def _resolve_department_fields(event: dict[str, Any]) -> dict[str, Any]:
    """
    Возвращает поля заведения для `OlapCheckSyncJournal`.

    Важно:
    1. терминал должен присутствовать в активном справочнике `TerminalDepartmentMap`;
    2. только такие терминалы попадают в OLAP-контур;
    3. `departmentId` в приоритете из webhook, иначе берётся из mapping.
    """
    organization_id = _normalize_text(event.get("organizationId"))
    terminal_group_id = _normalize_text(event.get("terminalGroupId"))

    explicit_department_id = _normalize_text(event.get("departmentId"))
    explicit_department_code = _normalize_text(event.get("departmentCode"))
    explicit_rest_group_id = _normalize_text(
        event.get("restaurantGroupId") or event.get("restorauntGroupId")
    )

    if not terminal_group_id:
        return {
            "organization_id": organization_id,
            "terminal_group_id": None,
            "department_id": None,
            "department_code": explicit_department_code,
            "restoraunt_group_id": explicit_rest_group_id,
            "mapping_used": False,
            "terminal_allowed": False,
        }

    mapping = (
        TerminalDepartmentMap.objects.filter(
            terminal_group_id=terminal_group_id,
            is_active=True,
        )
        .order_by("-verified_at", "-updated_at", "-id")
        .first()
    )
    if mapping is None:
        return {
            "organization_id": organization_id,
            "terminal_group_id": terminal_group_id,
            "department_id": None,
            "department_code": explicit_department_code,
            "restoraunt_group_id": explicit_rest_group_id,
            "mapping_used": False,
            "terminal_allowed": False,
        }

    mapping_organization_id = _normalize_text(mapping.organization_id)
    if (
        organization_id
        and mapping_organization_id
        and organization_id != mapping_organization_id
    ):
        logger.warning(
            "OLAP bridge: mapping terminal=%s не применен из-за несовпадения "
            "organization_id (webhook=%s, mapping=%s).",
            terminal_group_id,
            organization_id,
            mapping_organization_id,
        )
        return {
            "organization_id": organization_id,
            "terminal_group_id": terminal_group_id,
            "department_id": None,
            "department_code": explicit_department_code,
            "restoraunt_group_id": explicit_rest_group_id,
            "mapping_used": False,
            "terminal_allowed": False,
        }

    resolved_department_id = explicit_department_id or _normalize_text(mapping.department_id)
    return {
        "organization_id": organization_id or mapping_organization_id,
        "terminal_group_id": terminal_group_id,
        "department_id": resolved_department_id,
        "department_code": explicit_department_code or _normalize_text(mapping.department_code),
        "restoraunt_group_id": explicit_rest_group_id or _normalize_text(mapping.restoraunt_group_id),
        "mapping_used": bool((not explicit_department_id) and resolved_department_id),
        "terminal_allowed": True,
    }


def enqueue_olap_sync_from_webhook(
    *,
    webhook: dict[str, Any],
    guest: Guest | None = None,
) -> OlapWebhookBridgeResult:
    """
    Ставит задачу дозагрузки чека в `OlapCheckSyncJournal` по webhook `notificationType=1`.
    """
    event = webhook.get("parsed_body") or {}
    if not isinstance(event, dict):
        return OlapWebhookBridgeResult(
            created=False,
            row_id=None,
            reason="parsed_body не словарь",
        )

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
    source_webhook_id = _normalize_text(webhook_id)
    resolved_department = _resolve_department_fields(event)

    if not resolved_department.get("terminal_allowed"):
        terminal_id = resolved_department.get("terminal_group_id")
        return OlapWebhookBridgeResult(
            created=False,
            row_id=None,
            reason=(
                "Пропуск OLAP-задачи: terminalGroupId не входит в активный список заведений "
                f"(terminalGroupId={terminal_id or '-'})"
            ),
        )

    defaults = {
        "guest": guest,
        "source_webhook_id": source_webhook_id,
        "organization_id": resolved_department["organization_id"],
        "terminal_group_id": resolved_department["terminal_group_id"],
        "order_number": order_number,
        "order_external_id": _normalize_text(event.get("orderId")),
        "transaction_id": _normalize_text(event.get("transactionId")),
        "event_at": event_at,
        "business_date": event_at.date(),
        "department_id": resolved_department["department_id"],
        "department_code": resolved_department["department_code"],
        "restoraunt_group_id": resolved_department["restoraunt_group_id"],
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
        if resolved_department.get("mapping_used"):
            logger.info(
                "OLAP bridge: department_id восстановлен по mapping "
                "terminalGroupId=%s -> department_id=%s",
                defaults["terminal_group_id"],
                defaults["department_id"],
            )
        logger.info(
            "OLAP bridge: создана задача id=%s для webhook_id=%s order_number=%s",
            row.id,
            source_webhook_id,
            order_number,
        )
        return OlapWebhookBridgeResult(
            created=True,
            row_id=row.id,
            reason="Задача создана",
        )

    logger.info(
        "OLAP bridge: дубль задачи, пропуск (id=%s, webhook_id=%s, order_number=%s)",
        row.id,
        source_webhook_id,
        order_number,
    )
    return OlapWebhookBridgeResult(
        created=False,
        row_id=row.id,
        reason="Дубль задачи",
    )
