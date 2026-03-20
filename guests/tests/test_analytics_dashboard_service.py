"""
Тесты сервиса подготовки данных пользовательского дашборда.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    FocusCategory,
    Guest,
    GuestRestaurantDailyCategoryFact,
    GuestRestaurantWindowMetrics,
    OlapCategoryDict,
    OrderFact,
)
from guests.services.analytics_dashboard import (
    build_analytics_dashboard_payload,
    normalize_period_days,
)


class AnalyticsDashboardServiceTests(TestCase):
    """
    Проверки сводного payload для KPI и графиков.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest_1 = Guest.objects.create(
            first_name="Иван",
            phone="+79001001010",
            created_at=now,
            updated_at=now,
        )
        self.guest_2 = Guest.objects.create(
            first_name="Ольга",
            phone="+79002002020",
            created_at=now,
            updated_at=now,
        )

        OrderFact.objects.create(
            guest=self.guest_1,
            business_date=date(2026, 3, 18),
            department_id="dept-a",
            department_name="Грузин",
            order_number=1001,
            uniq_order_id="a-1001",
            gross_sum=Decimal("1100"),
            net_sum=Decimal("1000"),
        )
        OrderFact.objects.create(
            guest=self.guest_2,
            business_date=date(2026, 3, 19),
            department_id="dept-a",
            department_name="Грузин",
            order_number=1002,
            uniq_order_id="a-1002",
            gross_sum=Decimal("550"),
            net_sum=Decimal("500"),
        )
        OrderFact.objects.create(
            guest=self.guest_1,
            business_date=date(2026, 3, 19),
            department_id="dept-b",
            department_name="Ермолаев",
            order_number=2001,
            uniq_order_id="b-2001",
            gross_sum=Decimal("780"),
            net_sum=Decimal("700"),
        )

        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=date(2026, 3, 19),
            guest=self.guest_1,
            department_id="dept-a",
            window_days=30,
            orders_count=1,
            visits_count=1,
            sum_net=Decimal("1000"),
            avg_check_net=Decimal("1000"),
            rating_score=Decimal("15"),
        )
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=date(2026, 3, 19),
            guest=self.guest_1,
            department_id="dept-a",
            window_days=180,
            orders_count=3,
            visits_count=2,
            sum_net=Decimal("3000"),
            avg_check_net=Decimal("1000"),
            rating_score=Decimal("40"),
        )
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=date(2026, 3, 19),
            guest=self.guest_2,
            department_id="dept-a",
            window_days=30,
            orders_count=0,
            visits_count=0,
            sum_net=Decimal("0"),
            avg_check_net=Decimal("0"),
            rating_score=Decimal("0"),
        )
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=date(2026, 3, 19),
            guest=self.guest_2,
            department_id="dept-a",
            window_days=180,
            orders_count=1,
            visits_count=1,
            sum_net=Decimal("500"),
            avg_check_net=Decimal("500"),
            rating_score=Decimal("10"),
        )

        olap_category = OlapCategoryDict.objects.create(
            iiko_category_external_id="cat-meat",
            category_name="Шашлык",
            first_seen_at=now,
            last_seen_at=now,
        )
        focus_category = FocusCategory.objects.create(
            code="meat_focus",
            name="Любитель мяса",
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=olap_category,
            is_enabled=True,
        )
        GuestRestaurantDailyCategoryFact.objects.create(
            business_date=date(2026, 3, 19),
            guest=self.guest_1,
            department_id="dept-a",
            focus_category=focus_category,
            orders_count=2,
            items_count=3,
            sum_gross=Decimal("1200"),
            sum_net=Decimal("1200"),
            bonus_sum=Decimal("10"),
        )

    def test_build_payload_for_department(self):
        """
        Для выбранного заведения KPI и графики должны считаться корректно.
        """
        payload = build_analytics_dashboard_payload(
            period_days=30,
            department_id="dept-a",
            as_of_date=date(2026, 3, 19),
        )
        kpis_by_key = {item["key"]: item for item in payload["kpis"]}

        self.assertEqual(payload["filters"]["period_days"], 30)
        self.assertEqual(payload["filters"]["department_id"], "dept-a")
        self.assertEqual(kpis_by_key["orders_total"]["value"], 2)
        self.assertEqual(kpis_by_key["unique_guests"]["value"], 2)
        self.assertEqual(kpis_by_key["net_revenue"]["value"], 1500.0)
        self.assertEqual(kpis_by_key["avg_check"]["value"], 750.0)
        self.assertEqual(kpis_by_key["active_30d"]["value"], 1)
        self.assertEqual(kpis_by_key["sleeping_30_180d"]["value"], 1)

        self.assertEqual(len(payload["charts"]["daily_dynamics"]["labels"]), 30)
        self.assertTrue(payload["charts"]["department_revenue"]["labels"])
        self.assertTrue(payload["charts"]["focus_categories"]["pie_data"])

    def test_normalize_period_days_fallback(self):
        """
        Некорректный период должен приводиться к значению по умолчанию.
        """
        self.assertEqual(normalize_period_days("abc"), 30)
        self.assertEqual(normalize_period_days(999), 30)
        self.assertEqual(normalize_period_days(14), 14)
