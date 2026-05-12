"""
Представление экрана «Сегменты».

Экран использует единый источник данных workbench и показывает:
1. состав сегментов по выбранному срезу;
2. долю каждого сегмента в общей базе гостей;
3. быстрый переход в «Гости» с уже установленным фильтром сегмента.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlencode

from django.urls import reverse
from django.views.generic import TemplateView

from guests.services.guest_workbench import (
    SEGMENT_DEFINITIONS,
    build_guest_workbench_payload,
    normalize_window_days,
)

SEGMENT_CHART_LABELS = {
    "active_30d": "Активные 30д",
    "single_visit_30d": "1 визит за 30д",
    "cooling_30_60d": "Остывшие 30-60д",
    "lost_60d_plus": "Потерянные 60+д",
    "bot_active_no_visits_180d": "Активен в боте, без визитов 180д",
}
SEGMENT_CHART_COLORS = {
    "active_30d": "#0e9f6e",
    "single_visit_30d": "#2563eb",
    "cooling_30_60d": "#f59e0b",
    "lost_60d_plus": "#ef4444",
    "bot_active_no_visits_180d": "#7c3aed",
}


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
        total_unique_guests = int(payload.get("cards", {}).get("guests_total") or 0)

        rows = []
        segment_totals = payload.get("segments", {})
        segment_base_total = sum(int(segment_totals.get(code, 0)) for code, _ in SEGMENT_DEFINITIONS)
        filters = payload.get("filters", {})
        selected_as_of_date = (filters.get("as_of_date") or "").strip()
        for code, name in SEGMENT_DEFINITIONS:
            guests_count = int(segment_totals.get(code, 0))
            share_pct = round((guests_count * 100.0 / segment_base_total), 1) if segment_base_total > 0 else 0.0

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
        context["segment_base_total"] = segment_base_total
        context["total_unique_guests"] = total_unique_guests
        context["segment_charts_payload"] = self._build_segment_charts_payload(
            as_of_date=_parse_iso_date(selected_as_of_date) or as_of_value,
            window_days=selected_window_days,
            department_options=filters.get("department_options") or [],
            selected_department_id=(filters.get("department_id") or "").strip(),
        )
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
            return f"{base_url}#selected-guests"
        return f"{base_url}?{urlencode(params)}#selected-guests"

    @staticmethod
    def _build_segment_charts_payload(
        *,
        as_of_date: date | None,
        window_days: int,
        department_options: list[dict[str, Any]],
        selected_department_id: str,
    ) -> dict[str, Any]:
        """
        Готовит данные для блока графиков по сегментам гостей.

        Данные строятся по каждому заведению отдельно, чтобы UI мог
        динамически сравнивать сегменты между заведениями без доп. запросов.
        """
        segments = [
            {
                "code": code,
                "name": SEGMENT_CHART_LABELS.get(code, name),
                "color": SEGMENT_CHART_COLORS.get(code),
            }
            for code, name in SEGMENT_DEFINITIONS
        ]
        if as_of_date is None:
            return {
                "segments": segments,
                "departments": [],
                "initial_selected_department_ids": [],
            }

        departments: list[dict[str, Any]] = []
        for option in department_options:
            dep_id = (option.get("id") or "").strip()
            dep_name = (option.get("name") or "").strip()
            if not dep_id:
                continue
            dep_payload = build_guest_workbench_payload(
                as_of_date=as_of_date,
                window_days=window_days,
                department_id=dep_id,
            )
            dep_segment_totals = dep_payload.get("segments", {})
            departments.append(
                {
                    "id": dep_id,
                    "name": dep_name or dep_id,
                    "segments": {
                        code: int(dep_segment_totals.get(code, 0))
                        for code, _ in SEGMENT_DEFINITIONS
                    },
                }
            )

        default_selected_ids = [dep["id"] for dep in departments]
        initial_selected_ids = (
            [selected_department_id]
            if selected_department_id and selected_department_id in set(default_selected_ids)
            else default_selected_ids
        )

        return {
            "segments": segments,
            "departments": departments,
            "initial_selected_department_ids": initial_selected_ids,
        }


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
