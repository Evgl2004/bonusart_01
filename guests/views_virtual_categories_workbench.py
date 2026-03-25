"""
Представление экрана «Конструктор категорий».

Экран выделен отдельно от целевых категорий, чтобы маркетологу было проще:
1. искать и выбирать номенклатуру;
2. собирать виртуальные категории;
3. опционально сразу создавать целевую категорию на их базе.
"""

from __future__ import annotations

from datetime import date

from django.views.generic import TemplateView

from guests.services.focus_categories_workbench import (
    build_focus_categories_workbench_payload,
)
from guests.services.guest_workbench import normalize_window_days


class VirtualCategoriesWorkbenchView(TemplateView):
    """
    Рабочий экран конструктора виртуальных категорий.
    """

    template_name = "guests/virtual_categories_workbench.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        raw_as_of_date = (self.request.GET.get("as_of_date") or "").strip()
        raw_window_days = self.request.GET.get("window_days")
        selected_department_id = (self.request.GET.get("department_id") or "").strip()
        selected_focus_id = _parse_int(self.request.GET.get("selected_focus_id"))
        selected_nomenclature_query = (self.request.GET.get("nomenclature_query") or "").strip()
        selected_nomenclature_group_query = (self.request.GET.get("nomenclature_group_query") or "").strip()
        selected_nomenclature_olap_category_id = _parse_int(
            self.request.GET.get("nomenclature_olap_category_id")
        )

        payload = build_focus_categories_workbench_payload(
            as_of_date=_parse_iso_date(raw_as_of_date),
            window_days=normalize_window_days(raw_window_days),
            department_id=selected_department_id,
            selected_focus_id=selected_focus_id,
            nomenclature_query=selected_nomenclature_query,
            nomenclature_group_query=selected_nomenclature_group_query,
            nomenclature_olap_category_id=selected_nomenclature_olap_category_id,
        )

        context["payload"] = payload
        context["selected_as_of_date"] = payload["filters"]["as_of_date"]
        context["selected_window_days"] = payload["filters"]["window_days"]
        context["selected_department_id"] = payload["filters"]["department_id"]
        context["selected_focus_id"] = int(payload["filters"]["selected_focus_id"] or 0)
        context["selected_nomenclature_query"] = payload["filters"]["nomenclature_query"]
        context["selected_nomenclature_group_query"] = payload["filters"]["nomenclature_group_query"]
        context["selected_nomenclature_olap_category_id"] = int(
            payload["filters"]["nomenclature_olap_category_id"] or 0
        )
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


def _parse_int(raw_value: str | None) -> int | None:
    """
    Безопасно парсит целое число из query-параметра.
    """
    try:
        parsed = int(raw_value or "")
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
