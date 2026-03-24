"""
Тесты экрана «Гости (workbench)».
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from guests.models import Guest, GuestRestaurantWindowMetrics, OrderFact


class GuestsWorkbenchViewTests(TestCase):
    """
    Проверяем базовую доступность и ключевые показатели workbench-экрана.
    """

    def setUp(self):
        self.guest_1 = Guest.objects.create(phone="+79990001111", first_name="Анна")
        self.guest_2 = Guest.objects.create(phone="+79990002222", first_name="Иван")
        self.as_of_date = date(2026, 3, 23)
        self.department_id = "dep-1"

        OrderFact.objects.create(
            guest=self.guest_1,
            business_date=date(2026, 3, 20),
            department_id=self.department_id,
            department_name="Сами Сусами",
            order_number=101,
            uniq_order_id="uniq-101",
            net_sum=Decimal("1500.00"),
            gross_sum=Decimal("1500.00"),
        )

        # Гость 1: активный (2+ визита за 30 дней)
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_1,
            department_id=self.department_id,
            window_days=30,
            orders_count=3,
            visits_count=2,
            avg_check_net=Decimal("500.00"),
            sum_net=Decimal("1500.00"),
            bonus_in_sum=Decimal("50.00"),
            bonus_out_sum=Decimal("10.00"),
            rating_score=Decimal("15.00"),
            last_visit_at=date(2026, 3, 22),
        )
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_1,
            department_id=self.department_id,
            window_days=60,
            orders_count=3,
            visits_count=2,
            avg_check_net=Decimal("500.00"),
            sum_net=Decimal("1500.00"),
            bonus_in_sum=Decimal("50.00"),
            bonus_out_sum=Decimal("10.00"),
            rating_score=Decimal("15.00"),
            last_visit_at=date(2026, 3, 22),
        )
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_1,
            department_id=self.department_id,
            window_days=180,
            orders_count=3,
            visits_count=2,
            avg_check_net=Decimal("500.00"),
            sum_net=Decimal("1500.00"),
            bonus_in_sum=Decimal("50.00"),
            bonus_out_sum=Decimal("10.00"),
            rating_score=Decimal("15.00"),
            last_visit_at=date(2026, 3, 22),
        )

        # Гость 2: остывший (0 в 30, >0 в 60)
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_2,
            department_id=self.department_id,
            window_days=30,
            orders_count=0,
            visits_count=0,
            avg_check_net=Decimal("0.00"),
            sum_net=Decimal("0.00"),
            bonus_in_sum=Decimal("0.00"),
            bonus_out_sum=Decimal("0.00"),
            rating_score=Decimal("0.00"),
            last_visit_at=None,
        )
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_2,
            department_id=self.department_id,
            window_days=60,
            orders_count=1,
            visits_count=1,
            avg_check_net=Decimal("700.00"),
            sum_net=Decimal("700.00"),
            bonus_in_sum=Decimal("20.00"),
            bonus_out_sum=Decimal("0.00"),
            rating_score=Decimal("8.00"),
            last_visit_at=date(2026, 2, 20),
        )
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_2,
            department_id=self.department_id,
            window_days=180,
            orders_count=1,
            visits_count=1,
            avg_check_net=Decimal("700.00"),
            sum_net=Decimal("700.00"),
            bonus_in_sum=Decimal("20.00"),
            bonus_out_sum=Decimal("0.00"),
            rating_score=Decimal("8.00"),
            last_visit_at=date(2026, 2, 20),
        )

    def test_workbench_page_returns_payload_and_segments(self):
        """
        Страница должна отдавать данные и корректно считать сегменты.
        """
        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["window_days"], 30)
        self.assertEqual(payload["filters"]["department_id"], self.department_id)

        self.assertEqual(payload["segments"]["active_30d"], 1)
        self.assertEqual(payload["segments"]["cooling_30_60d"], 1)
        self.assertEqual(payload["cards"]["guests_total"], 2)

