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
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.db.models import Exists, Max, OuterRef

from guests.models import (
    FocusCategory,
    Guest,
    GuestRestaurantDailyCategoryFact,
    GuestRestaurantWindowCategoryMetrics,
    GuestWorkbenchFilterPreset,
    GuestRestaurantWindowMetrics,
    OrderFact,
    VtelemaxRecipientChannel,
)

WINDOW_OPTIONS = (7, 14, 30, 60, 180)
DEFAULT_WINDOW_DAYS = 30
BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE = "bot_active_no_visits_180d"
SEGMENT_DEFINITIONS = (
    ("active_30d", "Активные 30д (2+ визита)"),
    ("single_visit_30d", "1 визит за 30д"),
    ("cooling_30_60d", "Остывшие 30-60д"),
    ("lost_60d_plus", "Потерянные 60+д"),
    (BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE, "Активен в боте, без визитов 180д"),
)
SEGMENT_NAMES_MAP = dict(SEGMENT_DEFINITIONS)
SELECTED_GUESTS_LIMIT = 200
COMPLEX_FILTER_MAX_ITEMS = 6
COMPLEX_FILTER_FIELDS = (
    ("orders_count", "Заказов", "orders_count", "int"),
    ("visits_count", "Визитов", "visits_count", "int"),
    ("sum_net", "Сумма (нетто)", "sum_net", "decimal"),
    ("avg_check_net", "Средний чек", "avg_check_net", "decimal"),
    ("rating_score", "Рейтинг", "rating_score", "decimal"),
)
COMPLEX_FILTER_FIELD_META = {
    code: {
        "code": code,
        "name": name,
        "orm_field": orm_field,
        "value_type": value_type,
    }
    for code, name, orm_field, value_type in COMPLEX_FILTER_FIELDS
}
COMPLEX_FILTER_OPERATORS = (
    ("gt", "Больше"),
    ("gte", "Больше или равно"),
    ("lt", "Меньше"),
    ("lte", "Меньше или равно"),
    ("eq", "Равно"),
)
COMPLEX_FILTER_OPERATOR_META = {
    "gt": {"code": "gt", "name": "Больше", "orm_lookup": "gt"},
    "gte": {"code": "gte", "name": "Больше или равно", "orm_lookup": "gte"},
    "lt": {"code": "lt", "name": "Меньше", "orm_lookup": "lt"},
    "lte": {"code": "lte", "name": "Меньше или равно", "orm_lookup": "lte"},
    "eq": {"code": "eq", "name": "Равно", "orm_lookup": "exact"},
}


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


def normalize_complex_filters(raw_filters: list[dict[str, str]] | None) -> list[dict[str, Any]]:
    """
    Нормализует список сложных фильтров вида field/operator/value.

    На выходе остаются только валидные условия (по допустимым полям и операторам)
    с корректно распарсенным числовым значением.
    """
    if not raw_filters:
        return []

    result: list[dict[str, Any]] = []
    for item in raw_filters[:COMPLEX_FILTER_MAX_ITEMS]:
        field_code = (item.get("field") or "").strip()
        operator_code = (item.get("operator") or "").strip()
        raw_value = (item.get("value") or "").strip()

        if not field_code and not operator_code and not raw_value:
            continue

        field_meta = COMPLEX_FILTER_FIELD_META.get(field_code)
        operator_meta = COMPLEX_FILTER_OPERATOR_META.get(operator_code)
        if field_meta is None or operator_meta is None or not raw_value:
            continue

        parsed_value = _parse_numeric_filter_value(raw_value)
        if parsed_value is None:
            continue

        if field_meta["value_type"] == "int":
            parsed_value = Decimal(int(parsed_value))

        result.append(
            {
                "field": field_code,
                "field_name": field_meta["name"],
                "operator": operator_code,
                "operator_name": operator_meta["name"],
                "value": parsed_value,
                "value_str": _serialize_complex_filter_value(parsed_value, field_meta["value_type"]),
            }
        )
    return result


