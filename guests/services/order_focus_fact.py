"""
Сервис построения order-level слоя `guest_order_focus_fact`.

Источник данных:
1. `olap_sales_raw_line`;
2. `focus_category_nomenclature_resolved`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from guests.models import (
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    GuestOrderFocusFact,
    OlapSalesRawLine,
)

logger = logging.getLogger(__name__)


@dataclass
class OrderFocusFactBuildStats:
    """
    Сводная статистика пересчёта order-level слоя категорий.
    """

    scanned_raw_lines: int = 0
    skipped_invalid_lines: int = 0
    lines_without_focus_mapping: int = 0
    grouped_rows: int = 0
    created_rows: int = 0
    updated_rows: int = 0
    deleted_rows: int = 0


@dataclass
class _OrderFocusAggregate:
    business_date: date
    guest_id: int | None
    department_id: str
    order_number: int
    uniq_order_id: str
    focus_category_id: int
    items_count: int = 0
    sum_focus_net: Decimal = Decimal("0")


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _line_net_value(raw_row: dict[str, Any]) -> Decimal:
    net_value = _to_decimal(raw_row.get("dish_sum_after_discount"))
    if net_value == Decimal("0"):
        net_value = _to_decimal(raw_row.get("dish_sum_before_discount"))
    return net_value


def _build_nomenclature_to_focus_mapping() -> tuple[dict[str, list[int]], set[int]]:
    """
    Возвращает mapping:
    `iiko_nomenclature_external_id` -> `[focus_category_id, ...]`
    и множество активных focus-category id.
    """

    enabled_focus_ids = set(
        FocusCategory.objects.filter(is_enabled=True).values_list("id", flat=True)
    )

    query = (
        FocusCategoryNomenclatureResolved.objects.select_related("nomenclature")
        .filter(
            focus_category_id__in=enabled_focus_ids,
            nomenclature__is_active=True,
        )
        .values("focus_category_id", "nomenclature__iiko_nomenclature_external_id")
    )

    mapping: dict[str, list[int]] = {}
    for row in query.iterator(chunk_size=2000):
        external_id = _normalize_text(row["nomenclature__iiko_nomenclature_external_id"])
        if not external_id:
            continue
        focus_id = int(row["focus_category_id"])
        focus_list = mapping.setdefault(external_id, [])
        if focus_id not in focus_list:
            focus_list.append(focus_id)

    return mapping, enabled_focus_ids


def _delete_stale_order_focus_rows(
    *,
    expected_keys: set[tuple[date, str, int, str, int]],
    scope_focus_ids: set[int],
    business_date_from: date | None,
    business_date_to: date | None,
    department_id: str,
    batch_size: int,
) -> int:
    """
    Удаляет stale-строки в пределах точного scope.
    """

    if not scope_focus_ids:
        return 0

    query = GuestOrderFocusFact.objects.filter(
        focus_category_id__in=scope_focus_ids
    ).values(
        "id",
        "business_date",
        "department_id",
        "order_number",
        "uniq_order_id",
        "focus_category_id",
    )
    if business_date_from is not None:
        query = query.filter(business_date__gte=business_date_from)
    if business_date_to is not None:
        query = query.filter(business_date__lte=business_date_to)

    safe_department_id = (department_id or "").strip()
    if safe_department_id:
        query = query.filter(department_id=safe_department_id)

    chunk_size = max(100, int(batch_size))
    stale_ids_batch: list[int] = []
    deleted_rows = 0

    for row in query.iterator(chunk_size=chunk_size):
        row_key = (
            row["business_date"],
            _normalize_text(row["department_id"]),
            int(row["order_number"]),
            _normalize_text(row["uniq_order_id"]),
            int(row["focus_category_id"]),
        )
        if row_key in expected_keys:
            continue

        stale_ids_batch.append(int(row["id"]))
        if len(stale_ids_batch) >= chunk_size:
            deleted_rows += GuestOrderFocusFact.objects.filter(id__in=stale_ids_batch).delete()[0]
            stale_ids_batch.clear()

    if stale_ids_batch:
        deleted_rows += GuestOrderFocusFact.objects.filter(id__in=stale_ids_batch).delete()[0]

    return int(deleted_rows)


def rebuild_order_focus_fact_from_raw_lines(
    *,
    business_date_from: date | None = None,
    business_date_to: date | None = None,
    department_id: str | None = None,
    batch_size: int = 2000,
) -> OrderFocusFactBuildStats:
    """
    Пересобирает `guest_order_focus_fact` из сырого OLAP-слоя.
    """

    stats = OrderFocusFactBuildStats()
    safe_batch_size = max(100, int(batch_size))
    safe_department_id = (department_id or "").strip()
    mapping, enabled_focus_ids = _build_nomenclature_to_focus_mapping()

    if not mapping:
        logger.info("rebuild_order_focus_fact_from_raw_lines: нет активных связей focus -> nomenclature")
        with transaction.atomic():
            stats.deleted_rows = _delete_stale_order_focus_rows(
                expected_keys=set(),
                scope_focus_ids=enabled_focus_ids,
                business_date_from=business_date_from,
                business_date_to=business_date_to,
                department_id=safe_department_id,
                batch_size=safe_batch_size,
            )
        logger.info(
            "rebuild_order_focus_fact_from_raw_lines: deleted_stale=%s",
            stats.deleted_rows,
        )
        return stats

    aggregates: dict[tuple[date, str, int, str, int], _OrderFocusAggregate] = {}
    query = (
        OlapSalesRawLine.objects.filter(dish_code__in=list(mapping.keys()))
        .order_by("id")
        .values(
            "guest_id",
            "business_date",
            "department_id",
            "order_number",
            "uniq_order_id",
            "dish_code",
            "dish_sum_after_discount",
            "dish_sum_before_discount",
        )
    )
    if business_date_from is not None:
        query = query.filter(business_date__gte=business_date_from)
    if business_date_to is not None:
        query = query.filter(business_date__lte=business_date_to)
    if safe_department_id:
        query = query.filter(department_id=safe_department_id)

    for row in query.iterator(chunk_size=safe_batch_size):
        stats.scanned_raw_lines += 1

        business_day = row["business_date"]
        order_number = row["order_number"]
        if business_day is None or order_number is None:
            stats.skipped_invalid_lines += 1
            continue

        dish_code = _normalize_text(row["dish_code"])
        focus_ids = mapping.get(dish_code, [])
        if not focus_ids:
            stats.lines_without_focus_mapping += 1
            continue

        department_value = _normalize_text(row["department_id"])
        uniq_order_id = _normalize_text(row["uniq_order_id"])
        focus_line_sum = _line_net_value(row)
        guest_id_raw = row["guest_id"]
        guest_id = int(guest_id_raw) if guest_id_raw is not None else None

        for focus_category_id in focus_ids:
            key = (
                business_day,
                department_value,
                int(order_number),
                uniq_order_id,
                int(focus_category_id),
            )
            aggregate = aggregates.get(key)
            if aggregate is None:
                aggregate = _OrderFocusAggregate(
                    business_date=business_day,
                    guest_id=guest_id,
                    department_id=department_value,
                    order_number=int(order_number),
                    uniq_order_id=uniq_order_id,
                    focus_category_id=int(focus_category_id),
                )
                aggregates[key] = aggregate

            if aggregate.guest_id is None and guest_id is not None:
                aggregate.guest_id = guest_id

            aggregate.items_count += 1
            aggregate.sum_focus_net += focus_line_sum

    stats.grouped_rows = len(aggregates)
    now = timezone.now()

    with transaction.atomic():
        existing_by_key: dict[tuple[date, str, int, str, int], GuestOrderFocusFact] = {}
        if aggregates:
            business_dates = {key[0] for key in aggregates.keys()}
            department_ids = {key[1] for key in aggregates.keys()}
            order_numbers = {key[2] for key in aggregates.keys()}
            uniq_order_ids = {key[3] for key in aggregates.keys()}
            focus_ids = {key[4] for key in aggregates.keys()}
            existing_rows = GuestOrderFocusFact.objects.filter(
                business_date__in=business_dates,
                department_id__in=department_ids,
                order_number__in=order_numbers,
                uniq_order_id__in=uniq_order_ids,
                focus_category_id__in=focus_ids,
            )
            existing_by_key = {
                (
                    item.business_date,
                    _normalize_text(item.department_id),
                    int(item.order_number),
                    _normalize_text(item.uniq_order_id),
                    int(item.focus_category_id),
                ): item
                for item in existing_rows
            }

        to_create: list[GuestOrderFocusFact] = []
        to_update: list[GuestOrderFocusFact] = []

        for key, aggregate in aggregates.items():
            existing = existing_by_key.get(key)
            if existing is None:
                to_create.append(
                    GuestOrderFocusFact(
                        business_date=aggregate.business_date,
                        guest_id=aggregate.guest_id,
                        department_id=aggregate.department_id,
                        order_number=aggregate.order_number,
                        uniq_order_id=aggregate.uniq_order_id,
                        focus_category_id=aggregate.focus_category_id,
                        items_count=aggregate.items_count,
                        sum_focus_net=aggregate.sum_focus_net,
                    )
                )
                continue

            changed = False
            compare_fields = {
                "guest_id": aggregate.guest_id,
                "items_count": aggregate.items_count,
                "sum_focus_net": aggregate.sum_focus_net,
            }
            for field_name, expected_value in compare_fields.items():
                if getattr(existing, field_name) != expected_value:
                    setattr(existing, field_name, expected_value)
                    changed = True

            if changed:
                existing.updated_at = now
                to_update.append(existing)

        if to_create:
            GuestOrderFocusFact.objects.bulk_create(
                to_create,
                batch_size=safe_batch_size,
            )
        if to_update:
            GuestOrderFocusFact.objects.bulk_update(
                to_update,
                fields=[
                    "guest",
                    "items_count",
                    "sum_focus_net",
                    "updated_at",
                ],
                batch_size=safe_batch_size,
            )

        stats.created_rows = len(to_create)
        stats.updated_rows = len(to_update)
        stats.deleted_rows = _delete_stale_order_focus_rows(
            expected_keys=set(aggregates.keys()),
            scope_focus_ids=enabled_focus_ids,
            business_date_from=business_date_from,
            business_date_to=business_date_to,
            department_id=safe_department_id,
            batch_size=safe_batch_size,
        )

    logger.info(
        (
            "rebuild_order_focus_fact_from_raw_lines: scanned=%s grouped=%s without_mapping=%s "
            "skipped_invalid=%s created=%s updated=%s deleted=%s"
        ),
        stats.scanned_raw_lines,
        stats.grouped_rows,
        stats.lines_without_focus_mapping,
        stats.skipped_invalid_lines,
        stats.created_rows,
        stats.updated_rows,
        stats.deleted_rows,
    )
    return stats
