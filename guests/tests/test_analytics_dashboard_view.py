"""
Тесты страницы пользовательского дашборда аналитики.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from guests.models import Guest, OrderFact


class AnalyticsDashboardViewTests(TestCase):
    """
    Проверки доступности и базового контента дашборда.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        guest = Guest.objects.create(
            first_name="Тест",
            phone="+79000000000",
            created_at=now,
            updated_at=now,
        )
        OrderFact.objects.create(
            guest=guest,
            business_date=date(2026, 3, 19),
            department_id="dept-a",
            department_name="Грузин",
            order_number=3001,
            uniq_order_id="view-3001",
            gross_sum=Decimal("1000"),
            net_sum=Decimal("900"),
        )

    def test_dashboard_page_renders(self):
        """
        Страница должна открываться и рендерить блоки KPI/графиков.
        """
        response = self.client.get(reverse("analytics_dashboard"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "analytics/dashboard.html")
        self.assertContains(response, "Дашборд аналитики гостей")
        self.assertContains(response, "chart-daily-dynamics")
        self.assertContains(response, "analytics-dashboard-data")
        self.assertEqual(len(response.context["dashboard_payload"]["kpis"]), 6)

    @patch("guests.views_analytics.timezone.localdate", return_value=date(2026, 6, 25))
    def test_segment_dynamics_page_renders(self, _localdate_mock):
        """
        Дашборд динамики сегментов должен открываться и отдавать payload.
        """
        response = self.client.get(
            reverse("dashboard_segment_dynamics"),
            {"period_days": "14", "segment_code": "new_in_venue"},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "analytics/dashboard_segment_dynamics.html")
        self.assertContains(response, "Дашборд: динамика сегментов")
        self.assertContains(response, "segment-dynamics-data")
        self.assertContains(response, "Техническая детализация")
        self.assertContains(response, "Без выбранного заведения ряд «Новые за день» показан нулями")

        payload = response.context["segment_dynamics_payload"]
        self.assertFalse(payload["is_static_sketch"])
        self.assertTrue(payload["needs_department_hint"])
        self.assertEqual(payload["filters"]["date_from"], "2026-06-11")
        self.assertEqual(payload["filters"]["date_to"], "2026-06-24")
        self.assertEqual(payload["filters"]["period_days"], 14)
        self.assertEqual(len(payload["rows"]), 14)
        self.assertTrue(all(row["new_in_venue"] == 0 for row in payload["rows"]))
