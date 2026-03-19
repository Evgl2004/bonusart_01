"""
Сервис подготовки данных для пользовательского дашборда аналитики.

Сервис формирует:
1. KPI-блок (6 ключевых показателей);
2. 3 графика для Apache ECharts;
3. справочники фильтров (период и заведение).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db.models import Count, Max, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from guests.models import (
    GuestRestaurantDailyCategoryFact,
    GuestRestaurantWindowMetrics,
    OrderFact,
)

ALLOWED_PERIOD_DAYS = (7, 14, 30, 60, 180)
DEFAULT_PERIOD_DAYS = 30


def normalize_period_days(raw_value: int | str | None) -> int:
    """
    Нормализует размер периода в днях для фильтра дашборда.
    """
    try:
        value = int(raw_value or DEFAULT_PERIOD_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_PERIOD_DAYS
    if value not in ALLOWED_PERIOD_DAYS:
        return DEFAULT_PERIOD_DAYS
    return value


def build_analytics_dashboard_payload(
    *,
    period_days: int = DEFAULT_PERIOD_DAYS,
    department_id: str | None = None,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """
    Строит полный payload для рендера дашборда в UI.
    """
    normalized_period = normalize_period_days(period_days)
    target_date = as_of_date or timezone.localdate()
    date_from = target_date - timedelta(days=normalized_period - 1)
    selected_department_id = (department_id or "").strip()

    base_orders_qs = OrderFact.objects.filter(
        business_date__gte=date_from,
        business_date__lte=target_date,
    )
    if selected_department_id:
        base_orders_qs = base_orders_qs.filter(department_id=selected_department_id)

    # Основные сводные KPI по чекам за период.
    order_agg = base_orders_qs.aggregate(
        orders_total=Count("id"),
        unique_guests=Count("guest", distinct=True),
        net_revenue=Coalesce(Sum("net_sum"), Decimal("0")),
    )
    orders_total = int(order_agg["orders_total"] or 0)
    unique_guests = int(order_agg["unique_guests"] or 0)
    net_revenue = _to_decimal(order_agg["net_revenue"])
    avg_check = _quantize_money(net_revenue / Decimal(orders_total)) if orders_total else Decimal("0")

    # Активные/спящие гости считаем из оконного слоя, чтобы не сканировать сырые данные.
    active_30d, sleeping_30_180d, metrics_as_of = _build_activity_metrics(
        target_date=target_date,
        selected_department_id=selected_department_id,
    )

    kpis = [
        {
            "key": "orders_total",
            "title": "Заказы за период",
            "value": orders_total,
            "value_display": _format_int(orders_total),
            "description": f"Суммарно за последние {normalized_period} дней.",
        },
        {
            "key": "net_revenue",
            "title": "Выручка (нетто)",
            "value": _decimal_to_float(net_revenue),
            "value_display": _format_money(net_revenue),
            "description": "Сумма чеков после скидок.",
        },
        {
            "key": "avg_check",
            "title": "Средний чек",
            "value": _decimal_to_float(avg_check),
            "value_display": _format_money(avg_check),
            "description": "Нетто-выручка / количество заказов.",
        },
        {
            "key": "unique_guests",
            "title": "Уникальные гости",
            "value": unique_guests,
            "value_display": _format_int(unique_guests),
            "description": "Количество гостей, у которых были заказы в периоде.",
        },
        {
            "key": "active_30d",
            "title": "Активные гости (30 дней)",
            "value": active_30d,
            "value_display": _format_int(active_30d),
            "description": (
                "Гости с заказами за последние 30 дней "
                f"(слой оконных метрик от {metrics_as_of.strftime('%d.%m.%Y') if metrics_as_of else 'нет данных'})."
            ),
        },
        {
            "key": "sleeping_30_180d",
            "title": "Спящие гости (30-180 дней)",
            "value": sleeping_30_180d,
            "value_display": _format_int(sleeping_30_180d),
            "description": "Были активны в окне 180 дней, но неактивны за последние 30 дней.",
        },
    ]

    filters = {
        "period_days": normalized_period,
        "period_options": list(ALLOWED_PERIOD_DAYS),
        "department_id": selected_department_id,
        "departments": _build_department_options(),
        "date_from": date_from.isoformat(),
        "date_to": target_date.isoformat(),
    }

    charts = {
        "daily_dynamics": _build_daily_dynamics_chart(
            date_from=date_from,
            target_date=target_date,
            selected_department_id=selected_department_id,
        ),
        "department_revenue": _build_department_revenue_chart(
            date_from=date_from,
            target_date=target_date,
            selected_department_id=selected_department_id,
        ),
        "focus_categories": _build_focus_categories_chart(
            date_from=date_from,
            target_date=target_date,
            selected_department_id=selected_department_id,
        ),
    }

    return {
        "filters": filters,
        "kpis": kpis,
        "charts": charts,
    }


def _build_activity_metrics(
    *,
    target_date: date,
    selected_department_id: str,
) -> tuple[int, int, date | None]:
    """
    Возвращает количество активных и спящих гостей на основе оконных метрик.
    """
    window_qs = GuestRestaurantWindowMetrics.objects.filter(as_of_date__lte=target_date)
    if selected_department_id:
        window_qs = window_qs.filter(department_id=selected_department_id)

    metrics_as_of = window_qs.aggregate(max_as_of=Max("as_of_date"))["max_as_of"]
    if metrics_as_of is None:
        return 0, 0, None

    scoped = window_qs.filter(as_of_date=metrics_as_of)
    active_30d = scoped.filter(window_days=30, orders_count__gt=0).values("guest_id").distinct().count()
    active_180d = scoped.filter(window_days=180, orders_count__gt=0).values("guest_id").distinct().count()
    sleeping_30_180d = max(int(active_180d) - int(active_30d), 0)
    return int(active_30d), sleeping_30_180d, metrics_as_of


def _build_daily_dynamics_chart(
    *,
    date_from: date,
    target_date: date,
    selected_department_id: str,
) -> dict[str, Any]:
    """
    Готовит график «выручка и количество заказов по дням».
    """
    query = OrderFact.objects.filter(
        business_date__gte=date_from,
        business_date__lte=target_date,
    )
    if selected_department_id:
        query = query.filter(department_id=selected_department_id)

    grouped = (
        query.values("business_date")
        .annotate(
            orders_total=Count("id"),
            revenue_total=Coalesce(Sum("net_sum"), Decimal("0")),
        )
        .order_by("business_date")
    )

    by_date = {
        row["business_date"]: {
            "orders": int(row["orders_total"] or 0),
            "revenue": _decimal_to_float(_to_decimal(row["revenue_total"])),
        }
        for row in grouped
    }

    labels: list[str] = []
    orders_series: list[int] = []
    revenue_series: list[float] = []
    cursor = date_from
    while cursor <= target_date:
        labels.append(cursor.strftime("%d.%m"))
        payload = by_date.get(cursor, {"orders": 0, "revenue": 0.0})
        orders_series.append(payload["orders"])
        revenue_series.append(payload["revenue"])
        cursor += timedelta(days=1)

    return {
        "title": "Динамика выручки и заказов",
        "labels": labels,
        "orders_series": orders_series,
        "revenue_series": revenue_series,
    }


def _build_department_revenue_chart(
    *,
    date_from: date,
    target_date: date,
    selected_department_id: str,
) -> dict[str, Any]:
    """
    Готовит график «выручка по заведениям».
    """
    query = OrderFact.objects.filter(
        business_date__gte=date_from,
        business_date__lte=target_date,
    )
    if selected_department_id:
        query = query.filter(department_id=selected_department_id)

    grouped = (
        query.values("department_id")
        .annotate(
            department_name=Max("department_name"),
            orders_total=Count("id"),
            revenue_total=Coalesce(Sum("net_sum"), Decimal("0")),
        )
        .order_by("-revenue_total", "department_id")[:12]
    )

    labels: list[str] = []
    revenue_series: list[float] = []
    orders_series: list[int] = []
    for row in grouped:
        dep_id = (row["department_id"] or "").strip()
        dep_name = (row["department_name"] or "").strip()
        label = dep_name or dep_id or "Без заведения"
        labels.append(label)
        revenue_series.append(_decimal_to_float(_to_decimal(row["revenue_total"])))
        orders_series.append(int(row["orders_total"] or 0))

    return {
        "title": "Выручка по заведениям",
        "labels": labels,
        "revenue_series": revenue_series,
        "orders_series": orders_series,
    }


def _build_focus_categories_chart(
    *,
    date_from: date,
    target_date: date,
    selected_department_id: str,
) -> dict[str, Any]:
    """
    Готовит круговой график по фокусным категориям.
    """
    query = GuestRestaurantDailyCategoryFact.objects.filter(
        business_date__gte=date_from,
        business_date__lte=target_date,
    )
    if selected_department_id:
        query = query.filter(department_id=selected_department_id)

    grouped = (
        query.values("focus_category__name")
        .annotate(
            sum_net=Coalesce(Sum("sum_net"), Decimal("0")),
            orders_total=Coalesce(Sum("orders_count"), 0),
        )
        .order_by("-sum_net", "focus_category__name")[:10]
    )

    pie_data: list[dict[str, Any]] = []
    for row in grouped:
        label = (row["focus_category__name"] or "").strip() or "Без категории"
        pie_data.append(
            {
                "name": label,
                "value": _decimal_to_float(_to_decimal(row["sum_net"])),
                "orders": int(row["orders_total"] or 0),
            }
        )

    return {
        "title": "Фокусные категории (сумма чека)",
        "pie_data": pie_data,
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
        dep_id = (row["department_id"] or "").strip()
        if not dep_id:
            continue
        dep_name = (row["department_name"] or "").strip()
        result.append(
            {
                "id": dep_id,
                "name": dep_name or dep_id,
            }
        )
    return result


def _format_int(value: int) -> str:
    """
    Красиво форматирует целое число с пробелами.
    """
    return f"{int(value):,}".replace(",", " ")


def _format_money(value: Decimal) -> str:
    """
    Красиво форматирует денежную сумму в рублях.
    """
    normalized = _quantize_money(value)
    integer_part, fractional_part = f"{normalized:.2f}".split(".")
    integer_with_spaces = f"{int(integer_part):,}".replace(",", " ")
    return f"{integer_with_spaces},{fractional_part} ₽"


def _to_decimal(value: Decimal | int | float | None) -> Decimal:
    """
    Безопасно преобразует значение в Decimal.
    """
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _quantize_money(value: Decimal) -> Decimal:
    """
    Округляет деньги до копеек.
    """
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decimal_to_float(value: Decimal) -> float:
    """
    Преобразует Decimal в float для передачи в JSON-графики.
    """
    return float(_quantize_money(_to_decimal(value)))
