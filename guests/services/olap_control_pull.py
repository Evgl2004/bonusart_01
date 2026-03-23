"""
Контрольная дозагрузка OLAP-задач по данным первичного OLAP-отчёта.

Назначение:
1. разово/по расписанию получить из OLAP все заказы за период по целевым Department.Id;
2. поставить недостающие задачи в `olap_check_sync_journal` (идемпотентно);
3. далее штатный `run_olap_sync_worker` дозагрузит позиции в raw и витрины.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
import hashlib
import json
import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from guests.models import Guest, OlapCheckSyncJournal, TerminalDepartmentMap
from guests.services.iiko_olap_client import IikoOlapClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OlapControlPullOptions:
    """
    Параметры одного цикла контрольной дозагрузки OLAP.
    """

    business_date_from: date
    business_date_to: date
    department_ids: set[str] | None = None
    dry_run: bool = True


@dataclass
class OlapControlPullStats:
    """
    Сводная статистика контрольной дозагрузки.
    """

    departments_scanned: int = 0
    departments_failed: int = 0
    olap_rows_seen: int = 0
    olap_rows_with_phone: int = 0
    olap_rows_without_phone: int = 0
    olap_rows_phone_without_guest: int = 0
    distinct_order_keys_seen: int = 0
    skipped_invalid_rows: int = 0
    would_create_journal_rows: int = 0
    created_journal_rows: int = 0
    duplicate_journal_rows: int = 0
    phone_fields_used: set[str] = field(default_factory=set)


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _row_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            return row.get(key)
    return None


def _normalize_phone(value: Any) -> str | None:
    """
    Приводит произвольный номер к формату +7XXXXXXXXXX.
    """
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) == 10:
        digits = f"7{digits}"
    elif len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    elif not (len(digits) == 11 and digits.startswith("7")):
        return None
    return f"+{digits}"


def _phone10(value: Any) -> str | None:
    normalized = _normalize_phone(value)
    if not normalized:
        return None
    digits = "".join(ch for ch in normalized if ch.isdigit())
    if len(digits) != 11 or not digits.startswith("7"):
        return None
    return digits[-10:]


def _build_guest_phone10_map() -> dict[str, int]:
    """
    Строит справочник phone10 -> guest_id для быстрого связывания OLAP-строк с гостями.
    При дублях phone10 оставляем первое соответствие.
    """
    result: dict[str, int] = {}
    for row in (
        Guest.objects.exclude(phone__isnull=True)
        .exclude(phone="")
        .values("id", "phone")
        .iterator(chunk_size=5000)
    ):
        phone10 = _phone10(row.get("phone"))
        if not phone10:
            continue
        result.setdefault(phone10, int(row["id"]))
    return result


def _build_control_pull_idempotency_key(
    *,
    department_id: str,
    business_date: date,
    order_number: int,
    uniq_order_id: str | None,
) -> str:
    canonical = {
        "source": "olap_control_pull",
        "department_id": department_id,
        "business_date": business_date.isoformat(),
        "order_number": order_number,
        "uniq_order_id": uniq_order_id or "",
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return f"olap_control_pull:{digest}"


class OlapControlPullService:
    """
    Контрольная постановка задач в журнал по данным прямого OLAP-среза.
    """

    ORDER_GROUP_FIELDS = [
        "OpenDate.Typed",
        "OrderNum",
        "UniqOrderId.Id",
        "Department.Id",
        "Department.Code",
        "RestorauntGroup.Id",
    ]
    # Берем идентификацию гостя по телефону клиента из доставки.
    PHONE_FIELD = "Delivery.CustomerPhone"
    # Дополнительно читаем номер карты клиента для возможного будущего анализа.
    # В текущую модель/таблицы это поле не сохраняется.
    CARD_FIELD = "Delivery.CustomerCard"
    ORDER_AGG_FIELDS = ["DishSumInt"]

    def __init__(self, *, client: IikoOlapClient) -> None:
        self.client = client

    @staticmethod
    def _resolve_department_scope(*, department_ids: set[str] | None) -> list[dict[str, Any]]:
        qs = TerminalDepartmentMap.objects.filter(is_active=True).order_by(
            "department_id",
            "-verified_at",
            "-updated_at",
            "-id",
        )
        if department_ids:
            qs = qs.filter(department_id__in=sorted(department_ids))

        resolved: dict[str, dict[str, Any]] = {}
        for item in qs.values(
            "organization_id",
            "terminal_group_id",
            "department_id",
            "department_code",
            "restoraunt_group_id",
        ):
            dept_id = _normalize_text(item.get("department_id"))
            if not dept_id or dept_id in resolved:
                continue
            resolved[dept_id] = item
        return list(resolved.values())

    def _fetch_department_rows(
        self,
        *,
        department_id: str,
        business_date_from: date,
        business_date_to: date,
    ) -> tuple[list[dict[str, Any]], str]:
        group_fields = [*self.ORDER_GROUP_FIELDS, self.PHONE_FIELD, self.CARD_FIELD]
        payload = self.client.build_sales_payload_for_department_window(
            date_from=business_date_from,
            date_to=business_date_to,
            department_ids=[department_id],
            aggregate_fields=self.ORDER_AGG_FIELDS,
            group_by_row_fields=group_fields,
        )
        response = self.client.query_olap(payload)
        data_rows = response.get("data")
        if isinstance(data_rows, list):
            return data_rows, self.PHONE_FIELD
        return [], self.PHONE_FIELD

    @staticmethod
    def _extract_row_phone(*, payload: dict[str, Any], phone_field: str) -> str | None:
        return _normalize_phone(_row_value(payload, phone_field))

    @staticmethod
    def _build_journal_defaults(
        *,
        scope_row: dict[str, Any],
        guest_id: int,
        business_day: date,
        order_number: int,
        uniq_order_id: str | None,
        department_id: str,
        department_code: str | None,
        restoraunt_group_id: str | None,
    ) -> dict[str, Any]:
        event_at_local = timezone.make_aware(
            datetime.combine(business_day, dt_time(hour=12, minute=0)),
            timezone.get_current_timezone(),
        )
        return {
            "guest_id": guest_id,
            "status": OlapCheckSyncJournal.Status.NEW,
            "source_webhook_id": "control_pull",
            "organization_id": _normalize_text(scope_row.get("organization_id")),
            "terminal_group_id": _normalize_text(scope_row.get("terminal_group_id")),
            "order_number": order_number,
            "order_external_id": uniq_order_id,
            "transaction_id": None,
            "event_at": event_at_local,
            "business_date": business_day,
            "department_id": department_id,
            "department_code": department_code,
            "restoraunt_group_id": restoraunt_group_id,
        }

    def run_cycle(self, *, options: OlapControlPullOptions) -> OlapControlPullStats:
        stats = OlapControlPullStats()
        scope_rows = self._resolve_department_scope(department_ids=options.department_ids)
        guest_phone10_map = _build_guest_phone10_map()

        if not scope_rows:
            logger.info("OLAP control pull: активные department mapping не найдены, цикл пропущен.")
            return stats

        for scope_row in scope_rows:
            department_id = _normalize_text(scope_row.get("department_id"))
            if not department_id:
                continue

            stats.departments_scanned += 1
            try:
                data_rows, used_phone_field = self._fetch_department_rows(
                    department_id=department_id,
                    business_date_from=options.business_date_from,
                    business_date_to=options.business_date_to,
                )
            except Exception:
                stats.departments_failed += 1
                logger.exception(
                    "OLAP control pull: ошибка загрузки OLAP для department_id=%s",
                    department_id,
                )
                continue

            stats.olap_rows_seen += len(data_rows)
            stats.phone_fields_used.add(used_phone_field)
            pending_create: dict[str, OlapCheckSyncJournal] = {}

            for payload in data_rows:
                order_number = _to_int(_row_value(payload, "OrderNum"))
                business_day = _to_date(_row_value(payload, "OpenDate.Typed"))
                row_department_id = (
                    _normalize_text(_row_value(payload, "Department.Id")) or department_id
                )
                uniq_order_id = _normalize_text(_row_value(payload, "UniqOrderId.Id", "UniqOrderId"))
                department_code = (
                    _normalize_text(_row_value(payload, "Department.Code"))
                    or _normalize_text(scope_row.get("department_code"))
                )
                restoraunt_group_id = (
                    _normalize_text(_row_value(payload, "RestorauntGroup.Id"))
                    or _normalize_text(scope_row.get("restoraunt_group_id"))
                )

                if order_number is None or business_day is None or not row_department_id:
                    stats.skipped_invalid_rows += 1
                    continue

                normalized_phone = self._extract_row_phone(
                    payload=payload,
                    phone_field=used_phone_field,
                )
                if not normalized_phone:
                    stats.olap_rows_without_phone += 1
                    continue

                stats.olap_rows_with_phone += 1
                phone10 = _phone10(normalized_phone)
                guest_id = guest_phone10_map.get(phone10 or "")
                if guest_id is None:
                    stats.olap_rows_phone_without_guest += 1
                    continue

                idempotency_key = _build_control_pull_idempotency_key(
                    department_id=row_department_id,
                    business_date=business_day,
                    order_number=order_number,
                    uniq_order_id=uniq_order_id,
                )
                if idempotency_key in pending_create:
                    continue

                pending_create[idempotency_key] = OlapCheckSyncJournal(
                    idempotency_key=idempotency_key,
                    **self._build_journal_defaults(
                        scope_row=scope_row,
                        guest_id=int(guest_id),
                        business_day=business_day,
                        order_number=order_number,
                        uniq_order_id=uniq_order_id,
                        department_id=row_department_id,
                        department_code=department_code,
                        restoraunt_group_id=restoraunt_group_id,
                    ),
                )

            if not pending_create:
                continue

            stats.distinct_order_keys_seen += len(pending_create)
            idempotency_keys = list(pending_create.keys())
            existing_keys = set(
                OlapCheckSyncJournal.objects.filter(idempotency_key__in=idempotency_keys).values_list(
                    "idempotency_key",
                    flat=True,
                )
            )
            create_payload = [
                row
                for key, row in pending_create.items()
                if key not in existing_keys
            ]

            stats.duplicate_journal_rows += len(existing_keys)

            if options.dry_run:
                stats.would_create_journal_rows += len(create_payload)
                continue

            if create_payload:
                with transaction.atomic():
                    OlapCheckSyncJournal.objects.bulk_create(
                        create_payload,
                        batch_size=1000,
                        ignore_conflicts=True,
                    )
                stats.created_journal_rows += len(create_payload)

        return stats
