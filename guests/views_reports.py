"""
Разделы отчётности и реестра купонов.

Назначение модуля:
1. дать оператору отдельный экран реестра купонов с фильтрами и статусами;
2. дать маркетологу отдельный отчёт по купонным кампаниям с выбором кампании;
3. предоставить стабильные URL для дальнейшего развития (добавление графиков и экспортов).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from django.contrib import messages
from django.core.management import CommandError, call_command
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q, Sum
from django.http import FileResponse, Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from guests.models import (
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponAutomationConfig,
    CouponCampaignAssignment,
    CouponPoolBatch,
    CouponRegistryEntry,
    DispatchTask,
    Mailing,
    NotificationEvent,
    NotificationScenario,
    OlapSalesRawLine,
    OrderFact,
)
from guests.services.coupon_campaign_reporting import build_coupon_campaign_performance_snapshot
from guests.services.coupon_autoscenarios import COUPON_AUTOSCENARIO_STATUS_REASON_DELIVERY_FAILED
from guests.services.coupon_pool import CouponPoolGenerationError, CouponPoolService
from guests.services.coupon_venues import build_coupon_venue_choices
from guests.services.simple_mailing_reporting import (
    ALLOWED_PERIOD_DAYS,
    DEFAULT_PERIOD_DAYS,
    build_simple_mailing_order_details_page,
    build_simple_mailing_report_snapshot,
    normalize_simple_mailing_period_days,
    search_simple_mailings,
    simple_mailings_queryset,
)


def _parse_positive_int(value: str | None) -> int | None:
    """
    Возвращает положительное целое число или `None`.

    Используется для безопасного разбора query-параметров вида `campaign_id`.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        return None
    parsed = int(raw)
    return parsed if parsed > 0 else None


def _safe_token(value: str) -> str:
    """
    Нормализует строку для безопасного включения в имя файла.
    """
    token = "".join(ch if ch.isalnum() else "_" for ch in str(value or "").strip())
    return token or "NA"


def _money(value: Decimal | int | str | None) -> str:
    """
    Форматирует денежное значение для отчёта.
    """
    amount = Decimal(value or 0)
    return str(amount.quantize(Decimal("0.01")))


def _decimal_or_zero(value) -> Decimal:
    """
    Безопасно приводит значение из OLAP к Decimal.
    """
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _normalize_report_text(value) -> str:
    return str(value or "").strip()


def _order_identity(
    *,
    business_date,
    department_id,
    order_number,
    uniq_order_id,
) -> tuple[object, str, int | None, str]:
    return (
        business_date,
        _normalize_report_text(department_id),
        int(order_number) if order_number is not None else None,
        _normalize_report_text(uniq_order_id),
    )


def _raw_line_net_sum(row: dict[str, object]) -> Decimal:
    if row.get("dish_sum_after_discount") in (None, ""):
        return _raw_line_gross_sum(row)
    return _decimal_or_zero(row.get("dish_sum_after_discount"))


def _raw_line_gross_sum(row: dict[str, object]) -> Decimal:
    return _decimal_or_zero(row.get("dish_sum_before_discount"))


def _raw_line_quantity(row: dict[str, object]) -> Decimal:
    quantity = _decimal_or_zero(row.get("dish_amount"))
    return quantity if quantity > 0 else Decimal("1")


def _default_batch_csv_path(batch: CouponPoolBatch) -> Path:
    """
    Возвращает безопасный путь для восстановления CSV партии.

    CSV-файл может пропасть при пересоздании контейнера, поэтому путь должен
    зависеть от batch-кода, а не от временного состояния формы генерации.
    """
    return Path("tools") / f"iikocard_coupon_import_{_safe_token(batch.batch_code)}.csv"


class ReportsWorkbenchView(TemplateView):
    """
    Главная точка входа раздела «Отчёты».

    На текущем этапе это компактный хаб с переходами в:
    1. отчёты по купонным кампаниям;
    2. отчёты по купонным автосценариям;
    3. реестр купонов.
    """

    template_name = "reports/hub.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        coupon_campaigns_qs = Mailing.objects.exclude(coupon_series__isnull=True).exclude(coupon_series="")
        coupon_autoscenarios_qs = NotificationScenario.objects.filter(coupon_automation_config__isnull=False)
        context["reports_kpi"] = {
            "coupon_campaigns_total": int(coupon_campaigns_qs.count()),
            "coupon_campaigns_active": int(coupon_campaigns_qs.filter(is_active=True).count()),
            "coupon_autoscenarios_total": int(coupon_autoscenarios_qs.count()),
            "coupon_autoscenarios_active": int(
                coupon_autoscenarios_qs.filter(coupon_automation_config__execution_mode="automatic").count()
            ),
            "coupon_registry_total": int(CouponRegistryEntry.objects.count()),
            "coupon_registry_available": int(
                CouponRegistryEntry.objects.filter(
                    is_active=True,
                    pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
                ).count()
            ),
            "coupon_batches_total": int(CouponPoolBatch.objects.count()),
        }
        context["coupon_campaign_reports_url"] = reverse("reports_coupon_campaigns")
        context["coupon_autoscenario_reports_url"] = reverse("reports_coupon_autoscenarios")
        context["simple_mailing_reports_url"] = reverse("reports_simple_mailings")
        context["coupon_registry_url"] = reverse("coupon_registry")
        return context


class SimpleMailingReportsView(TemplateView):
    """Компактный отчёт по простой массовой рассылке без купонов."""

    template_name = "reports/simple_mailings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        raw_mailing_id = str(self.request.GET.get("mailing_id") or "").strip()
        period_days = normalize_simple_mailing_period_days(
            self.request.GET.get("period_days")
        )
        initial_mailings = list(search_simple_mailings())

        selected_mailing = None
        if raw_mailing_id:
            if not raw_mailing_id.isascii() or not raw_mailing_id.isdecimal():
                raise Http404("Простая рассылка не найдена.")
            mailing_id = int(raw_mailing_id)
            if mailing_id <= 0 or mailing_id > 9223372036854775807:
                raise Http404("Простая рассылка не найдена.")
            selected_mailing = get_object_or_404(
                simple_mailings_queryset(),
                id=mailing_id,
            )
        elif initial_mailings:
            selected_mailing = initial_mailings[0]

        if selected_mailing and all(
            mailing.id != selected_mailing.id for mailing in initial_mailings
        ):
            initial_mailings.insert(0, selected_mailing)

        report = None
        chart_payload = {"dates": [], "guests": [], "orders": []}
        period_links = []
        status_url = ""
        audience_url = ""
        orders_url = ""
        if selected_mailing is not None:
            report = build_simple_mailing_report_snapshot(
                mailing=selected_mailing,
                period_days=period_days,
            ).to_dict()
            chart_payload = {
                "dates": [row["business_date"].isoformat() for row in report["daily_rows"]],
                "guests": [row["guests_count"] for row in report["daily_rows"]],
                "orders": [row["orders_count"] for row in report["daily_rows"]],
            }
            for option_days in ALLOWED_PERIOD_DAYS:
                params = urlencode(
                    {
                        "mailing_id": selected_mailing.id,
                        "period_days": option_days,
                    }
                )
                period_links.append(
                    {
                        "days": option_days,
                        "is_selected": option_days == period_days,
                        "url": f"{reverse('reports_simple_mailings')}?{params}",
                    }
                )
            status_url = reverse(
                "mailings_v2_campaigns_status",
                kwargs={"pk": selected_mailing.id},
            )
            audience_url = reverse(
                "mailings_v2_campaigns_audience",
                kwargs={"pk": selected_mailing.id},
            )
            orders_url = reverse(
                "reports_simple_mailings_orders",
                kwargs={"mailing_id": selected_mailing.id},
            )

        context.update(
            {
                "mailing_options": [
                    _serialize_simple_mailing_option(mailing)
                    for mailing in initial_mailings[:10]
                ],
                "selected_mailing": selected_mailing,
                "selected_period_days": period_days,
                "period_links": period_links,
                "simple_mailing_report": report,
                "simple_mailing_chart_payload": chart_payload,
                "simple_mailing_search_url": reverse("reports_simple_mailings_search"),
                "simple_mailing_orders_url": orders_url,
                "mailing_status_url": status_url,
                "mailing_audience_url": audience_url,
                "back_to_reports_url": reverse("reports"),
            }
        )
        return context


class SimpleMailingSearchView(View):
    """Ограниченный серверный поиск простых рассылок для выпадающего списка."""

    def get(self, request, *args, **kwargs):
        results = [
            _serialize_simple_mailing_option(mailing)
            for mailing in search_simple_mailings(request.GET.get("q"))
        ]
        response = JsonResponse(
            {"results": results},
            json_dumps_params={"ensure_ascii": False},
        )
        response["Cache-Control"] = "no-store"
        return response


class SimpleMailingOrdersView(View):
    """Ленивая серверная выдача одной страницы заказов выбранной рассылки."""

    def get(self, request, mailing_id: int, *args, **kwargs):
        raw_period_days = str(
            request.GET.get("period_days") or DEFAULT_PERIOD_DAYS
        ).strip()
        if raw_period_days not in {str(value) for value in ALLOWED_PERIOD_DAYS}:
            response = JsonResponse(
                {"error": "Допустимы только периоды 7, 14 или 30 дней."},
                status=400,
                json_dumps_params={"ensure_ascii": False},
            )
            response["Cache-Control"] = "no-store"
            return response

        mailing = get_object_or_404(simple_mailings_queryset(), id=mailing_id)
        page = build_simple_mailing_order_details_page(
            mailing=mailing,
            period_days=int(raw_period_days),
            page_number=request.GET.get("page"),
            page_size=request.GET.get("page_size"),
        )
        payload = page.to_dict()
        payload["period_days"] = int(raw_period_days)
        response = JsonResponse(
            payload,
            json_dumps_params={"ensure_ascii": False},
        )
        response["Cache-Control"] = "no-store"
        return response


def _serialize_simple_mailing_option(mailing: Mailing) -> dict[str, object]:
    """Формирует безопасную краткую запись рассылки для выпадающего списка."""

    scheduled_date = mailing.scheduled_date
    return {
        "id": int(mailing.id),
        "name": mailing.name,
        "scheduled_date": scheduled_date.isoformat(),
        "label": f"#{mailing.id} · {mailing.name} · {scheduled_date.strftime('%d.%m.%Y')}",
        "status_url": reverse(
            "mailings_v2_campaigns_status",
            kwargs={"pk": mailing.id},
        ),
        "audience_url": reverse(
            "mailings_v2_campaigns_audience",
            kwargs={"pk": mailing.id},
        ),
    }


