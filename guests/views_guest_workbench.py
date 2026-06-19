"""
Представления нового экрана «Гости (workbench)».
"""

from __future__ import annotations

from datetime import date

from django.views.generic import TemplateView

from guests.models import BotProfile, Mailing, MessageTemplate
from guests.services.guest_workbench import (
    build_guest_workbench_payload,
    normalize_audience_channel_group,
    normalize_window_days,
)
from guests.services.guest_venue_selection import normalize_venue_selection_mode


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
        selected_venue_selection_mode = normalize_venue_selection_mode(
            self.request.GET.get("venue_selection_mode")
        )
        selected_segment_code = (self.request.GET.get("segment_code") or "").strip()
        selected_focus_category_code = (self.request.GET.get("focus_category_code") or "").strip()
        selected_audience_channel_group = normalize_audience_channel_group(
            self.request.GET.get("audience_channel_group")
        )
        complex_filters = _extract_complex_filters_from_query(self.request)
        show_all_presets = _to_bool_flag(self.request.GET.get("show_all_presets"))

        as_of_value = _parse_iso_date(raw_as_of_date)
        selected_window_days = normalize_window_days(raw_window_days)

        payload = build_guest_workbench_payload(
            as_of_date=as_of_value,
            window_days=selected_window_days,
            department_id=selected_department_id,
            venue_selection_mode=selected_venue_selection_mode,
            segment_code=selected_segment_code,
            focus_category_code=selected_focus_category_code,
            audience_channel_group=selected_audience_channel_group,
            complex_filters=complex_filters,
            show_all_presets=show_all_presets,
        )

        context["payload"] = payload
        context["selected_as_of_date"] = payload["filters"]["as_of_date"]
        context["selected_window_days"] = payload["filters"]["window_days"]
        context["selected_department_id"] = payload["filters"]["department_id"]
        context["selected_venue_selection_mode"] = payload["filters"]["venue_selection_mode"]
        context["selected_segment_code"] = payload["filters"]["segment_code"]
        context["selected_focus_category_code"] = payload["filters"]["focus_category_code"]
        context["selected_audience_channel_group"] = payload["filters"]["audience_channel_group"]
        context["selected_show_all_presets"] = bool(payload["filters"].get("show_all_presets"))
        context["workbench_message_templates"] = MessageTemplate.objects.filter(is_active=True).order_by(
            "-created_at", "name", "id"
        )
        context["workbench_bot_profiles"] = BotProfile.objects.filter(is_active=True).order_by(
            "provider_type", "name", "id"
        )
        context["workbench_target_mode_options"] = Mailing.TargetMode.choices
        context["workbench_queue_priority_options"] = Mailing.QueuePriority.choices
        context["workbench_default_target_mode"] = Mailing.TargetMode.PRIMARY_ONLY
        context["workbench_default_queue_priority"] = Mailing.QueuePriority.BULK
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


def _to_bool_flag(raw_value: str | None) -> bool:
    """
    Нормализует флаг из query-параметров (checkbox/select).
    """
    return (raw_value or "").strip().lower() in {"1", "true", "yes", "on"}


def _extract_complex_filters_from_query(request) -> list[dict[str, str]]:
    """
    Извлекает сложные условия фильтра из query-параметров вида:
    cf_field=...&cf_op=...&cf_value=... (повторяемые параметры).
    """
    fields = request.GET.getlist("cf_field")
    operators = request.GET.getlist("cf_op")
    values = request.GET.getlist("cf_value")

    length = max(len(fields), len(operators), len(values), 0)
    result: list[dict[str, str]] = []
    for idx in range(length):
        result.append(
            {
                "field": (fields[idx] if idx < len(fields) else "").strip(),
                "operator": (operators[idx] if idx < len(operators) else "").strip(),
                "value": (values[idx] if idx < len(values) else "").strip(),
            }
        )
    return result
