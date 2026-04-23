"""
Тесты экрана «Гости (workbench)».
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from guests.models import (
    FocusCategory,
    Guest,
    GuestWorkbenchFilterPreset,
    GuestRestaurantDailyCategoryFact,
    GuestRestaurantWindowCategoryMetrics,
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
        self.assertEqual(payload["cards"]["guests_total"], 1)
        self.assertEqual(payload["cards"]["orders_total"], 3)
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(len(payload["selected_guests"]["rows"]), 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], self.guest_1.phone)
        self.assertContains(response, 'data-segment-code="active_30d"')
        self.assertContains(response, 'data-focus-category-code="beer_ermolaev"')

    def test_workbench_applies_single_complex_filter(self):
        """
        Сложный фильтр по количеству заказов (orders_count) должен отбирать только подходящих гостей.
        """
        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "cf_field": ["orders_count"],
                "cf_op": ["gt"],
                "cf_value": ["1"],
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(len(payload["filters"]["complex_filters"]), 1)
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], self.guest_1.phone)

    def test_workbench_applies_multiple_complex_filters_with_and_logic(self):
        """
        Несколько сложных фильтров должны применяться с логикой И.
        """
        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "cf_field": ["orders_count", "sum_net"],
                "cf_op": ["gte", "lt"],
                "cf_value": ["3", "1600"],
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(len(payload["filters"]["complex_filters"]), 2)
        self.assertEqual(payload["cards"]["guests_total"], 1)
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], self.guest_1.phone)

    def test_workbench_ignores_invalid_complex_filters(self):
        """
        Невалидные сложные фильтры должны отбрасываться без поломки выборки.
        """
        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "cf_field": ["bad_field", "orders_count"],
                "cf_op": ["eq", "bad_operator"],
                "cf_value": ["1", "3"],
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(len(payload["filters"]["complex_filters"]), 0)
        self.assertEqual(payload["cards"]["guests_total"], 2)
        self.assertEqual(payload["selected_guests"]["total"], 2)

    @override_settings(WORKBENCH_CATEGORY_WINDOW_METRICS_V2=False)
    def test_focus_selected_uses_general_window_metrics_when_flag_disabled(self):
        """
        При выключенном флаге даже с выбранной категорией должен работать режим A.
        """
        self._create_category_window_metric(
            guest=self.guest_1,
            focus=self.focus_beer,
            orders_count=1,
            visits_count=1,
            sum_net="300.00",
            avg_check_net="300.00",
            rating_score="8.00",
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "focus_category_code": "beer_ermolaev",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["metrics_layer"], "window")
        self.assertEqual(payload["cards"]["orders_total"], 3)
        self.assertEqual(payload["selected_guests"]["rows"][0]["orders_count"], 3)

    @override_settings(WORKBENCH_CATEGORY_WINDOW_METRICS_V2=True)
    def test_focus_selected_uses_category_window_metrics_when_flag_enabled(self):
        """
        При включенном флаге и выбранной категории должен работать режим B.
        """
        self._create_category_window_metric(
            guest=self.guest_1,
            focus=self.focus_beer,
            orders_count=1,
            visits_count=1,
            sum_net="300.00",
            avg_check_net="300.00",
            rating_score="8.00",
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "focus_category_code": "beer_ermolaev",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["metrics_layer"], "category_window")
        self.assertEqual(payload["cards"]["orders_total"], 1)
        self.assertEqual(payload["cards"]["net_total"], "300,00")
        self.assertEqual(payload["selected_guests"]["rows"][0]["orders_count"], 1)

    @override_settings(WORKBENCH_CATEGORY_WINDOW_METRICS_V2=True)
    def test_complex_filters_are_applied_to_category_window_layer(self):
        """
        В режиме B сложные фильтры должны применяться к category-window метрикам.
        """
        self._create_category_window_metric(
            guest=self.guest_1,
            focus=self.focus_beer,
            orders_count=1,
            visits_count=1,
            sum_net="300.00",
            avg_check_net="300.00",
            rating_score="8.00",
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "focus_category_code": "beer_ermolaev",
                "cf_field": ["orders_count"],
                "cf_op": ["eq"],
                "cf_value": ["1"],
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["metrics_layer"], "category_window")
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["orders_count"], 1)

    def test_lost_segment_guest_visible_even_without_row_in_selected_window(self):
        """
        Гость из сегмента «Потерянные 60+д» должен попадать в таб «Гости»,
        даже если строка метрик есть только в окне 180 дней.
        """
        lost_guest = Guest.objects.create(phone="+79990003333", first_name="Потерянный")
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=lost_guest,
            department_id=self.department_id,
            window_days=180,
            orders_count=2,
            visits_count=2,
            avg_check_net=Decimal("600.00"),
            sum_net=Decimal("1200.00"),
            bonus_in_sum=Decimal("0.00"),
            bonus_out_sum=Decimal("0.00"),
            rating_score=Decimal("10.00"),
            last_visit_at=date(2025, 12, 20),
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "lost_60d_plus",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["segment_code"], "lost_60d_plus")
        self.assertGreaterEqual(payload["selected_guests"]["total"], 1)
        phones = {item["phone"] for item in payload["selected_guests"]["rows"]}
        self.assertIn(lost_guest.phone, phones)

        lost_row = next(item for item in payload["selected_guests"]["rows"] if item["phone"] == lost_guest.phone)
        self.assertEqual(lost_row["source_window_days"], 180)

    def test_lost_segment_with_complex_filter_uses_representative_window(self):
        """
        Для сегмента «Потерянные 60+д» сложный фильтр должен применяться
        к репрезентативной строке (fallback по окнам), а не только к выбранному окну.
        """
        lost_guest = Guest.objects.create(phone="+79990004444", first_name="Потерянный фильтр")
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=lost_guest,
            department_id=self.department_id,
            window_days=180,
            orders_count=2,
            visits_count=2,
            avg_check_net=Decimal("2500.00"),
            sum_net=Decimal("5000.00"),
            bonus_in_sum=Decimal("0.00"),
            bonus_out_sum=Decimal("0.00"),
            rating_score=Decimal("20.00"),
            last_visit_at=date(2025, 12, 10),
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "lost_60d_plus",
                "cf_field": ["avg_check_net"],
                "cf_op": ["gte"],
                "cf_value": ["2000"],
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["segment_code"], "lost_60d_plus")
        self.assertEqual(len(payload["filters"]["complex_filters"]), 1)
        self.assertGreaterEqual(payload["selected_guests"]["total"], 1)
        phones = {item["phone"] for item in payload["selected_guests"]["rows"]}
        self.assertIn(lost_guest.phone, phones)

        lost_row = next(item for item in payload["selected_guests"]["rows"] if item["phone"] == lost_guest.phone)
        self.assertEqual(lost_row["source_window_days"], 180)

    def test_without_segment_table_uses_selected_window_only(self):
        """
        Без выбранного сегмента таблица гостей должна использовать строго выбранное окно.
        """
        lost_guest = Guest.objects.create(phone="+79990005555", first_name="Только 180")
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=lost_guest,
            department_id=self.department_id,
            window_days=180,
            orders_count=1,
            visits_count=1,
            avg_check_net=Decimal("700.00"),
            sum_net=Decimal("700.00"),
            bonus_in_sum=Decimal("0.00"),
            bonus_out_sum=Decimal("0.00"),
            rating_score=Decimal("9.00"),
            last_visit_at=date(2025, 12, 1),
        )

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

        phones = {item["phone"] for item in payload["selected_guests"]["rows"]}
        self.assertNotIn(lost_guest.phone, phones)
        self.assertEqual(payload["selected_guests"]["total"], 2)

    def test_active_segment_uses_window_30_even_if_selected_window_is_180(self):
        """
        Для сегмента «Активные 30д» таблица должна брать строку окна 30,
        даже если в фильтре выбрано окно 180.
        """
        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 180,
                "department_id": self.department_id,
                "segment_code": "active_30d",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.context["payload"]
        self.assertEqual(payload["selected_guests"]["total"], 1)

        row = payload["selected_guests"]["rows"][0]
        self.assertEqual(row["phone"], self.guest_1.phone)
        self.assertEqual(row["source_window_days"], 30)

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

    def test_rename_filter_preset_from_workbench(self):
        """
        Действие rename_filter_preset должно менять имя активного пресета.
        """
        preset = GuestWorkbenchFilterPreset.objects.create(
            name="Старое имя",
            window_days=30,
            department_id=self.department_id,
            segment_code="active_30d",
            focus_category_code="beer_ermolaev",
            is_active=True,
        )

        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "rename_filter_preset",
                "preset_id": preset.id,
                "new_name": "Новое имя",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "active_30d",
                "focus_category_code": "beer_ermolaev",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        preset.refresh_from_db()
        self.assertEqual(preset.name, "Новое имя")

    def test_delete_filter_preset_from_workbench(self):
        """
        Действие delete_filter_preset должно деактивировать пресет.
        """
        preset = GuestWorkbenchFilterPreset.objects.create(
            name="Удаляемый пресет",
            window_days=30,
            department_id=self.department_id,
            segment_code="active_30d",
            focus_category_code="beer_ermolaev",
            is_active=True,
        )

        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "delete_filter_preset",
                "preset_id": preset.id,
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "active_30d",
                "focus_category_code": "beer_ermolaev",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        preset.refresh_from_db()
        self.assertFalse(preset.is_active)

    def test_show_all_presets_flag_displays_inactive_presets(self):
        """
        По умолчанию в workbench показываются только активные пресеты.
        При show_all_presets=1 должны отображаться и неактивные.
        """
        GuestWorkbenchFilterPreset.objects.create(
            name="Активный пресет",
            window_days=30,
            department_id=self.department_id,
            segment_code="active_30d",
            focus_category_code="beer_ermolaev",
            is_active=True,
        )
        GuestWorkbenchFilterPreset.objects.create(
            name="Архивный пресет",
            window_days=30,
            department_id=self.department_id,
            segment_code="cooling_30_60d",
            focus_category_code="wine",
            is_active=False,
        )

        default_response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
            },
            secure=True,
        )
        self.assertEqual(default_response.status_code, 200)
        default_presets = default_response.context["payload"]["filters"]["saved_presets"]
        self.assertEqual(len(default_presets), 1)
        self.assertTrue(default_presets[0]["is_active"])
        self.assertEqual(default_response.context["selected_show_all_presets"], False)

        all_response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "show_all_presets": "1",
            },
            secure=True,
        )
        self.assertEqual(all_response.status_code, 200)
        all_presets = all_response.context["payload"]["filters"]["saved_presets"]
        self.assertEqual(len(all_presets), 2)
        self.assertTrue(any(not item["is_active"] for item in all_presets))
        self.assertEqual(all_response.context["selected_show_all_presets"], True)

    def test_restore_filter_preset_from_workbench(self):
        """
        Действие restore_filter_preset должно возвращать пресет в активные.
        """
        preset = GuestWorkbenchFilterPreset.objects.create(
            name="Архивный для восстановления",
            window_days=30,
            department_id=self.department_id,
            segment_code="active_30d",
            focus_category_code="beer_ermolaev",
            is_active=False,
        )

        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "restore_filter_preset",
                "preset_id": preset.id,
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "active_30d",
                "focus_category_code": "beer_ermolaev",
                "show_all_presets": "1",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        preset.refresh_from_db()
        self.assertTrue(preset.is_active)
        self.assertIn("show_all_presets=1", response.url)

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

    def _create_category_window_metric(
        self,
        *,
        guest: Guest,
        focus: FocusCategory,
        orders_count: int,
        visits_count: int,
        sum_net: str,
        avg_check_net: str,
        rating_score: str,
    ) -> None:
        """
        Создаёт строку category-window метрик для проверок режима B.
        """
        GuestRestaurantWindowCategoryMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=guest,
            department_id=self.department_id,
            window_days=30,
            focus_category=focus,
            orders_count=orders_count,
            visits_count=visits_count,
            sum_net=Decimal(sum_net),
            sum_focus_net=Decimal(sum_net),
            avg_check_net=Decimal(avg_check_net),
            bonus_in_sum=Decimal("0.00"),
            bonus_out_sum=Decimal("0.00"),
            rating_score=Decimal(rating_score),
            last_visit_at=self.as_of_date,
        )