class CouponAutoscenarioReportsView(TemplateView):
    """
    Отчёт по купонным автосценариям.

    В отличие от отчёта по ручным кампаниям, этот экран агрегирует данные по
    механике целиком: все технические волны, назначения, доставку и применения.
    """

    template_name = "reports/coupon_autoscenarios.html"
    runs_limit = 50
    assignments_limit = 100
    followup_window_days = 30
    weekday_short_labels = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        scenario_code = str(self.request.GET.get("scenario_code") or "").strip()
        date_from_raw = str(self.request.GET.get("date_from") or "").strip()
        date_to_raw = str(self.request.GET.get("date_to") or "").strip()
        venue_code = str(self.request.GET.get("venue_code") or "").strip()
        date_from = parse_date(date_from_raw) if date_from_raw else None
        date_to = parse_date(date_to_raw) if date_to_raw else None

        scenarios = list(
            NotificationScenario.objects.filter(coupon_automation_config__isnull=False)
            .select_related("template", "coupon_automation_config")
            .order_by("code")
        )
        selected_scenario = None
        if scenario_code:
            selected_scenario = next((scenario for scenario in scenarios if scenario.code == scenario_code), None)
        if selected_scenario is None and scenarios:
            selected_scenario = scenarios[0]

        report = None
        if selected_scenario is not None:
            report = self._build_report(
                scenario=selected_scenario,
                date_from=date_from,
                date_to=date_to,
                venue_code=venue_code,
            )

        context["scenarios"] = scenarios
        context["selected_scenario"] = selected_scenario
        context["autoscenario_report"] = report
        context["report_venue_choices"] = self._build_venue_filter_choices(
            scenario=selected_scenario,
            report=report,
            selected_venue_code=venue_code,
        )
        context["filters"] = {
            "scenario_code": selected_scenario.code if selected_scenario else scenario_code,
            "date_from": date_from_raw,
            "date_to": date_to_raw,
            "venue_code": venue_code,
        }
        context["period_presets"] = self._build_period_presets(
            scenario_code=selected_scenario.code if selected_scenario else scenario_code,
            date_from=date_from,
            date_to=date_to,
            venue_code=venue_code,
        )
        context["back_to_reports_url"] = reverse("reports")
        context["scenarios_url"] = reverse("mailings_v2_scenarios")
        return context

    def _build_period_presets(self, *, scenario_code: str, date_from, date_to, venue_code: str = "") -> list[dict]:
        today = timezone.localdate()
        presets = [
            ("1 неделя", 7),
            ("2 недели", 14),
            ("1 месяц", 30),
            ("2 месяца", 60),
        ]
        result = []
        for label, days in presets:
            preset_from = today - timedelta(days=days - 1)
            preset_to = today
            params = {
                "scenario_code": scenario_code,
                "date_from": preset_from.isoformat(),
                "date_to": preset_to.isoformat(),
            }
            if venue_code:
                params["venue_code"] = venue_code
            result.append(
                {
                    "label": label,
                    "url": f"{reverse('reports_coupon_autoscenarios')}?{urlencode(params)}",
                    "active": date_from == preset_from and date_to == preset_to,
                }
            )
        return result

    @staticmethod
    def _build_venue_filter_choices(*, scenario: NotificationScenario | None, report: dict | None, selected_venue_code: str = "") -> list[dict]:
        """
        Собирает список заведений для фильтра отчёта.

        Фильтр нельзя строить только по выручке: до первого применения купона
        строк OLAP ещё нет, но пользователю уже нужно выбрать заведение.
        Поэтому берём общий справочник заведений, настройки сценария и
        фактические строки отчёта, если они появились.
        """

        selected_code = _normalize_report_text(selected_venue_code)
        choices: list[dict[str, object]] = [
            {
                "code": "",
                "label": "Все заведения",
                "selected": selected_code == "",
            }
        ]
        seen = {""}

        def add_choice(code: str | None, label: str | None = None):
            safe_code = _normalize_report_text(code)
            if not safe_code or safe_code == "__global__" or safe_code in seen:
                return
            safe_label = CouponAutoscenarioReportsView._human_venue_label(code=safe_code, label=label)
            choices.append(
                {
                    "code": safe_code,
                    "label": safe_label,
                    "selected": selected_code == safe_code,
                }
            )
            seen.add(safe_code)

        if scenario is not None and hasattr(scenario, "coupon_automation_config"):
            config = scenario.coupon_automation_config
            add_choice(config.audience_venue_code, config.audience_venue_name)
            add_choice(config.venue_code, config.venue_name)
            for rule in config.coupon_rules.filter(is_active=True).order_by("scope_type", "priority", "venue_name"):
                add_choice(rule.venue_code, rule.venue_name)

        venue_choices, venue_map = build_coupon_venue_choices(include_empty=False)
        for code, _label in venue_choices:
            add_choice(code, venue_map.get(code))

        for section_name in ("revenue", "followup"):
            section = (report or {}).get(section_name) or {}
            for row in section.get("venue_rows") or []:
                add_choice(row.get("venue_code"), row.get("venue_name"))

        if selected_code and selected_code not in seen:
            add_choice(selected_code, selected_code)
            for choice in choices:
                choice["selected"] = choice["code"] == selected_code

        return choices

    @staticmethod
    def _human_venue_label(*, code: str, label: str | None = None) -> str:
        safe_code = _normalize_report_text(code)
        safe_label = _normalize_report_text(label) or safe_code
        suffix = f" ({safe_code})"
        if safe_label.endswith(suffix):
            safe_label = safe_label[: -len(suffix)].strip()
        return safe_label or safe_code

    def _build_report(self, *, scenario: NotificationScenario, date_from, date_to, venue_code: str = "") -> dict:
        all_runs_qs = (
            CouponAutoscenarioRun.objects.filter(scenario=scenario)
            .select_related("scenario", "config")
            .order_by("-created_at")
        )
        all_assignments_qs = (
            CouponAutoscenarioAssignment.objects.filter(scenario=scenario)
            .select_related("run", "guest", "coupon", "coupon_rule")
            .order_by("-assigned_at", "-id")
        )

        if date_from:
            all_runs_qs = all_runs_qs.filter(created_at__date__gte=date_from)
            all_assignments_qs = all_assignments_qs.filter(assigned_at__date__gte=date_from)
        if date_to:
            all_runs_qs = all_runs_qs.filter(created_at__date__lte=date_to)
            all_assignments_qs = all_assignments_qs.filter(assigned_at__date__lte=date_to)

        active_mode = CouponAutomationConfig.ExecutionMode.AUTOMATIC
        pilot_mode = CouponAutomationConfig.ExecutionMode.PILOT
        runs_qs = all_runs_qs.filter(execution_mode=active_mode)
        assignments_qs = all_assignments_qs.filter(run__execution_mode=active_mode)
        pilot_runs_qs = all_runs_qs.filter(execution_mode=pilot_mode)
        pilot_assignments_qs = all_assignments_qs.filter(run__execution_mode=pilot_mode)

        run_totals = runs_qs.aggregate(
            scanned_guests=Sum("scanned_guests"),
            matched_guests=Sum("matched_guests"),
            sendable_guests=Sum("sendable_guests"),
            eligible_guests=Sum("eligible_guests"),
            planned_assignments=Sum("planned_assignments"),
            created_assignments=Sum("created_assignments"),
            queue_events_created=Sum("queue_events_created"),
            coupon_shortage=Sum("coupon_shortage"),
            blocked_without_channel=Sum("blocked_without_channel"),
            blocked_existing_active_coupon=Sum("blocked_existing_active_coupon"),
            blocked_existing_trigger=Sum("blocked_existing_trigger"),
            blocked_by_cooldown=Sum("blocked_by_cooldown"),
        )
        issue_runs_qs = runs_qs.filter(created_assignments__gt=0)
        decision_runs_qs = issue_runs_qs if issue_runs_qs.exists() else runs_qs
        decision_run_totals = decision_runs_qs.aggregate(
            scanned_guests=Sum("scanned_guests"),
            matched_guests=Sum("matched_guests"),
            sendable_guests=Sum("sendable_guests"),
            eligible_guests=Sum("eligible_guests"),
            planned_assignments=Sum("planned_assignments"),
            created_assignments=Sum("created_assignments"),
            queue_events_created=Sum("queue_events_created"),
            coupon_shortage=Sum("coupon_shortage"),
            blocked_without_channel=Sum("blocked_without_channel"),
            blocked_existing_active_coupon=Sum("blocked_existing_active_coupon"),
            blocked_existing_trigger=Sum("blocked_existing_trigger"),
            blocked_by_cooldown=Sum("blocked_by_cooldown"),
        )
        status_counts = {
            row["status"]: int(row["total"])
            for row in assignments_qs.values("status").annotate(total=Count("id"))
        }
        canceled_delivery_failed_total = int(
            assignments_qs.filter(
                status=CouponAutoscenarioAssignment.Status.CANCELED,
                status_reason=COUPON_AUTOSCENARIO_STATUS_REASON_DELIVERY_FAILED,
            ).count()
        )
        sync_status_counts = {
            row["vtelemax_sync_status"]: int(row["total"])
            for row in assignments_qs.values("vtelemax_sync_status").annotate(total=Count("id"))
        }
        iiko_status_counts = {
            row["iiko_category_add_status"]: int(row["total"])
            for row in assignments_qs.values("iiko_category_add_status").annotate(total=Count("id"))
        }

        assignment_ids = list(assignments_qs.values_list("id", flat=True))
        assignment_state_by_id = {
            int(row["id"]): row
            for row in assignments_qs.values(
                "id",
                "status",
                "vtelemax_sync_status",
                "iiko_category_add_status",
            )
        }
        delivery_context = self._build_assignment_delivery_context(
            scenario=scenario,
            assignment_ids=assignment_ids,
            assignment_state_by_id=assignment_state_by_id,
        )

        visible_assignments = list(assignments_qs[: self.assignments_limit])
        self._attach_delivery_context(
            assignments=visible_assignments,
            delivery_context=delivery_context,
        )

        pilot_assignment_ids = list(pilot_assignments_qs.values_list("id", flat=True))
        pilot_assignment_state_by_id = {
            int(row["id"]): row
            for row in pilot_assignments_qs.values(
                "id",
                "status",
                "vtelemax_sync_status",
                "iiko_category_add_status",
            )
        }
        pilot_delivery_context = self._build_assignment_delivery_context(
            scenario=scenario,
            assignment_ids=pilot_assignment_ids,
            assignment_state_by_id=pilot_assignment_state_by_id,
        )
        visible_pilot_assignments = list(pilot_assignments_qs[: self.assignments_limit])
        self._attach_delivery_context(
            assignments=visible_pilot_assignments,
            delivery_context=pilot_delivery_context,
        )

        revenue = self._build_revenue_snapshot(
            assignments_qs=assignments_qs,
            date_from=date_from,
            date_to=date_to,
            venue_code=venue_code,
        )
        followup = self._build_followup_snapshot(
            assignments_qs=assignments_qs,
            venue_code=venue_code,
        )
        daily_rows = self._build_daily_rows(
            runs_qs=runs_qs,
            assignments_qs=assignments_qs,
            delivery_context=delivery_context,
            date_from=date_from,
            date_to=date_to,
        )

        assignments_total = int(assignments_qs.count())
        sent_total = status_counts.get(CouponAutoscenarioAssignment.Status.SENT, 0)
        used_total = status_counts.get(CouponAutoscenarioAssignment.Status.USED, 0)
        used_after_total = status_counts.get(CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN, 0)
        delivery_summary = delivery_context["summary"]
        delivered_total = delivery_summary["tasks_done"]
        delivered_assignments = delivery_summary["assignments_delivered"]
        delivery_base = assignments_total
        delivery_rate = round(delivered_assignments * 100 / delivery_base, 2) if delivery_base else 0
        usage_base = sent_total or assignments_total
        usage_rate = round((used_total + used_after_total) * 100 / usage_base, 2) if usage_base else 0

        runs_rows = list(runs_qs[: self.runs_limit])
        latest_run = runs_rows[0] if runs_rows else None
        active_assignments = sent_total + status_counts.get(CouponAutoscenarioAssignment.Status.RESERVED, 0)
        applied_total = used_total + used_after_total
        runs_total = int(runs_qs.count())
        issue_runs_total = int(issue_runs_qs.count())
        decision_runs_total = int(decision_runs_qs.count())
        funnel_rows = [
            {
                "label": "Назначено купонов",
                "value": assignments_total,
                "percent": "100,0" if assignments_total else "0,0",
                "note": "Фактические назначения купонов реальным гостям, без пилотных проверок.",
            },
            {
                "label": "Доставлено гостям",
                "value": delivered_assignments,
                "percent": self._format_percent(delivered_assignments, assignments_total),
                "note": "Хотя бы один канал доставки подтвердил отправку сообщения гостю.",
            },
            {
                "label": "Отменено из-за недоставки",
                "value": canceled_delivery_failed_total,
                "percent": self._format_percent(canceled_delivery_failed_total, assignments_total),
                "note": "Все доступные каналы дали окончательную ошибку, купон поставлен на безопасную отмену.",
            },
            {
                "label": "Применили купон",
                "value": applied_total,
                "percent": self._format_percent(applied_total, assignments_total),
                "note": "Купон найден в OLAP/order_fact как использованный.",
            },
        ]
        if runs_total == 0:
            conclusion = "За выбранный период боевых запусков не было. Маркетинговую эффективность оценивать нельзя."
            recommendation = (
                "Пилотные проверки смотрите в отдельном журнале ниже. Для оценки акции нужен запуск "
                "в состоянии «Активен» и реальные гости, а не контрольные номера."
            )
            conclusion_tone = "muted"
        elif assignments_total == 0:
            conclusion = "Сценарий запускался, но купоны реальным гостям не выдавались."
            recommendation = "Проверьте, где сужается воронка: канал доставки, пауза перед повтором или дефицит купонов."
            conclusion_tone = "warning"
        elif applied_total > 0:
            conclusion = "Купоны применялись в заказах, можно оценивать выручку и средний чек."
            recommendation = "Сравните динамику по дням и проверьте, хватает ли купонов для следующих запусков."
            conclusion_tone = "success"
        elif delivered_total > 0:
            conclusion = "Сообщения доставлены, но применений купонов в OLAP пока нет."
            recommendation = "Проверьте оффер, срок действия и наличие купона в vtelemax; применения появятся после реального заказа и загрузки OLAP."
            conclusion_tone = "warning"
        else:
            conclusion = "Купоны были подготовлены, но сообщений с ними пока не доставлено."
            recommendation = "Проверьте очередь vtelemax, dispatch-задачи и каналы доставки."
            conclusion_tone = "warning"

        pilot_status_counts = {
            row["status"]: int(row["total"])
            for row in pilot_assignments_qs.values("status").annotate(total=Count("id"))
        }
        pilot_summary = {
            "runs_total": int(pilot_runs_qs.count()),
            "runs_rows": list(pilot_runs_qs[: self.runs_limit]),
            "assignments_total": int(pilot_assignments_qs.count()),
            "assignments_rows": visible_pilot_assignments,
            "dispatch_total": int(pilot_delivery_context["tasks_qs"].count()),
            "dispatch_done": int(
                pilot_delivery_context["tasks_qs"].filter(status=DispatchTask.Status.DONE).count()
            ),
            "reserved_total": pilot_status_counts.get(CouponAutoscenarioAssignment.Status.RESERVED, 0),
            "sent_total": pilot_status_counts.get(CouponAutoscenarioAssignment.Status.SENT, 0),
            "canceled_total": pilot_status_counts.get(CouponAutoscenarioAssignment.Status.CANCELED, 0),
            "used_total": pilot_status_counts.get(CouponAutoscenarioAssignment.Status.USED, 0)
            + pilot_status_counts.get(CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN, 0),
        }

        return {
            "runs_total": runs_total,
            "runs_rows": runs_rows,
            "latest_run": latest_run,
            "assignments_total": assignments_total,
            "issue_runs_total": issue_runs_total,
            "decision_runs_total": decision_runs_total,
            "assignments_rows": visible_assignments,
            "status_counts": status_counts,
            "sync_status_counts": sync_status_counts,
            "iiko_status_counts": iiko_status_counts,
            "events_total": len(delivery_context["event_rows"]),
            "dispatch_total": delivery_summary["tasks_total"],
            "dispatch_done": delivered_total,
            "dispatch_failed": delivery_summary["tasks_failed"],
            "dispatch_pending": delivery_summary["tasks_pending"],
            "dispatch_queued": delivery_summary["tasks_queued"],
            "dispatch_in_progress": delivery_summary["tasks_in_progress"],
            "dispatch_canceled": delivery_summary["tasks_canceled"],
            "dispatch_blocked": delivery_summary["tasks_failed_blocked"],
            "dispatch_failed_other": delivery_summary["tasks_failed_other"],
            "dispatch_final_failed_assignments": delivery_summary["assignments_final_failed"],
            "dispatch_waiting_assignments": delivery_summary["assignments_waiting"],
            "dispatch_without_event_assignments": delivery_summary["assignments_without_event"],
            "dispatch_without_task_assignments": delivery_summary["assignments_without_task"],
            "dispatch_problem_assignments": delivery_summary["assignments_problem"],
            "vtelemax_ok": sync_status_counts.get(CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK, 0),
            "vtelemax_pending": sync_status_counts.get(CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING, 0),
            "vtelemax_error": sync_status_counts.get(CouponAutoscenarioAssignment.VtelemaxSyncStatus.ERROR, 0),
            "iiko_ok": iiko_status_counts.get(CouponAutoscenarioAssignment.IikoCategorySyncStatus.OK, 0),
            "iiko_pending": iiko_status_counts.get(CouponAutoscenarioAssignment.IikoCategorySyncStatus.PENDING, 0),
            "iiko_error": iiko_status_counts.get(CouponAutoscenarioAssignment.IikoCategorySyncStatus.ERROR, 0),
            "iiko_disabled": iiko_status_counts.get(CouponAutoscenarioAssignment.IikoCategorySyncStatus.DISABLED, 0),
            "reserved_total": status_counts.get(CouponAutoscenarioAssignment.Status.RESERVED, 0),
            "sent_total": sent_total,
            "used_total": used_total,
            "used_after_total": used_after_total,
            "applied_total": applied_total,
            "expired_total": status_counts.get(CouponAutoscenarioAssignment.Status.EXPIRED, 0),
            "canceled_total": status_counts.get(CouponAutoscenarioAssignment.Status.CANCELED, 0),
            "canceled_delivery_failed_total": canceled_delivery_failed_total,
            "active_assignments": active_assignments,
            "error_total": status_counts.get(CouponAutoscenarioAssignment.Status.ERROR, 0),
            "usage_rate_percent": usage_rate,
            "delivery_rate_percent": delivery_rate,
            "delivery_assignment_base": delivery_base,
            "delivered_assignments": delivered_assignments,
            "run_totals": {key: int(value or 0) for key, value in run_totals.items()},
            "decision_run_totals": {key: int(value or 0) for key, value in decision_run_totals.items()},
            "decision_run_totals_label": (
                "запусках с выдачей купонов" if issue_runs_total else "запусках без выдачи купонов"
            ),
            "funnel_rows": funnel_rows,
            "summary": {
                "conclusion": conclusion,
                "recommendation": recommendation,
                "tone": conclusion_tone,
            },
            "revenue": revenue,
            "followup": followup,
            "daily_rows": daily_rows,
            "daily_has_data": any(
                row["runs_count"]
                or row["issue_runs_count"]
                or row["issued_coupons"]
                or row["delivered_coupons"]
                or row["canceled_delivery_failed_coupons"]
                or row["used_coupons"]
                for row in daily_rows
            ),
            "pilot": pilot_summary,
        }

    def _build_assignment_delivery_context(
        self,
        *,
        scenario: NotificationScenario,
        assignment_ids: list[int],
        assignment_state_by_id: dict[int, dict] | None = None,
    ) -> dict:
        source_refs = [self._assignment_source_ref(assignment_id) for assignment_id in assignment_ids]
        events_qs = NotificationEvent.objects.filter(scenario=scenario, source_ref__in=source_refs)
        tasks_qs = DispatchTask.objects.filter(notification_event__source_ref__in=source_refs)
        event_rows = list(events_qs.select_related("guest"))
        task_rows = list(tasks_qs.select_related("notification_event", "guest").order_by("-created_at"))
        event_by_source_ref = {event.source_ref: event for event in event_rows if event.source_ref}
        latest_task_by_event_id = {}
        for task in task_rows:
            if task.notification_event_id and task.notification_event_id not in latest_task_by_event_id:
                latest_task_by_event_id[task.notification_event_id] = task
        summary = self._build_assignment_delivery_summary(
            assignment_ids=assignment_ids,
            assignment_state_by_id=assignment_state_by_id or {},
            event_by_source_ref=event_by_source_ref,
            task_rows=task_rows,
        )
        return {
            "tasks_qs": tasks_qs,
            "task_rows": task_rows,
            "event_rows": event_rows,
            "event_by_source_ref": event_by_source_ref,
            "latest_task_by_event_id": latest_task_by_event_id,
            "summary": summary,
        }

    def _build_assignment_delivery_summary(
        self,
        *,
        assignment_ids: list[int],
        assignment_state_by_id: dict[int, dict],
        event_by_source_ref: dict[str, NotificationEvent],
        task_rows: list[DispatchTask],
    ) -> dict:
        tasks_by_event_id = defaultdict(list)
        task_status_counts = defaultdict(int)
        failed_blocked = 0
        for task in task_rows:
            task_status_counts[task.status] += 1
            if task.notification_event_id:
                tasks_by_event_id[task.notification_event_id].append(task)
            if task.status == DispatchTask.Status.FAILED and self._is_permanent_blocked_delivery(task):
                failed_blocked += 1

        assignment_counts = defaultdict(int)
        waiting_statuses = {
            DispatchTask.Status.PENDING,
            DispatchTask.Status.QUEUED,
            DispatchTask.Status.IN_PROGRESS,
        }
        for assignment_id in assignment_ids:
            assignment_state = assignment_state_by_id.get(int(assignment_id)) or {}
            event = event_by_source_ref.get(self._assignment_source_ref(assignment_id))
            if not event:
                if self._assignment_waits_before_dispatch(assignment_state):
                    assignment_counts["waiting"] += 1
                else:
                    assignment_counts["without_event"] += 1
                continue

            event_tasks = tasks_by_event_id.get(event.id) or []
            if not event_tasks:
                if self._assignment_waits_before_dispatch(assignment_state):
                    assignment_counts["waiting"] += 1
                else:
                    assignment_counts["without_task"] += 1
                continue

            statuses = {task.status for task in event_tasks}
            if DispatchTask.Status.DONE in statuses:
                assignment_counts["delivered"] += 1
            elif statuses & waiting_statuses:
                assignment_counts["waiting"] += 1
            elif DispatchTask.Status.FAILED in statuses:
                assignment_counts["final_failed"] += 1
            elif DispatchTask.Status.CANCELED in statuses:
                assignment_counts["canceled"] += 1
            else:
                assignment_counts["unknown"] += 1

        tasks_failed = int(task_status_counts.get(DispatchTask.Status.FAILED, 0))
        assignments_problem = (
            assignment_counts["final_failed"]
            + assignment_counts["without_event"]
            + assignment_counts["without_task"]
            + assignment_counts["unknown"]
        )
        return {
            "tasks_total": len(task_rows),
            "tasks_done": int(task_status_counts.get(DispatchTask.Status.DONE, 0)),
            "tasks_failed": tasks_failed,
            "tasks_pending": int(task_status_counts.get(DispatchTask.Status.PENDING, 0)),
            "tasks_queued": int(task_status_counts.get(DispatchTask.Status.QUEUED, 0)),
            "tasks_in_progress": int(task_status_counts.get(DispatchTask.Status.IN_PROGRESS, 0)),
            "tasks_canceled": int(task_status_counts.get(DispatchTask.Status.CANCELED, 0)),
            "tasks_failed_blocked": failed_blocked,
            "tasks_failed_other": max(tasks_failed - failed_blocked, 0),
            "assignments_delivered": int(assignment_counts["delivered"]),
            "assignments_final_failed": int(assignment_counts["final_failed"]),
            "assignments_waiting": int(assignment_counts["waiting"]),
            "assignments_canceled": int(assignment_counts["canceled"]),
            "assignments_without_event": int(assignment_counts["without_event"]),
            "assignments_without_task": int(assignment_counts["without_task"]),
            "assignments_unknown": int(assignment_counts["unknown"]),
            "assignments_problem": int(assignments_problem),
        }

    @staticmethod
    def _assignment_waits_before_dispatch(assignment_state: dict) -> bool:
        status = assignment_state.get("status")
        vtelemax_status = assignment_state.get("vtelemax_sync_status")
        iiko_status = assignment_state.get("iiko_category_add_status")
        if status == CouponAutoscenarioAssignment.Status.RESERVED:
            return True
        if vtelemax_status == CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING:
            return True
        if iiko_status == CouponAutoscenarioAssignment.IikoCategorySyncStatus.PENDING:
            return True
        return False

    @staticmethod
    def _is_permanent_blocked_delivery(task: DispatchTask) -> bool:
        error_text = str(task.last_error or "").lower()
        return any(
            marker in error_text
            for marker in (
                "blocked",
                "forbidden",
                "недоступ",
                "заблок",
            )
        )

    def _attach_delivery_context(self, *, assignments, delivery_context: dict) -> None:
        for assignment in assignments:
            event = delivery_context["event_by_source_ref"].get(self._assignment_source_ref(assignment.id))
            assignment.report_event = event
            assignment.report_task = (
                delivery_context["latest_task_by_event_id"].get(event.id)
                if event
                else None
            )

    def _build_daily_rows(self, *, runs_qs, assignments_qs, delivery_context, date_from, date_to) -> list[dict]:
        period_start, period_end = self._resolve_daily_period(date_from=date_from, date_to=date_to)
        daily = {
            current_date: {
                "date": current_date.isoformat(),
                "display_date": current_date.strftime("%d.%m.%Y"),
                "axis_label": self._daily_axis_label(current_date),
                "weekday_short": self.weekday_short_labels[current_date.weekday()],
                "is_weekend": current_date.weekday() >= 5,
                "runs_count": 0,
                "issue_runs_count": 0,
                "issued_coupons": 0,
                "delivered_coupons": 0,
                "canceled_delivery_failed_coupons": 0,
                "used_coupons": 0,
                "usage_rate_percent": "0,0",
            }
            for current_date in self._iter_dates(period_start, period_end)
        }

        for row in (
            runs_qs.filter(created_at__date__gte=period_start, created_at__date__lte=period_end)
            .values("created_at__date")
            .annotate(
                runs_count=Count("id"),
                issue_runs_count=Count("id", filter=Q(created_assignments__gt=0)),
            )
        ):
            row_date = row["created_at__date"]
            if row_date in daily:
                daily[row_date]["runs_count"] = int(row["runs_count"] or 0)
                daily[row_date]["issue_runs_count"] = int(row["issue_runs_count"] or 0)

        assignment_rows = list(
            assignments_qs.filter(assigned_at__date__gte=period_start, assigned_at__date__lte=period_end)
            .values("id", "assigned_at", "status", "status_reason")
            .order_by("assigned_at", "id")
        )
        assignment_date_by_id: dict[int, object] = {}
        canceled_by_delivery_failure = {
            CouponAutoscenarioAssignment.Status.CANCELED,
        }
        for row in assignment_rows:
            row_date = self._local_report_date(row.get("assigned_at"))
            if row_date in daily:
                daily[row_date]["issued_coupons"] += 1
            assignment_id = int(row["id"])
            assignment_date_by_id[assignment_id] = row_date
            if (
                row_date in daily
                and row.get("status") in canceled_by_delivery_failure
                and row.get("status_reason") == COUPON_AUTOSCENARIO_STATUS_REASON_DELIVERY_FAILED
            ):
                daily[row_date]["canceled_delivery_failed_coupons"] += 1

        delivered_assignment_ids: set[int] = set()
        event_id_to_assignment_id: dict[int, int] = {}
        for source_ref, event in delivery_context.get("event_by_source_ref", {}).items():
            assignment_id = self._assignment_id_from_source_ref(source_ref)
            if assignment_id is not None:
                event_id_to_assignment_id[int(event.id)] = assignment_id
        for task in delivery_context.get("task_rows", []):
            if task.status != DispatchTask.Status.DONE or not task.notification_event_id:
                continue
            assignment_id = event_id_to_assignment_id.get(int(task.notification_event_id))
            if assignment_id is not None:
                delivered_assignment_ids.add(assignment_id)
        for assignment_id in delivered_assignment_ids:
            row_date = assignment_date_by_id.get(int(assignment_id))
            if row_date in daily:
                daily[row_date]["delivered_coupons"] += 1

        used_statuses = [
            CouponAutoscenarioAssignment.Status.USED,
            CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN,
        ]
        for row in (
            assignments_qs.filter(status__in=used_statuses, used_business_date__isnull=False)
            .filter(used_business_date__gte=period_start, used_business_date__lte=period_end)
            .values("used_business_date")
            .annotate(total=Count("id"))
        ):
            row_date = row["used_business_date"]
            if row_date in daily:
                daily[row_date]["used_coupons"] += int(row["total"] or 0)

        for row in (
            assignments_qs.filter(status__in=used_statuses, used_business_date__isnull=True)
            .exclude(used_at__isnull=True)
            .filter(used_at__date__gte=period_start, used_at__date__lte=period_end)
            .values("used_at__date")
            .annotate(total=Count("id"))
        ):
            row_date = row["used_at__date"]
            if row_date in daily:
                daily[row_date]["used_coupons"] += int(row["total"] or 0)

        for row in daily.values():
            issued = row["issued_coupons"]
            used = row["used_coupons"]
            row["usage_rate_percent"] = str(round(used * 100 / issued, 1)).replace(".", ",") if issued else "0,0"
        return list(daily.values())

    @staticmethod
    def _resolve_daily_period(*, date_from, date_to):
        today = timezone.localdate()
        if date_from and date_to:
            return date_from, date_to
        if date_from:
            return date_from, today
        if date_to:
            return date_to - timedelta(days=13), date_to
        return today - timedelta(days=13), today

    @staticmethod
    def _iter_dates(start_date, end_date):
        current_date = start_date
        while current_date <= end_date:
            yield current_date
            current_date += timedelta(days=1)

    @classmethod
    def _daily_axis_label(cls, current_date) -> str:
        return f"{current_date.strftime('%d.%m')}\n{cls.weekday_short_labels[current_date.weekday()]}"

    @staticmethod
    def _local_report_date(value):
        if value is None:
            return None
        if timezone.is_aware(value):
            return timezone.localtime(value).date()
        return value.date()

    @staticmethod
    def _format_percent(numerator: int, denominator: int) -> str:
        if not denominator:
            return "0,0"
        return str(round(numerator * 100 / denominator, 1)).replace(".", ",")

    @staticmethod
    def _assignment_source_ref(assignment_id: int) -> str:
        return f"coupon_autoscenario_assignment:{int(assignment_id)}"

    @staticmethod
    def _assignment_id_from_source_ref(source_ref: str | None) -> int | None:
        prefix = "coupon_autoscenario_assignment:"
        raw = str(source_ref or "").strip()
        if not raw.startswith(prefix):
            return None
        suffix = raw[len(prefix) :].strip()
        if not suffix.isdigit():
            return None
        return int(suffix)

    @classmethod
    def _build_empty_followup_snapshot(cls, *, venue_code: str = "") -> dict:
        return {
            "window_days": cls.followup_window_days,
            "assignments_total": 0,
            "unique_guests": 0,
            "observation_complete": 0,
            "observation_pending": 0,
            "returned_after_assignment_guests": 0,
            "returned_after_assignment_rate": "0,0",
            "used_unique_guests": 0,
            "used_observation_complete": 0,
            "used_observation_pending": 0,
            "returned_after_use_guests": 0,
            "returned_after_use_rate": "0,0",
            "orders_after_use": 0,
            "revenue_after_use": _money(0),
            "avg_check_after_use": _money(0),
            "venue_rows": [],
            "product_rank_rows": [],
            "selected_venue_code": venue_code,
            "selected_venue_name": "",
        }

    @classmethod
    def _build_followup_snapshot(cls, *, assignments_qs, venue_code: str = "") -> dict:
        selected_venue_code = _normalize_report_text(venue_code)
        snapshot = cls._build_empty_followup_snapshot(venue_code=selected_venue_code)
        window_days = int(cls.followup_window_days)
        today = timezone.localdate()

        def _local_date(value):
            return value.date() if value else None

        used_statuses = {
            CouponAutoscenarioAssignment.Status.USED,
            CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN,
        }
        assignment_rows = list(
            assignments_qs.exclude(guest_id__isnull=True).values(
                "id",
                "guest_id",
                "assigned_at",
                "status",
                "used_at",
                "used_business_date",
                "venue_code",
                "venue_name",
            )
        )
        prepared_rows: list[dict[str, object]] = []
        used_rows: list[dict[str, object]] = []
        for row in assignment_rows:
            assigned_date = _local_date(row.get("assigned_at"))
            if assigned_date is None:
                continue
            row["_assigned_date"] = assigned_date
            used_date = row.get("used_business_date") or _local_date(row.get("used_at"))
            if row.get("status") in used_statuses and used_date is not None:
                row["_used_date"] = used_date
                used_rows.append(row)
            prepared_rows.append(row)

        if not prepared_rows:
            return snapshot

        guest_ids = sorted({int(row["guest_id"]) for row in prepared_rows if row.get("guest_id")})
        used_guest_ids = sorted({int(row["guest_id"]) for row in used_rows if row.get("guest_id")})
        snapshot["assignments_total"] = len(prepared_rows)
        snapshot["unique_guests"] = len(guest_ids)
        snapshot["observation_complete"] = sum(
            1 for row in prepared_rows if row["_assigned_date"] + timedelta(days=window_days) <= today
        )
        snapshot["observation_pending"] = len(prepared_rows) - int(snapshot["observation_complete"])
        snapshot["used_unique_guests"] = len(used_guest_ids)
        snapshot["used_observation_complete"] = sum(
            1 for row in used_rows if row["_used_date"] + timedelta(days=window_days) <= today
        )
        snapshot["used_observation_pending"] = len(used_rows) - int(snapshot["used_observation_complete"])

        anchor_dates = [row["_assigned_date"] for row in prepared_rows]
        anchor_dates.extend(row["_used_date"] for row in used_rows)
        query_from = min(anchor_dates) + timedelta(days=1)
        query_to = min(max(anchor_dates) + timedelta(days=window_days), today)
        if query_from > query_to:
            return snapshot

        orders_qs = OrderFact.objects.filter(
            guest_id__in=guest_ids,
            business_date__gte=query_from,
            business_date__lte=query_to,
        )
        if selected_venue_code:
            orders_qs = orders_qs.filter(department_id=selected_venue_code)

        orders_by_guest: dict[int, list[dict[str, object]]] = defaultdict(list)
        for order_row in orders_qs.values(
            "id",
            "guest_id",
            "business_date",
            "department_id",
            "department_name",
            "order_number",
            "uniq_order_id",
            "net_sum",
            "gross_sum",
        ).order_by("business_date", "id"):
            if order_row.get("guest_id"):
                orders_by_guest[int(order_row["guest_id"])].append(order_row)

        returned_after_assignment_guest_ids: set[int] = set()
        returned_after_use_guest_ids: set[int] = set()
        post_use_orders_by_key: dict[tuple[object, ...], dict[str, object]] = {}
        post_use_order_identities: set[tuple[object, str, int | None, str]] = set()

        for assignment_row in prepared_rows:
            guest_id = int(assignment_row["guest_id"])
            guest_orders = orders_by_guest.get(guest_id, [])
            assigned_from = assignment_row["_assigned_date"] + timedelta(days=1)
            assigned_to = assignment_row["_assigned_date"] + timedelta(days=window_days)
            used_date = assignment_row.get("_used_date")
            used_from = used_date + timedelta(days=1) if used_date else None
            used_to = used_date + timedelta(days=window_days) if used_date else None

            for order_row in guest_orders:
                business_date = order_row.get("business_date")
                if business_date is None:
                    continue
                if assigned_from <= business_date <= assigned_to:
                    returned_after_assignment_guest_ids.add(guest_id)
                if used_from and used_to and used_from <= business_date <= used_to:
                    returned_after_use_guest_ids.add(guest_id)
                    identity = _order_identity(
                        business_date=order_row.get("business_date"),
                        department_id=order_row.get("department_id"),
                        order_number=order_row.get("order_number"),
                        uniq_order_id=order_row.get("uniq_order_id"),
                    )
                    order_key = ("order", *identity) if identity[0] is not None and identity[2] is not None else (
                        "fact",
                        int(order_row["id"]),
                    )
                    post_use_orders_by_key.setdefault(order_key, order_row)
                    if order_key[0] == "order":
                        post_use_order_identities.add(identity)

        snapshot["returned_after_assignment_guests"] = len(returned_after_assignment_guest_ids)
        snapshot["returned_after_assignment_rate"] = cls._format_percent(
            len(returned_after_assignment_guest_ids),
            len(guest_ids),
        )
        snapshot["returned_after_use_guests"] = len(returned_after_use_guest_ids)
        snapshot["returned_after_use_rate"] = cls._format_percent(
            len(returned_after_use_guest_ids),
            len(used_guest_ids),
        )

        post_use_orders = list(post_use_orders_by_key.values())
        if not post_use_orders:
            return snapshot

        revenue_after_use = sum((_decimal_or_zero(row.get("net_sum")) for row in post_use_orders), Decimal("0"))
        orders_after_use = len(post_use_orders)
        snapshot["orders_after_use"] = orders_after_use
        snapshot["revenue_after_use"] = _money(revenue_after_use)
        snapshot["avg_check_after_use"] = _money(
            revenue_after_use / Decimal(orders_after_use) if orders_after_use else Decimal("0")
        )

        venue_stats: dict[str, dict[str, object]] = {}
        for order_row in post_use_orders:
            venue_key = _normalize_report_text(order_row.get("department_id"))
            venue_row = venue_stats.setdefault(
                venue_key,
                {
                    "venue_code": venue_key,
                    "venue_name": _normalize_report_text(order_row.get("department_name")) or venue_key or "Не указано",
                    "orders_count": 0,
                    "guests": set(),
                    "revenue_net": Decimal("0"),
                },
            )
            venue_row["orders_count"] = int(venue_row["orders_count"]) + 1
            if order_row.get("guest_id"):
                venue_row["guests"].add(int(order_row["guest_id"]))
            venue_row["revenue_net"] = venue_row["revenue_net"] + _decimal_or_zero(order_row.get("net_sum"))

        venue_rows = []
        selected_venue_name = ""
        for venue_row in sorted(
            venue_stats.values(),
            key=lambda row: (-int(row["orders_count"]), str(row["venue_name"])),
        ):
            orders_count = int(venue_row["orders_count"])
            revenue_value = venue_row["revenue_net"]
            avg_check = revenue_value / Decimal(orders_count) if orders_count else Decimal("0")
            is_selected = selected_venue_code and selected_venue_code == venue_row["venue_code"]
            if is_selected:
                selected_venue_name = str(venue_row["venue_name"])
            venue_rows.append(
                {
                    "venue_code": venue_row["venue_code"],
                    "venue_name": venue_row["venue_name"],
                    "orders_count": orders_count,
                    "unique_guests": len(venue_row["guests"]),
                    "revenue_net": _money(revenue_value),
                    "avg_check": _money(avg_check),
                    "is_selected": bool(is_selected),
                }
            )
        snapshot["venue_rows"] = venue_rows
        snapshot["selected_venue_name"] = selected_venue_name if selected_venue_name else selected_venue_code

        if not post_use_order_identities:
            return snapshot

        order_dates = sorted({identity[0] for identity in post_use_order_identities if identity[0]})
        order_numbers = sorted({identity[2] for identity in post_use_order_identities if identity[2] is not None})
        order_uniq_ids = sorted({identity[3] for identity in post_use_order_identities if identity[3]})
        raw_filter = Q()
        if order_uniq_ids:
            raw_filter |= Q(uniq_order_id__in=order_uniq_ids)
        if order_numbers:
            raw_filter |= Q(order_number__in=order_numbers)
        if not order_dates or not raw_filter:
            return snapshot

        product_stats: dict[tuple[str, str], dict[str, object]] = {}
        for raw_row in (
            OlapSalesRawLine.objects.filter(raw_filter, business_date__in=order_dates)
            .values(
                "business_date",
                "department_id",
                "order_number",
                "uniq_order_id",
                "dish_code",
                "dish_name",
                "dish_amount",
                "dish_sum_before_discount",
                "dish_sum_after_discount",
            )
            .order_by("business_date", "order_number", "id")
        ):
            raw_identity = _order_identity(
                business_date=raw_row.get("business_date"),
                department_id=raw_row.get("department_id"),
                order_number=raw_row.get("order_number"),
                uniq_order_id=raw_row.get("uniq_order_id"),
            )
            if raw_identity not in post_use_order_identities:
                continue
            product_key = (
                _normalize_report_text(raw_row.get("dish_code")),
                _normalize_report_text(raw_row.get("dish_name")) or "Без названия",
            )
            product_row = product_stats.setdefault(
                product_key,
                {
                    "dish_code": product_key[0],
                    "dish_name": product_key[1],
                    "orders": set(),
                    "quantity_total": Decimal("0"),
                    "gross_sum": Decimal("0"),
                    "revenue_net": Decimal("0"),
                },
            )
            product_row["orders"].add(raw_identity)
            product_row["quantity_total"] = product_row["quantity_total"] + _raw_line_quantity(raw_row)
            product_row["gross_sum"] = product_row["gross_sum"] + _raw_line_gross_sum(raw_row)
            product_row["revenue_net"] = product_row["revenue_net"] + _raw_line_net_sum(raw_row)

        snapshot["product_rank_rows"] = sorted(
            [
                {
                    "dish_code": row["dish_code"],
                    "dish_name": row["dish_name"],
                    "orders_count": len(row["orders"]),
                    "quantity_total": str(row["quantity_total"]),
                    "gross_sum": _money(row["gross_sum"]),
                    "revenue_net": _money(row["revenue_net"]),
                }
                for row in product_stats.values()
            ],
            key=lambda row: (
                -int(row["orders_count"]),
                -_decimal_or_zero(row["revenue_net"]),
                str(row["dish_name"]),
            ),
        )[:10]
        return snapshot

    @staticmethod
    def _build_empty_revenue_snapshot(*, venue_code: str = "") -> dict:
        return {
            "orders_total": 0,
            "unique_guests": 0,
            "revenue_net": _money(0),
            "avg_check": _money(0),
            "daily_rows": [],
            "venue_rows": [],
            "product_rank_rows": [],
            "selected_venue_code": venue_code,
            "selected_venue_name": "",
        }

    @staticmethod
    def _build_revenue_snapshot(*, assignments_qs, date_from, date_to, venue_code: str = "") -> dict:
        selected_venue_code = _normalize_report_text(venue_code)
        used_rows = list(
            assignments_qs.filter(
                status__in=[
                    CouponAutoscenarioAssignment.Status.USED,
                    CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN,
                ]
            ).values(
                "id",
                "guest_id",
                "coupon_series",
                "coupon_code",
                "used_order_id",
                "used_business_date",
            )
        )
        if not used_rows:
            return CouponAutoscenarioReportsView._build_empty_revenue_snapshot(venue_code=selected_venue_code)

        series = sorted({_normalize_report_text(row["coupon_series"]) for row in used_rows if row["coupon_series"]})
        codes = sorted({_normalize_report_text(row["coupon_code"]) for row in used_rows if row["coupon_code"]})
        if not series or not codes:
            return CouponAutoscenarioReportsView._build_empty_revenue_snapshot(venue_code=selected_venue_code)

        specific_keys = set()
        loose_keys = set()
        for row in used_rows:
            base_key = (
                row["guest_id"],
                _normalize_report_text(row["coupon_series"]),
                _normalize_report_text(row["coupon_code"]),
            )
            if row["used_order_id"] and row["used_business_date"]:
                specific_keys.add((*base_key, int(row["used_order_id"]), row["used_business_date"]))
            else:
                loose_keys.add(base_key)

        order_facts_qs = OrderFact.objects.filter(
            coupon_used=True,
            coupon_series__in=series,
            coupon_number__in=codes,
        )
        if date_from:
            order_facts_qs = order_facts_qs.filter(business_date__gte=date_from)
        if date_to:
            order_facts_qs = order_facts_qs.filter(business_date__lte=date_to)

        matched_order_rows: list[dict[str, object]] = []
        counted_order_keys: set[tuple[object, ...]] = set()
        for fact_row in order_facts_qs.values(
            "id",
            "guest_id",
            "business_date",
            "department_id",
            "department_name",
            "order_number",
            "uniq_order_id",
            "coupon_series",
            "coupon_number",
            "net_sum",
            "gross_sum",
        ).order_by("business_date", "id"):
            base_key = (
                fact_row.get("guest_id"),
                _normalize_report_text(fact_row.get("coupon_series")),
                _normalize_report_text(fact_row.get("coupon_number")),
            )
            specific_key = (
                *base_key,
                int(fact_row["order_number"]) if fact_row.get("order_number") is not None else None,
                fact_row.get("business_date"),
            )
            if specific_key not in specific_keys and base_key not in loose_keys:
                continue

            identity = _order_identity(
                business_date=fact_row.get("business_date"),
                department_id=fact_row.get("department_id"),
                order_number=fact_row.get("order_number"),
                uniq_order_id=fact_row.get("uniq_order_id"),
            )
            order_key = (
                ("order", *identity)
                if identity[0] is not None and identity[2] is not None
                else ("coupon", *base_key)
            )
            if order_key in counted_order_keys:
                continue
            counted_order_keys.add(order_key)
            matched_order_rows.append(
                {
                    "fact": fact_row,
                    "base_key": (_normalize_report_text(base_key[1]), _normalize_report_text(base_key[2])),
                    "identity": identity,
                    "order_key": order_key,
                }
            )

        if not matched_order_rows:
            return CouponAutoscenarioReportsView._build_empty_revenue_snapshot(venue_code=selected_venue_code)

        order_dates = sorted({item["fact"]["business_date"] for item in matched_order_rows if item["fact"].get("business_date")})
        order_uniq_ids = sorted({item["identity"][3] for item in matched_order_rows if item["identity"][3]})
        raw_filter = Q(coupon_series__in=series, coupon_number__in=codes)
        if order_uniq_ids:
            raw_filter |= Q(uniq_order_id__in=order_uniq_ids)

        raw_lines_qs = OlapSalesRawLine.objects.filter(raw_filter)
        if order_dates:
            raw_lines_qs = raw_lines_qs.filter(business_date__in=order_dates)

        raw_lines_by_identity: dict[tuple[object, str, int | None, str], list[dict[str, object]]] = {}
        raw_lines_by_coupon_key: dict[tuple[str, str], list[dict[str, object]]] = {}
        for raw_row in raw_lines_qs.values(
            "id",
            "business_date",
            "department_id",
            "department_name",
            "order_number",
            "uniq_order_id",
            "dish_code",
            "dish_name",
            "dish_amount",
            "dish_sum_before_discount",
            "dish_sum_after_discount",
            "coupon_series",
            "coupon_number",
        ).order_by("business_date", "order_number", "id"):
            raw_identity = _order_identity(
                business_date=raw_row.get("business_date"),
                department_id=raw_row.get("department_id"),
                order_number=raw_row.get("order_number"),
                uniq_order_id=raw_row.get("uniq_order_id"),
            )
            raw_lines_by_identity.setdefault(raw_identity, []).append(raw_row)
            raw_coupon_key = (
                _normalize_report_text(raw_row.get("coupon_series")),
                _normalize_report_text(raw_row.get("coupon_number")),
            )
            if raw_coupon_key[0] and raw_coupon_key[1]:
                raw_lines_by_coupon_key.setdefault(raw_coupon_key, []).append(raw_row)

        for item in matched_order_rows:
            raw_lines = raw_lines_by_identity.get(item["identity"]) or raw_lines_by_coupon_key.get(item["base_key"], [])
            item["raw_lines"] = raw_lines
            if raw_lines:
                item["order_net_sum"] = sum((_raw_line_net_sum(row) for row in raw_lines), Decimal("0"))
            else:
                item["order_net_sum"] = _decimal_or_zero(item["fact"].get("net_sum"))

        venue_stats: dict[str, dict[str, object]] = {}
        for item in matched_order_rows:
            fact_row = item["fact"]
            venue_key = _normalize_report_text(fact_row.get("department_id"))
            venue_row = venue_stats.setdefault(
                venue_key,
                {
                    "venue_code": venue_key,
                    "venue_name": _normalize_report_text(fact_row.get("department_name")) or venue_key or "Не указано",
                    "orders_count": 0,
                    "guests": set(),
                    "revenue_net": Decimal("0"),
                },
            )
            venue_row["orders_count"] = int(venue_row["orders_count"]) + 1
            if fact_row.get("guest_id"):
                venue_row["guests"].add(int(fact_row["guest_id"]))
            venue_row["revenue_net"] = venue_row["revenue_net"] + item["order_net_sum"]

        selected_venue_name = ""
        venue_rows = []
        for venue_row in sorted(
            venue_stats.values(),
            key=lambda row: (-int(row["orders_count"]), str(row["venue_name"])),
        ):
            orders_count = int(venue_row["orders_count"])
            revenue_value = venue_row["revenue_net"]
            avg_check = revenue_value / Decimal(orders_count) if orders_count else Decimal("0")
            is_selected = selected_venue_code and selected_venue_code == venue_row["venue_code"]
            if is_selected:
                selected_venue_name = str(venue_row["venue_name"])
            venue_rows.append(
                {
                    "venue_code": venue_row["venue_code"],
                    "venue_name": venue_row["venue_name"],
                    "orders_count": orders_count,
                    "unique_guests": len(venue_row["guests"]),
                    "revenue_net": _money(revenue_value),
                    "avg_check": _money(avg_check),
                    "is_selected": bool(is_selected),
                }
            )
        if selected_venue_code and not selected_venue_name:
            selected_venue_name = selected_venue_code

        revenue_net = Decimal("0")
        orders_total = 0
        unique_guests = set()
        daily = defaultdict(lambda: {"orders_count": 0, "revenue_net": Decimal("0")})
        product_stats: dict[tuple[str, str], dict[str, object]] = {}

        for item in matched_order_rows:
            fact_row = item["fact"]
            fact_venue_code = _normalize_report_text(fact_row.get("department_id"))
            if selected_venue_code and fact_venue_code != selected_venue_code:
                continue

            orders_total += 1
            if fact_row.get("guest_id"):
                unique_guests.add(int(fact_row["guest_id"]))
            fact_net = item["order_net_sum"]
            revenue_net += fact_net
            daily_row = daily[fact_row["business_date"]]
            daily_row["orders_count"] += 1
            daily_row["revenue_net"] += fact_net

            for raw_row in item.get("raw_lines") or []:
                product_key = (
                    _normalize_report_text(raw_row.get("dish_code")),
                    _normalize_report_text(raw_row.get("dish_name")) or "Без названия",
                )
                product_row = product_stats.setdefault(
                    product_key,
                    {
                        "dish_code": product_key[0],
                        "dish_name": product_key[1],
                        "orders": set(),
                        "quantity_total": Decimal("0"),
                        "gross_sum": Decimal("0"),
                        "revenue_net": Decimal("0"),
                    },
                )
                product_row["orders"].add(item["order_key"])
                product_row["quantity_total"] = product_row["quantity_total"] + _raw_line_quantity(raw_row)
                product_row["gross_sum"] = product_row["gross_sum"] + _raw_line_gross_sum(raw_row)
                product_row["revenue_net"] = product_row["revenue_net"] + _raw_line_net_sum(raw_row)

        avg_check = revenue_net / orders_total if orders_total else Decimal("0")
        daily_rows = [
            {
                "business_date": business_date.isoformat(),
                "orders_count": row["orders_count"],
                "revenue_net": _money(row["revenue_net"]),
            }
            for business_date, row in sorted(daily.items())
        ]
        product_rank_rows = sorted(
            [
                {
                    "dish_code": row["dish_code"],
                    "dish_name": row["dish_name"],
                    "orders_count": len(row["orders"]),
                    "quantity_total": str(row["quantity_total"]),
                    "gross_sum": _money(row["gross_sum"]),
                    "revenue_net": _money(row["revenue_net"]),
                }
                for row in product_stats.values()
            ],
            key=lambda row: (
                -int(row["orders_count"]),
                -_decimal_or_zero(row["revenue_net"]),
                str(row["dish_name"]),
            ),
        )[:10]
        return {
            "orders_total": orders_total,
            "unique_guests": len(unique_guests),
            "revenue_net": _money(revenue_net),
            "avg_check": _money(avg_check),
            "daily_rows": daily_rows,
            "venue_rows": venue_rows,
            "product_rank_rows": product_rank_rows,
            "selected_venue_code": selected_venue_code,
            "selected_venue_name": selected_venue_name,
        }


