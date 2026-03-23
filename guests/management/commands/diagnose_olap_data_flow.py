import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, DecimalField, F, Max, Min, Q, Sum, Value
from django.db.models.functions import Coalesce

from guests.models import (
    GuestRestaurantDailyCategoryFact,
    GuestRestaurantWindowMetrics,
    OlapCheckSyncJournal,
    OlapSalesRawLine,
    OrderFact,
    TerminalDepartmentMap,
)


def _parse_iso_date(raw: str, *, arg_name: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise CommandError(f"Некорректный формат даты в {arg_name}: {raw!r}. Ожидается YYYY-MM-DD.") from exc


def _uniq_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _serialize_decimal(value: Any) -> str:
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


class Command(BaseCommand):
    help = (
        "Сквозная диагностика OLAP-контура за период и заведение: "
        "mapping -> journal -> raw -> order_fact -> daily_fact -> window_metrics."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date-from", required=True, help="Начало периода (YYYY-MM-DD).")
        parser.add_argument("--date-to", required=True, help="Конец периода (YYYY-MM-DD).")
        parser.add_argument(
            "--department-id",
            action="append",
            default=[],
            help="Department.Id (можно указывать несколько раз).",
        )
        parser.add_argument(
            "--terminal-group-id",
            action="append",
            default=[],
            help="terminalGroupId (можно указывать несколько раз).",
        )
        parser.add_argument(
            "--department-name",
            default="",
            help="Поиск заведения по имени (icontains).",
        )
        parser.add_argument(
            "--include-inactive-mapping",
            action="store_true",
            help="Учитывать неактивные записи mapping.",
        )
        parser.add_argument(
            "--max-journal-details",
            type=int,
            default=200,
            help="Максимум строк журнала в детальном выводе.",
        )
        parser.add_argument(
            "--max-order-details",
            type=int,
            default=500,
            help="Максимум агрегированных строк заказов в детальном выводе.",
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
            help="Опциональный путь для сохранения полного JSON-отчета.",
        )

    def handle(self, *args, **options):
        date_from = _parse_iso_date(options["date_from"], arg_name="--date-from")
        date_to = _parse_iso_date(options["date_to"], arg_name="--date-to")
        if date_from > date_to:
            raise CommandError("--date-from не может быть больше --date-to.")

        department_ids_input = _uniq_text(options["department_id"])
        terminal_ids_input = _uniq_text(options["terminal_group_id"])
        department_name = str(options["department_name"] or "").strip()
        include_inactive_mapping = bool(options["include_inactive_mapping"])
        max_journal_details = max(0, int(options["max_journal_details"]))
        max_order_details = max(0, int(options["max_order_details"]))

        if not (department_ids_input or terminal_ids_input or department_name):
            raise CommandError(
                "Укажите хотя бы один фильтр заведения: --department-id, --terminal-group-id или --department-name."
            )

        mapping_qs = TerminalDepartmentMap.objects.all()
        if not include_inactive_mapping:
            mapping_qs = mapping_qs.filter(is_active=True)
        if department_ids_input:
            mapping_qs = mapping_qs.filter(department_id__in=department_ids_input)
        if terminal_ids_input:
            mapping_qs = mapping_qs.filter(terminal_group_id__in=terminal_ids_input)
        if department_name:
            mapping_qs = mapping_qs.filter(department_name__icontains=department_name)
        mapping_qs = mapping_qs.order_by("id")

        mapping_rows = list(
            mapping_qs.values(
                "id",
                "organization_id",
                "terminal_group_id",
                "department_id",
                "department_code",
                "department_name",
                "is_active",
                "updated_at",
            )
        )

        resolved_department_ids = set(department_ids_input)
        resolved_terminal_ids = set(terminal_ids_input)
        resolved_department_ids.update(
            x
            for x in mapping_qs.values_list("department_id", flat=True)
            if str(x or "").strip()
        )
        resolved_terminal_ids.update(
            x
            for x in mapping_qs.values_list("terminal_group_id", flat=True)
            if str(x or "").strip()
        )

        raw_name_q = Q()
        if department_name:
            raw_name_q = Q(department_name__icontains=department_name)
            derived = (
                OlapSalesRawLine.objects.filter(
                    business_date__gte=date_from,
                    business_date__lte=date_to,
                )
                .filter(raw_name_q)
                .exclude(department_id__isnull=True)
                .exclude(department_id="")
                .values_list("department_id", flat=True)
                .distinct()
            )
            resolved_department_ids.update(str(x).strip() for x in derived if str(x).strip())

        resolved_department_ids_list = sorted(resolved_department_ids)
        resolved_terminal_ids_list = sorted(resolved_terminal_ids)

        journal_scope_q = Q(
            business_date__gte=date_from,
            business_date__lte=date_to,
        )
        journal_entity_q = Q()
        if resolved_department_ids_list:
            journal_entity_q |= Q(department_id__in=resolved_department_ids_list)
        if resolved_terminal_ids_list:
            journal_entity_q |= Q(terminal_group_id__in=resolved_terminal_ids_list)
        journal_qs = (
            OlapCheckSyncJournal.objects.filter(journal_scope_q)
            .filter(journal_entity_q if journal_entity_q else Q(pk__in=[]))
            .order_by("id")
        )

        raw_scope_q = Q(
            business_date__gte=date_from,
            business_date__lte=date_to,
        )
        raw_entity_q = Q()
        if resolved_department_ids_list:
            raw_entity_q |= Q(department_id__in=resolved_department_ids_list)
        if resolved_terminal_ids_list:
            raw_entity_q |= Q(sync_journal__terminal_group_id__in=resolved_terminal_ids_list)
        if department_name:
            raw_entity_q |= Q(department_name__icontains=department_name)
        raw_qs = (
            OlapSalesRawLine.objects.filter(raw_scope_q)
            .filter(raw_entity_q if raw_entity_q else Q(pk__in=[]))
            .order_by("id")
        )

        order_fact_scope_q = Q(
            business_date__gte=date_from,
            business_date__lte=date_to,
        )
        order_fact_entity_q = Q()
        if resolved_department_ids_list:
            order_fact_entity_q |= Q(department_id__in=resolved_department_ids_list)
        if department_name:
            order_fact_entity_q |= Q(department_name__icontains=department_name)
        order_fact_qs = (
            OrderFact.objects.filter(order_fact_scope_q)
            .filter(order_fact_entity_q if order_fact_entity_q else Q(pk__in=[]))
            .order_by("id")
        )

        daily_scope_q = Q(
            business_date__gte=date_from,
            business_date__lte=date_to,
        )
        if resolved_department_ids_list:
            daily_qs = GuestRestaurantDailyCategoryFact.objects.filter(
                daily_scope_q,
                department_id__in=resolved_department_ids_list,
            ).order_by("id")
        else:
            daily_qs = GuestRestaurantDailyCategoryFact.objects.none()

        window_scope_q = Q(
            as_of_date__gte=date_from,
            as_of_date__lte=date_to,
        )
        if resolved_department_ids_list:
            window_qs = GuestRestaurantWindowMetrics.objects.filter(
                window_scope_q,
                department_id__in=resolved_department_ids_list,
            ).order_by("id")
        else:
            window_qs = GuestRestaurantWindowMetrics.objects.none()

        raw_sync_journal_ids = sorted(set(raw_qs.values_list("sync_journal_id", flat=True)))
        journal_linked_qs = OlapCheckSyncJournal.objects.filter(id__in=raw_sync_journal_ids).order_by("id")
        journal_without_raw_qs = journal_qs.exclude(id__in=raw_sync_journal_ids).order_by("id")
        journal_linked_outside_scope_qs = journal_linked_qs.exclude(journal_scope_q).order_by("id")
        raw_linked_outside_scope_qs = raw_qs.exclude(
            sync_journal__business_date__gte=date_from,
            sync_journal__business_date__lte=date_to,
        )

        dec_zero = Value(0, output_field=DecimalField(max_digits=18, decimal_places=2))
        raw_net = raw_qs.aggregate(
            v=Coalesce(Sum("dish_sum_after_discount"), dec_zero),
        )["v"]
        raw_gross = raw_qs.aggregate(
            v=Coalesce(Sum("dish_sum_before_discount"), dec_zero),
        )["v"]
        order_net = order_fact_qs.aggregate(
            v=Coalesce(Sum("net_sum"), dec_zero),
        )["v"]
        order_gross = order_fact_qs.aggregate(
            v=Coalesce(Sum("gross_sum"), dec_zero),
        )["v"]
        daily_sum_net = daily_qs.aggregate(
            v=Coalesce(Sum("sum_net"), dec_zero),
        )["v"]

        journal_status = list(
            journal_qs.values("status").annotate(c=Count("id")).order_by("status")
        )
        daily_by_focus = list(
            daily_qs.values("focus_category__code", "focus_category__name")
            .annotate(
                rows=Count("id"),
                orders=Coalesce(Sum("orders_count"), Value(0)),
                items=Coalesce(Sum("items_count"), Value(0)),
                sum_net=Coalesce(Sum("sum_net"), dec_zero),
            )
            .order_by("focus_category__code", "focus_category__name")
        )
        window_by_days = list(
            window_qs.values("window_days")
            .annotate(
                rows=Count("id"),
                orders=Coalesce(Sum("orders_count"), Value(0)),
                visits=Coalesce(Sum("visits_count"), Value(0)),
                sum_net=Coalesce(Sum("sum_net"), dec_zero),
            )
            .order_by("window_days")
        )

        raw_order_rows = list(
            raw_qs.values(
                "business_date",
                "department_id",
                "department_name",
                "order_number",
                "uniq_order_id",
            )
            .annotate(
                raw_rows=Count("id"),
                raw_net=Coalesce(Sum("dish_sum_after_discount"), dec_zero),
                raw_gross=Coalesce(Sum("dish_sum_before_discount"), dec_zero),
                min_sync_journal_id=Min("sync_journal_id"),
                max_sync_journal_id=Max("sync_journal_id"),
                min_sync_journal_business_date=Min("sync_journal__business_date"),
                max_sync_journal_business_date=Max("sync_journal__business_date"),
            )
            .order_by("business_date", "department_id", "order_number", "uniq_order_id")
        )

        fact_order_rows = list(
            order_fact_qs.values(
                "business_date",
                "department_id",
                "department_name",
                "order_number",
                "uniq_order_id",
                "items_count",
                "categories_count",
                "net_sum",
                "gross_sum",
                "discount_sum",
                "bonus_sum",
            ).order_by("business_date", "department_id", "order_number", "uniq_order_id")
        )

        raw_key_set = {
            (
                str(x["business_date"]),
                str(x["department_id"] or ""),
                int(x["order_number"] or 0),
                str(x["uniq_order_id"] or ""),
            )
            for x in raw_order_rows
        }
        fact_key_set = {
            (
                str(x["business_date"]),
                str(x["department_id"] or ""),
                int(x["order_number"] or 0),
                str(x["uniq_order_id"] or ""),
            )
            for x in fact_order_rows
        }

        only_raw = sorted(raw_key_set - fact_key_set)
        only_fact = sorted(fact_key_set - raw_key_set)

        mismatch_base_qs = raw_qs.exclude(sync_journal__business_date=F("business_date"))
        mismatch_rows_count = mismatch_base_qs.count()
        mismatch_rows = list(
            mismatch_base_qs
            .values(
                "id",
                "business_date",
                "order_number",
                "uniq_order_id",
                "department_id",
                "sync_journal_id",
                "sync_journal__business_date",
                "sync_journal__event_at",
            )
            .order_by("id")[: max_order_details]
        )

        payload = {
            "filters": {
                "date_from": str(date_from),
                "date_to": str(date_to),
                "department_ids_input": department_ids_input,
                "terminal_group_ids_input": terminal_ids_input,
                "department_name_input": department_name,
                "include_inactive_mapping": include_inactive_mapping,
            },
            "resolved_scope": {
                "department_ids": resolved_department_ids_list,
                "terminal_group_ids": resolved_terminal_ids_list,
            },
            "counts": {
                "mapping_rows": len(mapping_rows),
                "journal_rows": journal_qs.count(),
                "journal_rows_linked_from_raw": journal_linked_qs.count(),
                "journal_rows_without_raw": journal_without_raw_qs.count(),
                "journal_rows_linked_from_raw_outside_scope": journal_linked_outside_scope_qs.count(),
                "raw_rows_linked_to_journal_outside_scope": raw_linked_outside_scope_qs.count(),
                "raw_rows": raw_qs.count(),
                "order_fact_rows": order_fact_qs.count(),
                "daily_fact_rows": daily_qs.count(),
                "window_metrics_rows": window_qs.count(),
            },
            "sums": {
                "raw_net": _serialize_decimal(raw_net),
                "raw_gross": _serialize_decimal(raw_gross),
                "order_fact_net": _serialize_decimal(order_net),
                "order_fact_gross": _serialize_decimal(order_gross),
                "daily_sum_net": _serialize_decimal(daily_sum_net),
            },
            "quality_checks": {
                "raw_vs_order_fact_keys_equal": not only_raw and not only_fact,
                "raw_only_keys_count": len(only_raw),
                "order_fact_only_keys_count": len(only_fact),
                "raw_only_keys_sample": [
                    {
                        "business_date": x[0],
                        "department_id": x[1],
                        "order_number": x[2],
                        "uniq_order_id": x[3],
                    }
                    for x in only_raw[:max_order_details]
                ],
                "order_fact_only_keys_sample": [
                    {
                        "business_date": x[0],
                        "department_id": x[1],
                        "order_number": x[2],
                        "uniq_order_id": x[3],
                    }
                    for x in only_fact[:max_order_details]
                ],
                "raw_rows_with_journal_business_date_mismatch_count": mismatch_rows_count,
                "raw_rows_with_journal_business_date_mismatch_sample": mismatch_rows,
                "journal_status_counts": journal_status,
            },
            "details": {
                "mapping_rows": mapping_rows,
                "journal_rows_sample": list(
                    journal_qs.values(
                        "id",
                        "status",
                        "source_webhook_id",
                        "order_number",
                        "business_date",
                        "event_at",
                        "department_id",
                        "terminal_group_id",
                        "attempt_count",
                        "next_try_at",
                        "last_error",
                        "created_at",
                        "loaded_at",
                    )[:max_journal_details]
                ),
                "journal_rows_linked_from_raw_sample": list(
                    journal_linked_qs.values(
                        "id",
                        "status",
                        "source_webhook_id",
                        "order_number",
                        "business_date",
                        "event_at",
                        "department_id",
                        "terminal_group_id",
                        "created_at",
                        "loaded_at",
                    )[:max_journal_details]
                ),
                "journal_rows_without_raw_sample": list(
                    journal_without_raw_qs.values(
                        "id",
                        "status",
                        "source_webhook_id",
                        "order_number",
                        "business_date",
                        "event_at",
                        "department_id",
                        "terminal_group_id",
                        "created_at",
                        "loaded_at",
                    )[:max_journal_details]
                ),
                "journal_rows_linked_from_raw_outside_scope_sample": list(
                    journal_linked_outside_scope_qs.values(
                        "id",
                        "status",
                        "source_webhook_id",
                        "order_number",
                        "business_date",
                        "event_at",
                        "department_id",
                        "terminal_group_id",
                        "created_at",
                        "loaded_at",
                    )[:max_journal_details]
                ),
                "raw_rows_linked_to_journal_outside_scope_sample": list(
                    raw_linked_outside_scope_qs.values(
                        "id",
                        "business_date",
                        "order_number",
                        "uniq_order_id",
                        "department_id",
                        "department_name",
                        "sync_journal_id",
                        "sync_journal__business_date",
                        "sync_journal__event_at",
                    )[:max_order_details]
                ),
                "raw_by_order_sample": raw_order_rows[:max_order_details],
                "order_fact_by_order_sample": fact_order_rows[:max_order_details],
                "daily_by_focus": daily_by_focus,
                "window_by_days": window_by_days,
            },
        }

        output_file = str(options["output_file"] or "").strip()
        if output_file:
            target_path = Path(output_file)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

        output_format = str(options["output_format"] or "pretty").strip().lower()
        if output_format == "json":
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return

        self.stdout.write(self.style.SUCCESS("diagnose_olap_data_flow: готово"))
        self.stdout.write(f"period={date_from}..{date_to}")
        self.stdout.write(
            "scope: "
            f"departments={len(resolved_department_ids_list)} terminals={len(resolved_terminal_ids_list)} "
            f"mapping_rows={payload['counts']['mapping_rows']}"
        )
        self.stdout.write(
            "counts: "
            f"journal={payload['counts']['journal_rows']} "
            f"raw={payload['counts']['raw_rows']} "
            f"order_fact={payload['counts']['order_fact_rows']} "
            f"daily={payload['counts']['daily_fact_rows']} "
            f"window={payload['counts']['window_metrics_rows']}"
        )
        self.stdout.write(
            "sums: "
            f"raw_net={payload['sums']['raw_net']} "
            f"order_net={payload['sums']['order_fact_net']} "
            f"raw_gross={payload['sums']['raw_gross']} "
            f"order_gross={payload['sums']['order_fact_gross']}"
        )
        self.stdout.write(
            "checks: "
            f"keys_equal={payload['quality_checks']['raw_vs_order_fact_keys_equal']} "
            f"raw_only={payload['quality_checks']['raw_only_keys_count']} "
            f"fact_only={payload['quality_checks']['order_fact_only_keys_count']} "
            f"journal_date_mismatch_rows={payload['quality_checks']['raw_rows_with_journal_business_date_mismatch_count']} "
            f"journal_without_raw={payload['counts']['journal_rows_without_raw']} "
            f"linked_outside_scope={payload['counts']['journal_rows_linked_from_raw_outside_scope']}"
        )
        if output_file:
            self.stdout.write(f"saved_json={output_file}")
