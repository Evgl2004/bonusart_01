"""
Тесты страницы пользовательского дашборда аналитики.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

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