class CouponRegistryView(TemplateView):
    """
    Экран «Реестр купонов».

    Показывает:
    1. текущий статус каждого купона;
    2. статус проверки в iikoCard;
    3. связь с кампанией и гостем (если купон назначен).
    """

    template_name = "reports/coupon_registry.html"
    page_size = 100

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        series = str(self.request.GET.get("series") or "").strip()
        batch_code = str(self.request.GET.get("batch_code") or "").strip()
        venue_code = str(self.request.GET.get("venue_code") or "").strip()
        pool_status = str(self.request.GET.get("pool_status") or "").strip()
        iiko_check_status = str(self.request.GET.get("iiko_check_status") or "").strip()
        verified_from_raw = str(self.request.GET.get("verified_from") or "").strip()
        verified_to_raw = str(self.request.GET.get("verified_to") or "").strip()
        campaign_id = _parse_positive_int(self.request.GET.get("campaign_id"))

        verified_from = parse_date(verified_from_raw) if verified_from_raw else None
        verified_to = parse_date(verified_to_raw) if verified_to_raw else None

        coupons_qs = (
            CouponRegistryEntry.objects.select_related("batch")
            .prefetch_related(
                Prefetch(
                    "campaign_assignments",
                    queryset=CouponCampaignAssignment.objects.select_related("campaign", "guest").order_by("-assigned_at"),
                    to_attr="campaign_assignments_for_ui",
                ),
                Prefetch(
                    "autoscenario_assignments",
                    queryset=CouponAutoscenarioAssignment.objects.select_related("scenario", "guest", "run").order_by(
                        "-assigned_at"
                    ),
                    to_attr="autoscenario_assignments_for_ui",
                ),
            )
            .order_by("-id")
        )

        if series:
            coupons_qs = coupons_qs.filter(series__icontains=series)
        if batch_code:
            coupons_qs = coupons_qs.filter(batch__batch_code__icontains=batch_code)
        if venue_code:
            coupons_qs = coupons_qs.filter(venue_code__icontains=venue_code)
        if pool_status:
            coupons_qs = coupons_qs.filter(pool_status=pool_status)
        if iiko_check_status:
            coupons_qs = coupons_qs.filter(iiko_check_status=iiko_check_status)
        if verified_from:
            coupons_qs = coupons_qs.filter(iiko_checked_at__date__gte=verified_from)
        if verified_to:
            coupons_qs = coupons_qs.filter(iiko_checked_at__date__lte=verified_to)
        if campaign_id:
            coupons_qs = coupons_qs.filter(campaign_assignments__campaign_id=campaign_id)

        coupons_qs = coupons_qs.distinct()

        paginator = Paginator(coupons_qs, self.page_size)
        page_obj = paginator.get_page(self.request.GET.get("page"))
        coupons_rows = list(page_obj.object_list)
        for coupon in coupons_rows:
            campaign_assignments = list(getattr(coupon, "campaign_assignments_for_ui", []))
            autoscenario_assignments = list(getattr(coupon, "autoscenario_assignments_for_ui", []))
            if campaign_id:
                visible_assignment = next(
                    (assignment for assignment in campaign_assignments if assignment.campaign_id == campaign_id),
                    None,
                )
                visible_source = "campaign" if visible_assignment else None
            else:
                visible_source, visible_assignment = self._latest_coupon_assignment_for_ui(
                    campaign_assignments=campaign_assignments,
                    autoscenario_assignments=autoscenario_assignments,
                )
            self._attach_assignment_ui_fields(
                coupon=coupon,
                assignment=visible_assignment,
                source=visible_source,
            )

        total_filtered = int(coupons_qs.count())
        # Счётчики считаем явными запросами, чтобы логика была прозрачной для сопровождения.
        available_total = int(
            coupons_qs.filter(
                is_active=True,
                pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            ).count()
        )
        assigned_total = int(coupons_qs.filter(pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED).count())
        used_total = int(coupons_qs.filter(pool_status=CouponRegistryEntry.PoolStatus.USED).count())
        check_error_total = int(
            coupons_qs.filter(iiko_check_status=CouponRegistryEntry.IikoCheckStatus.CHECK_ERROR).count()
        )

        query_without_page = self.request.GET.copy()
        if "page" in query_without_page:
            query_without_page.pop("page")
        query_tail = query_without_page.urlencode()
        pagination_query = f"&{query_tail}" if query_tail else ""

        context["coupons_page"] = page_obj
        context["coupons_rows"] = coupons_rows
        context["pagination_query"] = pagination_query
        context["registry_stats"] = {
            "total_filtered": total_filtered,
            "available_total": available_total,
            "assigned_total": assigned_total,
            "used_total": used_total,
            "check_error_total": check_error_total,
        }
        context["filters"] = {
            "series": series,
            "batch_code": batch_code,
            "venue_code": venue_code,
            "pool_status": pool_status,
            "iiko_check_status": iiko_check_status,
            "verified_from": verified_from_raw,
            "verified_to": verified_to_raw,
            "campaign_id": campaign_id or "",
        }
        context["pool_status_choices"] = CouponRegistryEntry.PoolStatus.choices
        context["iiko_check_status_choices"] = CouponRegistryEntry.IikoCheckStatus.choices
        context["coupon_campaign_reports_url"] = reverse("reports_coupon_campaigns")
        context["coupon_generation_url"] = reverse("coupon_generation")
        return context

    @staticmethod
    def _latest_coupon_assignment_for_ui(
        *,
        campaign_assignments: list[CouponCampaignAssignment],
        autoscenario_assignments: list[CouponAutoscenarioAssignment],
    ) -> tuple[str | None, CouponCampaignAssignment | CouponAutoscenarioAssignment | None]:
        candidates: list[tuple[str, CouponCampaignAssignment | CouponAutoscenarioAssignment]] = [
            ("campaign", assignment) for assignment in campaign_assignments
        ]
        candidates.extend(("autoscenario", assignment) for assignment in autoscenario_assignments)
        if not candidates:
            return None, None
        source, assignment = max(
            candidates,
            key=lambda item: item[1].assigned_at or item[1].created_at,
        )
        return source, assignment

    @staticmethod
    def _attach_assignment_ui_fields(
        *,
        coupon: CouponRegistryEntry,
        assignment: CouponCampaignAssignment | CouponAutoscenarioAssignment | None,
        source: str | None,
    ) -> None:
        coupon.latest_assignment = assignment
        coupon.latest_assignment_source = source
        coupon.latest_assignment_source_label = ""
        coupon.latest_assignment_source_detail = ""
        coupon.latest_assignment_source_url = ""

        if assignment is None:
            return

        if source == "campaign":
            coupon.latest_assignment_source_label = f"Кампания #{assignment.campaign_id}"
            coupon.latest_assignment_source_detail = assignment.campaign.name if assignment.campaign else ""
            coupon.latest_assignment_source_url = reverse("mailings_v2_campaigns_status", args=[assignment.campaign_id])
            return

        if source == "autoscenario":
            scenario_code = assignment.scenario.code if assignment.scenario else ""
            coupon.latest_assignment_source_label = "Автосценарий"
            coupon.latest_assignment_source_detail = (
                f"{scenario_code}, run #{assignment.run_id}" if scenario_code else f"run #{assignment.run_id}"
            )
            coupon.latest_assignment_source_url = (
                f"{reverse('mailings_v2_scenarios')}?{urlencode({'coupon_scenario_code': scenario_code})}"
                if scenario_code
                else reverse("mailings_v2_scenarios")
            )


