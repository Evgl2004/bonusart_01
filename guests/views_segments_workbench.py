"""
Представление экрана «Сегменты».

Экран использует единый источник данных workbench и показывает:
1. состав сегментов по выбранному срезу;
2. долю каждого сегмента в общей базе гостей;
3. быстрый переход в «Гости» с уже установленным фильтром сегмента.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from django.urls import reverse
from django.views.generic import TemplateView

from guests.services.guest_workbench import (
    SEGMENT_DEFINITIONS,
    build_guest_workbench_payload,
    normalize_window_days,
)


class SegmentsWorkbenchView(TemplateView):
    """
    Рабочий экран сегментов с базовой аналитикой и быстрыми действиями.
    """

    template_name = "guests/segments_workbench.html"

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
        total_guests = int(payload.get("cards", {}).get("guests_total") or 0)

        rows = []
        segment_totals = payload.get("segments", {})
        filters = payload.get("filters", {})
        selected_as_of_date = (filters.get("as_of_date") or "").strip()
        for code, name in SEGMENT_DEFINITIONS:
            guests_count = int(segment_totals.get(code, 0))
            share_pct = round((guests_count * 100.0 / total_guests), 1) if total_guests > 0 else 0.0

            rows.append(
                {
                    "code": code,
                    "name": name,
                    "guests_count": guests_count,
                    "share_pct": f"{share_pct:.1f}",
                    "details_url": self._build_guest_workbench_url(
                        as_of_date=selected_as_of_date,
                        window_days=selected_window_days,
                        department_id=selected_department_id,
                        segment_code=code,
                    ),
                }
            )

        context["payload"] = payload
        context["segment_rows"] = rows
        context["selected_as_of_date"] = selected_as_of_date
        context["selected_window_days"] = selected_window_days
        context["selected_department_id"] = (filters.get("department_id") or "").strip()
        return context

    @staticmethod
    def _build_guest_workbench_url(
        *,
        as_of_date: str,
        window_days: int,
        department_id: str,
        segment_code: str,
    ) -> str:
        """
        Формирует ссылку в экран гостей с предустановленным сегментом.
        """
        params = {
            "as_of_date": as_of_date,
            "window_days": str(window_days),
            "department_id": department_id,
            "segment_code": segment_code,
        }
        params = {key: value for key, value in params.items() if value}
        base_url = reverse("guests_workbench")
        if not params:
            return base_url
        return f"{base_url}?{urlencode(params)}"


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

