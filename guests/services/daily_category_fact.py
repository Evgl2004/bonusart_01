"""
Сервис построения дневного слоя `guest_restaurant_daily_category_fact`.

Источник данных:
1. `olap_sales_raw_line`;
2. предрассчитанные связи `focus_category_nomenclature_resolved`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from guests.models import (
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    GuestRestaurantDailyCategoryFact,
    OlapSalesRawLine,
)

logger = logging.getLogger(__name__)


@dataclass
class DailyCategoryFactBuildStats:
    """
    Сводная статистика пересчёта дневного слоя по категориям.
    """

    scanned_raw_lines: int = 0
    lines_without_focus_mapping: int = 0
    grouped_rows: int = 0
    created_rows: int = 0
    updated_rows: int = 0
    deleted_rows: int = 0


@dataclass
class _DailyAggregate:
    business_date: date
    guest_id: int
    department_id: str
    focus_category_id: int
    orders_set: set[int] = field(default_factory=set)
    items_count: int = 0
    sum_gross: Decimal = Decimal("0")
    sum_net: Decimal = Decimal("0")
    bonus_sum: Decimal = Decimal("0")

    @property
    def orders_count(self) -> int:
        return len(self.orders_set)


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


def _build_nomenclature_to_focus_mapping() -> dict[str, list[int]]:
    """
    Строит отображение:
    `iiko_nomenclature_external_id` -> `[focus_category_id, ...]`.
    """

    links = (
        FocusCategoryNomenclatureResolved.objects.select_related("nomenclature")
        .filter(focus_category__is_enabled=True, nomenclature__is_active=True)
        .values("focus_category_id", "nomenclature__iiko_nomenclature_external_id")
    )

    mapping: dict[str, list[int]] = {}
    for row in links.iterator(chunk_size=2000):
        ext_id = _normalize_text(row["nomenclature__iiko_nomenclature_external_id"])
        if not ext_id:
            continue
        mapping.setdefault(ext_id, []).append(int(row["focus_category_id"]))
    return mapping


def _delete_stale_daily_rows(
    *,
    expected_keys: set[tuple[date, int, str, int]],
    scope_focus_ids: set[int],
    business_date_from: date | None,
    business_date_to: date | None,
    batch_size: int,
) -> int:
    """
    Удаляет строки дневного слоя, которые не входят в актуальные агрегаты
    в рамках заданного периода и набора активных фокусных категорий.
    """

    if not scope_focus_ids:
        return 0

    query = GuestRestaurantDailyCategoryFact.objects.filter(
        focus_category_id__in=scope_focus_ids
    ).values(
        "id",
        "business_date",
        "guest_id",
        "department_id",
        "focus_category_id",
    )
    if business_date_from is not None:
        query = query.filter(business_date__gte=business_date_from)
    if business_date_to is not None:
        query = query.filter(business_date__lte=business_date_to)

    chunk_size = max(100, int(batch_size))
    stale_ids_batch: list[int] = []
    deleted_rows = 0

    for row in query.iterator(chunk_size=chunk_size):
        row_key = (
            row["business_date"],
            int(row["guest_id"]),
            _normalize_text(row["department_id"]),
            int(row["focus_category_id"]),
        )
        if row_key in expected_keys:
            continue

        stale_ids_batch.append(int(row["id"]))
        if len(stale_ids_batch) >= chunk_size:
            deleted_rows += GuestRestaurantDailyCategoryFact.objects.filter(
                id__in=stale_ids_batch
            ).delete()[0]
            stale_ids_batch.clear()

    if stale_ids_batch:
        deleted_rows += GuestRestaurantDailyCategoryFact.objects.filter(
            id__in=stale_ids_batch
        ).delete()[0]

    return int(deleted_rows)


def rebuild_daily_category_fact_from_raw_lines(
    *,
    raw_line_id_from: int | None = None,
    raw_line_id_to: int | None = None,
    business_date_from: date | None = None,
    business_date_to: date | None = None,
    batch_size: int = 2000,
) -> DailyCategoryFactBuildStats:
    """
    Пересобирает `guest_restaurant_daily_category_fact` из сырого OLAP-слоя.
    """

    stats = DailyCategoryFactBuildStats()
    full_scope_rebuild = raw_line_id_from is None and raw_line_id_to is None
    enabled_focus_ids: set[int] = set()
    if full_scope_rebuild:
        enabled_focus_ids = set(
            FocusCategory.objects.filter(is_enabled=True).values_list("id", flat=True)
        )

    nomenclature_to_focus = _build_nomenclature_to_focus_mapping()
    if not nomenclature_to_focus:
        logger.info("rebuild_daily_category_fact_from_raw_lines: нет активных связей focus -> nomenclature")
        if full_scope_rebuild and enabled_focus_ids:
            with transaction.atomic():
                stats.deleted_rows = _delete_stale_daily_rows(
                    expected_keys=set(),
                    scope_focus_ids=enabled_focus_ids,
                    business_date_from=business_date_from,
                    business_date_to=business_date_to,
                    batch_size=batch_size,
                )
            logger.info(
                "rebuild_daily_category_fact_from_raw_lines: deleted_stale=%s",
                stats.deleted_rows,
            )
        return stats

    aggregates: dict[tuple[date, int, str, int], _DailyAggregate] = {}
    query = (
        OlapSalesRawLine.objects.all()
        .order_by("id")
        .values(
            "id",
            "guest_id",
            "business_date",
            "department_id",
            "order_number",
            "dish_code",
            "dish_sum_before_discount",
            "dish_sum_after_discount",
            "bonus_sum",
        )
    )

    if raw_line_id_from is not None:
        query = query.filter(id__gte=int(raw_line_id_from))
    if raw_line_id_to is not None:
        query = query.filter(id__lte=int(raw_line_id_to))
    if business_date_from is not None:
        query = query.filter(business_date__gte=business_date_from)
    if business_date_to is not None:
        query = query.filter(business_date__lte=business_date_to)

    for row in query.iterator(chunk_size=max(100, int(batch_size))):
        stats.scanned_raw_lines += 1

        guest_id = row["guest_id"]
        business_day = row["business_date"]
        order_number = row["order_number"]
        if guest_id is None or business_day is None or order_number is None:
            continue

        dish_code = _normalize_text(row["dish_code"])
        if not dish_code:
            stats.lines_without_focus_mapping += 1
            continue

        focus_ids = nomenclature_to_focus.get(dish_code, [])
        if not focus_ids:
            stats.lines_without_focus_mapping += 1
            continue

        department_id = _normalize_text(row["department_id"])
        gross = _to_decimal(row["dish_sum_before_discount"])
        net = _to_decimal(row["dish_sum_after_discount"])
        if net == Decimal("0"):
            net = gross
        bonus = _to_decimal(row["bonus_sum"])

        for focus_category_id in focus_ids:
            key = (business_day, int(guest_id), department_id, int(focus_category_id))
            aggregate = aggregates.get(key)
            if aggregate is None:
                aggregate = _DailyAggregate(
                    business_date=business_day,
                    guest_id=int(guest_id),
                    department_id=department_id,
                    focus_category_id=int(focus_category_id),
                )
                aggregates[key] = aggregate

            aggregate.orders_set.add(int(order_number))
            aggregate.items_count += 1
            aggregate.sum_gross += gross
            aggregate.sum_net += net
            aggregate.bonus_sum += bonus

    stats.grouped_rows = len(aggregates)
    if not aggregates:
        logger.info("rebuild_daily_category_fact_from_raw_lines: нет агрегатов для записи")
        if full_scope_rebuild and enabled_focus_ids:
            with transaction.atomic():
                stats.deleted_rows = _delete_stale_daily_rows(
                    expected_keys=set(),
                    scope_focus_ids=enabled_focus_ids,
                    business_date_from=business_date_from,
                    business_date_to=business_date_to,
                    batch_size=batch_size,
                )
            logger.info(
                "rebuild_daily_category_fact_from_raw_lines: deleted_stale=%s",
                stats.deleted_rows,
            )
        return stats

    business_dates = {key[0] for key in aggregates.keys()}
    guest_ids = {key[1] for key in aggregates.keys()}
    department_ids = {key[2] for key in aggregates.keys()}
    focus_ids = {key[3] for key in aggregates.keys()}
    now = timezone.now()

    with transaction.atomic():
        existing_rows = GuestRestaurantDailyCategoryFact.objects.filter(
            business_date__in=business_dates,
            guest_id__in=guest_ids,
            department_id__in=department_ids,
            focus_category_id__in=focus_ids,
        )
        existing_by_key = {
            (
                item.business_date,
                int(item.guest_id),
                item.department_id or "",
                int(item.focus_category_id),
            ): item
            for item in existing_rows
        }

        to_create: list[GuestRestaurantDailyCategoryFact] = []
        to_update: list[GuestRestaurantDailyCategoryFact] = []

        for key, aggregate in aggregates.items():
            existing = existing_by_key.get(key)
            if existing is None:
                to_create.append(
                    GuestRestaurantDailyCategoryFact(
                        business_date=aggregate.business_date,
                        guest_id=aggregate.guest_id,
                        department_id=aggregate.department_id,
                        focus_category_id=aggregate.focus_category_id,
                        orders_count=aggregate.orders_count,
                        items_count=aggregate.items_count,
                        sum_gross=aggregate.sum_gross,
                        sum_net=aggregate.sum_net,
                        bonus_sum=aggregate.bonus_sum,
                    )
                )
                continue

            changed = False
            compare_fields = {
                "orders_count": aggregate.orders_count,
                "items_count": aggregate.items_count,
                "sum_gross": aggregate.sum_gross,
                "sum_net": aggregate.sum_net,
                "bonus_sum": aggregate.bonus_sum,
            }
            for field_name, expected_value in compare_fields.items():
                if getattr(existing, field_name) != expected_value:
                    setattr(existing, field_name, expected_value)
                    changed = True

            if changed:
                existing.updated_at = now
                to_update.append(existing)

        if to_create:
            GuestRestaurantDailyCategoryFact.objects.bulk_create(
                to_create,
                batch_size=max(100, int(batch_size)),
            )
        if to_update:
            GuestRestaurantDailyCategoryFact.objects.bulk_update(
                to_update,
                fields=[
                    "orders_count",
                    "items_count",
                    "sum_gross",
                    "sum_net",
                    "bonus_sum",
                    "updated_at",
                ],
                batch_size=max(100, int(batch_size)),
            )

        stats.created_rows = len(to_create)
        stats.updated_rows = len(to_update)
        if full_scope_rebuild and enabled_focus_ids:
            stats.deleted_rows = _delete_stale_daily_rows(
                expected_keys=set(aggregates.keys()),
                scope_focus_ids=enabled_focus_ids,
                business_date_from=business_date_from,
                business_date_to=business_date_to,
                batch_size=batch_size,
            )

    logger.info(
        (
            "rebuild_daily_category_fact_from_raw_lines: scanned=%s grouped=%s "
            "without_mapping=%s created=%s updated=%s deleted=%s"
        ),
        stats.scanned_raw_lines,
        stats.grouped_rows,
        stats.lines_without_focus_mapping,
        stats.created_rows,
        stats.updated_rows,
        stats.deleted_rows,
    )
    return stats