class CouponGenerationView(TemplateView):
    """
    Экран операций с купонными пулами.

    Реестр купонов оставляем только для просмотра и фильтрации. На этой странице
    оператор выполняет прикладные действия: генерацию CSV, проверку загрузки в
    iikoCard и повторное скачивание CSV по коду партии.
    """

    template_name = "reports/coupon_generation.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        batch_code = str(self.request.GET.get("batch_code") or "").strip()
        series_hint = str(self.request.GET.get("series_hint") or "").strip()
        selected_batch = (
            CouponPoolBatch.objects.filter(batch_code=batch_code).first()
            if batch_code
            else None
        )
        recent_batches_qs = CouponPoolBatch.objects.order_by("-generated_at")
        if series_hint:
            recent_batches_qs = recent_batches_qs.filter(series__icontains=series_hint)

        context["selected_batch"] = selected_batch
        context["series_hint"] = series_hint
        context["recent_batches"] = list(recent_batches_qs[:20])
        context["coupon_ops_url"] = reverse("coupon_registry_ops")
        context["coupon_registry_url"] = reverse("coupon_registry")
        context["coupon_campaign_reports_url"] = reverse("reports_coupon_campaigns")
        context["alphabet_mode_choices"] = CouponPoolBatch.AlphabetMode.choices
        context["coupon_venue_choices"], _ = build_coupon_venue_choices()
        context["generate_command_hint"] = (
            "python manage.py generate_coupon_pool --series <SERIES> --venue-code <VENUE_CODE> "
            "--prefix TST- --count 1000 --random-length 12"
        )
        context["verify_command_hint"] = (
            "python manage.py verify_coupon_pool_iiko --series <SERIES> --sample-info-check-limit 50"
        )
        return context


