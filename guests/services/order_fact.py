"""
Сервис построения `order_fact` из сырых OLAP-строк.

Логика:
1. читает `olap_sales_raw_line` в заданном диапазоне;
2. агрегирует позиции в один факт заказа;
3. выполняет идемпотентный upsert в `order_fact`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from guests.models import OrderFact, OlapSalesRawLine

logger = logging.getLogger(__name__)


@dataclass
class OrderFactBuildStats:
    """
    Сводная статистика построения `order_fact`.
    """

    scanned_raw_lines: int = 0
    grouped_orders: int = 0
    skipped_invalid_lines: int = 0
    created_facts: int = 0
    updated_facts: int = 0


@dataclass
class _OrderAggregate:
    """
    Внутренний накопитель агрегатов по одному заказу.
    """

    guest_id: int | None
    business_date: date
    department_id: str
    department_name: str | None
    order_number: int
    uniq_order_id: str
    gross_sum: Decimal = Decimal("0")
    net_sum: Decimal = Decimal("0")
    discount_sum: Decimal = Decimal("0")
    bonus_sum: Decimal = Decimal("0")
    items_count: int = 0
    category_ids: set[str] = field(default_factory=set)
    coupon_series: str | None = None
    coupon_number: str | None = None
    order_type: str | None = None
    is_delivery: bool = False
    first_seen_at = None

    @property
    def categories_count(self) -> int:
        return len(self.category_ids)

    @property
    def coupon_used(self) -> bool:
        return bool(self.coupon_series or self.coupon_number)


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


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "да"}


def _aggregate_order_rows(
    *,
    raw_line_id_from: int | None,
    raw_line_id_to: int | None,
    business_date_from: date | None,
    business_date_to: date | None,
    batch_size: int,
) -> tuple[dict[tuple[date, str, int, str], _OrderAggregate], OrderFactBuildStats]:
    stats = OrderFactBuildStats()
    aggregates: dict[tuple[date, str, int, str], _OrderAggregate] = {}

    query = (
        OlapSalesRawLine.objects.all()
        .order_by("id")
        .values(
            "id",
            "guest_id",
            "business_date",
            "department_id",
            "department_name",
            "order_number",
            "uniq_order_id",
            "dish_category_id",
            "dish_sum_before_discount",
            "dish_sum_after_discount",
            "discount_sum",
            "bonus_sum",
            "coupon_series",
            "coupon_number",
            "created_at",
            "raw_payload",
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

        business_day = row["business_date"]
        order_number = row["order_number"]
        if business_day is None or order_number is None:
            stats.skipped_invalid_lines += 1
            continue

        department_id = _normalize_text(row["department_id"])
        uniq_order_id = _normalize_text(row["uniq_order_id"])
        key = (business_day, department_id, int(order_number), uniq_order_id)

        aggregate = aggregates.get(key)
        if aggregate is None:
            aggregate = _OrderAggregate(
                guest_id=row["guest_id"],
                business_date=business_day,
                department_id=department_id,
                department_name=row["department_name"],
                order_number=int(order_number),
                uniq_order_id=uniq_order_id,
            )
            aggregates[key] = aggregate

        aggregate.items_count += 1
        category_id = _normalize_text(row["dish_category_id"])
        if category_id:
            aggregate.category_ids.add(category_id)

        aggregate.gross_sum += _to_decimal(row["dish_sum_before_discount"])
        net_value = _to_decimal(row["dish_sum_after_discount"])
        if net_value == Decimal("0"):
            net_value = _to_decimal(row["dish_sum_before_discount"])
        aggregate.net_sum += net_value
        aggregate.discount_sum += _to_decimal(row["discount_sum"])
        aggregate.bonus_sum += _to_decimal(row["bonus_sum"])

        if not aggregate.department_name and row["department_name"]:
            aggregate.department_name = row["department_name"]

        if not aggregate.coupon_series and row["coupon_series"]:
            aggregate.coupon_series = row["coupon_series"]
        if not aggregate.coupon_number and row["coupon_number"]:
            aggregate.coupon_number = row["coupon_number"]

        created_at = row["created_at"]
        if aggregate.first_seen_at is None or (created_at and created_at < aggregate.first_seen_at):
            aggregate.first_seen_at = created_at

        raw_payload = row.get("raw_payload") or {}
        if not aggregate.order_type:
            aggregate.order_type = raw_payload.get("OrderType") or raw_payload.get("OrderType.Name")
        aggregate.is_delivery = aggregate.is_delivery or _to_bool(
            raw_payload.get("IsDelivery") or raw_payload.get("OrderType.IsDelivery")
        )

    stats.grouped_orders = len(aggregates)
    return aggregates, stats


def rebuild_order_fact_from_raw_lines(
    *,
    raw_line_id_from: int | None = None,
    raw_line_id_to: int | None = None,
    business_date_from: date | None = None,
    business_date_to: date | None = None,
    batch_size: int = 2000,
) -> OrderFactBuildStats:
    """
    Пересобирает `order_fact` по данным `olap_sales_raw_line`.

    Если факт заказа уже существует, запись обновляется (идемпотентный upsert).
    """

    aggregates, stats = _aggregate_order_rows(
        raw_line_id_from=raw_line_id_from,
        raw_line_id_to=raw_line_id_to,
        business_date_from=business_date_from,
        business_date_to=business_date_to,
        batch_size=batch_size,
    )

    if not aggregates:
        logger.info("rebuild_order_fact_from_raw_lines: нет данных для обработки")
        return stats

    business_dates = {key[0] for key in aggregates.keys()}
    department_ids = {key[1] for key in aggregates.keys()}
    order_numbers = {key[2] for key in aggregates.keys()}
    now = timezone.now()

    with transaction.atomic():
        existing_rows = OrderFact.objects.filter(
            business_date__in=business_dates,
            department_id__in=department_ids,
            order_number__in=order_numbers,
        )
        existing_by_key = {
            (
                item.business_date,
                item.department_id or "",
                int(item.order_number),
                item.uniq_order_id or "",
            ): item
            for item in existing_rows
        }

        to_create: list[OrderFact] = []
        to_update: list[OrderFact] = []

        for key, aggregate in aggregates.items():
            existing = existing_by_key.get(key)
            if existing is None:
                to_create.append(
                    OrderFact(
                        guest_id=aggregate.guest_id,
                        business_date=aggregate.business_date,
                        department_id=aggregate.department_id,
                        department_name=aggregate.department_name,
                        order_number=aggregate.order_number,
                        uniq_order_id=aggregate.uniq_order_id,
                        gross_sum=aggregate.gross_sum,
                        net_sum=aggregate.net_sum,
                        discount_sum=aggregate.discount_sum,
                        bonus_sum=aggregate.bonus_sum,
                        items_count=aggregate.items_count,
                        categories_count=aggregate.categories_count,
                        coupon_used=aggregate.coupon_used,
                        coupon_series=aggregate.coupon_series or None,
                        coupon_number=aggregate.coupon_number or None,
                        order_type=aggregate.order_type or None,
                        is_delivery=aggregate.is_delivery,
                        first_seen_at=aggregate.first_seen_at,
                    )
                )
                continue

            changed = False
            fields_to_compare = {
                "guest_id": aggregate.guest_id,
                "department_name": aggregate.department_name,
                "gross_sum": aggregate.gross_sum,
                "net_sum": aggregate.net_sum,
                "discount_sum": aggregate.discount_sum,
                "bonus_sum": aggregate.bonus_sum,
                "items_count": aggregate.items_count,
                "categories_count": aggregate.categories_count,
                "coupon_used": aggregate.coupon_used,
                "coupon_series": aggregate.coupon_series or None,
                "coupon_number": aggregate.coupon_number or None,
                "order_type": aggregate.order_type or None,
                "is_delivery": aggregate.is_delivery,
            }
            for field_name, expected_value in fields_to_compare.items():
                if getattr(existing, field_name) != expected_value:
                    setattr(existing, field_name, expected_value)
                    changed = True

            if aggregate.first_seen_at and (
                existing.first_seen_at is None or aggregate.first_seen_at < existing.first_seen_at
            ):
                existing.first_seen_at = aggregate.first_seen_at
                changed = True

            if changed:
                existing.updated_at = now
                to_update.append(existing)

        if to_create:
            OrderFact.objects.bulk_create(to_create, batch_size=max(100, int(batch_size)))
        if to_update:
            OrderFact.objects.bulk_update(
                to_update,
                fields=[
                    "guest",
                    "department_name",
                    "gross_sum",
                    "net_sum",
                    "discount_sum",
                    "bonus_sum",
                    "items_count",
                    "categories_count",
                    "coupon_used",
                    "coupon_series",
                    "coupon_number",
                    "order_type",
                    "is_delivery",
                    "first_seen_at",
                    "updated_at",
                ],
                batch_size=max(100, int(batch_size)),
            )

        stats.created_facts = len(to_create)
        stats.updated_facts = len(to_update)

    logger.info(
        (
            "rebuild_order_fact_from_raw_lines: scanned=%s grouped=%s skipped=%s "
            "created=%s updated=%s"
        ),
        stats.scanned_raw_lines,
        stats.grouped_orders,
        stats.skipped_invalid_lines,
        stats.created_facts,
        stats.updated_facts,
    )
    return stats