def _parse_numeric_filter_value(raw_value: str) -> Decimal | None:
    """
    Парсит числовое значение фильтра с поддержкой запятой и пробелов.
    """
    normalized = (raw_value or "").strip().replace(" ", "").replace(",", ".")
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except Exception:  # noqa: BLE001
        return None


def _serialize_complex_filter_value(value: Decimal, value_type: str) -> str:
    """
    Преобразует значение фильтра в строку для повторной подстановки в форму.
    """
    if value_type == "int":
        return str(int(value))
    return format(value.normalize(), "f")


def _build_complex_filter_options() -> dict[str, list[dict[str, str]]]:
    """
    Возвращает справочники полей и операторов для UI-конструктора условий.
    """
    return {
        "fields": [
            {"code": code, "name": name}
            for code, name, _, _ in COMPLEX_FILTER_FIELDS
        ],
        "operators": [{"code": code, "name": name} for code, name in COMPLEX_FILTER_OPERATORS],
    }


def _apply_complex_filters(scope_qs, normalized_filters: list[dict[str, Any]]):
    """
    Применяет список сложных условий к queryset оконных метрик (логика И).
    """
    filtered_scope = scope_qs
    for item in normalized_filters:
        field_meta = COMPLEX_FILTER_FIELD_META.get(item["field"])
        operator_meta = COMPLEX_FILTER_OPERATOR_META.get(item["operator"])
        if field_meta is None or operator_meta is None:
            continue

        value = item["value"]
        if field_meta["value_type"] == "int":
            value = int(value)

        lookup = f"{field_meta['orm_field']}__{operator_meta['orm_lookup']}"
        filtered_scope = filtered_scope.filter(**{lookup: value})
    return filtered_scope


def _build_preferred_windows(selected_window_days: int, segment_code: str) -> list[int]:
    """
    Возвращает приоритет окон для выбора репрезентативной строки гостя.

    Логика:
    1. если сегмент не выбран, таблица должна показывать метрики строго в выбранном окне;
    2. если выбран сегмент, окно определяется бизнес-смыслом сегмента:
       1. active_30d / single_visit_30d -> окно 30;
       2. cooling_30_60d -> окно 60 (fallback: 30);
       3. lost_60d_plus -> окно 180 (fallback: 60, 30).
    """
    if segment_code == "active_30d":
        preferred_windows = [30]
    elif segment_code == "single_visit_30d":
        preferred_windows = [30]
    elif segment_code == "cooling_30_60d":
        preferred_windows = [60, 30]
    elif segment_code == "lost_60d_plus":
        preferred_windows = [180, 60, 30]
    elif segment_code == BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE:
        preferred_windows = [180, 60, 30]
    else:
        preferred_windows = [selected_window_days]

    unique_windows: list[int] = []
    for window in preferred_windows:
        if window in WINDOW_OPTIONS and window not in unique_windows:
            unique_windows.append(window)
    return unique_windows or [DEFAULT_WINDOW_DAYS]


def _build_representative_rows(
    *,
    base_scope,
    selected_window_days: int,
    segment_code: str,
    allowed_guest_keys: set[tuple[int, str]] | None = None,
) -> list[Any]:
    """
    Выбирает по одной «лучшей» строке метрик на пару (гость, заведение)
    согласно приоритету окон для текущего сегмента/фильтра.
    """
    unique_windows = _build_preferred_windows(
        selected_window_days=selected_window_days,
        segment_code=segment_code,
    )
    window_rank = {window: idx for idx, window in enumerate(unique_windows)}
    default_rank = len(unique_windows) + 1

    representative_by_key: dict[tuple[int, str], Any] = {}
    for row in base_scope.select_related("guest"):
        key = (int(row.guest_id), _normalize_department_id(row.department_id))
        if allowed_guest_keys is not None and key not in allowed_guest_keys:
            continue
        current = representative_by_key.get(key)
        row_rank = window_rank.get(int(row.window_days), default_rank)
        if row_rank == default_rank:
            continue
        if current is None:
            representative_by_key[key] = row
            continue
        current_rank = window_rank.get(int(current.window_days), default_rank)
        if row_rank < current_rank:
            representative_by_key[key] = row

    return list(representative_by_key.values())


