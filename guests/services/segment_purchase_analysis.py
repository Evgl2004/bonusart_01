from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Max, Min

from guests.models import (
    GuestRestaurantDailyOrderFact,
    GuestRestaurantWindowMetrics,
    OlapNomenclatureDict,
    OlapSalesRawLine,
)
from guests.services.guest_workbench import (
    BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE,
    NEW_IN_VENUE_SEGMENT_CODE,
    SEGMENT_DEFINITIONS,
    SEGMENT_DESCRIPTIONS,
    SEGMENT_NAMES_MAP,
    _build_department_options,
    _build_segmentation_state,
    _normalize_department_id,
    _to_money_ui,
)

PURCHASE_PERIOD_OPTIONS = (30, 60, 180, 365)
TOP_LIMIT_OPTIONS = (10, 15, 30)
DEFAULT_PURCHASE_PERIOD_DAYS = 60
DEFAULT_TOP_LIMIT = 15
DEFAULT_SEGMENT_CODE = "cooling_30_60d"
DEFAULT_HIDE_ZERO_REVENUE = True

ANALYSIS_SEGMENT_DEFINITIONS = tuple(
    (code, name)
    for code, name in SEGMENT_DEFINITIONS
    if code != BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE
)
ANALYSIS_SEGMENT_NAMES_MAP = dict(ANALYSIS_SEGMENT_DEFINITIONS)


def build_segment_purchase_analysis_payload(
    *,
    department_id: str | None = None,
    segment_code: str | None = None,
    period_days: int | str | None = None,
    top_limit: int | str | None = None,
    hide_zero_revenue: bool | int | str | None = None,
) -> dict[str, Any]:
    selected_department_id = (department_id or "").strip()
    selected_segment_code = normalize_analysis_segment_code(segment_code)
    selected_period_days = normalize_purchase_period_days(period_days)
    selected_top_limit = normalize_top_limit(top_limit)
    selected_hide_zero_revenue = normalize_hide_zero_revenue(hide_zero_revenue)

    target_as_of = GuestRestaurantWindowMetrics.objects.aggregate(v=Max("as_of_date")).get("v")
    department_options = _build_department_options()
    selected_department_name = _find_department_name(department_options, selected_department_id)

    base_payload = _build_base_payload(
        as_of_date=target_as_of,
        department_options=department_options,
        selected_department_id=selected_department_id,
        selected_department_name=selected_department_name,
        selected_segment_code=selected_segment_code,
        selected_period_days=selected_period_days,
        selected_top_limit=selected_top_limit,
        selected_hide_zero_revenue=selected_hide_zero_revenue,
    )

    if target_as_of is None:
        base_payload["warnings"].append("Нет рассчитанной витрины сегментов.")
        return base_payload

    if not selected_department_id:
        base_payload["warnings"].append("Выберите заведение, чтобы построить топ номенклатуры.")
        return base_payload

    range_start = target_as_of - timedelta(days=selected_period_days - 1)
    segment_guest_keys = _collect_segment_guest_keys(
        as_of_date=target_as_of,
        department_id=selected_department_id,
        segment_code=selected_segment_code,
        period_days=selected_period_days,
    )
    segment_guest_ids = {guest_id for guest_id, _ in segment_guest_keys}
    base_payload["stats"]["segment_size"] = len(segment_guest_ids)

    if not segment_guest_ids:
        base_payload["warnings"].append("В выбранном сегменте пока нет гостей.")
        return base_payload

    first_purchase_dates: dict[int, date] | None = None
    if selected_segment_code == NEW_IN_VENUE_SEGMENT_CODE:
        first_purchase_dates = _load_first_purchase_dates(
            guest_ids=segment_guest_ids,
            department_id=selected_department_id,
        )

    aggregate = _aggregate_raw_lines(
        guest_ids=segment_guest_ids,
        department_id=selected_department_id,
        range_start=range_start,
        range_end=target_as_of,
        first_purchase_dates=first_purchase_dates,
        hide_zero_revenue=selected_hide_zero_revenue,
    )
    rows = _build_rows(
        aggregate["items"],
        segment_size=len(segment_guest_ids),
        top_limit=selected_top_limit,
    )

    base_payload["rows"] = rows
    base_payload["stats"].update(
        {
            "guests_with_purchases": len(aggregate["guest_ids_with_purchases"]),
            "orders_count": len(aggregate["order_keys"]),
            "raw_lines_count": aggregate["raw_lines_count"],
            "sales_total": _to_money_ui(aggregate["sales_total"]),
            "sales_total_value": str(aggregate["sales_total"]),
        }
    )
    base_payload["technical"] = _build_technical_rows(
        as_of_date=target_as_of,
        range_start=range_start,
        range_end=target_as_of,
        selected_department_name=selected_department_name,
        selected_department_id=selected_department_id,
        selected_segment_code=selected_segment_code,
        selected_period_days=selected_period_days,
        selected_top_limit=selected_top_limit,
        selected_hide_zero_revenue=selected_hide_zero_revenue,
        segment_size=len(segment_guest_ids),
        raw_lines_count=aggregate["raw_lines_count"],
    )

    if not rows:
        base_payload["warnings"].append("Для выбранного сегмента не найдено покупок за период.")

    return base_payload


