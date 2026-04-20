"""
Тесты сервиса построения дневного слоя по категориям (S7).
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
    GuestRestaurantDailyCategoryFact,
    OlapCategoryDict,
    OlapCheckSyncJournal,
    OlapNomenclatureDict,
    OlapSalesRawLine,
)
from guests.services.daily_category_fact import rebuild_daily_category_fact_from_raw_lines


class DailyCategoryFactServiceTests(TestCase):
    """
    Проверки пересчёта `guest_restaurant_daily_category_fact`.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest = Guest.objects.create(
            first_name="Гость",
            phone="+79990001234",
            created_at=now,
            updated_at=now,
        )
        self.journal = OlapCheckSyncJournal.objects.create(
            idempotency_key="daily-fact-journal",
            status=OlapCheckSyncJournal.Status.LOADED,
            guest=self.guest,
            order_number=501001,
            business_date=date(2026, 3, 18),
        )

        self.olap_category = OlapCategoryDict.objects.create(
            iiko_category_external_id="cat-main",
            category_name="Шашлык",
            first_seen_at=now,
            last_seen_at=now,
            is_active=True,
        )
        self.focus_category = FocusCategory.objects.create(
            code="meat_focus",
            name="Любитель мяса",
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=self.olap_category,
            is_enabled=True,
        )
        self.nomenclature = OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id="dish-100",
            nomenclature_name="Шашлык свиной",
            olap_category=self.olap_category,
            is_active=True,
        )
        FocusCategoryNomenclatureResolved.objects.create(
            focus_category=self.focus_category,
            nomenclature=self.nomenclature,
            source_reason=FocusCategoryNomenclatureResolved.SourceReason.DIRECT_OLAP,
        )

    def _create_raw_line(
        self,
        *,
        fingerprint: str,
        dish_code: str,
        order_number: int,
        sum_value: str,
    ) -> OlapSalesRawLine:
        return OlapSalesRawLine.objects.create(
            row_fingerprint=fingerprint,
            sync_journal=self.journal,
            guest=self.guest,
            business_date=date(2026, 3, 18),
            department_id="dept-42",
            department_name="Узбечка",
            order_number=order_number,
            uniq_order_id=f"uniq-{order_number}",
            item_sale_event_id=f"event-{fingerprint}",
            dish_code=dish_code,
            dish_name="Тестовое блюдо",
            dish_category_id="cat-main",
            dish_category_name="Шашлык",
            dish_group_id="group-1",
            dish_group_name="Группа 1",
            dish_sum_before_discount=Decimal(sum_value),
            dish_sum_after_discount=Decimal(sum_value),
            bonus_sum=Decimal("0"),
            raw_payload={},
        )

    def test_rebuild_daily_category_fact_creates_rows(self):
        """
        Сервис должен создать агрегатную запись по фокусной категории.
        """
        self._create_raw_line(
            fingerprint="dcf-1",
            dish_code="dish-100",
            order_number=501001,
            sum_value="700",
        )
        self._create_raw_line(
            fingerprint="dcf-2",
            dish_code="dish-100",
            order_number=501001,
            sum_value="300",
        )
        # Линия без сопоставления должна попасть в счётчик without_mapping.
        self._create_raw_line(
            fingerprint="dcf-3",
            dish_code="dish-unknown",
            order_number=501002,
            sum_value="999",
        )

        stats = rebuild_daily_category_fact_from_raw_lines()

        self.assertEqual(stats.scanned_raw_lines, 3)
        self.assertEqual(stats.lines_without_focus_mapping, 1)
        self.assertEqual(stats.grouped_rows, 1)
        self.assertEqual(stats.created_rows, 1)

        fact = GuestRestaurantDailyCategoryFact.objects.get()
        self.assertEqual(fact.guest_id, self.guest.id)
        self.assertEqual(fact.department_id, "dept-42")
        self.assertEqual(fact.focus_category_id, self.focus_category.id)
        self.assertEqual(fact.orders_count, 1)
        self.assertEqual(fact.items_count, 2)
        self.assertEqual(fact.sum_gross, Decimal("1000"))
        self.assertEqual(fact.sum_net, Decimal("1000"))

    def test_rebuild_daily_category_fact_updates_existing_row(self):
        """
        Повторный пересчёт должен обновлять текущую строку дневного факта.
        """
        self._create_raw_line(
            fingerprint="dcf-4",
            dish_code="dish-100",
            order_number=600001,
            sum_value="200",
        )
        rebuild_daily_category_fact_from_raw_lines()
        self.assertEqual(GuestRestaurantDailyCategoryFact.objects.count(), 1)

        self._create_raw_line(
            fingerprint="dcf-5",
            dish_code="dish-100",
            order_number=600002,
            sum_value="300",
        )
        stats = rebuild_daily_category_fact_from_raw_lines()

        self.assertEqual(stats.created_rows, 0)
        self.assertEqual(stats.updated_rows, 1)
        fact = GuestRestaurantDailyCategoryFact.objects.get()
        self.assertEqual(fact.orders_count, 2)
        self.assertEqual(fact.items_count, 2)
        self.assertEqual(fact.sum_gross, Decimal("500"))

    def test_rebuild_daily_category_fact_deletes_stale_rows_for_full_scope(self):
        """
        При полном пересчёте в выбранном периоде устаревшие строки должны удаляться.
        """
        self._create_raw_line(
            fingerprint="dcf-6",
            dish_code="dish-100",
            order_number=700001,
            sum_value="250",
        )
        stale_guest = Guest.objects.create(
            first_name="Старый гость",
            phone="+79990007777",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        GuestRestaurantDailyCategoryFact.objects.create(
            business_date=date(2026, 3, 18),
            guest=stale_guest,
            department_id="dept-stale",
            focus_category=self.focus_category,
            orders_count=2,
            items_count=2,
            sum_gross=Decimal("500"),
            sum_net=Decimal("500"),
            bonus_sum=Decimal("0"),
        )

        stats = rebuild_daily_category_fact_from_raw_lines(
            business_date_from=date(2026, 3, 18),
            business_date_to=date(2026, 3, 18),
        )

        self.assertEqual(stats.deleted_rows, 1)
        self.assertEqual(GuestRestaurantDailyCategoryFact.objects.count(), 1)
        fact = GuestRestaurantDailyCategoryFact.objects.get()
        self.assertEqual(fact.guest_id, self.guest.id)

    def test_rebuild_daily_category_fact_does_not_delete_on_incremental_raw_id_range(self):
        """
        При инкрементальном проходе по id сырого слоя удаление устаревших строк отключено.
        """
        raw_line = self._create_raw_line(
            fingerprint="dcf-7",
            dish_code="dish-100",
            order_number=800001,
            sum_value="350",
        )
        stale_guest = Guest.objects.create(
            first_name="Инкремент",
            phone="+79990008888",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        GuestRestaurantDailyCategoryFact.objects.create(
            business_date=date(2026, 3, 18),
            guest=stale_guest,
            department_id="dept-stale",
            focus_category=self.focus_category,
            orders_count=1,
            items_count=1,
            sum_gross=Decimal("120"),
            sum_net=Decimal("120"),
            bonus_sum=Decimal("0"),
        )

        stats = rebuild_daily_category_fact_from_raw_lines(
            raw_line_id_from=raw_line.id,
            raw_line_id_to=raw_line.id,
            business_date_from=date(2026, 3, 18),
            business_date_to=date(2026, 3, 18),
        )

        self.assertEqual(stats.deleted_rows, 0)
        self.assertEqual(GuestRestaurantDailyCategoryFact.objects.count(), 2)