def _row_matches_complex_filters(
    row: Any,
    normalized_filters: list[dict[str, Any]],
) -> bool:
    """
    Проверяет соответствие одной репрезентативной строки всем сложным условиям (логика И).
    """
    for item in normalized_filters:
        field_meta = COMPLEX_FILTER_FIELD_META.get(item["field"])
        if field_meta is None:
            return False

        left_raw = getattr(row, field_meta["orm_field"], None)
        right_raw = item["value"]

        if field_meta["value_type"] == "int":
            left_value = int(left_raw or 0)
            right_value = int(right_raw)
        else:
            left_value = Decimal(str(left_raw or 0))
            right_value = Decimal(str(right_raw))

        operator_code = item.get("operator")
        if operator_code == "gt" and not (left_value > right_value):
            return False
        if operator_code == "gte" and not (left_value >= right_value):
            return False
        if operator_code == "lt" and not (left_value < right_value):
            return False
        if operator_code == "lte" and not (left_value <= right_value):
            return False
        if operator_code == "eq" and not (left_value == right_value):
            return False
    return True


def _collect_allowed_guest_keys_by_complex_filters(
    *,
    base_scope,
    selected_window_days: int,
    segment_code: str,
    normalized_filters: list[dict[str, Any]],
) -> set[tuple[int, str]] | None:
    """
    Собирает ключи (guest_id, department_id), прошедшие сложный фильтр по той же
    репрезентативной строке, которая используется в таблице гостей.
    """
    if not normalized_filters:
        return None

    representative_rows = _build_representative_rows(
        base_scope=base_scope,
        selected_window_days=selected_window_days,
        segment_code=segment_code,
        allowed_guest_keys=None,
    )
    allowed_keys: set[tuple[int, str]] = set()
    for row in representative_rows:
        if _row_matches_complex_filters(row, normalized_filters):
            allowed_keys.add((int(row.guest_id), _normalize_department_id(row.department_id)))
    return allowed_keys


