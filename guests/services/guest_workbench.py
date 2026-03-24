"""
Сервис сборки данных для нового экрана «Гости (workbench)».

Экран ориентирован на маркетолога:
1. сегменты активности гостей по окнам 30/60/180 дней;
2. рейтинг и антирейтинг гостей;
3. сравнение заведений (конкуренция внутри сети);
4. визуализация базы гостей для быстрого анализа.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import Avg, Count, Max, Sum
from django.db.models.functions import Coalesce

from guests.models import GuestRestaurantWindowMetrics, OrderFact

WINDOW_OPTIONS = (7, 14, 30, 60, 180)
DEFAULT_WINDOW_DAYS = 30


def normalize_window_days(raw_value: int | str | None) -> int:
    """
    Нормализует размер окна в днях для витрины гостей.
    """
    try:
        value = int(raw_value or DEFAULT_WINDOW_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS
    if value not in WINDOW_OPTIONS:
        return DEFAULT_WINDOW_DAYS
    return value


def build_guest_workbench_payload(
    *,
    as_of_date: date | None = None,
    window_days: int | str | None = None,
    department_id: str | None = None,
) -> dict[str, Any]:
    """
    Формирует payload для страницы `guests/workbench`.
    """
    selected_window_days = normalize_window_days(window_days)
    selected_department_id = (department_id or "").strip()

    target_as_of = as_of_date
    if target_as_of is None:
        target_as_of = (
            GuestRestaurantWindowMetrics.objects.aggregate(v=Max("as_of_date")).get("v")
        )

    if target_as_of is None:
        return _build_empty_payload(
            as_of_date=None,
            selected_window_days=selected_window_days,
            selected_department_id=selected_department_id,
        )

    base_scope = GuestRestaurantWindowMetrics.objects.filter(as_of_date=target_as_of)
    if selected_department_id:
        base_scope = base_scope.filter(department_id=selected_department_id)

    work_scope = base_scope.filter(window_days=selected_window_days).select_related("guest")

    cards_agg = work_scope.aggregate(
        guests_total=Count("guest_id", distinct=True),
        orders_total=Coalesce(Sum("orders_count"), 0),
        visits_total=Coalesce(Sum("visits_count"), 0),
        net_total=Coalesce(Sum("sum_net"), Decimal("0")),
        bonus_in_total=Coalesce(Sum("bonus_in_sum"), Decimal("0")),
        bonus_out_total=Coalesce(Sum("bonus_out_sum"), Decimal("0")),
        avg_rating=Coalesce(Avg("rating_score"), Decimal("0")),
    )

    top_rating_rows = list(work_scope.order_by("-rating_score", "-sum_net", "guest_id")[:20])
    anti_rating_rows = list(
        work_scope.filter(orders_count__gt=0).order_by("rating_score", "sum_net", "guest_id")[:20]
    )

    department_rows = list(
        base_scope.filter(window_days=selected_window_days)
        .values("department_id")
        .annotate(
            guests_count=Count("guest_id", distinct=True),
            net_total=Coalesce(Sum("sum_net"), Decimal("0")),
            avg_rating=Coalesce(Avg("rating_score"), Decimal("0")),
            bonus_in_total=Coalesce(Sum("bonus_in_sum"), Decimal("0")),
            bonus_out_total=Coalesce(Sum("bonus_out_sum"), Decimal("0")),
        )
        .order_by("-net_total", "-guests_count", "department_id")
    )

    department_names_map = _load_department_names()
    department_competition = [
        {
            "department_id": row["department_id"] or "",
            "department_name": department_names_map.get(row["department_id"] or "", row["department_id"] or "—"),
            "guests_count": int(row["guests_count"] or 0),
            "net_total": _to_money_str(row["net_total"]),
            "avg_rating": _to_decimal_str(row["avg_rating"]),
            "bonus_in_total": _to_money_str(row["bonus_in_total"]),
            "bonus_out_total": _to_money_str(row["bonus_out_total"]),
        }
        for row in department_rows
    ]

    segmentation = _build_segmentation(base_scope)
    scatter_points = _build_scatter_points(work_scope)

    return {
        "filters": {
            "as_of_date": target_as_of.isoformat(),
            "window_days": selected_window_days,
            "window_options": list(WINDOW_OPTIONS),
            "department_id": selected_department_id,
            "department_options": _build_department_options(),
        },
        "cards": {
            "guests_total": int(cards_agg["guests_total"] or 0),
            "orders_total": int(cards_agg["orders_total"] or 0),
            "visits_total": int(cards_agg["visits_total"] or 0),
            "net_total": _to_money_str(cards_agg["net_total"]),
            "bonus_in_total": _to_money_str(cards_agg["bonus_in_total"]),
            "bonus_out_total": _to_money_str(cards_agg["bonus_out_total"]),
            "avg_rating": _to_decimal_str(cards_agg["avg_rating"]),
        },
        "segments": segmentation,
        "top_rating": [_serialize_metric_row(row) for row in top_rating_rows],
        "anti_rating": [_serialize_metric_row(row) for row in anti_rating_rows],
        "department_competition": department_competition,
        "visualization": {
            "scatter_points": scatter_points,
        },
    }


def _build_empty_payload(
    *,
    as_of_date: date | None,
    selected_window_days: int,
    selected_department_id: str,
) -> dict[str, Any]:
    """
    Строит пустой payload, если оконных данных еще нет.
    """
    return {
        "filters": {
            "as_of_date": as_of_date.isoformat() if as_of_date else "",
            "window_days": selected_window_days,
            "window_options": list(WINDOW_OPTIONS),
            "department_id": selected_department_id,
            "department_options": _build_department_options(),
        },
        "cards": {
            "guests_total": 0,
            "orders_total": 0,
            "visits_total": 0,
            "net_total": "0.00",
            "bonus_in_total": "0.00",
            "bonus_out_total": "0.00",
            "avg_rating": "0.00",
        },
        "segments": {
            "active_30d": 0,
            "single_visit_30d": 0,
            "cooling_30_60d": 0,
            "lost_60d_plus": 0,
        },
        "top_rating": [],
        "anti_rating": [],
        "department_competition": [],
        "visualization": {"scatter_points": []},
    }


def _build_department_options() -> list[dict[str, str]]:
    """
    Формирует список заведений для фильтра.
    """
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
    """
    Подгружает словарь названий заведений по Department.Id.
    """
    rows = (
        OrderFact.objects.exclude(department_id="")
        .values("department_id")
        .annotate(department_name=Max("department_name"))
    )
    result: dict[str, str] = {}
    for row in rows:
        dep_id = (row.get("department_id") or "").strip()
        if not dep_id:
            continue
        result[dep_id] = (row.get("department_name") or "").strip() or dep_id
    return result


def _build_segmentation(scope_qs) -> dict[str, int]:
    """
    Считает сегменты активности по окнам 30/60/180 дней.

    Логика:
    1. active_30d: visits_30 >= 2;
    2. single_visit_30d: visits_30 == 1;
    3. cooling_30_60d: visits_30 == 0 и visits_60 > 0;
    4. lost_60d_plus: visits_60 == 0 и visits_180 > 0.
    """
    rows = (
        scope_qs.filter(window_days__in=[30, 60, 180])
        .values("guest_id", "department_id", "window_days", "visits_count")
    )

    state: dict[tuple[int, str], dict[int, int]] = {}
    for row in rows:
        guest_id = int(row["guest_id"])
        department_id = (row.get("department_id") or "").strip()
        window = int(row["window_days"])
        visits = int(row["visits_count"] or 0)
        key = (guest_id, department_id)
        window_map = state.setdefault(key, {})
        window_map[window] = visits

    active_30d = 0
    single_visit_30d = 0
    cooling_30_60d = 0
    lost_60d_plus = 0

    for window_map in state.values():
        visits_30 = int(window_map.get(30, 0))
        visits_60 = int(window_map.get(60, 0))
        visits_180 = int(window_map.get(180, 0))

        if visits_30 >= 2:
            active_30d += 1
        if visits_30 == 1:
            single_visit_30d += 1
        if visits_30 == 0 and visits_60 > 0:
            cooling_30_60d += 1
        if visits_60 == 0 and visits_180 > 0:
            lost_60d_plus += 1

    return {
        "active_30d": active_30d,
        "single_visit_30d": single_visit_30d,
        "cooling_30_60d": cooling_30_60d,
        "lost_60d_plus": lost_60d_plus,
    }


def _build_scatter_points(scope_qs) -> list[dict[str, Any]]:
    """
    Готовит точки для диаграммы «частота × средний чек».
    """
    points: list[dict[str, Any]] = []
    for row in scope_qs.order_by("-rating_score", "-sum_net")[:500]:
        points.append(
            {
                "guest_id": int(row.guest_id),
                "phone": (row.guest.phone or "").strip(),
                "visits_count": int(row.visits_count or 0),
                "avg_check_net": float(row.avg_check_net or 0),
                "sum_net": float(row.sum_net or 0),
                "rating_score": float(row.rating_score or 0),
            }
        )
    return points


def _serialize_metric_row(row: GuestRestaurantWindowMetrics) -> dict[str, Any]:
    """
    Сериализует строку оконной метрики для UI-таблиц.
    """
    return {
        "guest_id": int(row.guest_id),
        "phone": (row.guest.phone or "").strip(),
        "first_name": (row.guest.first_name or "").strip(),
        "last_name": (row.guest.last_name or "").strip(),
        "department_id": (row.department_id or "").strip(),
        "orders_count": int(row.orders_count or 0),
        "visits_count": int(row.visits_count or 0),
        "sum_net": _to_money_str(row.sum_net),
        "avg_check_net": _to_money_str(row.avg_check_net),
        "bonus_in_sum": _to_money_str(row.bonus_in_sum),
        "bonus_out_sum": _to_money_str(row.bonus_out_sum),
        "rating_score": _to_decimal_str(row.rating_score),
        "last_visit_at": row.last_visit_at.isoformat() if row.last_visit_at else "",
    }


def _to_money_str(value: Any) -> str:
    """
    Приводит значение суммы к строке с 2 знаками после запятой.
    """
    if value is None:
        return "0.00"
    return f"{Decimal(str(value)):.2f}"


def _to_decimal_str(value: Any) -> str:
    """
    Приводит десятичное значение к строке с 2 знаками после запятой.
    """
    if value is None:
        return "0.00"
    return f"{Decimal(str(value)):.2f}"