def normalize_purchase_period_days(raw_value: int | str | None) -> int:
    try:
        value = int(raw_value or DEFAULT_PURCHASE_PERIOD_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_PURCHASE_PERIOD_DAYS
    return value if value in PURCHASE_PERIOD_OPTIONS else DEFAULT_PURCHASE_PERIOD_DAYS


def normalize_top_limit(raw_value: int | str | None) -> int:
    try:
        value = int(raw_value or DEFAULT_TOP_LIMIT)
    except (TypeError, ValueError):
        return DEFAULT_TOP_LIMIT
    return value if value in TOP_LIMIT_OPTIONS else DEFAULT_TOP_LIMIT


def normalize_analysis_segment_code(raw_value: str | None) -> str:
    value = (raw_value or DEFAULT_SEGMENT_CODE).strip()
    if value in ANALYSIS_SEGMENT_NAMES_MAP:
        return value
    return DEFAULT_SEGMENT_CODE


def normalize_hide_zero_revenue(raw_value: bool | int | str | None) -> bool:
    if raw_value is None:
        return DEFAULT_HIDE_ZERO_REVENUE
    value = str(raw_value).strip().lower()
    if value in {"0", "false", "off", "no"}:
        return False
    if value in {"1", "true", "on", "yes"}:
        return True
    return DEFAULT_HIDE_ZERO_REVENUE


def _collect_segment_guest_keys(
    *,
    as_of_date: date,
    department_id: str,
    segment_code: str,
    period_days: int,
) -> set[tuple[int, str]]:
    scope_qs = GuestRestaurantWindowMetrics.objects.filter(
        as_of_date=as_of_date,
        department_id=department_id,
    )
    _, _, segment_guest_keys_by_code = _build_segmentation_state(
        scope_qs,
        as_of_date=as_of_date,
        selected_window_days=period_days,
        selected_department_id=department_id,
    )
    return set(segment_guest_keys_by_code.get(segment_code, set()))


def _load_first_purchase_dates(
    *,
    guest_ids: set[int],
    department_id: str,
) -> dict[int, date]:
    if not guest_ids:
        return {}
    rows = (
        GuestRestaurantDailyOrderFact.objects.filter(
            guest_id__in=guest_ids,
            department_id=department_id,
            orders_count__gt=0,
        )
        .values("guest_id")
        .annotate(first_purchase_date=Min("business_date"))
    )
    return {
        int(row["guest_id"]): row["first_purchase_date"]
        for row in rows
        if row.get("first_purchase_date") is not None
    }


def _aggregate_raw_lines(
    *,
    guest_ids: set[int],
    department_id: str,
    range_start: date,
    range_end: date,
    first_purchase_dates: dict[int, date] | None,
    hide_zero_revenue: bool,
) -> dict[str, Any]:
    item_aggregate: dict[str, dict[str, Any]] = {}
    guest_ids_with_purchases: set[int] = set()
    order_keys: set[tuple[str, str, int, str]] = set()
    raw_lines_count = 0
    sales_total = Decimal("0")

    rows = (
        OlapSalesRawLine.objects.filter(
            guest_id__in=guest_ids,
            department_id=department_id,
            business_date__gte=range_start,
            business_date__lte=range_end,
        )
        .exclude(dish_code__isnull=True)
        .exclude(dish_code="")
        .values(
            "guest_id",
            "business_date",
            "department_id",
            "department_name",
            "order_number",
            "uniq_order_id",
            "dish_code",
            "dish_name",
            "dish_category_id",
            "dish_category_name",
            "dish_group_name",
            "dish_amount",
            "dish_sum_before_discount",
            "dish_sum_after_discount",
        )
    )

    for row in rows:
        guest_id = int(row["guest_id"])
        business_date = row["business_date"]
        if first_purchase_dates is not None and first_purchase_dates.get(guest_id) != business_date:
            continue

        dish_code = str(row.get("dish_code") or "").strip()
        if not dish_code:
            continue

        line_sum = _raw_line_net_sum(row)
        if hide_zero_revenue and line_sum == Decimal("0"):
            continue

        raw_lines_count += 1
        guest_ids_with_purchases.add(guest_id)
        order_key = _build_order_key(row)
        order_keys.add(order_key)

        item = item_aggregate.setdefault(
            dish_code,
            {
                "dish_code": dish_code,
                "dish_name": str(row.get("dish_name") or "").strip() or dish_code,
                "category_name": str(row.get("dish_category_name") or "").strip(),
                "group_name": str(row.get("dish_group_name") or "").strip(),
                "guest_ids": set(),
                "order_keys": set(),
                "quantity_total": Decimal("0"),
                "sales_sum": Decimal("0"),
            },
        )
        if not item["dish_name"] and row.get("dish_name"):
            item["dish_name"] = str(row["dish_name"]).strip()
        if not item["category_name"] and row.get("dish_category_name"):
            item["category_name"] = str(row["dish_category_name"]).strip()
        if not item["group_name"] and row.get("dish_group_name"):
            item["group_name"] = str(row["dish_group_name"]).strip()

        item["guest_ids"].add(guest_id)
        item["order_keys"].add(order_key)
        item["quantity_total"] += _raw_line_quantity(row)
        item["sales_sum"] += line_sum
        sales_total += line_sum

    return {
        "items": item_aggregate,
        "guest_ids_with_purchases": guest_ids_with_purchases,
        "order_keys": order_keys,
        "raw_lines_count": raw_lines_count,
        "sales_total": sales_total,
    }


def _build_rows(
    item_aggregate: dict[str, dict[str, Any]],
    *,
    segment_size: int,
    top_limit: int,
) -> list[dict[str, Any]]:
    _enrich_with_nomenclature_dict(item_aggregate)
    sorted_items = sorted(
        item_aggregate.values(),
        key=lambda item: (
            -item["quantity_total"],
            -len(item["guest_ids"]),
            -item["sales_sum"],
            item["dish_name"].casefold(),
        ),
    )

    rows: list[dict[str, Any]] = []
    for rank, item in enumerate(sorted_items[:top_limit], start=1):
        guests_count = len(item["guest_ids"])
        orders_count = len(item["order_keys"])
        share = _percent(guests_count, segment_size)
        quantity_total = item["quantity_total"]
        sales_sum = item["sales_sum"]
        rows.append(
            {
                "rank": rank,
                "dish_code": item["dish_code"],
                "dish_name": item["dish_name"],
                "category_name": item["category_name"] or "Без категории",
                "group_name": item["group_name"] or "Без группы",
                "quantity_total": _to_quantity_ui(quantity_total),
                "quantity_total_value": str(quantity_total),
                "guests_count": guests_count,
                "orders_count": orders_count,
                "share_of_segment": f"{share:.1f}",
                "share_of_segment_value": f"{share:.4f}",
                "sales_sum": _to_money_ui(sales_sum),
                "sales_sum_value": str(sales_sum),
            }
        )
    return rows


def _enrich_with_nomenclature_dict(item_aggregate: dict[str, dict[str, Any]]) -> None:
    if not item_aggregate:
        return
    rows = (
        OlapNomenclatureDict.objects.select_related("olap_category")
        .filter(iiko_nomenclature_external_id__in=list(item_aggregate.keys()))
        .values(
            "iiko_nomenclature_external_id",
            "nomenclature_name",
            "dish_group_name",
            "olap_category__category_name",
        )
    )
    for row in rows:
        dish_code = str(row.get("iiko_nomenclature_external_id") or "").strip()
        item = item_aggregate.get(dish_code)
        if item is None:
            continue
        nomenclature_name = str(row.get("nomenclature_name") or "").strip()
        category_name = str(row.get("olap_category__category_name") or "").strip()
        group_name = str(row.get("dish_group_name") or "").strip()
        if nomenclature_name:
            item["dish_name"] = nomenclature_name
        if category_name:
            item["category_name"] = category_name
        if group_name:
            item["group_name"] = group_name


def _build_base_payload(
    *,
    as_of_date: date | None,
    department_options: list[dict[str, str]],
    selected_department_id: str,
    selected_department_name: str,
    selected_segment_code: str,
    selected_period_days: int,
    selected_top_limit: int,
    selected_hide_zero_revenue: bool,
) -> dict[str, Any]:
    return {
        "filters": {
            "as_of_date": as_of_date.isoformat() if as_of_date else "",
            "department_id": selected_department_id,
            "department_name": selected_department_name,
            "department_options": department_options,
            "segment_code": selected_segment_code,
            "segment_name": SEGMENT_NAMES_MAP.get(selected_segment_code, selected_segment_code),
            "segment_options": _build_segment_options(),
            "period_days": selected_period_days,
            "period_options": list(PURCHASE_PERIOD_OPTIONS),
            "top_limit": selected_top_limit,
            "top_limit_options": list(TOP_LIMIT_OPTIONS),
            "hide_zero_revenue": selected_hide_zero_revenue,
        },
        "stats": {
            "segment_size": 0,
            "guests_with_purchases": 0,
            "orders_count": 0,
            "raw_lines_count": 0,
            "sales_total": _to_money_ui(0),
            "sales_total_value": "0",
        },
        "rows": [],
        "warnings": [],
        "technical": [],
    }


def _build_segment_options() -> list[dict[str, str]]:
    return [
        {
            "code": code,
            "name": name,
            "description": SEGMENT_DESCRIPTIONS.get(code, ""),
        }
        for code, name in ANALYSIS_SEGMENT_DEFINITIONS
    ]


def _build_technical_rows(
    *,
    as_of_date: date,
    range_start: date,
    range_end: date,
    selected_department_name: str,
    selected_department_id: str,
    selected_segment_code: str,
    selected_period_days: int,
    selected_top_limit: int,
    selected_hide_zero_revenue: bool,
    segment_size: int,
    raw_lines_count: int,
) -> list[dict[str, str]]:
    return [
        {"label": "Дата витрины", "value": as_of_date.isoformat()},
        {"label": "Период покупок", "value": f"{range_start.isoformat()} - {range_end.isoformat()}"},
        {"label": "Заведение", "value": f"{selected_department_name} ({selected_department_id})"},
        {"label": "Сегмент", "value": ANALYSIS_SEGMENT_NAMES_MAP.get(selected_segment_code, selected_segment_code)},
        {"label": "Глубина", "value": f"{selected_period_days} дней"},
        {"label": "Лимит строк", "value": str(selected_top_limit)},
        {"label": "Скрывать 0 выручку", "value": "да" if selected_hide_zero_revenue else "нет"},
        {"label": "Гостей в сегменте", "value": str(segment_size)},
        {"label": "Строк OLAP после фильтров", "value": str(raw_lines_count)},
    ]


def _find_department_name(options: list[dict[str, str]], department_id: str) -> str:
    if not department_id:
        return ""
    for item in options:
        if _normalize_department_id(item.get("id")) == department_id:
            return (item.get("name") or "").strip() or department_id
    return department_id


def _build_order_key(row: dict[str, Any]) -> tuple[str, str, int, str]:
    order_number = row.get("order_number")
    return (
        row["business_date"].isoformat(),
        _normalize_department_id(row.get("department_id")),
        int(order_number or 0),
        str(row.get("uniq_order_id") or "").strip(),
    )


def _raw_line_net_sum(row: dict[str, Any]) -> Decimal:
    net = _to_decimal(row.get("dish_sum_after_discount"))
    gross = _to_decimal(row.get("dish_sum_before_discount"))
    return gross if net == Decimal("0") else net


def _raw_line_quantity(row: dict[str, Any]) -> Decimal:
    quantity = _to_decimal(row.get("dish_amount"))
    return quantity if quantity > 0 else Decimal("1")


def _to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _to_quantity_ui(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"{int(value):,}".replace(",", " ")
    text = f"{value.normalize():f}".rstrip("0").rstrip(".")
    integer_part, _, fractional_part = text.partition(".")
    integer_with_spaces = f"{int(integer_part or 0):,}".replace(",", " ")
    return f"{integer_with_spaces},{fractional_part}"


def _percent(value: int, base: int) -> float:
    if base <= 0:
        return 0.0
    return round((float(value) * 100.0) / float(base), 1)
