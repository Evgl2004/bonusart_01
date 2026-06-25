"""
Сервис подготовки данных для дашборда «Динамика сегментов».

Первая реализация строит ряды на лету из существующих витрин:
1. `GuestRestaurantWindowMetrics` для сегментов активности;
2. `GuestRestaurantDailyOrderFact` для сегмента «Новые за день»;
3. `VtelemaxRecipientChannel` и `OrderFact` для сегмента «активен в боте, без визитов 180д».
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from django.db.models import Exists, Max, Min, OuterRef
from django.utils import timezone

from guests.models import (
    GuestRestaurantDailyOrderFact,
    GuestRestaurantWindowMetrics,
    OrderFact,
    VtelemaxRecipientChannel,
)

ALLOWED_PERIOD_DAYS = (7, 14, 30, 60)
DEFAULT_PERIOD_DAYS = 30

NEW_IN_VENUE_SEGMENT_CODE = "new_in_venue"
ACTIVE_30D_SEGMENT_CODE = "active_30d"
SINGLE_VISIT_30D_SEGMENT_CODE = "single_visit_30d"
COOLING_30_60D_SEGMENT_CODE = "cooling_30_60d"
LOST_60D_PLUS_SEGMENT_CODE = "lost_60d_plus"
BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE = "bot_active_no_visits_180d"

SEGMENT_DEFINITIONS = (
    {"code": NEW_IN_VENUE_SEGMENT_CODE, "name": "Новые за день", "color": "#0891b2"},
    {"code": ACTIVE_30D_SEGMENT_CODE, "name": "Активные 30д", "color": "#0e9f6e"},
    {"code": SINGLE_VISIT_30D_SEGMENT_CODE, "name": "1 визит за 30д", "color": "#2563eb"},
    {"code": COOLING_30_60D_SEGMENT_CODE, "name": "Остывшие 30-60д", "color": "#f59e0b"},
    {"code": LOST_60D_PLUS_SEGMENT_CODE, "name": "Потерянные 60+д", "color": "#ef4444"},
    {
        "code": BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE,
        "name": "Активен в боте, без визитов 180д",
        "color": "#7c3aed",
    },
)
SEGMENT_NAMES_MAP = {item["code"]: item["name"] for item in SEGMENT_DEFINITIONS}
SEGMENT_CODES = tuple(SEGMENT_NAMES_MAP.keys())


def normalize_period_days(raw_value: int | str | None) -> int:
    """
    Нормализует период графика в днях.
    """
    try:
        value = int(raw_value or DEFAULT_PERIOD_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_PERIOD_DAYS
    if value not in ALLOWED_PERIOD_DAYS:
        return DEFAULT_PERIOD_DAYS
    return value


def normalize_segment_code(raw_value: str | None) -> str:
    """
    Нормализует выбранный сегмент. `all` означает режим «все сегменты».
    """
    value = (raw_value or "").strip()
    return value if value in SEGMENT_NAMES_MAP else "all"


def build_segment_dynamics_dashboard_payload(
    *,
    period_days: int | str | None = None,
    department_id: str | None = None,
    segment_code: str | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    """
    Строит payload для страницы «Дашборд -> Динамика сегментов».
    """
    normalized_period_days = normalize_period_days(period_days)
    selected_department_id = (department_id or "").strip()
    selected_segment_code = normalize_segment_code(segment_code)
    target_date_to = _resolve_date_to(
        date_to=date_to,
        selected_department_id=selected_department_id,
    )
    date_from = target_date_to - timedelta(days=normalized_period_days - 1)
    days = _date_range(date_from, target_date_to)

    rows = _build_empty_rows(days)
    _apply_window_segment_counts(rows=rows, selected_department_id=selected_department_id)
    _apply_new_in_venue_counts(rows=rows, selected_department_id=selected_department_id)
    _apply_bot_active_no_visits_counts(rows=rows, selected_department_id=selected_department_id)
    _finalize_rows(rows)

    visible_segments = _build_visible_segment_defs(selected_segment_code)
    kpi_source_code = (
        visible_segments[0]["code"]
        if selected_segment_code != "all" and visible_segments
        else ""
    )
    needs_department_hint = (
        not selected_department_id
        and selected_segment_code in {"all", NEW_IN_VENUE_SEGMENT_CODE}
    )

    return {
        "filters": {
            "date_from": date_from.isoformat(),
            "date_to": target_date_to.isoformat(),
            "period_days": normalized_period_days,
            "period_options": list(ALLOWED_PERIOD_DAYS),
            "department_id": selected_department_id,
            "department_name": _find_department_name(selected_department_id),
            "segment_code": selected_segment_code,
            "segment_name": _find_segment_name(selected_segment_code),
            "departments": _build_department_options(),
            "segments": [
                {"code": "all", "name": "Все сегменты"},
                *[
                    {"code": item["code"], "name": item["name"]}
                    for item in SEGMENT_DEFINITIONS
                ],
            ],
        },
        "is_static_sketch": False,
        "needs_department_hint": needs_department_hint,
        "new_segment_hint": (
            "Для сегмента «Новые» нужно выбрать конкретное заведение. "
            "Без выбранного заведения ряд «Новые за день» показан нулями."
        ),
        "segments": visible_segments,
        "kpis": _build_kpis(rows=rows, segment_code=kpi_source_code),
        "rows": rows,
    }


def _resolve_date_to(*, date_to: date | None, selected_department_id: str) -> date:
    """
    Возвращает последнюю дату графика.

    Если оконные метрики отстают от вчерашнего дня, открываем дашборд на
    последней доступной дате метрик, чтобы пользователь сразу видел реальные ряды.
    """
    closed_day = date_to or (timezone.localdate() - timedelta(days=1))
    metrics_qs = GuestRestaurantWindowMetrics.objects.filter(as_of_date__lte=closed_day)
    if selected_department_id:
        metrics_qs = metrics_qs.filter(department_id=selected_department_id)
    metrics_date = metrics_qs.aggregate(max_date=Max("as_of_date")).get("max_date")
    return metrics_date or closed_day


def _build_empty_rows(days: list[date]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for day in days:
        row = {
            "day": day.isoformat(),
            "day_label": day.strftime("%d.%m"),
            "has_window_metrics": False,
        }
        for code in SEGMENT_CODES:
            row[code] = 0
            row[f"{code}_delta"] = 0
        row["technical_total"] = 0
        rows.append(row)
    return rows


def _apply_window_segment_counts(
    *,
    rows: list[dict[str, Any]],
    selected_department_id: str,
) -> None:
    if not rows:
        return

    date_from = date.fromisoformat(rows[0]["day"])
    date_to = date.fromisoformat(rows[-1]["day"])
    query = GuestRestaurantWindowMetrics.objects.filter(
        as_of_date__gte=date_from,
        as_of_date__lte=date_to,
        window_days__in=(30, 60, 180),
    )
    if selected_department_id:
        query = query.filter(department_id=selected_department_id)

    state: dict[tuple[date, int, str], dict[int, int]] = {}
    dates_with_metrics: set[date] = set()
    for item in query.values("as_of_date", "guest_id", "department_id", "window_days", "visits_count"):
        current_date = item["as_of_date"]
        dates_with_metrics.add(current_date)
        key = (
            current_date,
            int(item["guest_id"]),
            (item.get("department_id") or "").strip(),
        )
        window_map = state.setdefault(key, {})
        window_map[int(item["window_days"])] = int(item["visits_count"] or 0)

    row_by_day = {date.fromisoformat(row["day"]): row for row in rows}
    for metric_date in dates_with_metrics:
        if metric_date in row_by_day:
            row_by_day[metric_date]["has_window_metrics"] = True

    for (metric_date, _guest_id, _department_id), window_map in state.items():
        row = row_by_day.get(metric_date)
        if row is None:
            continue

        visits_30 = int(window_map.get(30, 0))
        visits_60 = int(window_map.get(60, 0))
        visits_180 = int(window_map.get(180, 0))

        if visits_30 >= 2:
            row[ACTIVE_30D_SEGMENT_CODE] += 1
            continue
        if visits_30 == 1:
            row[SINGLE_VISIT_30D_SEGMENT_CODE] += 1
            continue
        if visits_30 == 0 and visits_60 > 0:
            row[COOLING_30_60D_SEGMENT_CODE] += 1
            continue
        if visits_60 == 0 and visits_180 > 0:
            row[LOST_60D_PLUS_SEGMENT_CODE] += 1


def _apply_new_in_venue_counts(
    *,
    rows: list[dict[str, Any]],
    selected_department_id: str,
) -> None:
    if not rows or not selected_department_id:
        return

    date_from = date.fromisoformat(rows[0]["day"])
    date_to = date.fromisoformat(rows[-1]["day"])
    first_purchase_rows = (
        GuestRestaurantDailyOrderFact.objects.filter(
            department_id=selected_department_id,
            orders_count__gt=0,
        )
        .values("guest_id")
        .annotate(first_purchase_date=Min("business_date"))
    )

    row_by_day = {date.fromisoformat(row["day"]): row for row in rows}
    for item in first_purchase_rows:
        first_purchase_date = item["first_purchase_date"]
        if first_purchase_date < date_from or first_purchase_date > date_to:
            continue
        row = row_by_day.get(first_purchase_date)
        if row is not None:
            row[NEW_IN_VENUE_SEGMENT_CODE] += 1


def _apply_bot_active_no_visits_counts(
    *,
    rows: list[dict[str, Any]],
    selected_department_id: str,
) -> None:
    if not rows:
        return

    candidate_scope = (
        VtelemaxRecipientChannel.objects.filter(
            guest__isnull=False,
            is_registered=True,
            notifications_allowed=True,
        )
        .exclude(external_id__isnull=True)
        .exclude(external_id="")
        .exclude(phone_e164__isnull=True)
        .exclude(phone_e164="")
    )
    if not candidate_scope.exists():
        return

    for row in rows:
        as_of_date = date.fromisoformat(row["day"])
        range_start = as_of_date - timedelta(days=179)
        recent_visits_scope = OrderFact.objects.filter(
            guest_id=OuterRef("guest_id"),
            business_date__gte=range_start,
            business_date__lte=as_of_date,
        )
        if selected_department_id:
            recent_visits_scope = recent_visits_scope.filter(department_id=selected_department_id)

        row[BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE] = (
            candidate_scope.annotate(has_recent_visit=Exists(recent_visits_scope))
            .filter(has_recent_visit=False)
            .values("guest_id")
            .distinct()
            .count()
        )


def _finalize_rows(rows: list[dict[str, Any]]) -> None:
    previous_values: dict[str, int] = {}
    for row in rows:
        technical_total = 0
        for code in SEGMENT_CODES:
            value = int(row.get(code) or 0)
            row[code] = value
            row[f"{code}_delta"] = value - int(previous_values.get(code, value))
            previous_values[code] = value
            technical_total += value
        row["technical_total"] = technical_total


def _build_visible_segment_defs(selected_segment_code: str) -> list[dict[str, str]]:
    if selected_segment_code == "all":
        return [dict(item) for item in SEGMENT_DEFINITIONS]
    return [
        dict(item)
        for item in SEGMENT_DEFINITIONS
        if item["code"] == selected_segment_code
    ]


def _build_kpis(
    *,
    rows: list[dict[str, Any]],
    segment_code: str,
) -> list[dict[str, str]]:
    if not rows:
        return []

    first = rows[0]
    current = rows[-1]
    previous = rows[-2] if len(rows) > 1 else current
    week_ago = rows[-8] if len(rows) >= 8 else first

    def value(row: dict[str, Any]) -> int:
        if segment_code:
            return int(row.get(segment_code) or 0)
        return int(row.get("technical_total") or 0)

    current_value = value(current)
    first_value = value(first)
    delta = current_value - first_value
    target_title = _find_segment_name(segment_code) if segment_code else "Сумма рядов на графике"
    return [
        {
            "title": "На конец периода",
            "value": _format_int(current_value),
            "description": target_title,
        },
        {
            "title": "Предыдущий день",
            "value": _format_int(value(previous)),
            "description": previous["day"],
        },
        {
            "title": "7 дней назад",
            "value": _format_int(value(week_ago)),
            "description": week_ago["day"],
        },
        {
            "title": "Изменение за период",
            "value": _format_signed_int(delta),
            "description": f"{first['day']} - {current['day']}",
        },
    ]


def _build_department_options() -> list[dict[str, str]]:
    rows = (
        OrderFact.objects.exclude(department_id="")
        .values("department_id")
        .annotate(department_name=Max("department_name"))
        .order_by("department_name", "department_id")
    )
    result: list[dict[str, str]] = []
    for row in rows:
        dep_id = (row.get("department_id") or "").strip()
        if not dep_id:
            continue
        dep_name = (row.get("department_name") or "").strip() or dep_id
        result.append({"id": dep_id, "name": dep_name})
    return result


def _load_department_names() -> dict[str, str]:
    return {item["id"]: item["name"] for item in _build_department_options()}


def _find_department_name(department_id: str) -> str:
    if not department_id:
        return "Все заведения"
    return _load_department_names().get(department_id, department_id)


def _find_segment_name(segment_code: str) -> str:
    if segment_code == "all":
        return "Все сегменты"
    return SEGMENT_NAMES_MAP.get(segment_code, "Все сегменты")


def _date_range(date_from: date, date_to: date) -> list[date]:
    current = date_from
    result: list[date] = []
    while current <= date_to:
        result.append(current)
        current += timedelta(days=1)
    return result


def _format_int(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def _format_signed_int(value: int) -> str:
    if value > 0:
        return f"+{_format_int(value)}"
    if value < 0:
        return f"-{_format_int(abs(value))}"
    return "0"
