"""
Представления пользовательской аналитики и дашборда.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.views.generic import TemplateView
from django.utils import timezone

from guests.services.analytics_dashboard import (
    build_analytics_dashboard_payload,
    normalize_period_days,
)
from guests.services.bots_dashboard import (
    build_bots_dashboard_payload,
    normalize_bots_period_days,
)


class AnalyticsDashboardView(TemplateView):
    """
    Основная страница аналитического дашборда на ECharts.
    """

    template_name = "analytics/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        raw_period_days = self.request.GET.get("period_days")
        selected_period_days = normalize_period_days(raw_period_days)
        selected_department_id = (self.request.GET.get("department_id") or "").strip()
        # Для пользовательского дашборда показываем закрытый день (вчера),
        # чтобы не выводить временные нули за текущие незавершённые сутки.
        as_of_date = timezone.localdate() - timedelta(days=1)

        payload = build_analytics_dashboard_payload(
            period_days=selected_period_days,
            department_id=selected_department_id,
            as_of_date=as_of_date,
        )
        context["dashboard_payload"] = payload
        context["selected_period_days"] = payload["filters"]["period_days"]
        context["selected_department_id"] = payload["filters"]["department_id"]
        context["period_options"] = payload["filters"]["period_options"]
        context["department_options"] = payload["filters"]["departments"]
        return context


class BotsDashboardView(TemplateView):
    """
    Отдельная страница аналитики регистраций в ботах.
    """

    template_name = "analytics/dashboard_bots.html"
    default_days = 30

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        raw_period_days = self.request.GET.get("period_days")
        selected_period_days = normalize_bots_period_days(raw_period_days)
        selected_date_to = _parse_iso_date(self.request.GET.get("date_to")) or timezone.localdate()
        if raw_period_days:
            selected_date_from = selected_date_to - timedelta(days=selected_period_days - 1)
        else:
            selected_date_from = _parse_iso_date(self.request.GET.get("date_from"))
            if selected_date_from is None:
                selected_date_from = selected_date_to - timedelta(days=self.default_days - 1)
            if selected_date_from > selected_date_to:
                selected_date_from = selected_date_to - timedelta(days=self.default_days - 1)

        payload = build_bots_dashboard_payload(
            date_from=selected_date_from,
            date_to=selected_date_to,
            period_days=selected_period_days,
        )
        context["bots_dashboard_payload"] = payload
        context["selected_date_from"] = selected_date_from.isoformat()
        context["selected_date_to"] = selected_date_to.isoformat()
        context["selected_period_days"] = payload["filters"]["period_days"]
        context["period_options"] = payload["filters"]["period_options"]
        return context


def _parse_iso_date(raw_value: str | None):
    text = str(raw_value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
