from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from guests.models import (
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    GuestRestaurantDailyCategoryFact,
    OlapSalesRawLine,
    TerminalDepartmentMap,
)


def _parse_iso_date(raw: str, *, arg_name: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise CommandError(f"Некорректный формат даты в {arg_name}: {raw!r}. Ожидается YYYY-MM-DD.") from exc


def _to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _serialize_decimal(value: Decimal | int | float | None) -> str:
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _serialize_date(value: date | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


@dataclass
class _ExpectedDailyAggregate:
    business_date: date
    guest_id: int
    department_id: str
    orders_set: set[int] = field(default_factory=set)
    items_count: int = 0
    sum_gross: Decimal = Decimal("0")
    sum_net: Decimal = Decimal("0")
    bonus_sum: Decimal = Decimal("0")

    @property
    def orders_count(self) -> int:
        return len(self.orders_set)


@dataclass
class _DepartmentSummary:
    department_id: str
    department_name: str = ""
    rows: int = 0
    guests_set: set[int] = field(default_factory=set)
    orders_count: int = 0
    items_count: int = 0
    sum_gross: Decimal = Decimal("0")
    sum_net: Decimal = Decimal("0")
    bonus_sum: Decimal = Decimal("0")

    @property
    def guests_count(self) -> int:
        return len(self.guests_set)


class Command(BaseCommand):
    help = (
        "Аудит daily-слоя по одной целевой категории: "
        "сравнение ожидаемого состояния (raw + текущий resolved-состав) и факта в guest_restaurant_daily_category_fact."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--focus-code",
            default="",
            help="Технический код целевой категории (например focus-virtual-1).",
        )
        parser.add_argument(
            "--focus-id",
            type=int,
            default=None,
            help="ID целевой категории (альтернатива --focus-code).",
        )
        parser.add_argument(
            "--as-of-date",
            default="",
            help="Дата среза (YYYY-MM-DD). По умолчанию: localdate().",
        )
        parser.add_argument(
            "--window-days",
            type=int,
            default=180,
            help="Размер окна в днях (по умолчанию 180).",
        )
        parser.add_argument(
            "--department-id",
            action="append",
            default=[],
            help="Ограничить проверку конкретными Department.Id (можно указывать несколько раз).",
        )
        parser.add_argument(
            "--max-samples",
            type=int,
            default=20,
            help="Максимум строк в примерах расхождений.",
        )
        parser.add_argument(
            "--output-format",
            choices=("pretty", "json"),
            default="pretty",
            help="Формат вывода: human-readable или JSON.",
        )
        parser.add_argument(
            "--output-file",
            default="",
            help="Опциональный путь для сохранения полного JSON-отчёта.",
        )

    def handle(self, *args, **options):
        focus_code = _normalize_text(options["focus_code"])
        focus_id = options["focus_id"]
        if not focus_code and not focus_id:
            raise CommandError("Укажите --focus-code или --focus-id.")

        as_of_date_raw = _normalize_text(options["as_of_date"])
        as_of_date = _parse_iso_date(as_of_date_raw, arg_name="--as-of-date") if as_of_date_raw else timezone.localdate()
        window_days = int(options["window_days"] or 0)
        if window_days <= 0:
            raise CommandError("--window-days должен быть больше 0.")

        max_samples = max(0, int(options["max_samples"]))
        department_ids = sorted({_normalize_text(x) for x in options["department_id"] if _normalize_text(x)})

        focus_qs = FocusCategory.objects.select_related("olap_category", "virtual_category").all()
        if focus_id:
            focus_qs = focus_qs.filter(id=focus_id)
        if focus_code:
            focus_qs = focus_qs.filter(code=focus_code)
        focus = focus_qs.first()
        if focus is None:
            raise CommandError("Целевая категория не найдена по переданным фильтрам.")

        date_from = as_of_date - timedelta(days=window_days - 1)
        date_to = as_of_date

        terminal_map_by_dept = {
            _normalize_text(row["department_id"]): _normalize_text(row["department_name"])
            for row in TerminalDepartmentMap.objects.filter(is_active=True)
            .exclude(department_id__isnull=True)
            .exclude(department_id="")
            .values("department_id", "department_name")
        }

        resolved_rows = list(
            FocusCategoryNomenclatureResolved.objects.filter(
                focus_category_id=focus.id,
                nomenclature__is_active=True,
            )
            .select_related("nomenclature", "nomenclature__olap_category")
            .values(
                "nomenclature__iiko_nomenclature_external_id",
                "nomenclature__nomenclature_name",
                "nomenclature__dish_group_name",
                "nomenclature__olap_category__iiko_category_external_id",
                "nomenclature__olap_category__category_name",
                "source_reason",
            )
            .order_by("nomenclature__nomenclature_name")
        )
        resolved_codes = sorted(
            {
                _normalize_text(row["nomenclature__iiko_nomenclature_external_id"])
                for row in resolved_rows
                if _normalize_text(row["nomenclature__iiko_nomenclature_external_id"])
            }
        )

        raw_qs = OlapSalesRawLine.objects.filter(
            business_date__gte=date_from,
            business_date__lte=date_to,
            guest_id__isnull=False,
            order_number__isnull=False,
        )
        if resolved_codes:
            raw_qs = raw_qs.filter(dish_code__in=resolved_codes)
        else:
            raw_qs = raw_qs.filter(pk__in=[])
        if department_ids:
            raw_qs = raw_qs.filter(department_id__in=department_ids)

        raw_values = raw_qs.values(
            "id",
            "business_date",
            "guest_id",
            "department_id",
            "department_name",
            "order_number",
            "dish_code",
            "dish_name",
            "dish_sum_before_discount",
            "dish_sum_after_discount",
            "bonus_sum",
        )

        expected_by_key: dict[tuple[date, int, str], _ExpectedDailyAggregate] = {}
        expected_by_department: dict[str, _DepartmentSummary] = {}
        dish_presence_by_department: dict[tuple[str, str], dict[str, Any]] = {}
        raw_rows_count = 0

        for row in raw_values.iterator(chunk_size=2000):
            raw_rows_count += 1
            business_day = row["business_date"]
            guest_id = int(row["guest_id"])
            department_id = _normalize_text(row["department_id"])
            department_name = _normalize_text(row["department_name"])
            order_number = int(row["order_number"])
            dish_code = _normalize_text(row["dish_code"])
            dish_name = _normalize_text(row["dish_name"])

            key = (business_day, guest_id, department_id)
            expected = expected_by_key.get(key)
            if expected is None:
                expected = _ExpectedDailyAggregate(
                    business_date=business_day,
                    guest_id=guest_id,
                    department_id=department_id,
                )
                expected_by_key[key] = expected
            expected.orders_set.add(order_number)
            expected.items_count += 1

            gross = _to_decimal(row["dish_sum_before_discount"])
            net = _to_decimal(row["dish_sum_after_discount"])
            if net == Decimal("0"):
                net = gross
            bonus = _to_decimal(row["bonus_sum"])

            expected.sum_gross += gross
            expected.sum_net += net
            expected.bonus_sum += bonus

            dept_stats = expected_by_department.get(department_id)
            if dept_stats is None:
                dept_stats = _DepartmentSummary(
                    department_id=department_id,
                    department_name=department_name or terminal_map_by_dept.get(department_id, ""),
                )
                expected_by_department[department_id] = dept_stats
            dept_stats.department_name = dept_stats.department_name or department_name or terminal_map_by_dept.get(
                department_id, ""
            )
            dept_stats.guests_set.add(guest_id)
            dept_stats.items_count += 1
            dept_stats.sum_gross += gross
            dept_stats.sum_net += net
            dept_stats.bonus_sum += bonus

            dish_key = (department_id, dish_code)
            dish_info = dish_presence_by_department.get(dish_key)
            if dish_info is None:
                dish_info = {
                    "department_id": department_id,
                    "department_name": dept_stats.department_name,
                    "dish_code": dish_code,
                    "dish_name": dish_name,
                    "rows": 0,
                    "sum_net": Decimal("0"),
                }
                dish_presence_by_department[dish_key] = dish_info
            dish_info["rows"] += 1
            dish_info["sum_net"] += net

        for expected in expected_by_key.values():
            dept_stats = expected_by_department.setdefault(
                expected.department_id,
                _DepartmentSummary(
                    department_id=expected.department_id,
                    department_name=terminal_map_by_dept.get(expected.department_id, ""),
                ),
            )
            dept_stats.rows += 1
            dept_stats.orders_count += expected.orders_count

        actual_qs = GuestRestaurantDailyCategoryFact.objects.filter(
            focus_category_id=focus.id,
            business_date__gte=date_from,
            business_date__lte=date_to,
        )
        if department_ids:
            actual_qs = actual_qs.filter(department_id__in=department_ids)

        actual_by_key: dict[tuple[date, int, str], dict[str, Any]] = {}
        actual_by_department: dict[str, _DepartmentSummary] = {}

        for row in actual_qs.values(
            "business_date",
            "guest_id",
            "department_id",
            "orders_count",
            "items_count",
            "sum_gross",
            "sum_net",
            "bonus_sum",
        ).iterator(chunk_size=2000):
            business_day = row["business_date"]
            guest_id = int(row["guest_id"])
            department_id = _normalize_text(row["department_id"])
            key = (business_day, guest_id, department_id)
            payload = {
                "orders_count": int(row["orders_count"] or 0),
                "items_count": int(row["items_count"] or 0),
                "sum_gross": _to_decimal(row["sum_gross"]),
                "sum_net": _to_decimal(row["sum_net"]),
                "bonus_sum": _to_decimal(row["bonus_sum"]),
            }
            actual_by_key[key] = payload

            dept_stats = actual_by_department.get(department_id)
            if dept_stats is None:
                dept_stats = _DepartmentSummary(
                    department_id=department_id,
                    department_name=terminal_map_by_dept.get(department_id, ""),
                )
                actual_by_department[department_id] = dept_stats
            dept_stats.rows += 1
            dept_stats.guests_set.add(guest_id)
            dept_stats.orders_count += payload["orders_count"]
            dept_stats.items_count += payload["items_count"]
            dept_stats.sum_gross += payload["sum_gross"]
            dept_stats.sum_net += payload["sum_net"]
            dept_stats.bonus_sum += payload["bonus_sum"]

        expected_keys = set(expected_by_key.keys())
        actual_keys = set(actual_by_key.keys())
        missing_keys = sorted(expected_keys - actual_keys)
        stale_keys = sorted(actual_keys - expected_keys)

        mismatch_keys: list[tuple[date, int, str]] = []
        for key in sorted(expected_keys & actual_keys):
            expected = expected_by_key[key]
            actual = actual_by_key[key]
            if (
                expected.orders_count != actual["orders_count"]
                or expected.items_count != actual["items_count"]
                or expected.sum_gross != actual["sum_gross"]
                or expected.sum_net != actual["sum_net"]
                or expected.bonus_sum != actual["bonus_sum"]
            ):
                mismatch_keys.append(key)

        def _key_payload(key: tuple[date, int, str]) -> dict[str, Any]:
            return {
                "business_date": _serialize_date(key[0]),
                "guest_id": key[1],
                "department_id": key[2],
                "department_name": terminal_map_by_dept.get(key[2], ""),
            }

        missing_sample = [_key_payload(key) for key in missing_keys[:max_samples]]
        stale_sample = [_key_payload(key) for key in stale_keys[:max_samples]]

        mismatch_sample: list[dict[str, Any]] = []
        for key in mismatch_keys[:max_samples]:
            expected = expected_by_key[key]
            actual = actual_by_key[key]
            mismatch_sample.append(
                {
                    **_key_payload(key),
                    "expected": {
                        "orders_count": expected.orders_count,
                        "items_count": expected.items_count,
                        "sum_gross": _serialize_decimal(expected.sum_gross),
                        "sum_net": _serialize_decimal(expected.sum_net),
                        "bonus_sum": _serialize_decimal(expected.bonus_sum),
                    },
                    "actual": {
                        "orders_count": actual["orders_count"],
                        "items_count": actual["items_count"],
                        "sum_gross": _serialize_decimal(actual["sum_gross"]),
                        "sum_net": _serialize_decimal(actual["sum_net"]),
                        "bonus_sum": _serialize_decimal(actual["bonus_sum"]),
                    },
                }
            )

        expected_totals = _DepartmentSummary(department_id="ALL")
        for item in expected_by_department.values():
            expected_totals.rows += item.rows
            expected_totals.guests_set.update(item.guests_set)
            expected_totals.orders_count += item.orders_count
            expected_totals.items_count += item.items_count
            expected_totals.sum_gross += item.sum_gross
            expected_totals.sum_net += item.sum_net
            expected_totals.bonus_sum += item.bonus_sum

        actual_totals = _DepartmentSummary(department_id="ALL")
        for item in actual_by_department.values():
            actual_totals.rows += item.rows
            actual_totals.guests_set.update(item.guests_set)
            actual_totals.orders_count += item.orders_count
            actual_totals.items_count += item.items_count
            actual_totals.sum_gross += item.sum_gross
            actual_totals.sum_net += item.sum_net
            actual_totals.bonus_sum += item.bonus_sum

        department_rows: list[dict[str, Any]] = []
        all_departments = sorted(set(expected_by_department.keys()) | set(actual_by_department.keys()))
        for department_id in all_departments:
            expected = expected_by_department.get(department_id, _DepartmentSummary(department_id=department_id))
            actual = actual_by_department.get(department_id, _DepartmentSummary(department_id=department_id))
            department_name = (
                expected.department_name
                or actual.department_name
                or terminal_map_by_dept.get(department_id, "")
            )
            department_rows.append(
                {
                    "department_id": department_id,
                    "department_name": department_name,
                    "expected_rows": expected.rows,
                    "actual_rows": actual.rows,
                    "expected_guests": expected.guests_count,
                    "actual_guests": actual.guests_count,
                    "expected_orders": expected.orders_count,
                    "actual_orders": actual.orders_count,
                    "expected_sum_net": _serialize_decimal(expected.sum_net),
                    "actual_sum_net": _serialize_decimal(actual.sum_net),
                }
            )

        dish_presence_rows = sorted(
            [
                {
                    "department_id": item["department_id"],
                    "department_name": item["department_name"],
                    "dish_code": item["dish_code"],
                    "dish_name": item["dish_name"],
                    "rows": item["rows"],
                    "sum_net": _serialize_decimal(item["sum_net"]),
                }
                for item in dish_presence_by_department.values()
            ],
            key=lambda x: (x["department_name"], x["dish_name"], x["dish_code"]),
        )

        report = {
            "focus_category": {
                "id": focus.id,
                "code": focus.code,
                "name": focus.name,
                "source_type": focus.source_type,
                "is_enabled": bool(focus.is_enabled),
                "priority_weight": int(focus.priority_weight),
                "tag_code": _normalize_text(focus.tag_code),
            },
            "scope": {
                "as_of_date": _serialize_date(as_of_date),
                "window_days": window_days,
                "business_date_from": _serialize_date(date_from),
                "business_date_to": _serialize_date(date_to),
                "department_ids_input": department_ids,
            },
            "resolved_nomenclature": {
                "count": len(resolved_codes),
                "items": [
                    {
                        "dish_code": _normalize_text(row["nomenclature__iiko_nomenclature_external_id"]),
                        "dish_name": _normalize_text(row["nomenclature__nomenclature_name"]),
                        "dish_group_name": _normalize_text(row["nomenclature__dish_group_name"]),
                        "olap_category_id": _normalize_text(row["nomenclature__olap_category__iiko_category_external_id"]),
                        "olap_category_name": _normalize_text(row["nomenclature__olap_category__category_name"]),
                        "source_reason": _normalize_text(row["source_reason"]),
                    }
                    for row in resolved_rows
                ],
            },
            "counts": {
                "raw_rows_in_scope": raw_rows_count,
                "expected_daily_rows": len(expected_by_key),
                "actual_daily_rows": len(actual_by_key),
                "missing_daily_rows": len(missing_keys),
                "stale_daily_rows": len(stale_keys),
                "mismatch_daily_rows": len(mismatch_keys),
            },
            "totals": {
                "expected_rows": expected_totals.rows,
                "actual_rows": actual_totals.rows,
                "expected_guests": expected_totals.guests_count,
                "actual_guests": actual_totals.guests_count,
                "expected_orders": expected_totals.orders_count,
                "actual_orders": actual_totals.orders_count,
                "expected_sum_net": _serialize_decimal(expected_totals.sum_net),
                "actual_sum_net": _serialize_decimal(actual_totals.sum_net),
                "expected_sum_gross": _serialize_decimal(expected_totals.sum_gross),
                "actual_sum_gross": _serialize_decimal(actual_totals.sum_gross),
            },
            "by_department": department_rows,
            "samples": {
                "missing": missing_sample,
                "stale": stale_sample,
                "mismatch": mismatch_sample,
            },
            "dish_presence_by_department": dish_presence_rows,
        }

        output_file = _normalize_text(options["output_file"])
        if output_file:
            path = Path(output_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        if options["output_format"] == "json":
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            return

        self.stdout.write(
            f"[focus] {focus.code} ({focus.name}), source={focus.source_type}, enabled={focus.is_enabled}"
        )
        self.stdout.write(
            f"[scope] as_of={as_of_date.isoformat()} window_days={window_days} range={date_from.isoformat()}..{date_to.isoformat()}"
        )
        self.stdout.write(
            (
                "[counts] raw_rows={raw_rows} expected_daily={expected_daily} actual_daily={actual_daily} "
                "missing={missing} stale={stale} mismatch={mismatch}"
            ).format(
                raw_rows=report["counts"]["raw_rows_in_scope"],
                expected_daily=report["counts"]["expected_daily_rows"],
                actual_daily=report["counts"]["actual_daily_rows"],
                missing=report["counts"]["missing_daily_rows"],
                stale=report["counts"]["stale_daily_rows"],
                mismatch=report["counts"]["mismatch_daily_rows"],
            )
        )
        self.stdout.write(
            (
                "[totals] guests expected/actual={eg}/{ag} orders expected/actual={eo}/{ao} "
                "sum_net expected/actual={en}/{an}"
            ).format(
                eg=report["totals"]["expected_guests"],
                ag=report["totals"]["actual_guests"],
                eo=report["totals"]["expected_orders"],
                ao=report["totals"]["actual_orders"],
                en=report["totals"]["expected_sum_net"],
                an=report["totals"]["actual_sum_net"],
            )
        )

        self.stdout.write("[by_department]")
        for row in department_rows:
            self.stdout.write(
                (
                    "  - {department} ({department_id}): guests {eg}/{ag}, orders {eo}/{ao}, "
                    "sum_net {en}/{an}, rows {er}/{ar}"
                ).format(
                    department=row["department_name"] or "-",
                    department_id=row["department_id"] or "-",
                    eg=row["expected_guests"],
                    ag=row["actual_guests"],
                    eo=row["expected_orders"],
                    ao=row["actual_orders"],
                    en=row["expected_sum_net"],
                    an=row["actual_sum_net"],
                    er=row["expected_rows"],
                    ar=row["actual_rows"],
                )
            )

        if output_file:
            self.stdout.write(f"[file] report_saved={output_file}")

