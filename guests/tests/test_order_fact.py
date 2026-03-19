"""
Тесты сервиса построения `order_fact` из сырых OLAP-строк (S6).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from guests.models import Guest, OlapCheckSyncJournal, OlapSalesRawLine, OrderFact
from guests.services.order_fact import rebuild_order_fact_from_raw_lines


class OrderFactServiceTests(TestCase):
    """
    Проверки формирования и обновления фактов чеков.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest = Guest.objects.create(
            first_name="Гость",
            phone="+79990009999",
            created_at=now,
            updated_at=now,
        )
        self.journal = OlapCheckSyncJournal.objects.create(
            idempotency_key="order-fact-journal-1",
            status=OlapCheckSyncJournal.Status.LOADED,
            guest=self.guest,
            order_number=123456,
            business_date=date(2026, 3, 18),
        )

    def _create_raw_line(
        self,
        *,
        fingerprint: str,
        dish_code: str,
        dish_category_id: str,
        sum_value: str,
        discount: str = "0",
        coupon_number: str | None = None,
    ) -> OlapSalesRawLine:
        return OlapSalesRawLine.objects.create(
            row_fingerprint=fingerprint,
            sync_journal=self.journal,
            guest=self.guest,
            business_date=date(2026, 3, 18),
            department_id="dept-1",
            department_name="Узбечка",
            order_number=123456,
            uniq_order_id="uniq-order-1",
            item_sale_event_id=f"event-{dish_code}",
            dish_code=dish_code,
            dish_name=f"Блюдо {dish_code}",
            dish_category_id=dish_category_id,
            dish_category_name=f"Категория {dish_category_id}",
            dish_group_id="group-1",
            dish_group_name="Группа 1",
            dish_sum_before_discount=Decimal(sum_value),
            dish_sum_after_discount=Decimal(sum_value),
            discount_sum=Decimal(discount),
            bonus_sum=Decimal("0"),
            coupon_series="SER-1" if coupon_number else None,
            coupon_number=coupon_number,
            raw_payload={"OrderType": "DINING", "IsDelivery": False},
        )

    def test_rebuild_order_fact_creates_single_order_fact(self):
        """
        Сервис должен агрегировать позиции одного заказа в одну запись order_fact.
        """
        self._create_raw_line(
            fingerprint="of-fp-1",
            dish_code="54768",
            dish_category_id="cat-1",
            sum_value="490",
            coupon_number="111",
        )
        self._create_raw_line(
            fingerprint="of-fp-2",
            dish_code="54788",
            dish_category_id="cat-2",
            sum_value="110",
        )

        stats = rebuild_order_fact_from_raw_lines()

        self.assertEqual(stats.scanned_raw_lines, 2)
        self.assertEqual(stats.grouped_orders, 1)
        self.assertEqual(stats.created_facts, 1)
        self.assertEqual(stats.updated_facts, 0)

        fact = OrderFact.objects.get()
        self.assertEqual(fact.guest_id, self.guest.id)
        self.assertEqual(fact.order_number, 123456)
        self.assertEqual(fact.business_date, date(2026, 3, 18))
        self.assertEqual(fact.department_id, "dept-1")
        self.assertEqual(fact.gross_sum, Decimal("600"))
        self.assertEqual(fact.net_sum, Decimal("600"))
        self.assertEqual(fact.items_count, 2)
        self.assertEqual(fact.categories_count, 2)
        self.assertTrue(fact.coupon_used)
        self.assertEqual(fact.coupon_series, "SER-1")
        self.assertEqual(fact.coupon_number, "111")
        self.assertEqual(fact.order_type, "DINING")
        self.assertFalse(fact.is_delivery)

    def test_rebuild_order_fact_updates_existing_fact(self):
        """
        Повторный пересчёт должен обновлять существующий order_fact, а не создавать дубликат.
        """
        self._create_raw_line(
            fingerprint="of-fp-3",
            dish_code="90001",
            dish_category_id="cat-1",
            sum_value="100",
        )
        rebuild_order_fact_from_raw_lines()
        self.assertEqual(OrderFact.objects.count(), 1)

        self._create_raw_line(
            fingerprint="of-fp-4",
            dish_code="90002",
            dish_category_id="cat-1",
            sum_value="250",
        )

        stats = rebuild_order_fact_from_raw_lines()
        self.assertEqual(stats.created_facts, 0)
        self.assertEqual(stats.updated_facts, 1)
        self.assertEqual(OrderFact.objects.count(), 1)

        fact = OrderFact.objects.get()
        self.assertEqual(fact.gross_sum, Decimal("350"))
        self.assertEqual(fact.net_sum, Decimal("350"))
        self.assertEqual(fact.items_count, 2)
        self.assertEqual(fact.categories_count, 1)