class CouponRegistryOpsView(View):
    """
    POST-операции для экрана реестра купонов.

    Доступные действия:
    1. `generate_pool` — создать пул и экспортировать CSV;
    2. `verify_pool` — запустить проверку загрузки купонов в iikoCard;
    3. `download_csv` — скачать уже сформированный CSV по batch.
    """

    http_method_names = ["post"]

    @staticmethod
    def _resolve_next_url(request) -> str:
        """
        Возвращает безопасный URL возврата после POST-операции.
        """
        default_url = reverse("coupon_registry")
        next_url = str(request.POST.get("next") or "").strip()
        if next_url and url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return next_url
        return default_url

    @staticmethod
    def _redirect_with_query(base_url: str, query: dict[str, str | int]) -> HttpResponseRedirect:
        normalized = {k: str(v) for k, v in query.items() if str(v or "").strip()}
        if normalized:
            return redirect(f"{base_url}?{urlencode(normalized)}")
        return redirect(base_url)

    def post(self, request, *args, **kwargs):
        action = str(request.POST.get("action") or "").strip()
        if action == "generate_pool":
            return self._handle_generate_pool(request)
        if action == "verify_pool":
            return self._handle_verify_pool(request)
        if action == "download_csv":
            return self._handle_download_csv(request)

        messages.error(request, "Неизвестная операция реестра купонов.")
        return redirect(self._resolve_next_url(request))

    def _handle_generate_pool(self, request):
        series = str(request.POST.get("series") or "").strip()
        venue_code = str(request.POST.get("venue_code") or "").strip()
        prefix = str(request.POST.get("prefix") or "").strip().upper()
        batch_code = str(request.POST.get("batch_code") or "").strip()
        generated_by = str(request.POST.get("generated_by") or "").strip()
        export_path_override = str(request.POST.get("export_path") or "").strip()
        alphabet_mode = str(request.POST.get("alphabet_mode") or "").strip()
        include_optional_fields = str(request.POST.get("include_optional_fields") or "").strip() in {"1", "on", "true"}

        count = _parse_positive_int(request.POST.get("count"))
        random_length = _parse_positive_int(request.POST.get("random_length"))

        if not series:
            messages.error(request, "Серия купонов обязательна для генерации.")
            return redirect(self._resolve_next_url(request))
        if not venue_code:
            messages.error(request, "Выберите заведение для генерации пула.")
            return redirect(self._resolve_next_url(request))
        _, venue_map = build_coupon_venue_choices()
        if venue_code not in venue_map:
            messages.error(request, "Выбранное заведение не найдено в справочнике активных заведений.")
            return redirect(self._resolve_next_url(request))
        venue_name = venue_map[venue_code]
        if count is None:
            messages.error(request, "Количество купонов должно быть положительным числом.")
            return redirect(self._resolve_next_url(request))
        if random_length is None:
            messages.error(request, "Длина случайной части должна быть положительным числом.")
            return redirect(self._resolve_next_url(request))
        if alphabet_mode not in {choice[0] for choice in CouponPoolBatch.AlphabetMode.choices}:
            messages.error(request, "Некорректный режим алфавита генерации.")
            return redirect(self._resolve_next_url(request))

        if not generated_by:
            user = getattr(request, "user", None)
            generated_by = str(getattr(user, "username", "") or "").strip() or "ui_operator"

        service = CouponPoolService()
        try:
            result = service.generate_pool(
                series=series,
                prefix=prefix,
                venue_code=venue_code,
                venue_name=venue_name or None,
                count=count,
                random_length=random_length,
                alphabet_mode=alphabet_mode,
                generated_by=generated_by,
                batch_code=batch_code or None,
            )
            if export_path_override:
                export_path = Path(export_path_override)
            else:
                suffix = "series_number_optional" if include_optional_fields else "series_number"
                export_name = (
                    f"iikocard_coupon_import_{_safe_token(series)}_{_safe_token(prefix)}_"
                    f"{count}_{suffix}.csv"
                )
                export_path = Path("tools") / export_name
            csv_path = service.export_batch_csv(
                batch=result.batch,
                output_path=str(export_path),
                include_optional_fields=include_optional_fields,
            )
        except CouponPoolGenerationError as exc:
            messages.error(request, f"Ошибка генерации пула купонов: {exc}")
            return redirect(self._resolve_next_url(request))

        messages.success(
            request,
            (
                f"Пул создан: batch={result.batch.batch_code} (series={result.batch.series}, "
                f"count={result.created_count}, collisions={result.collisions_count}). "
                f"CSV: {csv_path}"
            ),
        )
        return self._redirect_with_query(
            reverse("coupon_generation"),
            {"batch_code": result.batch.batch_code},
        )

    def _handle_verify_pool(self, request):
        series = str(request.POST.get("series") or "").strip()
        batch_code = str(request.POST.get("batch_code") or "").strip()
        sample_info_check_limit = _parse_positive_int(request.POST.get("sample_info_check_limit")) or 2
        page_size = _parse_positive_int(request.POST.get("page_size")) or 500
        max_pages = _parse_positive_int(request.POST.get("max_pages")) or 200

        if not series and not batch_code:
            messages.error(request, "Для проверки укажите серию или batch-код.")
            return redirect(self._resolve_next_url(request))

        try:
            call_command(
                "verify_coupon_pool_iiko",
                series=series,
                batch_code=batch_code,
                sample_info_check_limit=sample_info_check_limit,
                page_size=page_size,
                max_pages=max_pages,
                dry_run=False,
            )
        except CommandError as exc:
            messages.error(request, f"Проверка в iikoCard завершилась с ошибкой: {exc}")
            return redirect(self._resolve_next_url(request))

        if batch_code:
            batch = CouponPoolBatch.objects.filter(batch_code=batch_code).first()
            if batch:
                messages.success(
                    request,
                    (
                        f"Проверка завершена: batch={batch.batch_code}, "
                        f"verification_status={batch.verification_status}, "
                        f"found={batch.verified_found_count}, not_found={batch.verified_not_found_count}."
                    ),
                )
                return self._redirect_with_query(reverse("coupon_generation"), {"batch_code": batch.batch_code})

        messages.success(
            request,
            "Проверка загрузки купонов в iikoCard успешно завершена.",
        )
        return self._redirect_with_query(
            reverse("coupon_registry"),
            {"series": series},
        )

    def _handle_download_csv(self, request):
        batch_code = str(request.POST.get("batch_code") or "").strip()
        if not batch_code:
            messages.error(request, "Укажите batch-код для скачивания CSV.")
            return redirect(self._resolve_next_url(request))

        batch = CouponPoolBatch.objects.filter(batch_code=batch_code).first()
        if batch is None:
            series_batches_exists = CouponPoolBatch.objects.filter(series=batch_code).exists()
            if series_batches_exists:
                messages.error(
                    request,
                    (
                        f"`{batch_code}` выглядит как серия купонов, а для скачивания нужен код партии. "
                        "Ниже показаны партии этой серии."
                    ),
                )
                return self._redirect_with_query(reverse("coupon_generation"), {"series_hint": batch_code})
            messages.error(request, f"Партия `{batch_code}` не найдена.")
            return redirect(self._resolve_next_url(request))
        csv_path = Path(batch.export_file_path).expanduser() if batch.export_file_path else _default_batch_csv_path(batch)
        if not csv_path.is_file():
            try:
                csv_path = CouponPoolService().export_batch_csv(
                    batch=batch,
                    output_path=str(csv_path),
                    include_optional_fields=False,
                )
            except (CouponPoolGenerationError, OSError) as exc:
                messages.error(request, f"CSV-файл не найден и не восстановлен: {exc}")
                return self._redirect_with_query(reverse("coupon_generation"), {"batch_code": batch.batch_code})

        return FileResponse(
            csv_path.open("rb"),
            as_attachment=True,
            filename=csv_path.name,
            content_type="text/csv",
        )


