"""
Сервис построения оконных метрик по гостю, заведению и фокусной категории.

Расчёт выполняется по order-level данным:
1. заказы категории определяются по `olap_sales_raw_line` через
   `focus_category_nomenclature_resolved`;
2. полная сумма чека и бонусы подтягиваются из `order_fact`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from guests.models import (
    FocusCategoryNomenclatureResolved,
    GuestRestaurantWindowCategoryMetrics,
    OlapSalesRawLine,
    OrderFact,
)

logger = logging.getLogger(__name__)

DEFAULT_WINDOWS = (7, 14, 30, 60, 180)

OrderKey = tuple[date, str, int, str]


@dataclass
class WindowCategoryMetricsBuildStats:
    """
    Сводная статистика пересчёта category-window метрик.
    """

    as_of_date: date
    windows_processed: int = 0
    scanned_raw_lines: int = 0
    grouped_rows: int = 0
    created_rows: int = 0
    updated_rows: int = 0
    deleted_rows: int = 0
    missing_order_facts: int = 0


@dataclass
class _WindowCategoryAggregate:
    guest_id: int
    department_id: str
    window_days: int
    focus_category_id: int
    order_keys: set[OrderKey] = field(default_factory=set)
    business_dates: set[date] = field(default_factory=set)
    sum_focus_net: Decimal = Decimal("0")
    sum_net: Decimal = Decimal("0")
    bonus_in_sum: Decimal = Decimal("0")
    bonus_out_sum: Decimal = Decimal("0")
    last_visit_at: date | None = None

    @property
    def orders_count(self) -> int:
        return len(self.order_keys)

    @property
    def visits_count(self) -> int:
        return len(self.business_dates)

    @property
    def avg_check_net(self) -> Decimal:
        if self.orders_count <= 0:
            return Decimal("0")
        return (self.sum_net / Decimal(self.orders_count)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def rating_score(self) -> Decimal:
        score = (
            Decimal(self.orders_count) * Decimal("3")
            + Decimal(self.visits_count) * Decimal("2")
            + (self.avg_check_net / Decimal("100"))
        )
        return score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _normalize_window_days(window_days: Iterable[int] | None) -> list[int]:
    if window_days is None:
        return list(DEFAULT_WINDOWS)
    normalized: list[int] = []
    for item in window_days:
        value = int(item)
        if value <= 0:
            continue
        if value not in normalized:
            normalized.append(value)
    return normalized or list(DEFAULT_WINDOWS)


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


def _build_nomenclature_to_focus_mapping() -> dict[str, list[int]]:
    """
    Возвращает mapping:
    `iiko_nomenclature_external_id` -> `[focus_category_id, ...]`.
    """
    query = (
        FocusCategoryNomenclatureResolved.objects.select_related("nomenclature")
        .filter(
            focus_category__is_enabled=True,
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
    return mapping


def _delete_stale_category_rows(
    *,
    as_of_date: date,
    window_days: int,
    department_id: str,
    expected_keys: set[tuple[int, str, int]],
    batch_size: int,
) -> int:
    """
    Удаляет stale-строки в пределах точного scope:
    `as_of_date + window_days + (department_id|ALL)`.
    """
    query = GuestRestaurantWindowCategoryMetrics.objects.filter(
        as_of_date=as_of_date,
        window_days=window_days,
    ).values(
        "id",
        "guest_id",
        "department_id",
        "focus_category_id",
    )
    safe_department_id = (department_id or "").strip()
    if safe_department_id:
        query = query.filter(department_id=safe_department_id)

    safe_batch_size = max(100, int(batch_size))
    deleted_rows = 0
    stale_ids_batch: list[int] = []
    for row in query.iterator(chunk_size=safe_batch_size):
        row_key = (
            int(row["guest_id"]),
            _normalize_text(row["department_id"]),
            int(row["focus_category_id"]),
        )
        if row_key in expected_keys:
            continue

        stale_ids_batch.append(int(row["id"]))
        if len(stale_ids_batch) >= safe_batch_size:
            deleted_rows += GuestRestaurantWindowCategoryMetrics.objects.filter(
                id__in=stale_ids_batch
            ).delete()[0]
            stale_ids_batch.clear()

    if stale_ids_batch:
        deleted_rows += GuestRestaurantWindowCategoryMetrics.objects.filter(
            id__in=stale_ids_batch
        ).delete()[0]

    return int(deleted_rows)


def _fetch_order_fact_map(
    *,
    order_keys: set[OrderKey],
    batch_size: int,
) -> dict[OrderKey, tuple[Decimal, Decimal]]:
    if not order_keys:
        return {}

    business_dates = {key[0] for key in order_keys}
    department_ids = {key[1] for key in order_keys}
    order_numbers = {key[2] for key in order_keys}
    uniq_order_ids = {key[3] for key in order_keys}

    query = OrderFact.objects.filter(
        business_date__in=business_dates,
        department_id__in=department_ids,
        order_number__in=order_numbers,
        uniq_order_id__in=uniq_order_ids,
    ).values(
        "business_date",
        "department_id",
        "order_number",
        "uniq_order_id",
        "net_sum",
        "bonus_sum",
    )

    result: dict[OrderKey, tuple[Decimal, Decimal]] = {}
    for row in query.iterator(chunk_size=max(100, int(batch_size))):
        key: OrderKey = (
            row["business_date"],
            _normalize_text(row["department_id"]),
            int(row["order_number"]),
            _normalize_text(row["uniq_order_id"]),
        )
        result[key] = (
            _to_decimal(row["net_sum"]),
            _to_decimal(row["bonus_sum"]),
        )
    return result


def _build_aggregates_for_window(
    *,
    as_of_date: date,
    window_days: int,
    department_id: str,
    nomenclature_to_focus: dict[str, list[int]],
    batch_size: int,
    stats: WindowCategoryMetricsBuildStats,
) -> dict[tuple[int, str, int, int], _WindowCategoryAggregate]:
    """
    Собирает агрегаты по окну из сырого слоя.
    """
    if not nomenclature_to_focus:
        return {}

    date_from = as_of_date - timedelta(days=window_days - 1)
    safe_department_id = (department_id or "").strip()
    dish_codes = list(nomenclature_to_focus.keys())

    query = OlapSalesRawLine.objects.filter(
        business_date__gte=date_from,
        business_date__lte=as_of_date,
        dish_code__in=dish_codes,
    ).values(
        "guest_id",
        "business_date",
        "department_id",
        "order_number",
        "uniq_order_id",
        "dish_code",
        "dish_sum_after_discount",
        "dish_sum_before_discount",
    )
    if safe_department_id:
        query = query.filter(department_id=safe_department_id)

    aggregates: dict[tuple[int, str, int, int], _WindowCategoryAggregate] = {}
    for row in query.iterator(chunk_size=max(100, int(batch_size))):
        stats.scanned_raw_lines += 1

        guest_id = row["guest_id"]
        business_day = row["business_date"]
        order_number = row["order_number"]
        if guest_id is None or business_day is None or order_number is None:
            continue

        dish_code = _normalize_text(row["dish_code"])
        focus_ids = nomenclature_to_focus.get(dish_code, [])
        if not focus_ids:
            continue

        dep_id = _normalize_text(row["department_id"])
        uniq_order_id = _normalize_text(row["uniq_order_id"])
        order_key: OrderKey = (business_day, dep_id, int(order_number), uniq_order_id)
        line_net = _line_net_value(row)

        for focus_category_id in focus_ids:
            key = (int(guest_id), dep_id, int(window_days), int(focus_category_id))
            aggregate = aggregates.get(key)
            if aggregate is None:
                aggregate = _WindowCategoryAggregate(
                    guest_id=int(guest_id),
                    department_id=dep_id,
                    window_days=int(window_days),
                    focus_category_id=int(focus_category_id),
                )
                aggregates[key] = aggregate

            aggregate.order_keys.add(order_key)
            aggregate.business_dates.add(business_day)
            aggregate.sum_focus_net += line_net
            if aggregate.last_visit_at is None or business_day > aggregate.last_visit_at:
                aggregate.last_visit_at = business_day

    return aggregates


def rebuild_window_category_metrics_from_order_facts(
    *,
    as_of_date: date | None = None,
    window_days: Iterable[int] | None = None,
    department_id: str | None = None,
    batch_size: int = 2000,
) -> WindowCategoryMetricsBuildStats:
    """
    Пересобирает `guest_restaurant_window_category_metrics`.

    Метрики строятся по заказам, где фокусная категория действительно встречалась
    в строках сырого OLAP-слоя.
    """
    target_date = as_of_date or timezone.localdate()
    windows = _normalize_window_days(window_days)
    safe_batch_size = max(100, int(batch_size))
    safe_department_id = (department_id or "").strip()
    stats = WindowCategoryMetricsBuildStats(as_of_date=target_date)

    nomenclature_to_focus = _build_nomenclature_to_focus_mapping()
    if not nomenclature_to_focus:
        logger.info("rebuild_window_category_metrics_from_order_facts: нет активных связей focus -> nomenclature")

    for window in windows:
        stats.windows_processed += 1
        aggregates = _build_aggregates_for_window(
            as_of_date=target_date,
            window_days=window,
            department_id=safe_department_id,
            nomenclature_to_focus=nomenclature_to_focus,
            batch_size=safe_batch_size,
            stats=stats,
        )
        stats.grouped_rows += len(aggregates)

        all_order_keys: set[OrderKey] = set()
        for aggregate in aggregates.values():
            all_order_keys.update(aggregate.order_keys)

        order_map = _fetch_order_fact_map(order_keys=all_order_keys, batch_size=safe_batch_size)
        expected_keys = {
            (key[0], key[1], key[3])
            for key in aggregates.keys()
        }

        now = timezone.now()
        with transaction.atomic():
            existing_by_key: dict[tuple[int, str, int], GuestRestaurantWindowCategoryMetrics] = {}
            if aggregates:
                guest_ids = {key[0] for key in aggregates.keys()}
                department_ids = {key[1] for key in aggregates.keys()}
                focus_ids = {key[3] for key in aggregates.keys()}
                existing_rows = GuestRestaurantWindowCategoryMetrics.objects.filter(
                    as_of_date=target_date,
                    window_days=window,
                    guest_id__in=guest_ids,
                    department_id__in=department_ids,
                    focus_category_id__in=focus_ids,
                )
                existing_by_key = {
                    (
                        int(item.guest_id),
                        _normalize_text(item.department_id),
                        int(item.focus_category_id),
                    ): item
                    for item in existing_rows
                }

            to_create: list[GuestRestaurantWindowCategoryMetrics] = []
            to_update: list[GuestRestaurantWindowCategoryMetrics] = []

            for key, aggregate in aggregates.items():
                sum_net = Decimal("0")
                bonus_in_sum = Decimal("0")
                bonus_out_sum = Decimal("0")
                for order_key in aggregate.order_keys:
                    order_fact_values = order_map.get(order_key)
                    if order_fact_values is None:
                        stats.missing_order_facts += 1
                        continue
                    order_net, order_bonus = order_fact_values
                    sum_net += order_net
                    if order_bonus >= 0:
                        bonus_in_sum += order_bonus
                    else:
                        bonus_out_sum += abs(order_bonus)

                aggregate.sum_net = sum_net
                aggregate.bonus_in_sum = bonus_in_sum
                aggregate.bonus_out_sum = bonus_out_sum

                lookup_key = (key[0], key[1], key[3])
                existing = existing_by_key.get(lookup_key)
                if existing is None:
                    to_create.append(
                        GuestRestaurantWindowCategoryMetrics(
                            as_of_date=target_date,
                            guest_id=aggregate.guest_id,
                            department_id=aggregate.department_id,
                            window_days=aggregate.window_days,
                            focus_category_id=aggregate.focus_category_id,
                            orders_count=aggregate.orders_count,
                            visits_count=aggregate.visits_count,
                            avg_check_net=aggregate.avg_check_net,
                            sum_net=aggregate.sum_net,
                            sum_focus_net=aggregate.sum_focus_net,
                            bonus_in_sum=aggregate.bonus_in_sum,
                            bonus_out_sum=aggregate.bonus_out_sum,
                            last_visit_at=aggregate.last_visit_at,
                            rating_score=aggregate.rating_score,
                        )
                    )
                    continue

                changed = False
                compare_fields = {
                    "orders_count": aggregate.orders_count,
                    "visits_count": aggregate.visits_count,
                    "avg_check_net": aggregate.avg_check_net,
                    "sum_net": aggregate.sum_net,
                    "sum_focus_net": aggregate.sum_focus_net,
                    "bonus_in_sum": aggregate.bonus_in_sum,
                    "bonus_out_sum": aggregate.bonus_out_sum,
                    "last_visit_at": aggregate.last_visit_at,
                    "rating_score": aggregate.rating_score,
                }
                for field_name, expected_value in compare_fields.items():
                    if getattr(existing, field_name) != expected_value:
                        setattr(existing, field_name, expected_value)
                        changed = True

                if changed:
                    existing.updated_at = now
                    to_update.append(existing)

            if to_create:
                GuestRestaurantWindowCategoryMetrics.objects.bulk_create(
                    to_create,
                    batch_size=safe_batch_size,
                )
            if to_update:
                GuestRestaurantWindowCategoryMetrics.objects.bulk_update(
                    to_update,
                    fields=[
                        "orders_count",
                        "visits_count",
                        "avg_check_net",
                        "sum_net",
                        "sum_focus_net",
                        "bonus_in_sum",
                        "bonus_out_sum",
                        "last_visit_at",
                        "rating_score",
                        "updated_at",
                    ],
                    batch_size=safe_batch_size,
                )

            stats.created_rows += len(to_create)
            stats.updated_rows += len(to_update)
            stats.deleted_rows += _delete_stale_category_rows(
                as_of_date=target_date,
                window_days=window,
                department_id=safe_department_id,
                expected_keys=expected_keys,
                batch_size=safe_batch_size,
            )

    logger.info(
        (
            "rebuild_window_category_metrics_from_order_facts: as_of=%s windows=%s scanned=%s grouped=%s "
            "created=%s updated=%s deleted=%s missing_order_facts=%s"
        ),
        target_date,
        windows,
        stats.scanned_raw_lines,
        stats.grouped_rows,
        stats.created_rows,
        stats.updated_rows,
        stats.deleted_rows,
        stats.missing_order_facts,
    )
    return stats
