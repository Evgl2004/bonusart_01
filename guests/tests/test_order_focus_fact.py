"""
Тесты сервиса построения `guest_order_focus_fact`.
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
    GuestOrderFocusFact,
    OlapCategoryDict,
    OlapCheckSyncJournal,
    OlapNomenclatureDict,
    OlapSalesRawLine,
)
from guests.services.order_focus_fact import rebuild_order_focus_fact_from_raw_lines


class OrderFocusFactServiceTests(TestCase):
    """
    Проверки пересчёта order-level слоя категорий.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest = Guest.objects.create(
            first_name="Гость",
            phone="+79990003333",
            created_at=now,
            updated_at=now,
        )
        self.department_id = "dept-77"

        self.category = OlapCategoryDict.objects.create(
            iiko_category_external_id="cat-focus",
            category_name="Категория фокуса",
            first_seen_at=now,
            last_seen_at=now,
        )
        self.focus = FocusCategory.objects.create(
            code="focus_rolls",
            name="Роллы",
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=self.category,
            is_enabled=True,
        )
        self.nomenclature = OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id="dish-focus",
            nomenclature_name="Фокусное блюдо",
            olap_category=self.category,
            first_seen_at=now,
            last_seen_at=now,
            is_active=True,
        )
        FocusCategoryNomenclatureResolved.objects.create(
            focus_category=self.focus,
            nomenclature=self.nomenclature,
            source_reason=FocusCategoryNomenclatureResolved.SourceReason.DIRECT_OLAP,
        )

    def _create_raw_line(
        self,
        *,
        business_day: date,
        order_number: int,
        uniq_order_id: str,
        fingerprint: str,
        line_sum: str,
        dish_code: str = "dish-focus",
    ) -> None:
        journal = OlapCheckSyncJournal.objects.create(
            idempotency_key=f"j-{fingerprint}",
            status=OlapCheckSyncJournal.Status.LOADED,
            guest=self.guest,
            order_number=order_number,
            business_date=business_day,
        )
        OlapSalesRawLine.objects.create(
            row_fingerprint=fingerprint,
            sync_journal=journal,
            guest=self.guest,
            business_date=business_day,
            department_id=self.department_id,
            department_name="Тестовое заведение",
            order_number=order_number,
            uniq_order_id=uniq_order_id,
            item_sale_event_id=f"event-{fingerprint}",
            dish_code=dish_code,
            dish_name="Фокусное блюдо",
            dish_category_id="cat-focus",
            dish_category_name="Категория фокуса",
            dish_sum_before_discount=Decimal(line_sum),
            dish_sum_after_discount=Decimal(line_sum),
            discount_sum=Decimal("0"),
            bonus_sum=Decimal("0"),
            raw_payload={},
        )

    def test_rebuild_order_focus_fact_creates_rows(self):
        """
        Сервис должен агрегировать позиции до уровня order+focus.
        """
        self._create_raw_line(
            business_day=date(2026, 3, 18),
            order_number=101,
            uniq_order_id="order-101",
            fingerprint="of-1",
            line_sum="100",
        )
        self._create_raw_line(
            business_day=date(2026, 3, 18),
            order_number=101,
            uniq_order_id="order-101",
            fingerprint="of-2",
            line_sum="50",
        )
        self._create_raw_line(
            business_day=date(2026, 3, 19),
            order_number=202,
            uniq_order_id="order-202",
            fingerprint="of-3",
            line_sum="80",
        )

        stats = rebuild_order_focus_fact_from_raw_lines(
            business_date_from=date(2026, 3, 18),
            business_date_to=date(2026, 3, 19),
            department_id=self.department_id,
        )

        self.assertEqual(stats.scanned_raw_lines, 3)
        self.assertEqual(stats.grouped_rows, 2)
        self.assertEqual(stats.created_rows, 2)
        self.assertEqual(stats.updated_rows, 0)
        self.assertEqual(stats.deleted_rows, 0)
        self.assertEqual(GuestOrderFocusFact.objects.count(), 2)

        row = GuestOrderFocusFact.objects.get(
            business_date=date(2026, 3, 18),
            department_id=self.department_id,
            order_number=101,
            focus_category=self.focus,
        )
        self.assertEqual(row.items_count, 2)
        self.assertEqual(row.sum_focus_net, Decimal("150"))
        self.assertEqual(row.guest_id, self.guest.id)

    def test_rebuild_order_focus_fact_updates_and_deletes_stale(self):
        """
        Повторный пересчёт должен обновлять существующую строку и удалять stale.
        """
        self._create_raw_line(
            business_day=date(2026, 3, 20),
            order_number=501,
            uniq_order_id="order-501",
            fingerprint="of-stale-1",
            line_sum="100",
        )
        rebuild_order_focus_fact_from_raw_lines(
            business_date_from=date(2026, 3, 20),
            business_date_to=date(2026, 3, 20),
            department_id=self.department_id,
        )
        self.assertEqual(GuestOrderFocusFact.objects.count(), 1)

        GuestOrderFocusFact.objects.create(
            business_date=date(2026, 3, 20),
            guest=self.guest,
            department_id=self.department_id,
            order_number=999,
            uniq_order_id="stale",
            focus_category=self.focus,
            items_count=1,
            sum_focus_net=Decimal("999"),
        )
        self.assertEqual(GuestOrderFocusFact.objects.count(), 2)

        self._create_raw_line(
            business_day=date(2026, 3, 20),
            order_number=501,
            uniq_order_id="order-501",
            fingerprint="of-stale-2",
            line_sum="50",
        )

        stats = rebuild_order_focus_fact_from_raw_lines(
            business_date_from=date(2026, 3, 20),
            business_date_to=date(2026, 3, 20),
            department_id=self.department_id,
        )

        self.assertEqual(stats.created_rows, 0)
        self.assertEqual(stats.updated_rows, 1)
        self.assertEqual(stats.deleted_rows, 1)
        self.assertEqual(GuestOrderFocusFact.objects.count(), 1)

        row = GuestOrderFocusFact.objects.get(
            business_date=date(2026, 3, 20),
            department_id=self.department_id,
            order_number=501,
            focus_category=self.focus,
        )
        self.assertEqual(row.items_count, 2)
        self.assertEqual(row.sum_focus_net, Decimal("150"))
