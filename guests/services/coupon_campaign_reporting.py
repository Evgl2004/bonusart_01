from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q

from guests.models import CouponCampaignAssignment, Mailing, OlapSalesRawLine, OrderFact


def _to_decimal(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _normalize_text(value) -> str:
    return str(value or "").strip()


def _order_identity(row: dict[str, object]) -> tuple[object, str, int | None, str]:
    order_number = row.get("order_number")
    return (
        row.get("business_date"),
        _normalize_text(row.get("department_id")),
        int(order_number) if order_number is not None else None,
        _normalize_text(row.get("uniq_order_id")),
    )


def _raw_line_net_sum(row: dict[str, object]) -> Decimal:
    if row.get("dish_sum_after_discount") in (None, ""):
        return _raw_line_gross_sum(row)
    return _to_decimal(row.get("dish_sum_after_discount"))


def _raw_line_gross_sum(row: dict[str, object]) -> Decimal:
    return _to_decimal(row.get("dish_sum_before_discount"))


def _raw_line_quantity(row: dict[str, object]) -> Decimal:
    quantity = _to_decimal(row.get("dish_amount"))
    return quantity if quantity > 0 else Decimal("1")


def _percent_share(value: Decimal, total: Decimal) -> float:
    if total <= 0:
        return 0.0
    return float(round((value / total) * Decimal("100"), 2))


@dataclass(slots=True)
class CouponCampaignPerformanceSnapshot:
    """
    Сводный срез по эффективности купонной кампании.
    """

    campaign_id: int
    coupon_series: str
    recipients_total: int = 0
    assignments_total: int = 0
    assignments_reserved: int = 0
    assignments_sent: int = 0
    assignments_used: int = 0
    assignments_used_after_campaign: int = 0
    assignments_expired: int = 0
    assignments_canceled: int = 0
    assignments_error: int = 0
    used_within_campaign: int = 0
    used_late_total: int = 0
    returned_guest_coupon: int = 0
    returned_window_days: int = 0
    revenue_net_used: Decimal = Decimal("0")
    unique_used_guests: int = 0
    coupon_orders_total: int = 0
    daily_usage_rows: list[dict[str, object]] = field(default_factory=list)
    product_rank_rows: list[dict[str, object]] = field(default_factory=list)
    order_detail_rows: list[dict[str, object]] = field(default_factory=list)
    late_usage_rows: list[dict[str, object]] = field(default_factory=list)

    @property
    def coupons_sent_total(self) -> int:
        """
        Количество купонов, которые фактически дошли до статуса отправки.

        Примечание:
        статус `used` означает, что купон ранее был отправлен, поэтому включаем
        его в общий счётчик отправленных купонов.
        """
        return int(self.assignments_sent + self.assignments_used)

    @property
    def usage_rate_percent(self) -> float:
        denominator = self.coupons_sent_total
        if denominator <= 0:
            return 0.0
        return round((self.assignments_used / denominator) * 100.0, 2)

    @property
    def returned_guests_rate_percent(self) -> float:
        """
        Доля вернувшихся гостей по купону.

        База для расчёта:
        1. `used_within_campaign`, если есть применения внутри окна кампании;
        2. иначе fallback на `assignments_used`.
        """
        denominator = int(self.used_within_campaign or 0)
        if denominator <= 0:
            denominator = int(self.assignments_used or 0)
        if denominator <= 0:
            return 0.0
        return round((self.returned_guest_coupon / denominator) * 100.0, 2)

    @property
    def coupon_orders_avg_check(self) -> Decimal:
        """
        Средний чек по заказам с применением купонов кампании.
        """
        denominator = int(self.coupon_orders_total or self.assignments_used or 0)
        if denominator <= 0:
            return Decimal("0")
        return self.revenue_net_used / Decimal(denominator)

    def to_dict(self) -> dict[str, object]:
        return {
            "campaign_id": int(self.campaign_id),
            "coupon_series": self.coupon_series,
            "recipients_total": int(self.recipients_total),
            "assignments_total": int(self.assignments_total),
            "assignments_reserved": int(self.assignments_reserved),
            "assignments_sent": int(self.assignments_sent),
            "assignments_used": int(self.assignments_used),
            "assignments_used_after_campaign": int(self.assignments_used_after_campaign),
            "assignments_expired": int(self.assignments_expired),
            "assignments_canceled": int(self.assignments_canceled),
            "assignments_error": int(self.assignments_error),
            "coupons_sent_total": int(self.coupons_sent_total),
            "used_within_campaign": int(self.used_within_campaign),
            "used_late_total": int(self.used_late_total),
            "returned_guest_coupon": int(self.returned_guest_coupon),
            "returned_window_days": int(self.returned_window_days),
            "revenue_net_used": str(self.revenue_net_used),
            "coupon_orders_avg_check": str(self.coupon_orders_avg_check),
            "unique_used_guests": int(self.unique_used_guests),
            "coupon_orders_total": int(self.coupon_orders_total),
            "daily_usage_rows": list(self.daily_usage_rows),
            "product_rank_rows": list(self.product_rank_rows),
            "order_detail_rows": list(self.order_detail_rows),
            "usage_rate_percent": float(self.usage_rate_percent),
            "returned_guests_rate_percent": float(self.returned_guests_rate_percent),
            "late_usage_rows": list(self.late_usage_rows),
        }


def _resolve_returned_window_days(mailing: Mailing, explicit_days: int | None) -> int:
    if explicit_days is not None and int(explicit_days) > 0:
        return int(explicit_days)
    duration_days = (mailing.scheduled_time_end.date() - mailing.scheduled_time_begin.date()).days + 1
    return max(1, int(duration_days))


def build_coupon_campaign_performance_snapshot(
    *,
    mailing: Mailing,
    returned_window_days: int | None = None,
    late_rows_limit: int = 20,
) -> CouponCampaignPerformanceSnapshot:
    """
    Строит аналитический срез по купонной кампании.

    Метрики:
    1. покрытие и статусы назначений;
    2. использование купонов внутри окна кампании и поздние применения;
    3. «вернувшиеся гости» по купону:
       - в pre-window (N дней до старта) не было заказов;
       - купон кампании использован в пределах окна кампании.
    """
    snapshot = CouponCampaignPerformanceSnapshot(
        campaign_id=int(mailing.id),
        coupon_series=str(getattr(mailing, "coupon_series", "") or "").strip(),
    )
    snapshot.recipients_total = int(mailing.guests_rows.count())

    if not snapshot.coupon_series:
        return snapshot

    assignments_qs = CouponCampaignAssignment.objects.filter(campaign=mailing)
    status_counts = assignments_qs.aggregate(
        total=Count("id"),
        reserved=Count("id", filter=Q(status=CouponCampaignAssignment.Status.RESERVED)),
        sent=Count("id", filter=Q(status=CouponCampaignAssignment.Status.SENT)),
        used=Count("id", filter=Q(status=CouponCampaignAssignment.Status.USED)),
        used_after_campaign=Count(
            "id",
            filter=Q(status=CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN),
        ),
        expired=Count("id", filter=Q(status=CouponCampaignAssignment.Status.EXPIRED)),
        canceled=Count("id", filter=Q(status=CouponCampaignAssignment.Status.CANCELED)),
        error=Count("id", filter=Q(status=CouponCampaignAssignment.Status.ERROR)),
    )
    snapshot.assignments_total = int(status_counts.get("total") or 0)
    snapshot.assignments_reserved = int(status_counts.get("reserved") or 0)
    snapshot.assignments_sent = int(status_counts.get("sent") or 0)
    used_regular = int(status_counts.get("used") or 0)
    used_after_campaign = int(status_counts.get("used_after_campaign") or 0)
    snapshot.assignments_used = int(used_regular + used_after_campaign)
    snapshot.assignments_used_after_campaign = used_after_campaign
    snapshot.assignments_expired = int(status_counts.get("expired") or 0)
    snapshot.assignments_canceled = int(status_counts.get("canceled") or 0)
    snapshot.assignments_error = int(status_counts.get("error") or 0)

    if snapshot.assignments_total <= 0:
        return snapshot

    assignments = list(
        assignments_qs.values(
            "id",
            "guest_id",
            "coupon_series",
            "coupon_code",
            "status",
            "used_at",
            "used_order_id",
        )
    )

    keys = {
        (
            str(item.get("coupon_series") or "").strip(),
            str(item.get("coupon_code") or "").strip(),
        )
        for item in assignments
        if str(item.get("coupon_series") or "").strip() and str(item.get("coupon_code") or "").strip()
    }
    if not keys:
        return snapshot

    series_values = sorted({item[0] for item in keys})
    code_values = sorted({item[1] for item in keys})
    order_fact_rows = list(
        OrderFact.objects.filter(
            coupon_used=True,
            coupon_series__in=series_values,
            coupon_number__in=code_values,
        )
        .values(
            "id",
            "guest_id",
            "business_date",
            "order_number",
            "coupon_series",
            "coupon_number",
            "department_id",
            "department_name",
            "uniq_order_id",
            "net_sum",
            "gross_sum",
            "discount_sum",
        )
        .order_by("business_date", "id")
    )

    first_order_fact_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for row in order_fact_rows:
        key = (
            str(row.get("coupon_series") or "").strip(),
            str(row.get("coupon_number") or "").strip(),
        )
        if key not in first_order_fact_by_key:
            first_order_fact_by_key[key] = row

    order_identities = {
        _order_identity(row)
        for row in order_fact_rows
        if row.get("business_date") is not None and row.get("order_number") is not None
    }
    order_uniq_ids = sorted({identity[3] for identity in order_identities if identity[3]})

    raw_filter = Q(coupon_series__in=series_values, coupon_number__in=code_values)
    if order_uniq_ids:
        raw_filter |= Q(uniq_order_id__in=order_uniq_ids)

    raw_line_rows = list(
        OlapSalesRawLine.objects.filter(raw_filter)
        .values(
            "id",
            "guest_id",
            "business_date",
            "department_id",
            "department_name",
            "order_number",
            "uniq_order_id",
            "dish_code",
            "dish_name",
            "dish_amount",
            "dish_sum_before_discount",
            "dish_sum_after_discount",
            "discount_sum",
            "coupon_series",
            "coupon_number",
        )
        .order_by("business_date", "order_number", "id")
    )

    raw_lines_by_identity: dict[tuple[object, str, int | None, str], list[dict[str, object]]] = {}
    raw_lines_by_coupon_key: dict[tuple[str, str], list[dict[str, object]]] = {}
    for raw_row in raw_line_rows:
        raw_identity = _order_identity(raw_row)
        raw_lines_by_identity.setdefault(raw_identity, []).append(raw_row)
        raw_coupon_key = (
            _normalize_text(raw_row.get("coupon_series")),
            _normalize_text(raw_row.get("coupon_number")),
        )
        if raw_coupon_key[0] and raw_coupon_key[1]:
            raw_lines_by_coupon_key.setdefault(raw_coupon_key, []).append(raw_row)

    campaign_start = mailing.scheduled_time_begin
    campaign_end = mailing.scheduled_time_end
    campaign_start_date = campaign_start.date()
    campaign_end_date = campaign_end.date()

    used_within_guest_ids: set[int] = set()
    unique_used_guest_ids: set[int] = set()
    late_rows: list[dict[str, object]] = []
    revenue_total = Decimal("0")
    counted_order_keys: set[tuple[object, ...]] = set()
    daily_stats: dict[object, dict[str, object]] = {}
    product_stats: dict[tuple[str, str], dict[str, object]] = {}
    order_detail_rows: list[dict[str, object]] = []
    used_statuses = {
        CouponCampaignAssignment.Status.USED,
        CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN,
    }

    for assignment in assignments:
        status = assignment.get("status")
        if status not in used_statuses:
            continue

        key = (
            str(assignment.get("coupon_series") or "").strip(),
            str(assignment.get("coupon_code") or "").strip(),
        )
        fact_row = first_order_fact_by_key.get(key)
        used_at = assignment.get("used_at")
        guest_id = assignment.get("guest_id")

        if guest_id:
            unique_used_guest_ids.add(int(guest_id))

        fact_business_date = fact_row.get("business_date") if fact_row else None
        forced_late = status == CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN
        in_campaign = False
        is_late = False

        if fact_business_date is not None:
            in_campaign = campaign_start_date <= fact_business_date <= campaign_end_date
            is_late = fact_business_date > campaign_end_date
            if fact_business_date == campaign_end_date and used_at is not None:
                in_campaign = campaign_start <= used_at <= campaign_end
                is_late = used_at > campaign_end
        elif used_at is not None:
            in_campaign = campaign_start <= used_at <= campaign_end
            is_late = used_at > campaign_end

        if forced_late:
            in_campaign = False
            is_late = True

        if in_campaign and guest_id:
            used_within_guest_ids.add(int(guest_id))
        if in_campaign:
            snapshot.used_within_campaign += 1
        if is_late:
            snapshot.used_late_total += 1
            if len(late_rows) < max(0, int(late_rows_limit)):
                late_rows.append(
                    {
                        "assignment_id": int(assignment.get("id")),
                        "guest_id": int(guest_id) if guest_id else None,
                        "coupon_code": key[1],
                        "used_at": used_at.isoformat() if used_at else None,
                        "business_date": fact_business_date.isoformat() if fact_business_date else None,
                        "order_number": fact_row.get("order_number") if fact_row else assignment.get("used_order_id"),
                    }
                )

        if fact_row is not None:
            identity = _order_identity(fact_row)
            order_key = ("order", *identity) if identity[0] is not None and identity[2] is not None else ("coupon", *key)
            if order_key in counted_order_keys:
                continue
            counted_order_keys.add(order_key)

            raw_lines = raw_lines_by_identity.get(identity) or raw_lines_by_coupon_key.get(key, [])
            if raw_lines:
                order_net_sum = sum((_raw_line_net_sum(row) for row in raw_lines), Decimal("0"))
                order_gross_sum = sum((_raw_line_gross_sum(row) for row in raw_lines), Decimal("0"))
            else:
                order_net_sum = _to_decimal(fact_row.get("net_sum"))
                order_gross_sum = _to_decimal(fact_row.get("gross_sum"))

            order_discount_sum = max(order_gross_sum - order_net_sum, Decimal("0"))
            revenue_total += order_net_sum
            snapshot.coupon_orders_total += 1

            business_date = fact_business_date or fact_row.get("business_date")
            if business_date is not None:
                daily_row = daily_stats.setdefault(
                    business_date,
                    {
                        "business_date": business_date.isoformat(),
                        "orders_count": 0,
                        "revenue_net": Decimal("0"),
                        "gross_sum": Decimal("0"),
                        "discount_sum": Decimal("0"),
                    },
                )
                daily_row["orders_count"] = int(daily_row["orders_count"]) + 1
                daily_row["revenue_net"] = daily_row["revenue_net"] + order_net_sum
                daily_row["gross_sum"] = daily_row["gross_sum"] + order_gross_sum
                daily_row["discount_sum"] = daily_row["discount_sum"] + order_discount_sum

            order_items: list[dict[str, object]] = []
            for raw_row in raw_lines:
                product_key = (
                    _normalize_text(raw_row.get("dish_code")),
                    _normalize_text(raw_row.get("dish_name")) or "Без названия",
                )
                product_row = product_stats.setdefault(
                    product_key,
                    {
                        "dish_code": product_key[0],
                        "dish_name": product_key[1],
                        "orders": set(),
                        "quantity_total": Decimal("0"),
                        "gross_sum": Decimal("0"),
                        "revenue_net": Decimal("0"),
                        "discount_sum": Decimal("0"),
                    },
                )
                quantity = _raw_line_quantity(raw_row)
                raw_gross_sum = _raw_line_gross_sum(raw_row)
                raw_net_sum = _raw_line_net_sum(raw_row)
                raw_discount_sum = max(raw_gross_sum - raw_net_sum, Decimal("0"))
                product_row["orders"].add(order_key)
                product_row["quantity_total"] = product_row["quantity_total"] + quantity
                product_row["gross_sum"] = product_row["gross_sum"] + raw_gross_sum
                product_row["revenue_net"] = product_row["revenue_net"] + raw_net_sum
                product_row["discount_sum"] = product_row["discount_sum"] + raw_discount_sum
                order_items.append(
                    {
                        "dish_code": product_key[0],
                        "dish_name": product_key[1],
                        "quantity": str(quantity),
                        "gross_sum": str(raw_gross_sum),
                        "revenue_net": str(raw_net_sum),
                        "discount_sum": str(raw_discount_sum),
                    }
                )

            order_detail_rows.append(
                {
                    "business_date": business_date.isoformat() if business_date else None,
                    "order_number": fact_row.get("order_number"),
                    "guest_id": int(guest_id) if guest_id else fact_row.get("guest_id"),
                    "coupon_code": key[1],
                    "department_name": fact_row.get("department_name") or "",
                    "gross_sum": str(order_gross_sum),
                    "revenue_net": str(order_net_sum),
                    "discount_sum": str(order_discount_sum),
                    "items_count": len(order_items) if order_items else int(fact_row.get("items_count") or 0),
                    "items": order_items,
                }
            )

    snapshot.late_usage_rows = late_rows
    snapshot.revenue_net_used = revenue_total
    snapshot.unique_used_guests = len(unique_used_guest_ids)

    max_daily_revenue = max(
        (row["revenue_net"] for row in daily_stats.values()),
        default=Decimal("0"),
    )
    snapshot.daily_usage_rows = [
        {
            "business_date": row["business_date"],
            "orders_count": int(row["orders_count"]),
            "revenue_net": str(row["revenue_net"]),
            "gross_sum": str(row["gross_sum"]),
            "discount_sum": str(row["discount_sum"]),
            "revenue_share_percent": _percent_share(row["revenue_net"], max_daily_revenue),
        }
        for _, row in sorted(daily_stats.items(), key=lambda item: item[0])
    ]
    snapshot.product_rank_rows = sorted(
        [
            {
                "dish_code": row["dish_code"],
                "dish_name": row["dish_name"],
                "orders_count": len(row["orders"]),
                "quantity_total": str(row["quantity_total"]),
                "gross_sum": str(row["gross_sum"]),
                "revenue_net": str(row["revenue_net"]),
                "discount_sum": str(row["discount_sum"]),
            }
            for row in product_stats.values()
        ],
        key=lambda row: (
            -int(row["orders_count"]),
            -_to_decimal(row["revenue_net"]),
            str(row["dish_name"]),
        ),
    )[:20]
    snapshot.order_detail_rows = sorted(
        order_detail_rows,
        key=lambda row: (
            str(row.get("business_date") or ""),
            int(row.get("order_number") or 0),
            str(row.get("coupon_code") or ""),
        ),
    )

    # Метрика returned_guest_coupon.
    window_days = _resolve_returned_window_days(mailing, returned_window_days)
    snapshot.returned_window_days = window_days
    if used_within_guest_ids:
        pre_window_from = campaign_start_date - timedelta(days=window_days)
        pre_window_to = campaign_start_date - timedelta(days=1)
        pre_orders_guest_ids = set(
            OrderFact.objects.filter(
                guest_id__in=list(used_within_guest_ids),
                business_date__gte=pre_window_from,
                business_date__lte=pre_window_to,
            )
            .values_list("guest_id", flat=True)
            .distinct()
        )
        snapshot.returned_guest_coupon = sum(
            1 for guest_id in used_within_guest_ids if int(guest_id) not in pre_orders_guest_ids
        )

    return snapshot
