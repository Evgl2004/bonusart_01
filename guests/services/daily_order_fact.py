"""
Сервис построения дневного слоя `guest_restaurant_daily_order_fact`.

Источник данных:
1. `order_fact` (полные чеки).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from guests.models import GuestRestaurantDailyOrderFact, OrderFact

logger = logging.getLogger(__name__)


@dataclass
class DailyOrderFactBuildStats:
    """
    Сводная статистика пересчёта дневного слоя по полным чекам.
    """

    scanned_order_facts: int = 0
    skipped_without_guest: int = 0
    grouped_rows: int = 0
    created_rows: int = 0
    updated_rows: int = 0
    deleted_rows: int = 0


@dataclass
class _DailyOrderAggregate:
    business_date: date
    guest_id: int
    department_id: str
    orders_count: int = 0
    sum_net: Decimal = Decimal("0")
    bonus_in_sum: Decimal = Decimal("0")
    bonus_out_sum: Decimal = Decimal("0")


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


def _delete_stale_daily_order_rows(
    *,
    expected_keys: set[tuple[date, int, str]],
    business_date_from: date | None,
    business_date_to: date | None,
    department_id: str,
    batch_size: int,
) -> int:
    """
    Удаляет stale-строки дневного слоя в пределах точного scope.
    """

    query = GuestRestaurantDailyOrderFact.objects.all().values(
        "id",
        "business_date",
        "guest_id",
        "department_id",
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
            int(row["guest_id"]),
            _normalize_text(row["department_id"]),
        )
        if row_key in expected_keys:
            continue

        stale_ids_batch.append(int(row["id"]))
        if len(stale_ids_batch) >= chunk_size:
            deleted_rows += GuestRestaurantDailyOrderFact.objects.filter(
                id__in=stale_ids_batch
            ).delete()[0]
            stale_ids_batch.clear()

    if stale_ids_batch:
        deleted_rows += GuestRestaurantDailyOrderFact.objects.filter(
            id__in=stale_ids_batch
        ).delete()[0]

    return int(deleted_rows)


def rebuild_daily_order_fact_from_order_facts(
    *,
    business_date_from: date | None = None,
    business_date_to: date | None = None,
    department_id: str | None = None,
    batch_size: int = 2000,
) -> DailyOrderFactBuildStats:
    """
    Пересобирает `guest_restaurant_daily_order_fact` из `order_fact`.
    """

    stats = DailyOrderFactBuildStats()
    safe_batch_size = max(100, int(batch_size))
    safe_department_id = (department_id or "").strip()

    aggregates: dict[tuple[date, int, str], _DailyOrderAggregate] = {}
    query = OrderFact.objects.all().order_by("id").values(
        "guest_id",
        "business_date",
        "department_id",
        "net_sum",
        "bonus_sum",
    )

    if business_date_from is not None:
        query = query.filter(business_date__gte=business_date_from)
    if business_date_to is not None:
        query = query.filter(business_date__lte=business_date_to)
    if safe_department_id:
        query = query.filter(department_id=safe_department_id)

    for row in query.iterator(chunk_size=safe_batch_size):
        stats.scanned_order_facts += 1

        guest_id = row["guest_id"]
        business_day = row["business_date"]
        if guest_id is None or business_day is None:
            stats.skipped_without_guest += 1
            continue

        dep_id = _normalize_text(row["department_id"])
        key = (business_day, int(guest_id), dep_id)
        aggregate = aggregates.get(key)
        if aggregate is None:
            aggregate = _DailyOrderAggregate(
                business_date=business_day,
                guest_id=int(guest_id),
                department_id=dep_id,
            )
            aggregates[key] = aggregate

        aggregate.orders_count += 1
        aggregate.sum_net += _to_decimal(row["net_sum"])
        bonus_value = _to_decimal(row["bonus_sum"])
        if bonus_value >= 0:
            aggregate.bonus_in_sum += bonus_value
        else:
            aggregate.bonus_out_sum += abs(bonus_value)

    stats.grouped_rows = len(aggregates)
    now = timezone.now()

    with transaction.atomic():
        existing_by_key: dict[tuple[date, int, str], GuestRestaurantDailyOrderFact] = {}
        if aggregates:
            business_dates = {key[0] for key in aggregates.keys()}
            guest_ids = {key[1] for key in aggregates.keys()}
            department_ids = {key[2] for key in aggregates.keys()}
            existing_rows = GuestRestaurantDailyOrderFact.objects.filter(
                business_date__in=business_dates,
                guest_id__in=guest_ids,
                department_id__in=department_ids,
            )
            existing_by_key = {
                (
                    item.business_date,
                    int(item.guest_id),
                    _normalize_text(item.department_id),
                ): item
                for item in existing_rows
            }

        to_create: list[GuestRestaurantDailyOrderFact] = []
        to_update: list[GuestRestaurantDailyOrderFact] = []

        for key, aggregate in aggregates.items():
            existing = existing_by_key.get(key)
            if existing is None:
                to_create.append(
                    GuestRestaurantDailyOrderFact(
                        business_date=aggregate.business_date,
                        guest_id=aggregate.guest_id,
                        department_id=aggregate.department_id,
                        orders_count=aggregate.orders_count,
                        sum_net=aggregate.sum_net,
                        bonus_in_sum=aggregate.bonus_in_sum,
                        bonus_out_sum=aggregate.bonus_out_sum,
                    )
                )
                continue

            changed = False
            compare_fields = {
                "orders_count": aggregate.orders_count,
                "sum_net": aggregate.sum_net,
                "bonus_in_sum": aggregate.bonus_in_sum,
                "bonus_out_sum": aggregate.bonus_out_sum,
            }
            for field_name, expected_value in compare_fields.items():
                if getattr(existing, field_name) != expected_value:
                    setattr(existing, field_name, expected_value)
                    changed = True

            if changed:
                existing.updated_at = now
                to_update.append(existing)

        if to_create:
            GuestRestaurantDailyOrderFact.objects.bulk_create(
                to_create,
                batch_size=safe_batch_size,
            )
        if to_update:
            GuestRestaurantDailyOrderFact.objects.bulk_update(
                to_update,
                fields=[
                    "orders_count",
                    "sum_net",
                    "bonus_in_sum",
                    "bonus_out_sum",
                    "updated_at",
                ],
                batch_size=safe_batch_size,
            )

        stats.created_rows = len(to_create)
        stats.updated_rows = len(to_update)
        stats.deleted_rows = _delete_stale_daily_order_rows(
            expected_keys=set(aggregates.keys()),
            business_date_from=business_date_from,
            business_date_to=business_date_to,
            department_id=safe_department_id,
            batch_size=safe_batch_size,
        )

    logger.info(
        (
            "rebuild_daily_order_fact_from_order_facts: scanned=%s skipped_without_guest=%s grouped=%s "
            "created=%s updated=%s deleted=%s"
        ),
        stats.scanned_order_facts,
        stats.skipped_without_guest,
        stats.grouped_rows,
        stats.created_rows,
        stats.updated_rows,
        stats.deleted_rows,
    )
    return stats