class CouponCampaignReportsView(TemplateView):
    """
    Раздел отчётов «Купонные кампании».

    Доступны два сценария:
    1. выбор кампании из списка и просмотр KPI;
    2. прямой вход по query `campaign_id` (например, из карточки кампании).
    """

    template_name = "reports/coupon_campaigns.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        query = str(self.request.GET.get("q") or "").strip()
        campaign_id = _parse_positive_int(self.request.GET.get("campaign_id"))

        campaigns_qs = (
            Mailing.objects.exclude(coupon_series__isnull=True)
            .exclude(coupon_series="")
            .order_by("-scheduled_date", "-id")
        )
        if query:
            campaign_filter = Q(name__icontains=query) | Q(coupon_series__icontains=query)
            if query.isdigit():
                campaign_filter = campaign_filter | Q(id=int(query))
            campaigns_qs = campaigns_qs.filter(campaign_filter)

        campaigns = list(campaigns_qs[:200])
        campaigns_map = {campaign.id: campaign for campaign in campaigns}

        selected_campaign = None
        report_dict = None
        report_error = ""
        if campaign_id:
            selected_campaign = campaigns_map.get(campaign_id)
            if selected_campaign is None:
                selected_campaign = (
                    Mailing.objects.exclude(coupon_series__isnull=True)
                    .exclude(coupon_series="")
                    .filter(id=campaign_id)
                    .first()
                )

            if selected_campaign:
                try:
                    report_dict = build_coupon_campaign_performance_snapshot(mailing=selected_campaign).to_dict()
                except Exception as exc:  # noqa: BLE001
                    report_error = (
                        "Не удалось построить купонный отчёт. "
                        "Проверьте логи сервиса и корректность данных кампании."
                    )
                    context["report_error_debug"] = str(exc)
            else:
                report_error = "Кампания не найдена или не является купонной."

        context["campaigns"] = campaigns
        context["filters"] = {
            "q": query,
            "campaign_id": campaign_id or "",
        }
        context["selected_campaign"] = selected_campaign
        context["coupon_campaign_report"] = report_dict
        context["coupon_campaign_report_error"] = report_error
        context["registry_url"] = reverse("coupon_registry")
        context["back_to_reports_url"] = reverse("reports")
        if selected_campaign:
            status_url = reverse("mailings_v2_campaigns_status", kwargs={"pk": selected_campaign.id})
            context["campaign_status_url"] = status_url
            context["registry_for_campaign_url"] = f"{reverse('coupon_registry')}?{urlencode({'campaign_id': selected_campaign.id})}"
        else:
            context["campaign_status_url"] = ""
            context["registry_for_campaign_url"] = ""
        return context
