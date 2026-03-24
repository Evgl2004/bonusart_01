"""
Тесты экрана «Сегменты».
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from django.test import TestCase
from django.urls import reverse

from guests.models import Guest, GuestRestaurantWindowMetrics, OrderFact


class SegmentsWorkbenchViewTests(TestCase):
    """
    Проверяем, что экран сегментов строится на реальных метриках и даёт рабочие ссылки.
    """

    def setUp(self):
        self.as_of_date = date(2026, 3, 23)
        self.department_id = "dep-1"
        self.guest_active = Guest.objects.create(phone="+79990001111", first_name="Анна")
        self.guest_cooling = Guest.objects.create(phone="+79990002222", first_name="Иван")

        OrderFact.objects.create(
            guest=self.guest_active,
            business_date=date(2026, 3, 20),
            department_id=self.department_id,
            department_name="Сами Сусами",
            order_number=101,
            uniq_order_id="uniq-101",
            net_sum=Decimal("1500.00"),
            gross_sum=Decimal("1500.00"),
        )

        # active_30d: visits_30 >= 2
        for window in (30, 60, 180):
            GuestRestaurantWindowMetrics.objects.create(
                as_of_date=self.as_of_date,
                guest=self.guest_active,
                department_id=self.department_id,
                window_days=window,
                orders_count=3,
                visits_count=2,
                avg_check_net=Decimal("500.00"),
                sum_net=Decimal("1500.00"),
                bonus_in_sum=Decimal("50.00"),
                bonus_out_sum=Decimal("10.00"),
                rating_score=Decimal("15.00"),
                last_visit_at=date(2026, 3, 22),
            )

        # cooling_30_60d: visits_30 == 0 и visits_60 > 0
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_cooling,
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
            guest=self.guest_cooling,
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
            guest=self.guest_cooling,
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

    def test_segments_page_renders_rows_and_links(self):
        """
        Страница сегментов должна показывать состав сегментов и ссылку в список гостей.
        """
        response = self.client.get(
            reverse("segments"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Состав сегментов")
        self.assertContains(response, "Открыть гостей сегмента")

        rows = response.context["segment_rows"]
        active_row = next(item for item in rows if item["code"] == "active_30d")
        cooling_row = next(item for item in rows if item["code"] == "cooling_30_60d")
        self.assertEqual(active_row["guests_count"], 1)
        self.assertEqual(cooling_row["guests_count"], 1)

        parsed = urlparse(active_row["details_url"])
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.path, reverse("guests_workbench"))
        self.assertEqual(params.get("segment_code"), ["active_30d"])
        self.assertEqual(params.get("department_id"), [self.department_id])
