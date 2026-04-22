from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum

from guests.models import (
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    Guest,
    GuestRestaurantDailyCategoryFact,
    GuestRestaurantWindowCategoryMetrics,
    GuestRestaurantWindowMetrics,
    OlapSalesRawLine,
    OrderFact,
)
from guests.services import guest_workbench


def _to_iso_date(raw: str, *, arg_name: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise CommandError(f"Некорректный формат даты в {arg_name}: {raw!r}. Ожидается YYYY-MM-DD.") from exc


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _phone10(value: str) -> str:
    digits = _phone_digits(value)
    return digits[-10:] if len(digits) >= 10 else digits


def _parse_ui_decimal(value: Any) -> Decimal:
    normalized = _norm_text(value).replace(" ", "").replace(",", ".")
    if not normalized:
        return Decimal("0.00")
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _serialize_decimal(value: Any) -> str:
    try:
        return f"{Decimal(str(value or 0)):.2f}"
    except (InvalidOperation, ValueError):
        return "0.00"


def _serialize_metric_row(row: Any) -> dict[str, Any]:
    return {
        "guest_id": int(row.guest_id),
        "department_id": _norm_text(row.department_id),
        "window_days": int(row.window_days or 0),
        "orders_count": int(row.orders_count or 0),
        "visits_count": int(row.visits_count or 0),
        "sum_net": _serialize_decimal(row.sum_net),
        "avg_check_net": _serialize_decimal(row.avg_check_net),
        "rating_score": _serialize_decimal(row.rating_score),
        "last_visit_at": row.last_visit_at.isoformat() if row.last_visit_at else "",
    }


def _find_guest_by_phone(raw_phone: str) -> Guest | None:
    digits = _phone_digits(raw_phone)
    if not digits:
        return None

    variants = [digits, f"+{digits}"]
    if len(digits) >= 10:
        phone10 = digits[-10:]
        variants.extend([phone10, f"+7{phone10}"])

    guest = Guest.objects.filter(phone__in=variants).order_by("id").first()
    if guest is not None:
        return guest

    if len(digits) >= 10:
        guest = Guest.objects.filter(phone__endswith=digits[-10:]).order_by("id").first()
    return guest


def _line_net_value(raw_row: dict[str, Any]) -> Decimal:
    """
    Значение позиции для focus-суммы: после скидки, fallback на сумму до скидки.
    """
    net_value = _to_decimal(raw_row.get("dish_sum_after_discount"))
    if net_value == Decimal("0"):
        net_value = _to_decimal(raw_row.get("dish_sum_before_discount"))
    return net_value


def _order_key_from_parts(
    *,
    business_date: date | None,
    department_id: Any,
    order_number: Any,
    uniq_order_id: Any,
) -> tuple[date, str, int, str] | None:
    if business_date is None or order_number is None:
        return None
    return (
        business_date,
        _norm_text(department_id),
        int(order_number),
        _norm_text(uniq_order_id),
    )


def _compute_rating(*, orders_count: int, visits_count: int, avg_check_net: Decimal) -> Decimal:
    score = (
        Decimal(orders_count) * Decimal("3")
        + Decimal(visits_count) * Decimal("2")
        + (avg_check_net / Decimal("100"))
    )
    return score.quantize(Decimal("0.01"))


def _collect_focus_dish_codes(focus: FocusCategory) -> list[str]:
    query = (
        FocusCategoryNomenclatureResolved.objects.filter(
            focus_category_id=focus.id,
            nomenclature__is_active=True,
        )
        .values_list("nomenclature__iiko_nomenclature_external_id", flat=True)
        .order_by("nomenclature__iiko_nomenclature_external_id")
    )
    result: list[str] = []
    seen: set[str] = set()
    for item in query.iterator(chunk_size=2000):
        code = _norm_text(item)
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return result


def _build_full_trace_for_guest(
    *,
    guest_id: int,
    as_of_date: date,
    window_days: int,
    department_id: str,
    focus: FocusCategory | None,
    max_rows: int,
) -> dict[str, Any]:
    """
    Глубокая трассировка расчёта по цепочке:
    raw -> order_fact -> daily_fact -> window/category_window.
    """
    date_from = as_of_date - timedelta(days=max(window_days, 1) - 1)

    raw_scope = OlapSalesRawLine.objects.filter(
        guest_id=guest_id,
        business_date__gte=date_from,
        business_date__lte=as_of_date,
    )
    if department_id:
        raw_scope = raw_scope.filter(department_id=department_id)

    raw_scope_values = raw_scope.values(
        "business_date",
        "department_id",
        "order_number",
        "uniq_order_id",
        "dish_code",
        "dish_name",
        "dish_sum_after_discount",
        "dish_sum_before_discount",
    ).order_by("business_date", "department_id", "order_number")

    raw_line_count = 0
    raw_order_keys: set[tuple[date, str, int, str]] = set()
    raw_visit_dates: set[date] = set()
    raw_sum_after = Decimal("0")
    raw_sum_before = Decimal("0")
    raw_sample_rows: list[dict[str, Any]] = []
    for row in raw_scope_values.iterator(chunk_size=2000):
        raw_line_count += 1
        raw_sum_after += _to_decimal(row.get("dish_sum_after_discount"))
        raw_sum_before += _to_decimal(row.get("dish_sum_before_discount"))
        order_key = _order_key_from_parts(
            business_date=row.get("business_date"),
            department_id=row.get("department_id"),
            order_number=row.get("order_number"),
            uniq_order_id=row.get("uniq_order_id"),
        )
        if order_key is not None:
            raw_order_keys.add(order_key)
            raw_visit_dates.add(order_key[0])
        if len(raw_sample_rows) < max_rows:
            raw_sample_rows.append(
                {
                    "business_date": row["business_date"].isoformat() if row.get("business_date") else "",
                    "department_id": _norm_text(row.get("department_id")),
                    "order_number": int(row["order_number"]) if row.get("order_number") is not None else None,
                    "uniq_order_id": _norm_text(row.get("uniq_order_id")),
                    "dish_code": _norm_text(row.get("dish_code")),
                    "dish_name": _norm_text(row.get("dish_name")),
                    "dish_sum_after_discount": _serialize_decimal(row.get("dish_sum_after_discount")),
                    "dish_sum_before_discount": _serialize_decimal(row.get("dish_sum_before_discount")),
                }
            )

    order_fact_scope = OrderFact.objects.filter(
        guest_id=guest_id,
        business_date__gte=date_from,
        business_date__lte=as_of_date,
    )
    if department_id:
        order_fact_scope = order_fact_scope.filter(department_id=department_id)

    order_fact_values = order_fact_scope.values(
        "business_date",
        "department_id",
        "order_number",
        "uniq_order_id",
        "net_sum",
        "gross_sum",
        "bonus_sum",
        "items_count",
        "categories_count",
    ).order_by("business_date", "department_id", "order_number", "uniq_order_id")

    order_fact_count = 0
    order_fact_net_total = Decimal("0")
    order_fact_bonus_total = Decimal("0")
    order_fact_visit_dates: set[date] = set()
    order_fact_rows_sample: list[dict[str, Any]] = []
    for row in order_fact_values.iterator(chunk_size=2000):
        order_fact_count += 1
        order_fact_net_total += _to_decimal(row.get("net_sum"))
        order_fact_bonus_total += _to_decimal(row.get("bonus_sum"))
        key = _order_key_from_parts(
            business_date=row.get("business_date"),
            department_id=row.get("department_id"),
            order_number=row.get("order_number"),
            uniq_order_id=row.get("uniq_order_id"),
        )
        if key is not None:
            order_fact_visit_dates.add(key[0])
        if len(order_fact_rows_sample) < max_rows:
            order_fact_rows_sample.append(
                {
                    "business_date": row["business_date"].isoformat() if row.get("business_date") else "",
                    "department_id": _norm_text(row.get("department_id")),
                    "order_number": int(row["order_number"]) if row.get("order_number") is not None else None,
                    "uniq_order_id": _norm_text(row.get("uniq_order_id")),
                    "net_sum": _serialize_decimal(row.get("net_sum")),
                    "gross_sum": _serialize_decimal(row.get("gross_sum")),
                    "bonus_sum": _serialize_decimal(row.get("bonus_sum")),
                    "items_count": int(row.get("items_count") or 0),
                    "categories_count": int(row.get("categories_count") or 0),
                }
            )

    daily_scope = GuestRestaurantDailyCategoryFact.objects.filter(
        guest_id=guest_id,
        business_date__gte=date_from,
        business_date__lte=as_of_date,
    )
    if department_id:
        daily_scope = daily_scope.filter(department_id=department_id)
    if focus is not None:
        daily_scope = daily_scope.filter(focus_category_id=focus.id)

    daily_rows = list(
        daily_scope.values(
            "business_date",
            "department_id",
            "focus_category_id",
            "orders_count",
            "items_count",
            "sum_net",
            "sum_gross",
            "bonus_sum",
        ).order_by("business_date", "department_id")[:max_rows]
    )
    daily_agg = daily_scope.aggregate(
        rows_count=Count("id"),
        orders_total=Sum("orders_count"),
        items_total=Sum("items_count"),
        sum_net_total=Sum("sum_net"),
        sum_gross_total=Sum("sum_gross"),
        bonus_total=Sum("bonus_sum"),
    )

    focus_trace: dict[str, Any] = {
        "focus_selected": focus is not None,
        "focus_category_id": int(focus.id) if focus else None,
        "focus_category_code": _norm_text(focus.code) if focus else "",
        "focus_category_name": _norm_text(focus.name) if focus else "",
        "resolved_dish_codes_count": 0,
        "resolved_dish_codes_sample": [],
        "raw_focus_scope": {},
        "order_fact_focus_scope": {},
        "recomputed_category_window": {},
    }

    if focus is not None:
        dish_codes = _collect_focus_dish_codes(focus)
        focus_trace["resolved_dish_codes_count"] = len(dish_codes)
        focus_trace["resolved_dish_codes_sample"] = dish_codes[:max_rows]

        raw_focus_scope = raw_scope.filter(dish_code__in=dish_codes) if dish_codes else raw_scope.none()
        raw_focus_values = raw_focus_scope.values(
            "business_date",
            "department_id",
            "order_number",
            "uniq_order_id",
            "dish_code",
            "dish_name",
            "dish_sum_after_discount",
            "dish_sum_before_discount",
        ).order_by("business_date", "department_id", "order_number")

        raw_focus_line_count = 0
        raw_focus_sum_net = Decimal("0")
        raw_focus_order_keys: set[tuple[date, str, int, str]] = set()
        raw_focus_visit_dates: set[date] = set()
        raw_focus_rows_sample: list[dict[str, Any]] = []
        for row in raw_focus_values.iterator(chunk_size=2000):
            raw_focus_line_count += 1
            raw_focus_sum_net += _line_net_value(row)
            order_key = _order_key_from_parts(
                business_date=row.get("business_date"),
                department_id=row.get("department_id"),
                order_number=row.get("order_number"),
                uniq_order_id=row.get("uniq_order_id"),
            )
            if order_key is not None:
                raw_focus_order_keys.add(order_key)
                raw_focus_visit_dates.add(order_key[0])
            if len(raw_focus_rows_sample) < max_rows:
                raw_focus_rows_sample.append(
                    {
                        "business_date": row["business_date"].isoformat() if row.get("business_date") else "",
                        "department_id": _norm_text(row.get("department_id")),
                        "order_number": int(row["order_number"]) if row.get("order_number") is not None else None,
                        "uniq_order_id": _norm_text(row.get("uniq_order_id")),
                        "dish_code": _norm_text(row.get("dish_code")),
                        "dish_name": _norm_text(row.get("dish_name")),
                        "line_net": _serialize_decimal(_line_net_value(row)),
                    }
                )

        focus_business_dates = {key[0] for key in raw_focus_order_keys}
        focus_department_ids = {key[1] for key in raw_focus_order_keys}
        focus_order_numbers = {key[2] for key in raw_focus_order_keys}
        focus_uniq_ids = {key[3] for key in raw_focus_order_keys}
        order_fact_focus_scope = OrderFact.objects.none()
        if raw_focus_order_keys:
            order_fact_focus_scope = OrderFact.objects.filter(
                guest_id=guest_id,
                business_date__in=focus_business_dates,
                department_id__in=focus_department_ids,
                order_number__in=focus_order_numbers,
                uniq_order_id__in=focus_uniq_ids,
            )

        order_fact_focus_values = order_fact_focus_scope.values(
            "business_date",
            "department_id",
            "order_number",
            "uniq_order_id",
            "net_sum",
            "bonus_sum",
        ).order_by("business_date", "department_id", "order_number", "uniq_order_id")

        order_fact_focus_count = 0
        order_fact_focus_sum_net = Decimal("0")
        order_fact_focus_bonus_in = Decimal("0")
        order_fact_focus_bonus_out = Decimal("0")
        order_fact_focus_keys: set[tuple[date, str, int, str]] = set()
        order_fact_focus_visit_dates: set[date] = set()
        order_fact_focus_rows_sample: list[dict[str, Any]] = []
        for row in order_fact_focus_values.iterator(chunk_size=2000):
            key = _order_key_from_parts(
                business_date=row.get("business_date"),
                department_id=row.get("department_id"),
                order_number=row.get("order_number"),
                uniq_order_id=row.get("uniq_order_id"),
            )
            if key is None or key not in raw_focus_order_keys:
                continue

            order_fact_focus_count += 1
            order_fact_focus_keys.add(key)
            order_fact_focus_visit_dates.add(key[0])
            net_sum = _to_decimal(row.get("net_sum"))
            bonus_sum = _to_decimal(row.get("bonus_sum"))
            order_fact_focus_sum_net += net_sum
            if bonus_sum >= 0:
                order_fact_focus_bonus_in += bonus_sum
            else:
                order_fact_focus_bonus_out += abs(bonus_sum)

            if len(order_fact_focus_rows_sample) < max_rows:
                order_fact_focus_rows_sample.append(
                    {
                        "business_date": row["business_date"].isoformat() if row.get("business_date") else "",
                        "department_id": _norm_text(row.get("department_id")),
                        "order_number": int(row["order_number"]) if row.get("order_number") is not None else None,
                        "uniq_order_id": _norm_text(row.get("uniq_order_id")),
                        "net_sum": _serialize_decimal(net_sum),
                        "bonus_sum": _serialize_decimal(bonus_sum),
                    }
                )

        missing_order_fact_keys = [
            {
                "business_date": key[0].isoformat(),
                "department_id": key[1],
                "order_number": key[2],
                "uniq_order_id": key[3],
            }
            for key in sorted(raw_focus_order_keys - order_fact_focus_keys)[:max_rows]
        ]

        recomputed_orders_count = len(raw_focus_order_keys)
        recomputed_visits_count = len(raw_focus_visit_dates)
        recomputed_sum_net = order_fact_focus_sum_net
        recomputed_avg_check = (
            (recomputed_sum_net / Decimal(recomputed_orders_count))
            if recomputed_orders_count > 0
            else Decimal("0")
        ).quantize(Decimal("0.01"))
        recomputed_rating = _compute_rating(
            orders_count=recomputed_orders_count,
            visits_count=recomputed_visits_count,
            avg_check_net=recomputed_avg_check,
        )

        focus_trace["raw_focus_scope"] = {
            "line_count": raw_focus_line_count,
            "orders_count_raw": len(raw_focus_order_keys),
            "visits_count_raw": len(raw_focus_visit_dates),
            "sum_focus_net_raw_lines": _serialize_decimal(raw_focus_sum_net),
            "rows_sample": raw_focus_rows_sample,
        }
        focus_trace["order_fact_focus_scope"] = {
            "orders_count_fact": order_fact_focus_count,
            "visits_count_fact": len(order_fact_focus_visit_dates),
            "sum_net_full_checks": _serialize_decimal(order_fact_focus_sum_net),
            "bonus_in_sum": _serialize_decimal(order_fact_focus_bonus_in),
            "bonus_out_sum": _serialize_decimal(order_fact_focus_bonus_out),
            "missing_order_fact_keys_count": len(raw_focus_order_keys - order_fact_focus_keys),
            "missing_order_fact_keys_sample": missing_order_fact_keys,
            "rows_sample": order_fact_focus_rows_sample,
        }
        focus_trace["recomputed_category_window"] = {
            "orders_count": recomputed_orders_count,
            "visits_count": recomputed_visits_count,
            "sum_net": _serialize_decimal(recomputed_sum_net),
            "avg_check_net": _serialize_decimal(recomputed_avg_check),
            "rating_score": _serialize_decimal(recomputed_rating),
        }

    return {
        "range": {
            "date_from": date_from.isoformat(),
            "date_to": as_of_date.isoformat(),
            "window_days": int(window_days),
            "department_id": department_id,
        },
        "raw_scope": {
            "line_count": raw_line_count,
            "orders_count_raw": len(raw_order_keys),
            "visits_count_raw": len(raw_visit_dates),
            "sum_after_discount": _serialize_decimal(raw_sum_after),
            "sum_before_discount": _serialize_decimal(raw_sum_before),
            "rows_sample": raw_sample_rows,
        },
        "order_fact_scope": {
            "orders_count_fact": order_fact_count,
            "visits_count_fact": len(order_fact_visit_dates),
            "sum_net_total": _serialize_decimal(order_fact_net_total),
            "bonus_sum_total": _serialize_decimal(order_fact_bonus_total),
            "rows_sample": order_fact_rows_sample,
        },
        "daily_fact_scope": {
            "rows_count": int(daily_agg.get("rows_count") or 0),
            "orders_total": int(daily_agg.get("orders_total") or 0),
            "items_total": int(daily_agg.get("items_total") or 0),
            "sum_net_total": _serialize_decimal(daily_agg.get("sum_net_total")),
            "sum_gross_total": _serialize_decimal(daily_agg.get("sum_gross_total")),
            "bonus_total": _serialize_decimal(daily_agg.get("bonus_total")),
            "rows_sample": [
                {
                    "business_date": row["business_date"].isoformat() if row.get("business_date") else "",
                    "department_id": _norm_text(row.get("department_id")),
                    "focus_category_id": int(row.get("focus_category_id") or 0),
                    "orders_count": int(row.get("orders_count") or 0),
                    "items_count": int(row.get("items_count") or 0),
                    "sum_net": _serialize_decimal(row.get("sum_net")),
                    "sum_gross": _serialize_decimal(row.get("sum_gross")),
                    "bonus_sum": _serialize_decimal(row.get("bonus_sum")),
                }
                for row in daily_rows
            ],
        },
        "focus_scope": focus_trace,
    }


def _build_payload_with_limit(
    *,
    as_of_date: date | None,
    window_days: int,
    department_id: str,
    segment_code: str,
    focus_category_code: str,
    complex_filters: list[dict[str, str]],
    selected_limit: int,
) -> dict[str, Any]:
    """
    Собирает payload workbench с временной подменой лимита selected_guests.

    Это позволяет использовать ту же бизнес-логику, что и UI, но с расширенным
    лимитом для точечной сверки телефонов.
    """
    previous_limit = guest_workbench.SELECTED_GUESTS_LIMIT
    guest_workbench.SELECTED_GUESTS_LIMIT = selected_limit
    try:
        return guest_workbench.build_guest_workbench_payload(
            as_of_date=as_of_date,
            window_days=window_days,
            department_id=department_id,
            segment_code=segment_code,
            focus_category_code=focus_category_code,
            complex_filters=complex_filters,
        )
    finally:
        guest_workbench.SELECTED_GUESTS_LIMIT = previous_limit


class Command(BaseCommand):
    help = (
        "Проверка метрик workbench по телефону(ам): строит payload с выбранными "
        "фильтрами и сверяет строки гостей с активным слоем БД (window/category_window)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--phone",
            action="append",
            default=[],
            help="Телефон гостя (можно указывать несколько раз).",
        )
        parser.add_argument(
            "--as-of-date",
            default="",
            help="Дата среза YYYY-MM-DD. По умолчанию используется максимальная доступная дата слоя window.",
        )
        parser.add_argument("--window-days", type=int, default=30, help="Окно в днях (7/14/30/60/180).")
        parser.add_argument("--department-id", default="", help="Department.Id. Пусто = все заведения.")
        parser.add_argument("--segment-code", default="", help="Код сегмента workbench.")
        parser.add_argument("--focus-category-code", default="", help="Код фокусной категории.")
        parser.add_argument(
            "--cf-field",
            action="append",
            default=[],
            help="Поле сложного фильтра (orders_count/visits_count/sum_net/avg_check_net/rating_score).",
        )
        parser.add_argument(
            "--cf-op",
            action="append",
            default=[],
            help="Оператор сложного фильтра (gt/gte/lt/lte/eq).",
        )
        parser.add_argument(
            "--cf-value",
            action="append",
            default=[],
            help="Значение сложного фильтра.",
        )
        parser.add_argument(
            "--selected-limit",
            type=int,
            default=5000,
            help="Лимит selected_guests для расширенного payload (по умолчанию 5000).",
        )
        parser.add_argument(
            "--max-db-rows",
            type=int,
            default=15,
            help="Сколько DB-строк (window/category-window) показывать на гостя.",
        )
        parser.add_argument(
            "--status-mode",
            choices=("brief", "full"),
            default="brief",
            help="Режим отчёта: brief (кратко) или full (полная трассировка по слоям).",
        )
        parser.add_argument(
            "--max-full-rows",
            type=int,
            default=100,
            help="Лимит sample-строк на каждый слой в режиме full.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Завершать команду с ошибкой, если есть FAIL.",
        )
        parser.add_argument(
            "--output-format",
            choices=("pretty", "json"),
            default="pretty",
            help="Формат вывода.",
        )
        parser.add_argument(
            "--output-file",
            default="",
            help="Опционально сохранить полный JSON-отчёт в файл.",
        )

    def handle(self, *args, **options):
        phones = [_norm_text(x) for x in options["phone"] if _norm_text(x)]
        if not phones:
            raise CommandError("Укажите минимум один --phone.")

        raw_as_of = _norm_text(options["as_of_date"])
        as_of_date = _to_iso_date(raw_as_of, arg_name="--as-of-date") if raw_as_of else None

        selected_limit = int(options["selected_limit"] or 0)
        if selected_limit <= 0:
            raise CommandError("--selected-limit должен быть больше 0.")

        max_db_rows = max(1, int(options["max_db_rows"] or 1))
        status_mode = _norm_text(options["status_mode"]).lower() or "brief"
        max_full_rows = max(1, int(options["max_full_rows"] or 1))
        normalized_window_days = guest_workbench.normalize_window_days(options["window_days"])
        normalized_department_id = _norm_text(options["department_id"])
        normalized_segment_code = guest_workbench.normalize_segment_code(options["segment_code"])
        normalized_focus_code = _norm_text(options["focus_category_code"])

        cf_fields = [_norm_text(x) for x in options["cf_field"]]
        cf_ops = [_norm_text(x) for x in options["cf_op"]]
        cf_values = [_norm_text(x) for x in options["cf_value"]]
        if not (len(cf_fields) == len(cf_ops) == len(cf_values)):
            raise CommandError("Списки --cf-field/--cf-op/--cf-value должны быть одинаковой длины.")
        raw_complex_filters = [
            {"field": cf_fields[idx], "operator": cf_ops[idx], "value": cf_values[idx]}
            for idx in range(len(cf_fields))
        ]

        payload_default = guest_workbench.build_guest_workbench_payload(
            as_of_date=as_of_date,
            window_days=normalized_window_days,
            department_id=normalized_department_id,
            segment_code=normalized_segment_code,
            focus_category_code=normalized_focus_code,
            complex_filters=raw_complex_filters,
        )
        payload_expanded = _build_payload_with_limit(
            as_of_date=as_of_date,
            window_days=normalized_window_days,
            department_id=normalized_department_id,
            segment_code=normalized_segment_code,
            focus_category_code=normalized_focus_code,
            complex_filters=raw_complex_filters,
            selected_limit=selected_limit,
        )

        filters = payload_expanded.get("filters", {})
        layer = _norm_text(filters.get("metrics_layer"))
        target_as_of_raw = _norm_text(filters.get("as_of_date"))
        if not target_as_of_raw:
            raise CommandError("Не удалось определить as_of_date из payload.")
        target_as_of = _to_iso_date(target_as_of_raw, arg_name="payload.filters.as_of_date")
        target_focus_code = _norm_text(filters.get("focus_category_code"))
        target_department_id = _norm_text(filters.get("department_id"))

        focus_for_trace: FocusCategory | None = None
        if target_focus_code:
            focus_for_trace = FocusCategory.objects.filter(code=target_focus_code).first()
        if layer == "category_window" and target_focus_code and focus_for_trace is None:
            raise CommandError("В payload выбран слой category_window, но фокус-категория не найдена в БД.")

        selected_rows = payload_expanded.get("selected_guests", {}).get("rows", []) or []
        selected_rows_by_guest10: dict[str, list[dict[str, Any]]] = {}
        for item in selected_rows:
            p10 = _phone10(_norm_text(item.get("phone")))
            if not p10:
                continue
            selected_rows_by_guest10.setdefault(p10, []).append(item)

        default_rows = payload_default.get("selected_guests", {}).get("rows", []) or []
        default_phone10_set = {_phone10(_norm_text(item.get("phone"))) for item in default_rows if item.get("phone")}

        report_phones: list[dict[str, Any]] = []
        pass_count = 0
        fail_count = 0

        for input_phone in phones:
            phone10 = _phone10(input_phone)
            guest = _find_guest_by_phone(input_phone)
            phone_report: dict[str, Any] = {
                "input_phone": input_phone,
                "phone10": phone10,
                "guest_found": guest is not None,
                "guest_id": int(guest.id) if guest else None,
                "guest_phone": _norm_text(guest.phone) if guest else "",
                "in_default_payload_rows": bool(phone10 and phone10 in default_phone10_set),
                "status": "PASS",
                "selected_rows_for_phone": [],
                "selected_rows_count": 0,
                "checks": [],
                "window_rows": [],
                "category_window_rows": [],
            }

            if guest is None:
                phone_report["status"] = "FAIL_GUEST_NOT_FOUND"
                fail_count += 1
                report_phones.append(phone_report)
                continue

            if status_mode == "full":
                phone_report["full_trace"] = _build_full_trace_for_guest(
                    guest_id=int(guest.id),
                    as_of_date=target_as_of,
                    window_days=normalized_window_days,
                    department_id=target_department_id,
                    focus=focus_for_trace,
                    max_rows=max_full_rows,
                )

            window_qs = GuestRestaurantWindowMetrics.objects.filter(
                as_of_date=target_as_of,
                guest_id=guest.id,
            )
            if target_department_id:
                window_qs = window_qs.filter(department_id=target_department_id)
            window_rows = list(window_qs.order_by("department_id", "-window_days")[:max_db_rows])
            phone_report["window_rows"] = [_serialize_metric_row(row) for row in window_rows]

            category_qs = GuestRestaurantWindowCategoryMetrics.objects.filter(
                as_of_date=target_as_of,
                guest_id=guest.id,
            )
            if target_department_id:
                category_qs = category_qs.filter(department_id=target_department_id)
            if focus_for_trace is not None:
                category_qs = category_qs.filter(focus_category_id=focus_for_trace.id)
            category_rows = list(category_qs.order_by("department_id", "-window_days")[:max_db_rows])
            phone_report["category_window_rows"] = [
                {
                    **_serialize_metric_row(row),
                    "focus_category_id": int(row.focus_category_id),
                }
                for row in category_rows
            ]

            matched_rows = selected_rows_by_guest10.get(phone10, [])
            phone_report["selected_rows_count"] = len(matched_rows)
            phone_report["selected_rows_for_phone"] = matched_rows
            if not matched_rows:
                phone_report["status"] = "FAIL_NOT_IN_SELECTION"
                fail_count += 1
                report_phones.append(phone_report)
                continue

            candidate = matched_rows[0]
            candidate_department_id = _norm_text(candidate.get("department_id"))
            candidate_window_days = int(candidate.get("source_window_days") or filters.get("window_days") or 0)
            active_db_row: GuestRestaurantWindowMetrics | GuestRestaurantWindowCategoryMetrics | None
            if layer == "category_window":
                active_qs = GuestRestaurantWindowCategoryMetrics.objects.filter(
                    as_of_date=target_as_of,
                    guest_id=guest.id,
                    department_id=candidate_department_id,
                    window_days=candidate_window_days,
                )
                if focus_for_trace is not None:
                    active_qs = active_qs.filter(focus_category_id=focus_for_trace.id)
                active_db_row = active_qs.first()
            else:
                active_db_row = GuestRestaurantWindowMetrics.objects.filter(
                    as_of_date=target_as_of,
                    guest_id=guest.id,
                    department_id=candidate_department_id,
                    window_days=candidate_window_days,
                ).first()

            if active_db_row is None:
                phone_report["status"] = "FAIL_DB_ROW_NOT_FOUND"
                fail_count += 1
                report_phones.append(phone_report)
                continue

            phone_report["active_db_row"] = _serialize_metric_row(active_db_row)
            comparisons = [
                (
                    "orders_count",
                    int(candidate.get("orders_count") or 0),
                    int(active_db_row.orders_count or 0),
                ),
                (
                    "visits_count",
                    int(candidate.get("visits_count") or 0),
                    int(active_db_row.visits_count or 0),
                ),
                (
                    "sum_net",
                    _parse_ui_decimal(candidate.get("sum_net")),
                    Decimal(str(active_db_row.sum_net or 0)),
                ),
                (
                    "avg_check_net",
                    _parse_ui_decimal(candidate.get("avg_check_net")),
                    Decimal(str(active_db_row.avg_check_net or 0)),
                ),
                (
                    "rating_score",
                    _parse_ui_decimal(candidate.get("rating_score")),
                    Decimal(str(active_db_row.rating_score or 0)),
                ),
            ]

            checks: list[dict[str, Any]] = []
            is_phone_ok = True
            for metric, payload_value, db_value in comparisons:
                payload_norm = Decimal(str(payload_value)).quantize(Decimal("0.01"))
                db_norm = Decimal(str(db_value)).quantize(Decimal("0.01"))
                is_ok = payload_norm == db_norm
                checks.append(
                    {
                        "metric": metric,
                        "ok": is_ok,
                        "payload": _serialize_decimal(payload_norm),
                        "db": _serialize_decimal(db_norm),
                    }
                )
                if not is_ok:
                    is_phone_ok = False

            phone_report["checks"] = checks
            phone_report["status"] = "PASS" if is_phone_ok else "FAIL_VALUE_MISMATCH"
            if is_phone_ok:
                pass_count += 1
            else:
                fail_count += 1
            report_phones.append(phone_report)

        result_status = "PASS" if fail_count == 0 else "FAIL"
        report = {
            "flags": {
                "WORKBENCH_CATEGORY_WINDOW_METRICS_V2": bool(
                    getattr(settings, "WORKBENCH_CATEGORY_WINDOW_METRICS_V2", False)
                )
            },
            "input": {
                "phones": phones,
                "as_of_date": as_of_date.isoformat() if as_of_date else "",
                "window_days": normalized_window_days,
                "department_id": normalized_department_id,
                "segment_code": normalized_segment_code,
                "focus_category_code": normalized_focus_code,
                "raw_complex_filters": raw_complex_filters,
                "selected_limit": selected_limit,
                "status_mode": status_mode,
                "max_full_rows": max_full_rows,
            },
            "payload": {
                "as_of_date": target_as_of.isoformat(),
                "metrics_layer": layer,
                "department_id": target_department_id,
                "segment_code": _norm_text(filters.get("segment_code")),
                "focus_category_code": target_focus_code,
                "complex_filters": filters.get("complex_filters", []),
                "default_selected_total": int(payload_default.get("selected_guests", {}).get("total") or 0),
                "default_selected_rows": len(payload_default.get("selected_guests", {}).get("rows", []) or []),
                "expanded_selected_total": int(payload_expanded.get("selected_guests", {}).get("total") or 0),
                "expanded_selected_rows": len(selected_rows),
                "expanded_is_truncated": bool(
                    payload_expanded.get("selected_guests", {}).get("is_truncated")
                ),
            },
            "phones": report_phones,
            "result": {
                "status": result_status,
                "pass_count": pass_count,
                "fail_count": fail_count,
            },
        }

        output_file = _norm_text(options["output_file"])
        if output_file:
            file_path = Path(output_file)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

        output_format = _norm_text(options["output_format"]).lower() or "pretty"
        if output_format == "json":
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            self.stdout.write(self.style.SUCCESS("audit_workbench_phone_metrics: готово"))
            self.stdout.write(
                "payload: "
                f"as_of={report['payload']['as_of_date']} "
                f"layer={report['payload']['metrics_layer']} "
                f"default_selected={report['payload']['default_selected_total']} "
                f"expanded_selected={report['payload']['expanded_selected_total']}"
            )
            for item in report_phones:
                self.stdout.write("")
                self.stdout.write(f"[PHONE] {item['input_phone']} -> {item['status']}")
                if not item["guest_found"]:
                    continue
                self.stdout.write(
                    f"guest_id={item['guest_id']} guest_phone={item['guest_phone']} "
                    f"in_default_rows={item['in_default_payload_rows']} "
                    f"selected_rows={item['selected_rows_count']}"
                )
                for check in item.get("checks", []):
                    status = "PASS" if check["ok"] else "FAIL"
                    self.stdout.write(
                        f"  [{status}] {check['metric']}: payload={check['payload']} db={check['db']}"
                    )
            self.stdout.write("")
            self.stdout.write(
                f"[RESULT] {report['result']['status']} pass={report['result']['pass_count']} fail={report['result']['fail_count']}"
            )
            if output_file:
                self.stdout.write(f"saved_json={output_file}")

        if options["strict"] and fail_count > 0:
            raise CommandError("Проверка завершилась с ошибками (есть FAIL).")
