"""
Сервис подготовки данных для отдельной страницы аналитики по чат-ботам.

Источник данных:
1. VtelemaxRecipientChannel.
2. Метрики по дням строятся в накопительном виде + прирост за день.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from django.db.models import Count, Min
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from guests.models import VtelemaxRecipientChannel

ALLOWED_PERIOD_DAYS = (7, 14, 30)
DEFAULT_PERIOD_DAYS = 30


def normalize_bots_period_days(raw_value: int | str | None) -> int:
    """
    Нормализует размер периода в днях для страницы аналитики ботов.
    """
    try:
        value = int(raw_value or DEFAULT_PERIOD_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_PERIOD_DAYS
    if value not in ALLOWED_PERIOD_DAYS:
        return DEFAULT_PERIOD_DAYS
    return value


def build_bots_dashboard_payload(
    *,
    date_from: date,
    date_to: date,
    period_days: int | None = None,
) -> dict[str, Any]:
    """
    Готовит payload для страницы "Дашборд -> Боты".
    """
    if date_from > date_to:
        raise ValueError("date_from не может быть позже date_to")

    platforms = ("telegram", "vk", "max")
    days_list = _daterange(date_from, date_to)

    base_total_by_platform: dict[str, int] = {}
    for platform in platforms:
        base_total_by_platform[platform] = VtelemaxRecipientChannel.objects.filter(
            platform=platform,
            account_created_at__isnull=False,
            account_created_at__date__lt=date_from,
        ).count()

    registered_optin_qs = (
        VtelemaxRecipientChannel.objects.annotate(
            registration_at=Coalesce("registered_at", "account_created_at")
        )
        .filter(
            is_registered=True,
            notifications_allowed=True,
            registration_at__isnull=False,
        )
        .exclude(external_id__isnull=True)
        .exclude(external_id="")
    )

    base_optin_by_platform: dict[str, int] = {}
    for platform in platforms:
        base_optin_by_platform[platform] = registered_optin_qs.filter(
            platform=platform,
            registration_at__date__lt=date_from,
        ).count()

    created_daily_raw = (
        VtelemaxRecipientChannel.objects.filter(
            account_created_at__isnull=False,
            account_created_at__date__gte=date_from,
            account_created_at__date__lte=date_to,
        )
        .annotate(day=TruncDate("account_created_at"))
        .values("day", "platform")
        .annotate(total=Count("id"))
        .order_by("day", "platform")
    )
    created_daily: dict[tuple[date, str], int] = defaultdict(int)
    for row in created_daily_raw:
        created_daily[(row["day"], row["platform"])] = int(row["total"] or 0)

    optin_daily_raw = (
        registered_optin_qs.filter(
            registration_at__date__gte=date_from,
            registration_at__date__lte=date_to,
        )
        .annotate(day=TruncDate("registration_at"))
        .values("day", "platform")
        .annotate(total=Count("id"))
        .order_by("day", "platform")
    )
    optin_daily: dict[tuple[date, str], int] = defaultdict(int)
    for row in optin_daily_raw:
        optin_daily[(row["day"], row["platform"])] = int(row["total"] or 0)

    person_total_min_dates_qs = (
        VtelemaxRecipientChannel.objects.filter(account_created_at__isnull=False)
        .values("person_id")
        .annotate(first_at=Min("account_created_at"))
        .values("person_id", "first_at")
    )
    base_unique_total = 0
    unique_total_daily_add: dict[date, int] = defaultdict(int)
    for row in person_total_min_dates_qs:
        first_at = row["first_at"]
        if not first_at:
            continue
        first_day = timezone.localdate(first_at)
        if first_day < date_from:
            base_unique_total += 1
        elif first_day <= date_to:
            unique_total_daily_add[first_day] += 1

    person_optin_min_dates_qs = (
        registered_optin_qs.values("person_id")
        .annotate(first_at=Min("registration_at"))
        .values("person_id", "first_at")
    )
    base_unique_optin = 0
    unique_optin_daily_add: dict[date, int] = defaultdict(int)
    for row in person_optin_min_dates_qs:
        first_at = row["first_at"]
        if not first_at:
            continue
        first_day = timezone.localdate(first_at)
        if first_day < date_from:
            base_unique_optin += 1
        elif first_day <= date_to:
            unique_optin_daily_add[first_day] += 1

    running_total = dict(base_total_by_platform)
    running_optin = dict(base_optin_by_platform)
    running_unique_total = base_unique_total
    running_unique_optin = base_unique_optin

    rows: list[dict[str, Any]] = []
    for day in days_list:
        for platform in platforms:
            running_total[platform] += created_daily[(day, platform)]
            running_optin[platform] += optin_daily[(day, platform)]

        running_unique_total += unique_total_daily_add[day]
        running_unique_optin += unique_optin_daily_add[day]

        rows.append(
            {
                "day": day.isoformat(),
                "channels_total_telegram": running_total["telegram"],
                "channels_registered_optin_telegram": running_optin["telegram"],
                "channels_total_vk": running_total["vk"],
                "channels_registered_optin_vk": running_optin["vk"],
                "channels_total_max": running_total["max"],
                "channels_registered_optin_max": running_optin["max"],
                "unique_persons_total": running_unique_total,
                "unique_persons_registered_optin": running_unique_optin,
            }
        )

    kpis = _build_kpis(rows)
    quick_growth = _build_quick_growth(date_to=date_to, periods=(7, 14, 30))
    normalized_period_days = normalize_bots_period_days(period_days)
    return {
        "filters": {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "days": len(days_list),
            "period_days": normalized_period_days,
            "period_options": list(ALLOWED_PERIOD_DAYS),
        },
        "kpis": kpis,
        "quick_growth": quick_growth,
        "rows": rows,
    }


def _build_kpis(rows: list[dict[str, Any]]) -> dict[str, int]:
    if not rows:
        return {
            "channels_total": 0,
            "channels_registered_optin": 0,
            "unique_persons_total": 0,
            "unique_persons_registered_optin": 0,
        }
    last = rows[-1]
    return {
        "channels_total": int(last["channels_total_telegram"])
        + int(last["channels_total_vk"])
        + int(last["channels_total_max"]),
        "channels_registered_optin": int(last["channels_registered_optin_telegram"])
        + int(last["channels_registered_optin_vk"])
        + int(last["channels_registered_optin_max"]),
        "unique_persons_total": int(last["unique_persons_total"]),
        "unique_persons_registered_optin": int(last["unique_persons_registered_optin"]),
    }


def _daterange(date_from: date, date_to: date) -> list[date]:
    current = date_from
    result: list[date] = []
    while current <= date_to:
        result.append(current)
        current += timedelta(days=1)
    return result


def _build_quick_growth(*, date_to: date, periods: tuple[int, ...]) -> list[dict[str, int]]:
    snapshot_now = _build_snapshot_totals(as_of=date_to)
    result: list[dict[str, int]] = []
    for period_days in periods:
        prev_date = date_to - timedelta(days=period_days)
        snapshot_prev = _build_snapshot_totals(as_of=prev_date)
        result.append(
            {
                "days": int(period_days),
                "channels_total_delta": snapshot_now["channels_total"] - snapshot_prev["channels_total"],
                "channels_registered_optin_delta": snapshot_now["channels_registered_optin"]
                - snapshot_prev["channels_registered_optin"],
                "unique_persons_total_delta": snapshot_now["unique_persons_total"]
                - snapshot_prev["unique_persons_total"],
                "unique_persons_registered_optin_delta": snapshot_now["unique_persons_registered_optin"]
                - snapshot_prev["unique_persons_registered_optin"],
            }
        )
    return result


def _build_snapshot_totals(*, as_of: date) -> dict[str, int]:
    channels_total = VtelemaxRecipientChannel.objects.filter(
        account_created_at__isnull=False,
        account_created_at__date__lte=as_of,
    ).count()

    registered_optin_qs = (
        VtelemaxRecipientChannel.objects.annotate(
            registration_at=Coalesce("registered_at", "account_created_at")
        )
        .filter(
            is_registered=True,
            notifications_allowed=True,
            registration_at__isnull=False,
            registration_at__date__lte=as_of,
        )
        .exclude(external_id__isnull=True)
        .exclude(external_id="")
    )
    channels_registered_optin = registered_optin_qs.count()

    unique_persons_total = (
        VtelemaxRecipientChannel.objects.filter(account_created_at__isnull=False)
        .values("person_id")
        .annotate(first_at=Min("account_created_at"))
        .filter(first_at__date__lte=as_of)
        .count()
    )
    unique_persons_registered_optin = (
        registered_optin_qs.values("person_id")
        .annotate(first_at=Min("registration_at"))
        .filter(first_at__date__lte=as_of)
        .count()
    )

    return {
        "channels_total": int(channels_total),
        "channels_registered_optin": int(channels_registered_optin),
        "unique_persons_total": int(unique_persons_total),
        "unique_persons_registered_optin": int(unique_persons_registered_optin),
    }
