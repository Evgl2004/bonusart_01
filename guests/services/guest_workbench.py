"""
Сервис сборки данных для нового экрана «Гости (workbench)».

Экран ориентирован на маркетолога:
1. сегменты активности гостей по окнам 30/60/180 дней;
2. рейтинг и антирейтинг гостей;
3. сравнение заведений (конкуренция внутри сети);
4. визуализация базы гостей для быстрого анализа.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Avg, Count, Max, Sum
from django.db.models.functions import Coalesce

from guests.models import (
    FocusCategory,
    GuestRestaurantDailyCategoryFact,
    GuestWorkbenchFilterPreset,
    GuestRestaurantWindowMetrics,
    OrderFact,
)

WINDOW_OPTIONS = (7, 14, 30, 60, 180)
DEFAULT_WINDOW_DAYS = 30
SEGMENT_DEFINITIONS = (
    ("active_30d", "Активные 30д (2+ визита)"),
    ("single_visit_30d", "1 визит за 30д"),
    ("cooling_30_60d", "Остывшие 30-60д"),
    ("lost_60d_plus", "Потерянные 60+д"),
)
SEGMENT_NAMES_MAP = dict(SEGMENT_DEFINITIONS)
SELECTED_GUESTS_LIMIT = 200


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


def normalize_segment_code(raw_value: str | None) -> str:
    """
    Нормализует код сегмента из UI-фильтра.
    """
    value = (raw_value or "").strip()
    return value if value in SEGMENT_NAMES_MAP else ""


def build_guest_workbench_payload(
    *,
    as_of_date: date | None = None,
    window_days: int | str | None = None,
    department_id: str | None = None,
    segment_code: str | None = None,
    focus_category_code: str | None = None,
    show_all_presets: bool = False,
) -> dict[str, Any]:
    """
    Формирует payload для страницы `guests/workbench`.
    """
    selected_window_days = normalize_window_days(window_days)
    selected_department_id = (department_id or "").strip()
    selected_segment_code = normalize_segment_code(segment_code)
    selected_focus_category_code_raw = (focus_category_code or "").strip()
    saved_presets = _build_saved_presets(show_all_presets=show_all_presets)

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
            selected_segment_code=selected_segment_code,
            selected_focus_category_code=selected_focus_category_code_raw,
            show_all_presets=show_all_presets,
            saved_presets=saved_presets,
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
            "net_total": _to_money_ui(row["net_total"]),
            "avg_rating": _to_decimal_str(row["avg_rating"]),
            "bonus_in_total": _to_money_str(row["bonus_in_total"]),
            "bonus_out_total": _to_money_str(row["bonus_out_total"]),
        }
        for row in department_rows
    ]

    segmentation, segment_by_key = _build_segmentation_state(base_scope)
    segment_focus_matrix, focus_guest_keys_by_code = _build_segment_focus_matrix(
        as_of_date=target_as_of,
        selected_window_days=selected_window_days,
        selected_department_id=selected_department_id,
        segment_by_key=segment_by_key,
        segment_totals=segmentation,
    )
    focus_category_options = [
        {
            "code": (col.get("focus_category_code") or "").strip(),
            "name": (col.get("focus_category_name") or "").strip(),
        }
        for col in segment_focus_matrix.get("columns", [])
        if (col.get("focus_category_code") or "").strip()
    ]
    focus_codes = {item["code"] for item in focus_category_options}
    selected_focus_category_code = (
        selected_focus_category_code_raw if selected_focus_category_code_raw in focus_codes else ""
    )
    selected_guests = _build_selected_guests_rows(
        base_scope=base_scope,
        selected_window_days=selected_window_days,
        segment_by_key=segment_by_key,
        focus_guest_keys_by_code=focus_guest_keys_by_code,
        segment_code=selected_segment_code,
        focus_category_code=selected_focus_category_code,
        limit=SELECTED_GUESTS_LIMIT,
    )
    scatter_points = _build_scatter_points(work_scope)

    return {
        "filters": {
            "as_of_date": target_as_of.isoformat(),
            "window_days": selected_window_days,
            "window_options": list(WINDOW_OPTIONS),
            "department_id": selected_department_id,
            "department_options": _build_department_options(),
            "segment_code": selected_segment_code,
            "segment_options": _build_segment_options(),
            "focus_category_code": selected_focus_category_code,
            "focus_category_options": focus_category_options,
            "show_all_presets": show_all_presets,
            "saved_presets": saved_presets,
        },
        "cards": {
            "guests_total": int(cards_agg["guests_total"] or 0),
            "orders_total": int(cards_agg["orders_total"] or 0),
            "visits_total": int(cards_agg["visits_total"] or 0),
            "net_total": _to_money_ui(cards_agg["net_total"]),
            "bonus_in_total": _to_money_str(cards_agg["bonus_in_total"]),
            "bonus_out_total": _to_money_str(cards_agg["bonus_out_total"]),
            "avg_rating": _to_decimal_str(cards_agg["avg_rating"]),
        },
        "segments": segmentation,
        "segment_focus_matrix": segment_focus_matrix,
        "top_rating": [_serialize_metric_row(row) for row in top_rating_rows],
        "anti_rating": [_serialize_metric_row(row) for row in anti_rating_rows],
        "selected_guests": selected_guests,
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
    selected_segment_code: str,
    selected_focus_category_code: str,
    show_all_presets: bool,
    saved_presets: list[dict[str, Any]],
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
            "segment_code": selected_segment_code,
            "segment_options": _build_segment_options(),
            "focus_category_code": selected_focus_category_code,
            "focus_category_options": [],
            "show_all_presets": show_all_presets,
            "saved_presets": saved_presets,
        },
        "cards": {
            "guests_total": 0,
            "orders_total": 0,
            "visits_total": 0,
            "net_total": "0,00",
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
        "segment_focus_matrix": {
            "rows": [
                {
                    "segment_code": code,
                    "segment_name": name,
                    "guests_total": 0,
                    "cells": [],
                }
                for code, name in SEGMENT_DEFINITIONS
            ],
            "columns": [],
            "heatmap": {"max_value": 0, "items": []},
        },
        "top_rating": [],
        "anti_rating": [],
        "selected_guests": {
            "total": 0,
            "limit": SELECTED_GUESTS_LIMIT,
            "is_truncated": False,
            "rows": [],
        },
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


def _build_segment_options() -> list[dict[str, str]]:
    """
    Формирует справочник сегментов для фильтра на экране workbench.
    """
    return [{"code": code, "name": name} for code, name in SEGMENT_DEFINITIONS]


def _build_saved_presets(*, show_all_presets: bool = False) -> list[dict[str, Any]]:
    """
    Возвращает пресеты фильтров для экрана workbench.

    По умолчанию отображаются только активные пресеты.
    В режиме show_all_presets=True возвращаются и деактивированные.
    """
    focus_name_map = {
        (row.get("code") or "").strip(): (row.get("name") or "").strip()
        for row in FocusCategory.objects.filter(is_enabled=True).values("code", "name")
    }
    department_name_map = _load_department_names()

    presets_qs = GuestWorkbenchFilterPreset.objects.all()
    if not show_all_presets:
        presets_qs = presets_qs.filter(is_active=True)

    rows = presets_qs.order_by("-is_active", "-updated_at", "name").values(
        "id",
        "name",
        "description",
        "window_days",
        "department_id",
        "segment_code",
        "focus_category_code",
        "is_active",
        "updated_at",
    )

    result: list[dict[str, Any]] = []
    for row in rows:
        department_id = (row.get("department_id") or "").strip()
        segment_code = (row.get("segment_code") or "").strip()
        focus_code = (row.get("focus_category_code") or "").strip()

        result.append(
            {
                "id": int(row["id"]),
                "name": (row.get("name") or "").strip(),
                "description": (row.get("description") or "").strip(),
                "window_days": int(row.get("window_days") or DEFAULT_WINDOW_DAYS),
                "department_id": department_id,
                "department_name": department_name_map.get(department_id, department_id) if department_id else "Все заведения",
                "segment_code": segment_code,
                "segment_name": SEGMENT_NAMES_MAP.get(segment_code, "Все сегменты") if segment_code else "Все сегменты",
                "focus_category_code": focus_code,
                "focus_category_name": focus_name_map.get(focus_code, focus_code) if focus_code else "Все категории",
                "is_active": bool(row.get("is_active")),
                "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else "",
            }
        )

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


def _build_segmentation_state(scope_qs) -> tuple[dict[str, int], dict[tuple[int, str], str]]:
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

    segment_totals = {code: 0 for code, _ in SEGMENT_DEFINITIONS}
    segment_by_key: dict[tuple[int, str], str] = {}

    for key, window_map in state.items():
        visits_30 = int(window_map.get(30, 0))
        visits_60 = int(window_map.get(60, 0))
        visits_180 = int(window_map.get(180, 0))

        if visits_30 >= 2:
            segment_by_key[key] = "active_30d"
            segment_totals["active_30d"] += 1
            continue
        if visits_30 == 1:
            segment_by_key[key] = "single_visit_30d"
            segment_totals["single_visit_30d"] += 1
            continue
        if visits_30 == 0 and visits_60 > 0:
            segment_by_key[key] = "cooling_30_60d"
            segment_totals["cooling_30_60d"] += 1
            continue
        if visits_60 == 0 and visits_180 > 0:
            segment_by_key[key] = "lost_60d_plus"
            segment_totals["lost_60d_plus"] += 1

    return segment_totals, segment_by_key


def _build_segment_focus_matrix(
    *,
    as_of_date: date,
    selected_window_days: int,
    selected_department_id: str,
    segment_by_key: dict[tuple[int, str], str],
    segment_totals: dict[str, int],
) -> tuple[dict[str, Any], dict[str, set[tuple[int, str]]]]:
    """
    Формирует матрицу «Сегменты × фокусные категории» для наглядного покрытия.

    Для каждой ячейки считаются:
    1. количество гостей;
    2. доля внутри сегмента;
    3. доля внутри выбранной фокусной категории.
    """
    focus_rows = list(
        FocusCategory.objects.filter(is_enabled=True)
        .values("id", "code", "name")
        .order_by("name", "id")
    )
    if not focus_rows:
        return (
            {
                "rows": [
                    {
                        "segment_code": code,
                        "segment_name": name,
                        "guests_total": int(segment_totals.get(code, 0)),
                        "cells": [],
                    }
                    for code, name in SEGMENT_DEFINITIONS
                ],
                "columns": [],
                "heatmap": {"max_value": 0, "items": []},
            },
            {},
        )

    focus_ids = [int(row["id"]) for row in focus_rows]
    range_start = as_of_date - timedelta(days=max(selected_window_days, 1) - 1)

    daily_scope = GuestRestaurantDailyCategoryFact.objects.filter(
        business_date__gte=range_start,
        business_date__lte=as_of_date,
        focus_category_id__in=focus_ids,
    )
    if selected_department_id:
        daily_scope = daily_scope.filter(department_id=selected_department_id)

    cell_sets: dict[tuple[str, int], set[tuple[int, str]]] = defaultdict(set)
    category_sets: dict[int, set[tuple[int, str]]] = defaultdict(set)

    for row in daily_scope.values("guest_id", "department_id", "focus_category_id"):
        guest_id = int(row["guest_id"])
        department_id = _normalize_department_id(row.get("department_id"))
        segment_code = segment_by_key.get((guest_id, department_id))
        if not segment_code:
            continue
        focus_id = int(row["focus_category_id"])
        guest_key = (guest_id, department_id)
        cell_sets[(segment_code, focus_id)].add(guest_key)
        category_sets[focus_id].add(guest_key)

    columns: list[dict[str, Any]] = []
    focus_code_by_id: dict[int, str] = {}
    for row in focus_rows:
        focus_id = int(row["id"])
        focus_code = (row.get("code") or "").strip()
        focus_code_by_id[focus_id] = focus_code
        columns.append(
            {
                "focus_category_id": focus_id,
                "focus_category_code": focus_code,
                "focus_category_name": (row.get("name") or "").strip() or f"Категория {focus_id}",
                "guests_total": len(category_sets.get(focus_id, set())),
            }
        )

    rows: list[dict[str, Any]] = []
    heatmap_items: list[dict[str, Any]] = []
    max_value = 0

    for row_idx, (segment_code, segment_name) in enumerate(SEGMENT_DEFINITIONS):
        segment_total = int(segment_totals.get(segment_code, 0))
        cells: list[dict[str, Any]] = []
        for col_idx, col in enumerate(columns):
            category_total = int(col["guests_total"])
            guests_count = len(cell_sets.get((segment_code, int(col["focus_category_id"])), set()))
            max_value = max(max_value, guests_count)
            cells.append(
                {
                    "guests_count": guests_count,
                    "share_of_segment_pct": _to_percent(guests_count, segment_total),
                    "share_of_category_pct": _to_percent(guests_count, category_total),
                    "share_of_segment": _to_percent_str(guests_count, segment_total),
                    "share_of_category": _to_percent_str(guests_count, category_total),
                    "segment_code": segment_code,
                    "focus_category_id": int(col["focus_category_id"]),
                    "focus_category_code": (col.get("focus_category_code") or "").strip(),
                }
            )
            heatmap_items.append(
                {
                    "x": col_idx,
                    "y": row_idx,
                    "value": guests_count,
                    "segment_code": segment_code,
                    "focus_category_id": int(col["focus_category_id"]),
                    "focus_category_code": (col.get("focus_category_code") or "").strip(),
                }
            )

        rows.append(
            {
                "segment_code": segment_code,
                "segment_name": segment_name,
                "guests_total": segment_total,
                "cells": cells,
            }
        )

    focus_guest_keys_by_code: dict[str, set[tuple[int, str]]] = {}
    for focus_id, guest_keys in category_sets.items():
        code = focus_code_by_id.get(focus_id, "")
        if code:
            focus_guest_keys_by_code[code] = guest_keys

    return (
        {
            "rows": rows,
            "columns": columns,
            "heatmap": {"max_value": max_value, "items": heatmap_items},
        },
        focus_guest_keys_by_code,
    )


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


def _build_selected_guests_rows(
    *,
    base_scope,
    selected_window_days: int,
    segment_by_key: dict[tuple[int, str], str],
    focus_guest_keys_by_code: dict[str, set[tuple[int, str]]],
    segment_code: str,
    focus_category_code: str,
    limit: int,
) -> dict[str, Any]:
    """
    Строит список гостей для выбранных фильтров сегмента и фокусной категории.
    """
    rows: list[dict[str, Any]] = []
    total = 0
    focus_keys = focus_guest_keys_by_code.get(focus_category_code, set()) if focus_category_code else None

    # Для «остывших/потерянных» гостей строки в выбранном окне может не быть.
    # Берем представителя гостя с fallback по окнам:
    # выбранное окно -> 180 -> 60 -> 30 -> 14 -> 7.
    preferred_windows = [selected_window_days] + [180, 60, 30, 14, 7]
    unique_windows: list[int] = []
    for window in preferred_windows:
        if window not in unique_windows:
            unique_windows.append(window)
    window_rank = {window: idx for idx, window in enumerate(unique_windows)}
    default_rank = len(unique_windows) + 1

    representative_by_key: dict[tuple[int, str], GuestRestaurantWindowMetrics] = {}
    for row in base_scope.select_related("guest"):
        key = (int(row.guest_id), _normalize_department_id(row.department_id))
        current = representative_by_key.get(key)
        row_rank = window_rank.get(int(row.window_days), default_rank)
        if current is None:
            representative_by_key[key] = row
            continue
        current_rank = window_rank.get(int(current.window_days), default_rank)
        if row_rank < current_rank:
            representative_by_key[key] = row

    representative_rows = list(representative_by_key.values())
    representative_rows.sort(
        key=lambda item: (
            -(float(item.rating_score or 0)),
            -(float(item.sum_net or 0)),
            int(item.guest_id),
        )
    )

    for row in representative_rows:
        key = (int(row.guest_id), _normalize_department_id(row.department_id))
        row_segment_code = segment_by_key.get(key, "")

        if not row_segment_code:
            continue
        if segment_code and row_segment_code != segment_code:
            continue
        if focus_keys is not None and key not in focus_keys:
            continue

        total += 1
        if len(rows) < limit:
            item = _serialize_metric_row(row)
            item["segment_code"] = row_segment_code
            item["segment_name"] = SEGMENT_NAMES_MAP.get(row_segment_code, "Вне сегмента")
            item["source_window_days"] = int(row.window_days or 0)
            rows.append(item)

    return {
        "total": total,
        "limit": limit,
        "is_truncated": total > limit,
        "rows": rows,
    }


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
        "sum_net": _to_money_ui(row.sum_net),
        "avg_check_net": _to_money_ui(row.avg_check_net),
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


def _to_money_ui(value: Any) -> str:
    """
    Форматирует сумму для UI-таблиц: `1 234 567,89` (без знака валюты).
    """
    if value is None:
        return "0,00"
    normalized = Decimal(str(value))
    integer_part, fractional_part = f"{normalized:.2f}".split(".")
    integer_with_spaces = f"{int(integer_part):,}".replace(",", " ")
    return f"{integer_with_spaces},{fractional_part}"


def _to_decimal_str(value: Any) -> str:
    """
    Приводит десятичное значение к строке с 2 знаками после запятой.
    """
    if value is None:
        return "0.00"
    return f"{Decimal(str(value)):.2f}"


def _to_percent(value: int, base: int) -> float:
    """
    Считает долю в процентах для визуализации матрицы покрытий.
    """
    if base <= 0:
        return 0.0
    return round((float(value) * 100.0) / float(base), 1)


def _to_percent_str(value: int, base: int) -> str:
    """
    Представляет долю в процентах строкой с одним знаком после запятой.
    """
    return f"{_to_percent(value, base):.1f}"


def _normalize_department_id(value: Any) -> str:
    """
    Нормализует Department.Id для корректного сравнения ключей сегментации.
    """
    return (value or "").strip()
