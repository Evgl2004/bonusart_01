"""
Сервис построения оконных метрик по гостю и заведению.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from guests.models import GuestRestaurantDailyOrderFact, GuestRestaurantWindowMetrics

logger = logging.getLogger(__name__)

DEFAULT_WINDOWS = (7, 14, 30, 60, 180)


@dataclass
class WindowMetricsBuildStats:
    """
    Сводная статистика пересчёта оконных метрик.
    """

    as_of_date: date
    windows_processed: int = 0
    scanned_daily_rows: int = 0
    grouped_rows: int = 0
    created_rows: int = 0
    updated_rows: int = 0


@dataclass
class _WindowAggregate:
    guest_id: int
    department_id: str
    window_days: int
    orders_count: int = 0
    business_dates: set[date] = field(default_factory=set)
    sum_net: Decimal = Decimal("0")
    bonus_in_sum: Decimal = Decimal("0")
    bonus_out_sum: Decimal = Decimal("0")
    last_visit_at: date | None = None

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
        """
        Базовая формула рейтинга:
        1. активность (orders + visits);
        2. вклад среднего чека.
        """
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


def rebuild_window_metrics_from_daily_facts(
    *,
    as_of_date: date | None = None,
    window_days: Iterable[int] | None = None,
    department_id: str | None = None,
    batch_size: int = 2000,
) -> WindowMetricsBuildStats:
    """
    Пересобирает `guest_restaurant_window_metrics` по дневному слою полных чеков.
    """

    target_date = as_of_date or timezone.localdate()
    windows = _normalize_window_days(window_days)
    stats = WindowMetricsBuildStats(as_of_date=target_date)
    safe_batch_size = max(100, int(batch_size))
    target_department_id = (department_id or "").strip()

    all_aggregates: dict[tuple[int, str, int], _WindowAggregate] = {}

    for window in windows:
        stats.windows_processed += 1
        date_from = target_date - timedelta(days=window - 1)

        query = GuestRestaurantDailyOrderFact.objects.filter(
            business_date__gte=date_from,
            business_date__lte=target_date,
        ).values(
            "guest_id",
            "department_id",
            "business_date",
            "orders_count",
            "sum_net",
            "bonus_in_sum",
            "bonus_out_sum",
        )
        if target_department_id:
            query = query.filter(department_id=target_department_id)

        for row in query.iterator(chunk_size=safe_batch_size):
            stats.scanned_daily_rows += 1
            guest_id = row["guest_id"]
            if guest_id is None:
                continue

            dep_id = (row["department_id"] or "").strip()
            key = (int(guest_id), dep_id, int(window))
            aggregate = all_aggregates.get(key)
            if aggregate is None:
                aggregate = _WindowAggregate(
                    guest_id=int(guest_id),
                    department_id=dep_id,
                    window_days=int(window),
                )
                all_aggregates[key] = aggregate

            business_day = row["business_date"]
            aggregate.business_dates.add(business_day)
            aggregate.orders_count += int(row["orders_count"] or 0)
            sum_net_value = Decimal(str(row["sum_net"] or 0))
            aggregate.sum_net += sum_net_value

            aggregate.bonus_in_sum += Decimal(str(row["bonus_in_sum"] or 0))
            aggregate.bonus_out_sum += Decimal(str(row["bonus_out_sum"] or 0))

            if aggregate.last_visit_at is None or business_day > aggregate.last_visit_at:
                aggregate.last_visit_at = business_day

    stats.grouped_rows = len(all_aggregates)
    if not all_aggregates:
        logger.info("rebuild_window_metrics_from_daily_facts: нет данных для пересчёта")
        return stats

    guest_ids = {key[0] for key in all_aggregates.keys()}
    departments = {key[1] for key in all_aggregates.keys()}
    windows_set = {key[2] for key in all_aggregates.keys()}
    now = timezone.now()

    with transaction.atomic():
        existing_rows = GuestRestaurantWindowMetrics.objects.filter(
            as_of_date=target_date,
            guest_id__in=guest_ids,
            department_id__in=departments,
            window_days__in=windows_set,
        )
        existing_by_key = {
            (
                int(item.guest_id),
                item.department_id or "",
                int(item.window_days),
            ): item
            for item in existing_rows
        }

        to_create: list[GuestRestaurantWindowMetrics] = []
        to_update: list[GuestRestaurantWindowMetrics] = []

        for key, aggregate in all_aggregates.items():
            existing = existing_by_key.get(key)
            if existing is None:
                to_create.append(
                    GuestRestaurantWindowMetrics(
                        as_of_date=target_date,
                        guest_id=aggregate.guest_id,
                        department_id=aggregate.department_id,
                        window_days=aggregate.window_days,
                        orders_count=aggregate.orders_count,
                        visits_count=aggregate.visits_count,
                        avg_check_net=aggregate.avg_check_net,
                        sum_net=aggregate.sum_net,
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
            GuestRestaurantWindowMetrics.objects.bulk_create(
                to_create,
                batch_size=safe_batch_size,
            )
        if to_update:
            GuestRestaurantWindowMetrics.objects.bulk_update(
                to_update,
                fields=[
                    "orders_count",
                    "visits_count",
                    "avg_check_net",
                    "sum_net",
                    "bonus_in_sum",
                    "bonus_out_sum",
                    "last_visit_at",
                    "rating_score",
                    "updated_at",
                ],
                batch_size=safe_batch_size,
            )

        stats.created_rows = len(to_create)
        stats.updated_rows = len(to_update)

    logger.info(
        (
            "rebuild_window_metrics_from_daily_facts: as_of=%s windows=%s scanned=%s grouped=%s "
            "created=%s updated=%s"
        ),
        target_date,
        windows,
        stats.scanned_daily_rows,
        stats.grouped_rows,
        stats.created_rows,
        stats.updated_rows,
    )
    return stats
