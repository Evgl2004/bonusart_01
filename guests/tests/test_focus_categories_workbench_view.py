"""
Тесты экрана «Категории и цели».
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from guests.models import (
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    Guest,
    GuestRestaurantDailyCategoryFact,
    OlapCategoryDict,
    OlapNomenclatureDict,
    OrderFact,
    VirtualCategory,
    VirtualCategoryNomenclatureLink,
)


class FocusCategoriesWorkbenchTests(TestCase):
    """
    Проверяем базовый UX экрана категорий и ключевые действия управления.
    """

    def setUp(self):
        self.as_of_date = date(2026, 3, 23)
        self.department_id = "dep-1"
        self.guest = Guest.objects.create(phone="+79990000011", first_name="Анна")

        OrderFact.objects.create(
            guest=self.guest,
            business_date=date(2026, 3, 20),
            department_id=self.department_id,
            department_name="Сами Сусами",
            order_number=101,
            uniq_order_id="uniq-101",
            net_sum=Decimal("1500.00"),
            gross_sum=Decimal("1500.00"),
        )

        self.olap_category_1 = OlapCategoryDict.objects.create(
            iiko_category_external_id="cat-1",
            category_name="Пиво Ермолаевъ",
            is_active=True,
        )
        self.olap_category_2 = OlapCategoryDict.objects.create(
            iiko_category_external_id="cat-2",
            category_name="Вино",
            is_active=True,
        )

        self.nomenclature_1 = OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id="dish-1",
            nomenclature_name="Пиво светлое",
            olap_category=self.olap_category_1,
            is_active=True,
        )
        OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id="dish-2",
            nomenclature_name="Вино белое",
            olap_category=self.olap_category_2,
            is_active=True,
        )

        self.focus = FocusCategory.objects.create(
            code="beer_ermolaev",
            name="Пиво Ермолаевъ",
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=self.olap_category_1,
            is_enabled=True,
        )
        FocusCategoryNomenclatureResolved.objects.create(
            focus_category=self.focus,
            nomenclature=self.nomenclature_1,
            source_reason=FocusCategoryNomenclatureResolved.SourceReason.DIRECT_OLAP,
        )
        GuestRestaurantDailyCategoryFact.objects.create(
            business_date=self.as_of_date,
            guest=self.guest,
            department_id=self.department_id,
            focus_category=self.focus,
            orders_count=1,
            items_count=1,
            sum_gross=Decimal("350.00"),
            sum_net=Decimal("350.00"),
            bonus_sum=Decimal("0.00"),
        )

    def test_focus_categories_page_renders_data(self):
        """
        Экран должен открываться и показывать таблицу целевых категорий.
        """
        response = self.client.get(
            reverse("focus_categories"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Целевые категории")
        self.assertContains(response, self.focus.name)
        payload = response.context["payload"]
        self.assertEqual(payload["stats"]["focus_total"], 1)
        self.assertEqual(len(payload["focus_rows"]), 1)

    def test_create_focus_from_olap_action_creates_focus_and_resolved(self):
        """
        Действие create_focus_from_olap должно создать фокус и заполнить resolved-связи.
        """
        response = self.client.post(
            reverse("focus_categories_actions"),
            {
                "action": "create_focus_from_olap",
                "olap_category_id": self.olap_category_2.id,
                "focus_name": "Вино",
                "focus_code": "wine",
                "priority_weight": 2,
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        created_focus = FocusCategory.objects.get(code="wine")
        self.assertEqual(created_focus.source_type, FocusCategory.SourceType.OLAP_DIRECT)
        self.assertEqual(created_focus.olap_category_id, self.olap_category_2.id)
        self.assertTrue(
            FocusCategoryNomenclatureResolved.objects.filter(focus_category=created_focus).exists()
        )

    def test_set_focus_enabled_action_disables_and_clears_resolved(self):
        """
        При отключении фокуса связи resolved должны быть очищены.
        """
        response = self.client.post(
            reverse("focus_categories_actions"),
            {
                "action": "set_focus_enabled",
                "focus_id": self.focus.id,
                "enabled": "0",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "selected_focus_id": self.focus.id,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        self.focus.refresh_from_db()
        self.assertFalse(self.focus.is_enabled)
        self.assertFalse(
            FocusCategoryNomenclatureResolved.objects.filter(focus_category=self.focus).exists()
        )

    def test_create_virtual_category_from_nomenclature_with_target(self):
        """
        Конструктор должен создавать виртуальную и целевую категории из выбранной номенклатуры.
        """
        response = self.client.post(
            reverse("focus_categories_actions"),
            {
                "action": "create_virtual_category_from_nomenclature",
                "virtual_name": "Мангал",
                "virtual_code": "grill_virtual",
                "create_target_category": "1",
                "focus_name": "Мангал",
                "focus_code": "grill_target",
                "priority_weight": 2,
                "nomenclature_ids": [str(self.nomenclature_1.id)],
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        virtual_category = VirtualCategory.objects.get(code="grill_virtual")
        self.assertEqual(virtual_category.name, "Мангал")
        self.assertTrue(
            VirtualCategoryNomenclatureLink.objects.filter(
                virtual_category=virtual_category,
                nomenclature_id=self.nomenclature_1.id,
            ).exists()
        )

        target_category = FocusCategory.objects.get(code="grill_target")
        self.assertEqual(target_category.source_type, FocusCategory.SourceType.VIRTUAL)
        self.assertEqual(target_category.virtual_category_id, virtual_category.id)
