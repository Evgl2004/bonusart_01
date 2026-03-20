"""
Представления пользовательской аналитики и дашборда.
"""

from __future__ import annotations

from django.views.generic import TemplateView

from guests.services.analytics_dashboard import (
    build_analytics_dashboard_payload,
    normalize_period_days,
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

        payload = build_analytics_dashboard_payload(
            period_days=selected_period_days,
            department_id=selected_department_id,
        )
        context["dashboard_payload"] = payload
        context["selected_period_days"] = payload["filters"]["period_days"]
        context["selected_department_id"] = payload["filters"]["department_id"]
        context["period_options"] = payload["filters"]["period_options"]
        context["department_options"] = payload["filters"]["departments"]
        return context
