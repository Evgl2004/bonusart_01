"""
Представления нового экрана «Гости (workbench)».
"""

from __future__ import annotations

from datetime import date

from django.views.generic import TemplateView

from guests.services.guest_workbench import build_guest_workbench_payload, normalize_window_days


class GuestsWorkbenchView(TemplateView):
    """
    Рабочий экран для маркетолога по гостям.

    Экран не заменяет legacy-список гостей на уровне URL:
    1. новый UX доступен по `guests/workbench/`;
    2. старый список остается по `guests/` для обратной совместимости.
    """

    template_name = "guests/workbench.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        raw_as_of_date = (self.request.GET.get("as_of_date") or "").strip()
        raw_window_days = self.request.GET.get("window_days")
        selected_department_id = (self.request.GET.get("department_id") or "").strip()

        as_of_value = _parse_iso_date(raw_as_of_date)
        selected_window_days = normalize_window_days(raw_window_days)

        payload = build_guest_workbench_payload(
            as_of_date=as_of_value,
            window_days=selected_window_days,
            department_id=selected_department_id,
        )

        context["payload"] = payload
        context["selected_as_of_date"] = payload["filters"]["as_of_date"]
        context["selected_window_days"] = payload["filters"]["window_days"]
        context["selected_department_id"] = payload["filters"]["department_id"]
        return context


def _parse_iso_date(raw_value: str) -> date | None:
    """
    Безопасно парсит дату формата YYYY-MM-DD.
    """
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return None