def build_guest_workbench_payload(
    *,
    as_of_date: date | None = None,
    window_days: int | str | None = None,
    department_id: str | None = None,
    segment_code: str | None = None,
    focus_category_code: str | None = None,
    complex_filters: list[dict[str, str]] | None = None,
    show_all_presets: bool = False,
) -> dict[str, Any]:
    """
    Формирует payload для страницы `guests/workbench`.
    """
    selected_window_days = normalize_window_days(window_days)
    selected_department_id = (department_id or "").strip()
    selected_segment_code = normalize_segment_code(segment_code)
    selected_focus_category_code_raw = (focus_category_code or "").strip()
    normalized_complex_filters = normalize_complex_filters(complex_filters)
    complex_filter_options = _build_complex_filter_options()
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
            normalized_complex_filters=normalized_complex_filters,
            complex_filter_options=complex_filter_options,
            show_all_presets=show_all_presets,
            saved_presets=saved_presets,
        )

    base_scope = GuestRestaurantWindowMetrics.objects.filter(as_of_date=target_as_of)
    if selected_department_id:
        base_scope = base_scope.filter(department_id=selected_department_id)

    base_segmentation, base_segment_by_key = _build_segmentation_state(
        base_scope,
        as_of_date=target_as_of,
        selected_department_id=selected_department_id,
        allowed_guest_keys=None,
    )
    base_segment_focus_matrix, base_focus_guest_keys_by_code = _build_segment_focus_matrix(
        as_of_date=target_as_of,
        selected_window_days=selected_window_days,
        selected_department_id=selected_department_id,
        segment_by_key=base_segment_by_key,
        segment_totals=base_segmentation,
        allowed_guest_keys=None,
    )
    initial_focus_options = [
        {
            "code": (col.get("focus_category_code") or "").strip(),
            "name": (col.get("focus_category_name") or "").strip(),
        }
        for col in base_segment_focus_matrix.get("columns", [])
        if (col.get("focus_category_code") or "").strip()
    ]
    initial_focus_codes = {item["code"] for item in initial_focus_options}
    selected_focus_category_code = (
        selected_focus_category_code_raw if selected_focus_category_code_raw in initial_focus_codes else ""
    )
    selected_focus_category_id: int | None = None
    if selected_focus_category_code:
        selected_focus_category_id = (
            FocusCategory.objects.filter(code=selected_focus_category_code, is_enabled=True)
            .values_list("id", flat=True)
            .first()
        )
        if selected_focus_category_id is None:
            selected_focus_category_code = ""

    category_window_enabled = bool(getattr(settings, "WORKBENCH_CATEGORY_WINDOW_METRICS_V2", False))
    use_category_window_metrics = bool(
        category_window_enabled and selected_focus_category_code and selected_focus_category_id
    )
    active_metrics_scope = base_scope
    if use_category_window_metrics and selected_focus_category_id is not None:
        active_metrics_scope = GuestRestaurantWindowCategoryMetrics.objects.filter(
            as_of_date=target_as_of,
            focus_category_id=selected_focus_category_id,
        )
        if selected_department_id:
            active_metrics_scope = active_metrics_scope.filter(department_id=selected_department_id)

    allowed_guest_keys = _collect_allowed_guest_keys_by_complex_filters(
        base_scope=active_metrics_scope,
        selected_window_days=selected_window_days,
        segment_code=selected_segment_code,
        normalized_filters=normalized_complex_filters,
    )

    if not use_category_window_metrics and allowed_guest_keys is None:
        # Оптимизация: для базового режима без сложных условий
        # сегментация и матрица совпадают с уже посчитанными стартовыми данными.
        segmentation = base_segmentation
        segment_by_key = base_segment_by_key
        segment_focus_matrix = base_segment_focus_matrix
        focus_guest_keys_by_code = base_focus_guest_keys_by_code
    else:
        segmentation, segment_by_key = _build_segmentation_state(
            active_metrics_scope,
            as_of_date=target_as_of,
            selected_department_id=selected_department_id,
            allowed_guest_keys=allowed_guest_keys,
        )
        segment_focus_matrix, focus_guest_keys_by_code = _build_segment_focus_matrix(
            as_of_date=target_as_of,
            selected_window_days=selected_window_days,
            selected_department_id=selected_department_id,
            segment_by_key=segment_by_key,
            segment_totals=segmentation,
            allowed_guest_keys=allowed_guest_keys,
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
    if selected_focus_category_code and selected_focus_category_code not in focus_codes:
        selected_focus_name = (
            FocusCategory.objects.filter(id=selected_focus_category_id).values_list("name", flat=True).first()
            if selected_focus_category_id
            else ""
        )
        focus_category_options.append(
            {
                "code": selected_focus_category_code,
                "name": str(selected_focus_name or selected_focus_category_code).strip(),
            }
        )

    selected_guest_rows = _collect_selected_guest_rows(
        base_scope=active_metrics_scope,
        selected_window_days=selected_window_days,
        segment_by_key=segment_by_key,
        focus_guest_keys_by_code=focus_guest_keys_by_code,
        segment_code=selected_segment_code,
        focus_category_code=selected_focus_category_code,
        allowed_guest_keys=allowed_guest_keys,
    )

    selected_guests = _build_selected_guests_rows(
        base_scope=active_metrics_scope,
        selected_window_days=selected_window_days,
        segment_by_key=segment_by_key,
        focus_guest_keys_by_code=focus_guest_keys_by_code,
        segment_code=selected_segment_code,
        focus_category_code=selected_focus_category_code,
        allowed_guest_keys=allowed_guest_keys,
        limit=SELECTED_GUESTS_LIMIT,
        selected_guest_rows=selected_guest_rows,
    )

    cards_orders_total = 0
    cards_visits_total = 0
    cards_net_total = Decimal("0")
    cards_bonus_in_total = Decimal("0")
    cards_bonus_out_total = Decimal("0")
    cards_rating_total = Decimal("0")

    for row, _ in selected_guest_rows:
        cards_orders_total += int(row.orders_count or 0)
        cards_visits_total += int(row.visits_count or 0)
        cards_net_total += Decimal(str(row.sum_net or 0))
        cards_bonus_in_total += Decimal(str(row.bonus_in_sum or 0))
        cards_bonus_out_total += Decimal(str(row.bonus_out_sum or 0))
        cards_rating_total += Decimal(str(row.rating_score or 0))

    cards_guests_total = len(selected_guest_rows)
    cards_avg_rating = (
        (cards_rating_total / Decimal(cards_guests_total))
        if cards_guests_total > 0
        else Decimal("0")
    )

    top_rating_rows = [row for row, _ in selected_guest_rows[:20]]
    anti_rating_rows = sorted(
        [row for row, _ in selected_guest_rows if int(row.orders_count or 0) > 0],
        key=lambda row: (
            float(row.rating_score or 0),
            float(row.sum_net or 0),
            int(row.guest_id),
        ),
    )[:20]

    department_names_map = _load_department_names()
    department_agg: dict[str, dict[str, Any]] = {}
    for row, _ in selected_guest_rows:
        department_id = (row.department_id or "").strip()
        bucket = department_agg.setdefault(
            department_id,
            {
                "guests_count": 0,
                "net_total": Decimal("0"),
                "rating_total": Decimal("0"),
                "bonus_in_total": Decimal("0"),
                "bonus_out_total": Decimal("0"),
            },
        )
        bucket["guests_count"] += 1
        bucket["net_total"] += Decimal(str(row.sum_net or 0))
        bucket["rating_total"] += Decimal(str(row.rating_score or 0))
        bucket["bonus_in_total"] += Decimal(str(row.bonus_in_sum or 0))
        bucket["bonus_out_total"] += Decimal(str(row.bonus_out_sum or 0))

    department_competition = [
        {
            "department_id": department_id,
            "department_name": department_names_map.get(department_id, department_id or "—"),
            "guests_count": int(values["guests_count"] or 0),
            "net_total": _to_money_ui(values["net_total"]),
            "avg_rating": _to_decimal_str(
                (values["rating_total"] / Decimal(values["guests_count"]))
                if int(values["guests_count"] or 0) > 0
                else Decimal("0")
            ),
            "bonus_in_total": _to_money_str(values["bonus_in_total"]),
            "bonus_out_total": _to_money_str(values["bonus_out_total"]),
        }
        for department_id, values in sorted(
            department_agg.items(),
            key=lambda item: (
                -float(item[1]["net_total"]),
                -int(item[1]["guests_count"]),
                item[0],
            ),
        )
    ]
    scatter_points = _build_scatter_points(selected_guest_rows)

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
            "metrics_layer": "category_window" if use_category_window_metrics else "window",
            "metrics_layer_name": (
                "Метрики по выбранной категории"
                if use_category_window_metrics
                else "Общие метрики по окну"
            ),
            "complex_filters": normalized_complex_filters,
            "complex_filter_fields": complex_filter_options["fields"],
            "complex_filter_operators": complex_filter_options["operators"],
            "show_all_presets": show_all_presets,
            "saved_presets": saved_presets,
        },
        "cards": {
            "guests_total": int(cards_guests_total),
            "orders_total": int(cards_orders_total),
            "visits_total": int(cards_visits_total),
            "net_total": _to_money_ui(cards_net_total),
            "bonus_in_total": _to_money_str(cards_bonus_in_total),
            "bonus_out_total": _to_money_str(cards_bonus_out_total),
            "avg_rating": _to_decimal_str(cards_avg_rating),
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
    normalized_complex_filters: list[dict[str, Any]],
    complex_filter_options: dict[str, list[dict[str, str]]],
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
            "metrics_layer": "window",
            "metrics_layer_name": "Общие метрики по окну",
            "complex_filters": normalized_complex_filters,
            "complex_filter_fields": complex_filter_options["fields"],
            "complex_filter_operators": complex_filter_options["operators"],
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
            BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE: 0,
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


def _collect_bot_active_no_visits_guest_keys(
    *,
    as_of_date: date,
    selected_department_id: str,
    allowed_guest_keys: set[tuple[int, str]] | None = None,
) -> set[tuple[int, str]]:
    """
    Возвращает гостевые ключи для сегмента:
    `активен в боте, но без визитов за 180 дней`.

    Условия канала:
    1. is_registered=true;
    2. notifications_allowed=true;
    3. external_id заполнен;
    4. есть связанный guest.
    """
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
    if allowed_guest_keys is not None:
        allowed_guest_ids = {guest_id for guest_id, _ in allowed_guest_keys}
        if not allowed_guest_ids:
            return set()
        candidate_scope = candidate_scope.filter(guest_id__in=allowed_guest_ids)

    range_start = as_of_date - timedelta(days=179)
    recent_visits_scope = OrderFact.objects.filter(
        guest_id=OuterRef("guest_id"),
        business_date__gte=range_start,
        business_date__lte=as_of_date,
    )
    if selected_department_id:
        recent_visits_scope = recent_visits_scope.filter(department_id=selected_department_id)

    idle_guest_ids = (
        candidate_scope.annotate(has_recent_visit=Exists(recent_visits_scope))
        .filter(has_recent_visit=False)
        .values_list("guest_id", flat=True)
        .distinct()
    )
    department_key = selected_department_id or ""
    return {(int(guest_id), department_key) for guest_id in idle_guest_ids}


def _build_segmentation_state(
    scope_qs,
    *,
    as_of_date: date,
    selected_department_id: str,
    allowed_guest_keys: set[tuple[int, str]] | None = None,
) -> tuple[dict[str, int], dict[tuple[int, str], str]]:
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
        if allowed_guest_keys is not None and key not in allowed_guest_keys:
            continue
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

    bot_segment_keys = _collect_bot_active_no_visits_guest_keys(
        as_of_date=as_of_date,
        selected_department_id=selected_department_id,
        allowed_guest_keys=allowed_guest_keys,
    )
    for key in sorted(bot_segment_keys):
        if key in segment_by_key:
            continue
        segment_by_key[key] = BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE
        segment_totals[BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE] += 1

    return segment_totals, segment_by_key


def _build_segment_focus_matrix(
    *,
    as_of_date: date,
    selected_window_days: int,
    selected_department_id: str,
    segment_by_key: dict[tuple[int, str], str],
    segment_totals: dict[str, int],
    allowed_guest_keys: set[tuple[int, str]] | None = None,
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

    for row in daily_scope.values("guest_id", "department_id", "focus_category_id").distinct():
        guest_id = int(row["guest_id"])
        department_id = _normalize_department_id(row.get("department_id"))
        guest_key = (guest_id, department_id)
        if allowed_guest_keys is not None and guest_key not in allowed_guest_keys:
            continue
        segment_code = segment_by_key.get((guest_id, department_id))
        if not segment_code:
            continue
        focus_id = int(row["focus_category_id"])
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


def _build_scatter_points(selected_guest_rows: list[tuple[Any, str]]) -> list[dict[str, Any]]:
    """
    Готовит точки для диаграммы «частота × средний чек».
    """
    points: list[dict[str, Any]] = []
    for row, _ in selected_guest_rows[:500]:
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


def _collect_selected_guest_rows(
    *,
    base_scope,
    selected_window_days: int,
    segment_by_key: dict[tuple[int, str], str],
    focus_guest_keys_by_code: dict[str, set[tuple[int, str]]],
    segment_code: str,
    focus_category_code: str,
    allowed_guest_keys: set[tuple[int, str]] | None = None,
) -> list[tuple[Any, str]]:
    """
    Возвращает отфильтрованные строки гостей для таблицы/карточек в единой логике.
    """
    focus_keys = focus_guest_keys_by_code.get(focus_category_code, set()) if focus_category_code else None
    representative_rows = _build_representative_rows(
        base_scope=base_scope,
        selected_window_days=selected_window_days,
        segment_code=segment_code,
        allowed_guest_keys=allowed_guest_keys,
    )

    selected: list[tuple[Any, str]] = []
    selected_keys: set[tuple[int, str]] = set()
    for row in representative_rows:
        key = (int(row.guest_id), _normalize_department_id(row.department_id))
        row_segment_code = segment_by_key.get(key, "")

        if not row_segment_code:
            continue
        if segment_code and row_segment_code != segment_code:
            continue
        if focus_keys is not None and key not in focus_keys:
            continue

        selected.append((row, row_segment_code))
        selected_keys.add(key)

    if segment_code == BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE:
        synthetic_keys = [
            key
            for key, row_segment_code in segment_by_key.items()
            if row_segment_code == BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE and key not in selected_keys
        ]
        if focus_keys is not None:
            synthetic_keys = [key for key in synthetic_keys if key in focus_keys]
        if synthetic_keys:
            guest_map = {
                int(guest.id): guest
                for guest in Guest.objects.filter(id__in=[guest_id for guest_id, _ in synthetic_keys]).only(
                    "id",
                    "phone",
                    "first_name",
                    "last_name",
                )
            }
            for guest_id, department_id in synthetic_keys:
                guest = guest_map.get(int(guest_id))
                if guest is None:
                    continue
                synthetic_row = SimpleNamespace(
                    guest_id=int(guest_id),
                    guest=guest,
                    department_id=department_id,
                    window_days=selected_window_days,
                    orders_count=0,
                    visits_count=0,
                    sum_net=Decimal("0"),
                    avg_check_net=Decimal("0"),
                    bonus_in_sum=Decimal("0"),
                    bonus_out_sum=Decimal("0"),
                    rating_score=Decimal("0"),
                    last_visit_at=None,
                )
                selected.append((synthetic_row, BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE))

    selected.sort(
        key=lambda item: (
            -(float(item[0].rating_score or 0)),
            -(float(item[0].sum_net or 0)),
            int(item[0].guest_id),
        )
    )
    return selected


def _build_selected_guests_rows(
    *,
    base_scope,
    selected_window_days: int,
    segment_by_key: dict[tuple[int, str], str],
    focus_guest_keys_by_code: dict[str, set[tuple[int, str]]],
    segment_code: str,
    focus_category_code: str,
    allowed_guest_keys: set[tuple[int, str]] | None = None,
    limit: int,
    selected_guest_rows: list[tuple[Any, str]] | None = None,
) -> dict[str, Any]:
    """
    Строит список гостей для выбранных фильтров сегмента и фокусной категории.
    """
    rows: list[dict[str, Any]] = []
    selected_rows = selected_guest_rows
    if selected_rows is None:
        selected_rows = _collect_selected_guest_rows(
            base_scope=base_scope,
            selected_window_days=selected_window_days,
            segment_by_key=segment_by_key,
            focus_guest_keys_by_code=focus_guest_keys_by_code,
            segment_code=segment_code,
            focus_category_code=focus_category_code,
            allowed_guest_keys=allowed_guest_keys,
        )

    total = len(selected_rows)
    for row, row_segment_code in selected_rows:
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


def _serialize_metric_row(row: Any) -> dict[str, Any]:
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
