"""
Тесты экрана «Гости (workbench)».
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from guests.models import (
    FocusCategory,
    Guest,
    GuestWorkbenchFilterPreset,
    GuestRestaurantDailyCategoryFact,
    GuestRestaurantWindowMetrics,
    Mailing,
    MailingGuest,
    MessageTemplate,
    OlapCategoryDict,
    OrderFact,
)


class GuestsWorkbenchViewTests(TestCase):
    """
    Проверяем базовую доступность и ключевые показатели workbench-экрана.
    """

    def setUp(self):
        self.guest_1 = Guest.objects.create(phone="+79990001111", first_name="Анна")
        self.guest_2 = Guest.objects.create(phone="+79990002222", first_name="Иван")
        self.template = MessageTemplate.objects.create(
            name="Тестовый шаблон",
            message_text="Привет, {{first_name}}",
            created_by="tests",
            is_active=True,
        )
        self.as_of_date = date(2026, 3, 23)
        self.department_id = "dep-1"
        self.focus_beer = self._create_focus_category("beer_ermolaev", "Пиво Ермолаевъ")
        self.focus_wine = self._create_focus_category("wine", "Вино")

        OrderFact.objects.create(
            guest=self.guest_1,
            business_date=date(2026, 3, 20),
            department_id=self.department_id,
            department_name="Сами Сусами",
            order_number=101,
            uniq_order_id="uniq-101",
            net_sum=Decimal("1500.00"),
            gross_sum=Decimal("1500.00"),
        )

        # Гость 1: активный (2+ визита за 30 дней)
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_1,
            department_id=self.department_id,
            window_days=30,
            orders_count=3,
            visits_count=2,
            avg_check_net=Decimal("500.00"),
            sum_net=Decimal("1500.00"),
            bonus_in_sum=Decimal("50.00"),
            bonus_out_sum=Decimal("10.00"),
            rating_score=Decimal("15.00"),
            last_visit_at=date(2026, 3, 22),
        )
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_1,
            department_id=self.department_id,
            window_days=60,
            orders_count=3,
            visits_count=2,
            avg_check_net=Decimal("500.00"),
            sum_net=Decimal("1500.00"),
            bonus_in_sum=Decimal("50.00"),
            bonus_out_sum=Decimal("10.00"),
            rating_score=Decimal("15.00"),
            last_visit_at=date(2026, 3, 22),
        )
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_1,
            department_id=self.department_id,
            window_days=180,
            orders_count=3,
            visits_count=2,
            avg_check_net=Decimal("500.00"),
            sum_net=Decimal("1500.00"),
            bonus_in_sum=Decimal("50.00"),
            bonus_out_sum=Decimal("10.00"),
            rating_score=Decimal("15.00"),
            last_visit_at=date(2026, 3, 22),
        )

        # Гость 2: остывший (0 в 30, >0 в 60)
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_2,
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
            guest=self.guest_2,
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

        # Дневные факты для матрицы «сегменты × фокусные категории».
        GuestRestaurantDailyCategoryFact.objects.create(
            business_date=date(2026, 3, 22),
            guest=self.guest_1,
            department_id=self.department_id,
            focus_category=self.focus_beer,
            orders_count=1,
            items_count=2,
            sum_gross=Decimal("900.00"),
            sum_net=Decimal("900.00"),
            bonus_sum=Decimal("0.00"),
        )
        GuestRestaurantDailyCategoryFact.objects.create(
            business_date=date(2026, 3, 15),
            guest=self.guest_2,
            department_id=self.department_id,
            focus_category=self.focus_wine,
            orders_count=1,
            items_count=1,
            sum_gross=Decimal("700.00"),
            sum_net=Decimal("700.00"),
            bonus_sum=Decimal("0.00"),
        )
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=self.guest_2,
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

    def test_workbench_page_returns_payload_and_segments(self):
        """
        Страница должна отдавать данные и корректно считать сегменты.
        """
        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["window_days"], 30)
        self.assertEqual(payload["filters"]["department_id"], self.department_id)

        self.assertEqual(payload["segments"]["active_30d"], 1)
        self.assertEqual(payload["segments"]["cooling_30_60d"], 1)
        self.assertEqual(payload["cards"]["guests_total"], 2)
        self.assertEqual(payload["selected_guests"]["total"], 2)
        self.assertIn("saved_presets", payload["filters"])

        matrix = payload["segment_focus_matrix"]
        col_index = {col["focus_category_code"]: idx for idx, col in enumerate(matrix["columns"])}
        row_index = {row["segment_code"]: row for row in matrix["rows"]}

        self.assertIn("beer_ermolaev", col_index)
        self.assertIn("wine", col_index)
        self.assertEqual(row_index["active_30d"]["guests_total"], 1)
        self.assertEqual(row_index["cooling_30_60d"]["guests_total"], 1)

        active_beer_cell = row_index["active_30d"]["cells"][col_index["beer_ermolaev"]]
        cooling_wine_cell = row_index["cooling_30_60d"]["cells"][col_index["wine"]]
        self.assertEqual(active_beer_cell["guests_count"], 1)
        self.assertEqual(cooling_wine_cell["guests_count"], 1)

    def test_workbench_filters_by_segment_and_focus_category(self):
        """
        Фильтр по сегменту и фокусной категории должен возвращать целевого гостя.
        """
        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "active_30d",
                "focus_category_code": "beer_ermolaev",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["segment_code"], "active_30d")
        self.assertEqual(payload["filters"]["focus_category_code"], "beer_ermolaev")
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(len(payload["selected_guests"]["rows"]), 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], self.guest_1.phone)
        self.assertContains(response, 'data-segment-code="active_30d"')
        self.assertContains(response, 'data-focus-category-code="beer_ermolaev"')

    def test_create_mailing_draft_from_workbench_selection(self):
        """
        Быстрое действие должно создавать черновик рассылки по отобранным гостям.
        """
        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "create_mailing_draft",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "active_30d",
                "focus_category_code": "beer_ermolaev",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        mailing = Mailing.objects.get()
        self.assertEqual(mailing.template_id, self.template.id)
        self.assertEqual(
            response.url,
            reverse("mailing_edit", kwargs={"pk": mailing.id}),
        )

        rows = list(MailingGuest.objects.filter(mailing=mailing).order_by("guest_id"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].guest_id, self.guest_1.id)

    def test_save_filter_preset_from_workbench(self):
        """
        Быстрое действие должно сохранять пресет текущих фильтров.
        """
        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "save_filter_preset",
                "preset_name": "Остывшие + Вино",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "cooling_30_60d",
                "focus_category_code": "wine",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("guests_workbench")))

        preset = GuestWorkbenchFilterPreset.objects.get(name="Остывшие + Вино")
        self.assertEqual(preset.window_days, 30)
        self.assertEqual(preset.department_id, self.department_id)
        self.assertEqual(preset.segment_code, "cooling_30_60d")
        self.assertEqual(preset.focus_category_code, "wine")

    def _create_focus_category(self, code: str, name: str) -> FocusCategory:
        """
        Создаёт минимальный набор сущностей для активной фокусной категории.
        """
        olap_category = OlapCategoryDict.objects.create(
            iiko_category_external_id=f"ext-{code}",
            category_name=name,
            is_active=True,
        )
        return FocusCategory.objects.create(
            code=code,
            name=name,
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=olap_category,
            is_enabled=True,
        )
