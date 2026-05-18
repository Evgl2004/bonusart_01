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
                # Готовый суточный прирост: источник для "нижних" графиков дельты.
                "channels_total_telegram_delta": created_daily[(day, "telegram")],
                "channels_registered_optin_telegram_delta": optin_daily[(day, "telegram")],
                "channels_total_vk_delta": created_daily[(day, "vk")],
                "channels_registered_optin_vk_delta": optin_daily[(day, "vk")],
                "channels_total_max_delta": created_daily[(day, "max")],
                "channels_registered_optin_max_delta": optin_daily[(day, "max")],
                "unique_persons_total_delta": unique_total_daily_add[day],
                "unique_persons_registered_optin_delta": unique_optin_daily_add[day],
            }
        )

    kpis = _build_kpis(rows)
    header_totals = _build_snapshot_totals(as_of=date_to)
    quick_growth = _build_quick_growth(
        date_to=date_to,
        periods=(7, 14, 30),
        snapshot_now=header_totals,
    )
    yesterday_growth = _build_yesterday_growth(date_to=date_to, rows=rows)
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
        "header_totals": header_totals,
        "yesterday_growth": yesterday_growth,
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


def _build_quick_growth(
    *,
    date_to: date,
    periods: tuple[int, ...],
    snapshot_now: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    snapshot_current = snapshot_now or _build_snapshot_totals(as_of=date_to)
    result: list[dict[str, Any]] = []
    for period_days in periods:
        prev_date = date_to - timedelta(days=period_days)
        snapshot_prev = _build_snapshot_totals(as_of=prev_date)
        delta_channels_total = snapshot_current["channels_total"] - snapshot_prev["channels_total"]
        delta_channels_optin = (
            snapshot_current["channels_registered_optin"] - snapshot_prev["channels_registered_optin"]
        )
        delta_unique_total = snapshot_current["unique_persons_total"] - snapshot_prev["unique_persons_total"]
        delta_unique_optin = (
            snapshot_current["unique_persons_registered_optin"]
            - snapshot_prev["unique_persons_registered_optin"]
        )
        result.append(
            {
                "days": int(period_days),
                "channels_total_delta": delta_channels_total,
                "channels_total_delta_display": _format_signed(delta_channels_total),
                "channels_registered_optin_delta": delta_channels_optin,
                "channels_registered_optin_delta_display": _format_signed(delta_channels_optin),
                "unique_persons_total_delta": delta_unique_total,
                "unique_persons_total_delta_display": _format_signed(delta_unique_total),
                "unique_persons_registered_optin_delta": delta_unique_optin,
                "unique_persons_registered_optin_delta_display": _format_signed(delta_unique_optin),
            }
        )
    return result


def _build_yesterday_growth(*, date_to: date, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """
    Собирает карточку прироста за последний отображаемый день.

    На странице `date_to` равен локальному вчера, поэтому пользовательский заголовок
    остаётся "Прирост за вчера". Значения берём из последней строки графика, чтобы
    карточка и нижние daily-графики не расходились на один день.
    """
    if rows:
        last_row = rows[-1]
        delta_channels_total = _sum_row_ints(
            last_row,
            (
                "channels_total_telegram_delta",
                "channels_total_vk_delta",
                "channels_total_max_delta",
            ),
        )
        delta_channels_optin = _sum_row_ints(
            last_row,
            (
                "channels_registered_optin_telegram_delta",
                "channels_registered_optin_vk_delta",
                "channels_registered_optin_max_delta",
            ),
        )
        delta_unique_total = int(last_row.get("unique_persons_total_delta") or 0)
        delta_unique_optin = int(last_row.get("unique_persons_registered_optin_delta") or 0)
    else:
        previous_day = date_to - timedelta(days=1)
        snapshot_current = _build_snapshot_totals(as_of=date_to)
        snapshot_previous = _build_snapshot_totals(as_of=previous_day)
        delta_channels_total = snapshot_current["channels_total"] - snapshot_previous["channels_total"]
        delta_channels_optin = (
            snapshot_current["channels_registered_optin"]
            - snapshot_previous["channels_registered_optin"]
        )
        delta_unique_total = snapshot_current["unique_persons_total"] - snapshot_previous["unique_persons_total"]
        delta_unique_optin = (
            snapshot_current["unique_persons_registered_optin"]
            - snapshot_previous["unique_persons_registered_optin"]
        )
    return {
        "date": date_to.isoformat(),
        "date_label": date_to.strftime("%d.%m"),
        "channels_total_delta": delta_channels_total,
        "channels_total_delta_display": _format_signed(delta_channels_total),
        "channels_registered_optin_delta": delta_channels_optin,
        "channels_registered_optin_delta_display": _format_signed(delta_channels_optin),
        "unique_persons_total_delta": delta_unique_total,
        "unique_persons_total_delta_display": _format_signed(delta_unique_total),
        "unique_persons_registered_optin_delta": delta_unique_optin,
        "unique_persons_registered_optin_delta_display": _format_signed(delta_unique_optin),
    }


def _sum_row_ints(row: dict[str, Any], keys: tuple[str, ...]) -> int:
    return sum(int(row.get(key) or 0) for key in keys)


def _format_signed(value: int) -> str:
    if value > 0:
        return f"+{value}"
    return str(value)


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
