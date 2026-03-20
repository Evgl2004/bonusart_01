"""
Тесты сервиса построения оконных метрик (S8).
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
)
from guests.services.window_metrics import rebuild_window_metrics_from_daily_facts


class WindowMetricsServiceTests(TestCase):
    """
    Проверки формирования оконных метрик по дневному слою.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest = Guest.objects.create(
            first_name="Гость",
            phone="+79990007777",
            created_at=now,
            updated_at=now,
        )
        self.category = OlapCategoryDict.objects.create(
            iiko_category_external_id="cat-window",
            category_name="Категория окна",
            first_seen_at=now,
            last_seen_at=now,
        )
        self.focus = FocusCategory.objects.create(
            code="window_focus",
            name="Фокус окна",
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=self.category,
            is_enabled=True,
        )

    def _create_daily_row(
        self,
        *,
        business_day: date,
        orders_count: int,
        items_count: int,
        sum_net: str,
        bonus_sum: str,
    ) -> None:
        GuestRestaurantDailyCategoryFact.objects.create(
            business_date=business_day,
            guest=self.guest,
            department_id="dept-77",
            focus_category=self.focus,
            orders_count=orders_count,
            items_count=items_count,
            sum_gross=Decimal(sum_net),
            sum_net=Decimal(sum_net),
            bonus_sum=Decimal(bonus_sum),
        )

    def test_rebuild_window_metrics_creates_row(self):
        """
        Сервис должен корректно агрегировать окно по orders/visits/sum/bonus/rating.
        """
        self._create_daily_row(
            business_day=date(2026, 3, 14),
            orders_count=2,
            items_count=4,
            sum_net="1000",
            bonus_sum="10",
        )
        self._create_daily_row(
            business_day=date(2026, 3, 18),
            orders_count=1,
            items_count=2,
            sum_net="600",
            bonus_sum="-5",
        )

        stats = rebuild_window_metrics_from_daily_facts(
            as_of_date=date(2026, 3, 18),
            window_days=[7],
        )

        self.assertEqual(stats.windows_processed, 1)
        self.assertEqual(stats.created_rows, 1)
        self.assertEqual(stats.updated_rows, 0)

        metric = GuestRestaurantWindowMetrics.objects.get()
        self.assertEqual(metric.as_of_date, date(2026, 3, 18))
        self.assertEqual(metric.window_days, 7)
        self.assertEqual(metric.orders_count, 3)
        self.assertEqual(metric.visits_count, 2)
        self.assertEqual(metric.sum_net, Decimal("1600"))
        self.assertEqual(metric.avg_check_net, Decimal("533.33"))
        self.assertEqual(metric.bonus_in_sum, Decimal("10"))
        self.assertEqual(metric.bonus_out_sum, Decimal("5"))
        self.assertEqual(metric.last_visit_at, date(2026, 3, 18))
        self.assertEqual(metric.rating_score, Decimal("18.33"))

    def test_rebuild_window_metrics_updates_existing_row(self):
        """
        Повторный пересчёт должен обновлять существующую строку окна.
        """
        self._create_daily_row(
            business_day=date(2026, 3, 18),
            orders_count=1,
            items_count=1,
            sum_net="500",
            bonus_sum="0",
        )
        rebuild_window_metrics_from_daily_facts(as_of_date=date(2026, 3, 18), window_days=[7])
        self.assertEqual(GuestRestaurantWindowMetrics.objects.count(), 1)

        self._create_daily_row(
            business_day=date(2026, 3, 17),
            orders_count=1,
            items_count=1,
            sum_net="700",
            bonus_sum="2",
        )
        stats = rebuild_window_metrics_from_daily_facts(as_of_date=date(2026, 3, 18), window_days=[7])

        self.assertEqual(stats.created_rows, 0)
        self.assertEqual(stats.updated_rows, 1)
        metric = GuestRestaurantWindowMetrics.objects.get()
        self.assertEqual(metric.orders_count, 2)
        self.assertEqual(metric.visits_count, 2)
        self.assertEqual(metric.sum_net, Decimal("1200"))
        self.assertEqual(metric.avg_check_net, Decimal("600.00"))

