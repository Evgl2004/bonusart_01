"""
Тесты сервиса построения category-window метрик (F21).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    Guest,
    GuestRestaurantWindowCategoryMetrics,
    OlapCategoryDict,
    OlapCheckSyncJournal,
    OlapNomenclatureDict,
    OlapSalesRawLine,
    OrderFact,
)
from guests.services.window_category_metrics import (
    rebuild_window_category_metrics_from_order_facts,
)


class WindowCategoryMetricsServiceTests(TestCase):
    """
    Проверки расчёта category-window слоя для workbench.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest = Guest.objects.create(
            first_name="Гость",
            phone="+79990008888",
            created_at=now,
            updated_at=now,
        )
        self.department_id = "dept-77"

        self.olap_category = OlapCategoryDict.objects.create(
            iiko_category_external_id="cat-main",
            category_name="Основная категория",
            first_seen_at=now,
            last_seen_at=now,
        )
        self.focus = FocusCategory.objects.create(
            code="focus_main",
            name="Фокусная категория",
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=self.olap_category,
            is_enabled=True,
        )
        self.nomenclature_focus = OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id="dish-focus",
            nomenclature_name="Фокусное блюдо",
            olap_category=self.olap_category,
            first_seen_at=now,
            last_seen_at=now,
            is_active=True,
        )
        FocusCategoryNomenclatureResolved.objects.create(
            focus_category=self.focus,
            nomenclature=self.nomenclature_focus,
            source_reason=FocusCategoryNomenclatureResolved.SourceReason.DIRECT_OLAP,
        )

    def _create_order_with_focus_raw_lines(
        self,
        *,
        business_day: date,
        order_number: int,
        uniq_order_id: str,
        full_order_net_sum: str,
        full_order_bonus_sum: str,
        focus_item_net_sum: str,
    ) -> None:
        OrderFact.objects.create(
            guest=self.guest,
            business_date=business_day,
            department_id=self.department_id,
            department_name="Тестовое заведение",
            order_number=order_number,
            uniq_order_id=uniq_order_id,
            net_sum=Decimal(full_order_net_sum),
            gross_sum=Decimal(full_order_net_sum),
            bonus_sum=Decimal(full_order_bonus_sum),
            items_count=2,
            categories_count=2,
        )

        journal = OlapCheckSyncJournal.objects.create(
            idempotency_key=f"f21-{business_day.isoformat()}-{order_number}",
            status=OlapCheckSyncJournal.Status.LOADED,
            guest=self.guest,
            order_number=order_number,
            business_date=business_day,
        )
        OlapSalesRawLine.objects.create(
            row_fingerprint=f"f21-focus-{business_day.isoformat()}-{order_number}",
            sync_journal=journal,
            guest=self.guest,
            business_date=business_day,
            department_id=self.department_id,
            department_name="Тестовое заведение",
            order_number=order_number,
            uniq_order_id=uniq_order_id,
            item_sale_event_id=f"event-{order_number}-1",
            dish_code="dish-focus",
            dish_name="Фокусное блюдо",
            dish_category_id="cat-main",
            dish_category_name="Основная категория",
            dish_sum_before_discount=Decimal(focus_item_net_sum),
            dish_sum_after_discount=Decimal(focus_item_net_sum),
            discount_sum=Decimal("0"),
            bonus_sum=Decimal("0"),
            raw_payload={},
        )

    def test_rebuild_category_metrics_creates_expected_row(self):
        """
        Сервис должен считать orders/visits/sum_net/sum_focus_net/avg/rating по заказам категории.
        """
        self._create_order_with_focus_raw_lines(
            business_day=date(2026, 3, 14),
            order_number=101,
            uniq_order_id="order-101",
            full_order_net_sum="300",
            full_order_bonus_sum="10",
            focus_item_net_sum="100",
        )
        self._create_order_with_focus_raw_lines(
            business_day=date(2026, 3, 18),
            order_number=202,
            uniq_order_id="order-202",
            full_order_net_sum="200",
            full_order_bonus_sum="-5",
            focus_item_net_sum="50",
        )

        stats = rebuild_window_category_metrics_from_order_facts(
            as_of_date=date(2026, 3, 18),
            window_days=[7],
            department_id=self.department_id,
        )

        self.assertEqual(stats.windows_processed, 1)
        self.assertEqual(stats.created_rows, 1)
        self.assertEqual(stats.updated_rows, 0)
        self.assertEqual(stats.deleted_rows, 0)
        self.assertEqual(stats.missing_order_facts, 0)

        metric = GuestRestaurantWindowCategoryMetrics.objects.get()
        self.assertEqual(metric.as_of_date, date(2026, 3, 18))
        self.assertEqual(metric.window_days, 7)
        self.assertEqual(metric.department_id, self.department_id)
        self.assertEqual(metric.focus_category_id, self.focus.id)
        self.assertEqual(metric.orders_count, 2)
        self.assertEqual(metric.visits_count, 2)
        self.assertEqual(metric.sum_focus_net, Decimal("150"))
        self.assertEqual(metric.sum_net, Decimal("500"))
        self.assertEqual(metric.avg_check_net, Decimal("250.00"))
        self.assertEqual(metric.bonus_in_sum, Decimal("10"))
        self.assertEqual(metric.bonus_out_sum, Decimal("5"))
        self.assertEqual(metric.last_visit_at, date(2026, 3, 18))
        self.assertEqual(metric.rating_score, Decimal("12.50"))

    def test_rebuild_category_metrics_deletes_stale_rows_in_scope(self):
        """
        При отсутствии актуальных заказов в scope stale-строки должны удаляться.
        """
        GuestRestaurantWindowCategoryMetrics.objects.create(
            as_of_date=date(2026, 3, 20),
            guest=self.guest,
            department_id=self.department_id,
            window_days=7,
            focus_category=self.focus,
            orders_count=1,
            visits_count=1,
            avg_check_net=Decimal("100.00"),
            sum_net=Decimal("100"),
            sum_focus_net=Decimal("50"),
            bonus_in_sum=Decimal("0"),
            bonus_out_sum=Decimal("0"),
            rating_score=Decimal("6.00"),
            last_visit_at=date(2026, 3, 20),
        )
        self.assertEqual(GuestRestaurantWindowCategoryMetrics.objects.count(), 1)

        stats = rebuild_window_category_metrics_from_order_facts(
            as_of_date=date(2026, 3, 20),
            window_days=[7],
            department_id=self.department_id,
        )

        self.assertEqual(stats.created_rows, 0)
        self.assertEqual(stats.updated_rows, 0)
        self.assertEqual(stats.deleted_rows, 1)
        self.assertEqual(GuestRestaurantWindowCategoryMetrics.objects.count(), 0)
