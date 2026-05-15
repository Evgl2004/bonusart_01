from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q

from guests.models import CouponCampaignAssignment, Mailing, OrderFact


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
    assignments_expired: int = 0
    assignments_canceled: int = 0
    assignments_error: int = 0
    used_within_campaign: int = 0
    used_late_total: int = 0
    returned_guest_coupon: int = 0
    returned_window_days: int = 0
    revenue_net_used: Decimal = Decimal("0")
    unique_used_guests: int = 0
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
        if self.assignments_used <= 0:
            return Decimal("0")
        return self.revenue_net_used / Decimal(self.assignments_used)

    def to_dict(self) -> dict[str, object]:
        return {
            "campaign_id": int(self.campaign_id),
            "coupon_series": self.coupon_series,
            "recipients_total": int(self.recipients_total),
            "assignments_total": int(self.assignments_total),
            "assignments_reserved": int(self.assignments_reserved),
            "assignments_sent": int(self.assignments_sent),
            "assignments_used": int(self.assignments_used),
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
        expired=Count("id", filter=Q(status=CouponCampaignAssignment.Status.EXPIRED)),
        canceled=Count("id", filter=Q(status=CouponCampaignAssignment.Status.CANCELED)),
        error=Count("id", filter=Q(status=CouponCampaignAssignment.Status.ERROR)),
    )
    snapshot.assignments_total = int(status_counts.get("total") or 0)
    snapshot.assignments_reserved = int(status_counts.get("reserved") or 0)
    snapshot.assignments_sent = int(status_counts.get("sent") or 0)
    snapshot.assignments_used = int(status_counts.get("used") or 0)
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
            "net_sum",
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

    campaign_start = mailing.scheduled_time_begin
    campaign_end = mailing.scheduled_time_end
    campaign_start_date = campaign_start.date()
    campaign_end_date = campaign_end.date()

    used_within_guest_ids: set[int] = set()
    unique_used_guest_ids: set[int] = set()
    late_rows: list[dict[str, object]] = []
    revenue_total = Decimal("0")

    for assignment in assignments:
        if assignment.get("status") != CouponCampaignAssignment.Status.USED:
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
        in_campaign = False
        is_late = False

        if used_at is not None:
            in_campaign = campaign_start <= used_at <= campaign_end
            is_late = used_at > campaign_end
        elif fact_business_date is not None:
            in_campaign = campaign_start_date <= fact_business_date <= campaign_end_date
            is_late = fact_business_date > campaign_end_date

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
            try:
                revenue_total += Decimal(str(fact_row.get("net_sum") or "0"))
            except Exception:  # noqa: BLE001
                continue

    snapshot.late_usage_rows = late_rows
    snapshot.revenue_net_used = revenue_total
    snapshot.unique_used_guests = len(unique_used_guest_ids)

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
