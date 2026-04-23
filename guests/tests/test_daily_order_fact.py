"""
Тесты сервиса построения `guest_restaurant_daily_order_fact`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from guests.models import Guest, GuestRestaurantDailyOrderFact, OrderFact
from guests.services.daily_order_fact import rebuild_daily_order_fact_from_order_facts


class DailyOrderFactServiceTests(TestCase):
    """
    Проверки пересчёта дневного слоя по полным чекам.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest_1 = Guest.objects.create(
            first_name="Гость 1",
            phone="+79990001111",
            created_at=now,
            updated_at=now,
        )
        self.guest_2 = Guest.objects.create(
            first_name="Гость 2",
            phone="+79990002222",
            created_at=now,
            updated_at=now,
        )

    def _create_order_fact(
        self,
        *,
        business_day: date,
        order_number: int,
        guest_id: int | None,
        department_id: str,
        net_sum: str,
        bonus_sum: str,
    ) -> None:
        OrderFact.objects.create(
            guest_id=guest_id,
            business_date=business_day,
            department_id=department_id,
            department_name="Тестовый департамент",
            order_number=order_number,
            uniq_order_id=f"uniq-{order_number}",
            gross_sum=Decimal(net_sum),
            net_sum=Decimal(net_sum),
            bonus_sum=Decimal(bonus_sum),
            items_count=1,
            categories_count=1,
        )

    def test_rebuild_daily_order_fact_creates_rows(self):
        """
        Сервис должен корректно агрегировать количество заказов, суммы и бонусы.
        """
        self._create_order_fact(
            business_day=date(2026, 3, 18),
            order_number=101,
            guest_id=self.guest_1.id,
            department_id="dept-1",
            net_sum="100",
            bonus_sum="10",
        )
        self._create_order_fact(
            business_day=date(2026, 3, 18),
            order_number=102,
            guest_id=self.guest_1.id,
            department_id="dept-1",
            net_sum="50",
            bonus_sum="-3",
        )
        self._create_order_fact(
            business_day=date(2026, 3, 19),
            order_number=201,
            guest_id=self.guest_1.id,
            department_id="dept-1",
            net_sum="200",
            bonus_sum="0",
        )
        self._create_order_fact(
            business_day=date(2026, 3, 18),
            order_number=999,
            guest_id=None,
            department_id="dept-1",
            net_sum="999",
            bonus_sum="0",
        )

        stats = rebuild_daily_order_fact_from_order_facts(
            business_date_from=date(2026, 3, 18),
            business_date_to=date(2026, 3, 19),
        )

        self.assertEqual(stats.scanned_order_facts, 4)
        self.assertEqual(stats.skipped_without_guest, 1)
        self.assertEqual(stats.grouped_rows, 2)
        self.assertEqual(stats.created_rows, 2)
        self.assertEqual(stats.updated_rows, 0)
        self.assertEqual(stats.deleted_rows, 0)
        self.assertEqual(GuestRestaurantDailyOrderFact.objects.count(), 2)

        row_18 = GuestRestaurantDailyOrderFact.objects.get(
            business_date=date(2026, 3, 18),
            guest=self.guest_1,
            department_id="dept-1",
        )
        self.assertEqual(row_18.orders_count, 2)
        self.assertEqual(row_18.sum_net, Decimal("150"))
        self.assertEqual(row_18.bonus_in_sum, Decimal("10"))
        self.assertEqual(row_18.bonus_out_sum, Decimal("3"))

    def test_rebuild_daily_order_fact_updates_and_deletes_stale(self):
        """
        Повторный пересчёт должен обновлять существующую строку и удалять stale.
        """
        self._create_order_fact(
            business_day=date(2026, 3, 20),
            order_number=301,
            guest_id=self.guest_1.id,
            department_id="dept-1",
            net_sum="100",
            bonus_sum="0",
        )
        rebuild_daily_order_fact_from_order_facts(
            business_date_from=date(2026, 3, 20),
            business_date_to=date(2026, 3, 20),
        )
        self.assertEqual(GuestRestaurantDailyOrderFact.objects.count(), 1)

        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 20),
            guest=self.guest_2,
            department_id="dept-1",
            orders_count=1,
            sum_net=Decimal("777"),
            bonus_in_sum=Decimal("0"),
            bonus_out_sum=Decimal("0"),
        )
        self.assertEqual(GuestRestaurantDailyOrderFact.objects.count(), 2)

        self._create_order_fact(
            business_day=date(2026, 3, 20),
            order_number=302,
            guest_id=self.guest_1.id,
            department_id="dept-1",
            net_sum="50",
            bonus_sum="2",
        )

        stats = rebuild_daily_order_fact_from_order_facts(
            business_date_from=date(2026, 3, 20),
            business_date_to=date(2026, 3, 20),
        )

        self.assertEqual(stats.created_rows, 0)
        self.assertEqual(stats.updated_rows, 1)
        self.assertEqual(stats.deleted_rows, 1)
        self.assertEqual(GuestRestaurantDailyOrderFact.objects.count(), 1)

        row = GuestRestaurantDailyOrderFact.objects.get(
            business_date=date(2026, 3, 20),
            guest=self.guest_1,
            department_id="dept-1",
        )
        self.assertEqual(row.orders_count, 2)
        self.assertEqual(row.sum_net, Decimal("150"))
        self.assertEqual(row.bonus_in_sum, Decimal("2"))
        self.assertEqual(row.bonus_out_sum, Decimal("0"))
