"""
Тесты сервисов синхронизации OLAP-справочников и resolved-связей.
"""

from __future__ import annotations

from datetime import date

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    OlapCategoryDict,
    OlapCheckSyncJournal,
    OlapNomenclatureDict,
    OlapSalesRawLine,
    VirtualCategory,
    VirtualCategoryNomenclatureLink,
    VirtualCategoryOlapCategoryLink,
)
from guests.services.olap_catalogs import (
    rebuild_focus_category_nomenclature_resolved,
    sync_olap_catalogs_from_raw_lines,
)


class OlapCatalogSyncServiceTests(TestCase):
    """
    Проверки сервиса заполнения справочников из сырых строк OLAP.
    """

    def _create_journal(self, key: str) -> OlapCheckSyncJournal:
        return OlapCheckSyncJournal.objects.create(
            idempotency_key=key,
            status=OlapCheckSyncJournal.Status.NEW,
        )

    def _create_raw_line(
        self,
        *,
        fingerprint: str,
        order_number: int,
        category_id: str | None,
        category_name: str | None,
        dish_code: str | None,
        dish_name: str | None,
    ) -> OlapSalesRawLine:
        journal = self._create_journal(key=f"journal:{fingerprint}")
        return OlapSalesRawLine.objects.create(
            row_fingerprint=fingerprint,
            sync_journal=journal,
            business_date=date(2026, 3, 18),
            order_number=order_number,
            dish_category_id=category_id,
            dish_category_name=category_name,
            dish_code=dish_code,
            dish_name=dish_name,
            dish_group_id="group-1",
            dish_group_name="Группа 1",
        )

    def test_sync_creates_category_and_nomenclature_dicts(self):
        """
        Сервис должен создать записи категорий и номенклатуры из raw-строк.
        """
        self._create_raw_line(
            fingerprint="fp-001",
            order_number=1001,
            category_id="cat-olap-1",
            category_name="Бизнес-ланч",
            dish_code="54768",
            dish_name="Обед 490р",
        )
        self._create_raw_line(
            fingerprint="fp-002",
            order_number=1001,
            category_id="cat-olap-1",
            category_name="Бизнес-ланч",
            dish_code="54788",
            dish_name="Лепешка тандырная 1/2",
        )

        stats = sync_olap_catalogs_from_raw_lines()

        self.assertEqual(stats.scanned_raw_lines, 2)
        self.assertEqual(stats.categories_created, 1)
        self.assertEqual(stats.nomenclatures_created, 2)
        self.assertEqual(stats.skipped_without_category, 0)
        self.assertEqual(stats.skipped_without_nomenclature, 0)

        category = OlapCategoryDict.objects.get(iiko_category_external_id="cat-olap-1")
        self.assertEqual(category.category_name, "Бизнес-ланч")

        nom = OlapNomenclatureDict.objects.get(iiko_nomenclature_external_id="54768")
        self.assertEqual(nom.nomenclature_name, "Обед 490р")
        self.assertEqual(nom.olap_category_id, category.id)

    def test_sync_uses_name_fallback_when_category_id_missing(self):
        """
        При отсутствии `dish_category_id` используется fallback по имени категории.
        """
        self._create_raw_line(
            fingerprint="fp-003",
            order_number=1002,
            category_id=None,
            category_name="Десерты СП",
            dish_code="97974654165468810341",
            dish_name="Шахерезада 1/2 БЛ",
        )

        stats = sync_olap_catalogs_from_raw_lines()

        self.assertEqual(stats.categories_created, 1)
        self.assertTrue(
            OlapCategoryDict.objects.filter(
                iiko_category_external_id="name::десерты сп",
                category_name="Десерты СП",
            ).exists()
        )


class FocusResolvedRebuildServiceTests(TestCase):
    """
    Проверки пересборки `focus_category_nomenclature_resolved`.
    """

    def test_rebuild_supports_direct_and_virtual_focus_categories(self):
        """
        Пересборка должна корректно развернуть:
        1. прямую OLAP-категорию;
        2. виртуальную категорию (по номенклатурам и по OLAP-категориям).
        """
        cat_main = OlapCategoryDict.objects.create(
            iiko_category_external_id="cat-main",
            category_name="Бизнес-ланч",
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        cat_extra = OlapCategoryDict.objects.create(
            iiko_category_external_id="cat-extra",
            category_name="Десерты СП",
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )

        nom_a = OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id="dish-a",
            nomenclature_name="Обед 490р",
            olap_category=cat_main,
        )
        nom_b = OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id="dish-b",
            nomenclature_name="Лепешка",
            olap_category=cat_main,
        )
        nom_c = OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id="dish-c",
            nomenclature_name="Шахерезада",
            olap_category=cat_extra,
        )

        direct_focus = FocusCategory.objects.create(
            code="direct_main",
            name="Прямая OLAP категория",
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=cat_main,
            is_enabled=True,
        )

        virtual = VirtualCategory.objects.create(
            code="virt_meat",
            name="Виртуальная категория",
            is_active=True,
        )
        VirtualCategoryNomenclatureLink.objects.create(virtual_category=virtual, nomenclature=nom_c)
        VirtualCategoryOlapCategoryLink.objects.create(virtual_category=virtual, olap_category=cat_main)

        virtual_focus = FocusCategory.objects.create(
            code="virt_focus",
            name="Фокус виртуальной категории",
            source_type=FocusCategory.SourceType.VIRTUAL,
            virtual_category=virtual,
            is_enabled=True,
        )

        disabled_focus = FocusCategory.objects.create(
            code="disabled_focus",
            name="Отключенный фокус",
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=cat_extra,
            is_enabled=False,
        )
        FocusCategoryNomenclatureResolved.objects.create(
            focus_category=disabled_focus,
            nomenclature=nom_c,
            source_reason=FocusCategoryNomenclatureResolved.SourceReason.DIRECT_OLAP,
        )

        stats = rebuild_focus_category_nomenclature_resolved()

        self.assertEqual(stats.scanned_focus_categories, 3)
        self.assertEqual(stats.rebuilt_focus_categories, 2)
        self.assertEqual(stats.disabled_focus_categories_cleared, 1)
        self.assertEqual(stats.skipped_invalid_focus_categories, 0)

        direct_links = list(
            FocusCategoryNomenclatureResolved.objects.filter(focus_category=direct_focus)
            .order_by("nomenclature__iiko_nomenclature_external_id")
            .values_list("nomenclature__iiko_nomenclature_external_id", "source_reason")
        )
        self.assertEqual(
            direct_links,
            [
                ("dish-a", FocusCategoryNomenclatureResolved.SourceReason.DIRECT_OLAP),
                ("dish-b", FocusCategoryNomenclatureResolved.SourceReason.DIRECT_OLAP),
            ],
        )

        virtual_links = list(
            FocusCategoryNomenclatureResolved.objects.filter(focus_category=virtual_focus)
            .order_by("nomenclature__iiko_nomenclature_external_id")
            .values_list("nomenclature__iiko_nomenclature_external_id", "source_reason")
        )
        self.assertEqual(
            virtual_links,
            [
                ("dish-a", FocusCategoryNomenclatureResolved.SourceReason.VIRTUAL_OLAP_CATEGORY),
                ("dish-b", FocusCategoryNomenclatureResolved.SourceReason.VIRTUAL_OLAP_CATEGORY),
                ("dish-c", FocusCategoryNomenclatureResolved.SourceReason.VIRTUAL_NOMENCLATURE),
            ],
        )
        self.assertFalse(
            FocusCategoryNomenclatureResolved.objects.filter(focus_category=disabled_focus).exists()
        )
