"""
Тесты экрана «Гости (workbench)».
"""

from __future__ import annotations

import uuid
from datetime import date, time
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from guests.models import (
    BotProfile,
    FocusCategory,
    Guest,
    GuestBotBinding,
    GuestRestaurantDailyOrderFact,
    GuestWorkbenchFilterPreset,
    GuestRestaurantDailyCategoryFact,
    GuestRestaurantWindowCategoryMetrics,
    GuestRestaurantWindowMetrics,
    Mailing,
    MailingGuest,
    MessageTemplate,
    OlapCategoryDict,
    OrderFact,
    VtelemaxRecipientChannel,
)


class GuestsWorkbenchViewTests(TestCase):
    """
    Проверяем базовую доступность и ключевые показатели workbench-экрана.
    """

    def setUp(self):
        self.guest_1 = Guest.objects.create(phone="+79990001111", first_name="Анна")
        self.guest_2 = Guest.objects.create(phone="+79990002222", first_name="Иван")
        self.bot_telegram = BotProfile.objects.create(
            code="workbench_tg",
            name="Рабочий Telegram",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self._create_bot_binding(self.guest_1, external_chat_id="tg-guest-1", is_primary=True)
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
        self.assertIn(self.template, list(response.context["workbench_message_templates"]))
        self.assertIn(self.bot_telegram, list(response.context["workbench_bot_profiles"]))

        delivery_preview = response.context["workbench_delivery_preview"]
        self.assertEqual(len(delivery_preview["guests"]), 2)
        self.assertEqual(delivery_preview["bots"][0]["id"], self.bot_telegram.id)
        guest_1_preview = next(
            item for item in delivery_preview["guests"] if item["guest_id"] == self.guest_1.id
        )
        self.assertEqual(guest_1_preview["bindings"][0]["bot_profile_id"], self.bot_telegram.id)
        self.assertTrue(guest_1_preview["bindings"][0]["permitted"])

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

    def test_workbench_filters_by_favorite_venue_mode(self):
        """
        Режим «любимое заведение» должен оставлять гостей, у которых выбранное
        заведение лидирует по числу заказов в выбранном окне.
        """
        another_department_id = "dep-2"
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 21),
            guest=self.guest_1,
            department_id=self.department_id,
            orders_count=1,
            sum_net=Decimal("500.00"),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 21),
            guest=self.guest_1,
            department_id=another_department_id,
            orders_count=5,
            sum_net=Decimal("2500.00"),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 21),
            guest=self.guest_2,
            department_id=self.department_id,
            orders_count=4,
            sum_net=Decimal("2800.00"),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 21),
            guest=self.guest_2,
            department_id=another_department_id,
            orders_count=1,
            sum_net=Decimal("700.00"),
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "venue_selection_mode": "favorite",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["venue_selection_mode"], "favorite")
        self.assertEqual(payload["filters"]["venue_selection"]["total"], 1)
        self.assertEqual(payload["cards"]["guests_total"], 1)
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], self.guest_2.phone)

    def test_workbench_filters_guests_with_new_bot_delivery(self):
        """
        Фильтр аудитории должен оставлять гостей с рабочей доставкой в новых ботах.
        """
        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "audience_channel_group": "new_bots_sendable",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["audience_channel_group"], "new_bots_sendable")
        self.assertEqual(payload["cards"]["guests_total"], 1)
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], self.guest_1.phone)

    def test_workbench_filters_legacy_guests_without_new_bot_binding(self):
        """
        Фильтр legacy должен оставлять гостей без привязки к новым ботам.
        """
        self._legacy_telegram_channel(self.guest_2, external_id="legacy-tg-guest-2")

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "audience_channel_group": "legacy_no_new_bot",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["audience_channel_group"], "legacy_no_new_bot")
        self.assertEqual(payload["cards"]["guests_total"], 1)
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], self.guest_2.phone)

    def test_workbench_filters_new_bot_guests_blocked_for_messages(self):
        """
        Отдельная диагностическая группа не должна смешиваться с legacy.
        """
        self._create_bot_binding(
            self.guest_2,
            external_chat_id="tg-guest-2",
            is_primary=False,
            is_opt_in=False,
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "audience_channel_group": "new_bots_blocked",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["audience_channel_group"], "new_bots_blocked")
        self.assertEqual(payload["cards"]["guests_total"], 1)
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], self.guest_2.phone)

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
                "venue_selection_mode": "visited_once",
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

    def test_new_in_venue_segment_includes_first_purchase_in_selected_period(self):
        """
        Гость попадает в «Новые», если первая покупка в выбранном заведении
        пришлась на выбранный период.
        """
        new_guest = Guest.objects.create(phone="+79990007771", first_name="Новый")
        self._create_window_metric(
            guest=new_guest,
            orders_count=2,
            visits_count=2,
            sum_net="1800.00",
            avg_check_net="900.00",
            rating_score="25.00",
            last_visit_at=date(2026, 3, 20),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 20),
            guest=new_guest,
            department_id=self.department_id,
            orders_count=2,
            sum_net=Decimal("1800.00"),
        )
        GuestRestaurantDailyCategoryFact.objects.create(
            business_date=date(2026, 3, 20),
            guest=new_guest,
            department_id=self.department_id,
            focus_category=self.focus_beer,
            orders_count=1,
            items_count=1,
            sum_gross=Decimal("900.00"),
            sum_net=Decimal("900.00"),
            bonus_sum=Decimal("0.00"),
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "new_in_venue",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["segments"]["new_in_venue"], 1)
        self.assertEqual(payload["segments"]["active_30d"], 2)
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], new_guest.phone)
        self.assertEqual(payload["selected_guests"]["rows"][0]["segment_code"], "new_in_venue")

        matrix = payload["segment_focus_matrix"]
        col_index = {col["focus_category_code"]: idx for idx, col in enumerate(matrix["columns"])}
        row_index = {row["segment_code"]: row for row in matrix["rows"]}
        self.assertEqual(row_index["new_in_venue"]["guests_total"], 1)
        self.assertEqual(
            row_index["new_in_venue"]["cells"][col_index["beer_ermolaev"]]["guests_count"],
            1,
        )

    def test_new_in_venue_segment_excludes_first_purchase_before_selected_period(self):
        """
        Гость не попадает в «Новые», если первая покупка в выбранном заведении
        была раньше выбранного периода.
        """
        old_guest = Guest.objects.create(phone="+79990007772", first_name="Старый")
        self._create_window_metric(
            guest=old_guest,
            orders_count=1,
            visits_count=1,
            sum_net="700.00",
            avg_check_net="700.00",
            rating_score="12.00",
            last_visit_at=date(2026, 2, 20),
            window_days=180,
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 2, 20),
            guest=old_guest,
            department_id=self.department_id,
            orders_count=1,
            sum_net=Decimal("700.00"),
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "new_in_venue",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["segments"]["new_in_venue"], 0)
        self.assertEqual(payload["selected_guests"]["total"], 0)

    def test_new_in_venue_segment_is_calculated_per_selected_venue(self):
        """
        Старые покупки в другом заведении не мешают считать гостя новым
        для выбранного заведения.
        """
        other_department_id = "dep-2"
        network_guest = Guest.objects.create(phone="+79990007773", first_name="Сеть")
        self._create_window_metric(
            guest=network_guest,
            orders_count=1,
            visits_count=1,
            sum_net="900.00",
            avg_check_net="900.00",
            rating_score="14.00",
            last_visit_at=date(2026, 3, 18),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 1, 10),
            guest=network_guest,
            department_id=other_department_id,
            orders_count=1,
            sum_net=Decimal("500.00"),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 18),
            guest=network_guest,
            department_id=self.department_id,
            orders_count=1,
            sum_net=Decimal("900.00"),
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "new_in_venue",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["segments"]["new_in_venue"], 1)
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], network_guest.phone)

    def test_new_in_venue_segment_excludes_repeat_visit_in_selected_venue(self):
        """
        Повторный визит в выбранное заведение не делает гостя новым, если
        первая покупка в этом заведении была раньше.
        """
        repeat_guest = Guest.objects.create(phone="+79990007774", first_name="Повтор")
        self._create_window_metric(
            guest=repeat_guest,
            orders_count=1,
            visits_count=1,
            sum_net="1100.00",
            avg_check_net="1100.00",
            rating_score="16.00",
            last_visit_at=date(2026, 3, 19),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 1, 15),
            guest=repeat_guest,
            department_id=self.department_id,
            orders_count=1,
            sum_net=Decimal("600.00"),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 19),
            guest=repeat_guest,
            department_id=self.department_id,
            orders_count=1,
            sum_net=Decimal("1100.00"),
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "new_in_venue",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["segments"]["new_in_venue"], 0)
        self.assertEqual(payload["selected_guests"]["total"], 0)

    def test_new_in_venue_segment_without_selected_venue_returns_empty_audience(self):
        """
        Если заведение не выбрано, сегмент «Новые» не падает и возвращает
        нулевой счётчик с пустой аудиторией.
        """
        new_guest = Guest.objects.create(phone="+79990007775", first_name="Без заведения")
        self._create_window_metric(
            guest=new_guest,
            orders_count=1,
            visits_count=1,
            sum_net="1000.00",
            avg_check_net="1000.00",
            rating_score="15.00",
            last_visit_at=date(2026, 3, 21),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 21),
            guest=new_guest,
            department_id=self.department_id,
            orders_count=1,
            sum_net=Decimal("1000.00"),
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "segment_code": "new_in_venue",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Для сегмента «Новые» выберите конкретное заведение.")

        payload = response.context["payload"]
        self.assertEqual(payload["segments"]["new_in_venue"], 0)
        self.assertEqual(payload["selected_guests"]["total"], 0)

    def test_new_in_venue_segment_respects_complex_filters(self):
        """
        Дополнительные условия должны сужать аудиторию сегмента «Новые».
        """
        matching_guest = Guest.objects.create(phone="+79990007776", first_name="Фильтр да")
        filtered_guest = Guest.objects.create(phone="+79990007777", first_name="Фильтр нет")
        self._create_window_metric(
            guest=matching_guest,
            orders_count=3,
            visits_count=2,
            sum_net="2100.00",
            avg_check_net="700.00",
            rating_score="27.00",
            last_visit_at=date(2026, 3, 22),
        )
        self._create_window_metric(
            guest=filtered_guest,
            orders_count=1,
            visits_count=1,
            sum_net="800.00",
            avg_check_net="800.00",
            rating_score="13.00",
            last_visit_at=date(2026, 3, 22),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 22),
            guest=matching_guest,
            department_id=self.department_id,
            orders_count=3,
            sum_net=Decimal("2100.00"),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 22),
            guest=filtered_guest,
            department_id=self.department_id,
            orders_count=1,
            sum_net=Decimal("800.00"),
        )

        response = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "new_in_venue",
                "cf_field": ["orders_count"],
                "cf_op": ["gte"],
                "cf_value": ["2"],
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)

        payload = response.context["payload"]
        self.assertEqual(payload["segments"]["new_in_venue"], 1)
        self.assertEqual(payload["selected_guests"]["total"], 1)
        self.assertEqual(payload["selected_guests"]["rows"][0]["phone"], matching_guest.phone)

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
            reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}),
        )

        snapshots = self.client.session.get("mailings_v2_workbench_snapshots", {})
        self.assertIn(str(mailing.id), snapshots)
        snapshot = snapshots[str(mailing.id)]
        self.assertEqual(snapshot["window_days"], "30")
        self.assertEqual(snapshot["venue_selection_mode"], "visited_once")
        self.assertEqual(snapshot["segment_code"], "active_30d")
        self.assertEqual(snapshot["focus_category_code"], "beer_ermolaev")
        self.assertEqual(snapshot["audience_channel_group"], "all")
        self.assertEqual(snapshot["selected_total"], 1)
        self.assertEqual(snapshot["delivery_available_guests"], 1)
        self.assertEqual(snapshot["delivery_planned_tasks"], 1)

        mailing.refresh_from_db()
        self.assertEqual(mailing.source_filter_snapshot["window_days"], "30")
        self.assertEqual(mailing.source_filter_snapshot["venue_selection_mode"], "visited_once")
        self.assertEqual(mailing.source_filter_snapshot["audience_channel_group"], "all")

        rows = list(MailingGuest.objects.filter(mailing=mailing).order_by("guest_id"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].guest_id, self.guest_1.id)
        self.assertEqual(list(mailing.bot_profiles.values_list("id", flat=True)), [self.bot_telegram.id])

    def test_create_mailing_draft_from_new_in_venue_selection_preserves_audience(self):
        """
        Черновик кампании из сегмента «Новые» должен использовать тот же отбор,
        что и экран гостей, включая дополнительные условия.
        """
        matching_guest = Guest.objects.create(phone="+79990008881", first_name="Новый купон")
        filtered_guest = Guest.objects.create(phone="+79990008882", first_name="Новый мимо")
        self._create_bot_binding(matching_guest, external_chat_id="tg-new-coupon-ok", is_primary=True)
        self._create_bot_binding(filtered_guest, external_chat_id="tg-new-coupon-skip", is_primary=True)
        self._create_window_metric(
            guest=matching_guest,
            orders_count=3,
            visits_count=2,
            sum_net="2400.00",
            avg_check_net="800.00",
            rating_score="28.00",
            last_visit_at=date(2026, 3, 21),
        )
        self._create_window_metric(
            guest=filtered_guest,
            orders_count=1,
            visits_count=1,
            sum_net="600.00",
            avg_check_net="600.00",
            rating_score="11.00",
            last_visit_at=date(2026, 3, 21),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 21),
            guest=matching_guest,
            department_id=self.department_id,
            orders_count=3,
            sum_net=Decimal("2400.00"),
        )
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=date(2026, 3, 21),
            guest=filtered_guest,
            department_id=self.department_id,
            orders_count=1,
            sum_net=Decimal("600.00"),
        )

        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "create_mailing_draft",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "new_in_venue",
                "cf_field": ["orders_count"],
                "cf_op": ["gte"],
                "cf_value": ["2"],
                "audience_limit_enabled": "0",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        mailing = Mailing.objects.get()
        self.assertEqual(
            response.url,
            reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}),
        )
        rows = list(MailingGuest.objects.filter(mailing=mailing).order_by("guest_id"))
        self.assertEqual([row.guest_id for row in rows], [matching_guest.id])

        snapshot = self.client.session["mailings_v2_workbench_snapshots"][str(mailing.id)]
        self.assertEqual(snapshot["segment_code"], "new_in_venue")
        self.assertEqual(snapshot["department_id"], self.department_id)
        self.assertEqual(snapshot["selected_total"], 1)
        self.assertEqual(snapshot["selected_rows_count"], 1)
        self.assertEqual(snapshot["delivery_available_guests"], 1)
        self.assertEqual(snapshot["complex_filters"], [{"field": "orders_count", "operator": "gte", "value": "2"}])

        mailing.refresh_from_db()
        self.assertEqual(mailing.source_filter_snapshot["segment_code"], "new_in_venue")
        self.assertEqual(mailing.source_filter_snapshot["complex_filters"], snapshot["complex_filters"])

        audience_response = self.client.get(
            reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(audience_response.status_code, 200)
        self.assertContains(audience_response, "Аудитория собрана из экрана «Гости»")
        self.assertContains(audience_response, "new_in_venue")
        self.assertContains(audience_response, "segment_code=new_in_venue")

    def test_create_mailing_draft_uses_selected_mailing_settings(self):
        """
        Черновик должен учитывать выбранные на workbench параметры рассылки.
        """
        custom_template = MessageTemplate.objects.create(
            name="Ручной шаблон workbench",
            message_text="Спецтекст для {{ first_name }}",
            created_by="tests",
            is_active=True,
        )

        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "create_mailing_draft",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "active_30d",
                "mailing_template_id": str(custom_template.id),
                "mailing_bot_profile_ids_present": "1",
                "mailing_bot_profile_ids": [str(self.bot_telegram.id)],
                "mailing_target_mode": Mailing.TargetMode.ALL_BOTS,
                "mailing_queue_priority": Mailing.QueuePriority.HIGH,
                "mailing_send_window_begin": "10:00",
                "mailing_send_window_end": "18:30",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        mailing = Mailing.objects.get()
        self.assertEqual(mailing.template_id, custom_template.id)
        self.assertEqual(mailing.target_mode, Mailing.TargetMode.ALL_BOTS)
        self.assertEqual(mailing.queue_priority, Mailing.QueuePriority.HIGH)
        self.assertEqual(mailing.send_window_begin, time(10, 0))
        self.assertEqual(mailing.send_window_end, time(18, 30))
        self.assertEqual(list(mailing.bot_profiles.values_list("id", flat=True)), [self.bot_telegram.id])

        row = MailingGuest.objects.get(mailing=mailing)
        self.assertEqual(row.text_mailing_list, "Спецтекст для Анна")

        snapshot = self.client.session["mailings_v2_workbench_snapshots"][str(mailing.id)]
        self.assertEqual(snapshot["mailing_template_id"], custom_template.id)
        self.assertEqual(snapshot["mailing_template_name"], custom_template.name)
        self.assertEqual(snapshot["mailing_target_mode"], Mailing.TargetMode.ALL_BOTS)
        self.assertEqual(snapshot["mailing_queue_priority"], Mailing.QueuePriority.HIGH)
        self.assertEqual(snapshot["mailing_send_window_begin"], "10:00")
        self.assertEqual(snapshot["mailing_send_window_end"], "18:30")
        self.assertEqual(snapshot["mailing_bot_profile_ids"], [self.bot_telegram.id])

        mailing.refresh_from_db()
        self.assertEqual(mailing.source_filter_snapshot["mailing_template_id"], custom_template.id)
        self.assertEqual(mailing.source_filter_snapshot["mailing_template_name"], custom_template.name)
        self.assertEqual(mailing.source_filter_snapshot["mailing_target_mode"], Mailing.TargetMode.ALL_BOTS)
        self.assertEqual(mailing.source_filter_snapshot["mailing_queue_priority"], Mailing.QueuePriority.HIGH)

    def test_create_mailing_draft_skips_guests_without_delivery(self):
        """
        В черновик попадают только гости с доступной доставкой через активные боты.
        """
        guest_without_delivery = Guest.objects.create(phone="+79990003333", first_name="Без доставки")
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=guest_without_delivery,
            department_id=self.department_id,
            window_days=30,
            orders_count=3,
            visits_count=2,
            avg_check_net=Decimal("400.00"),
            sum_net=Decimal("1200.00"),
            bonus_in_sum=Decimal("0.00"),
            bonus_out_sum=Decimal("0.00"),
            rating_score=Decimal("10.00"),
            last_visit_at=self.as_of_date,
        )

        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "create_mailing_draft",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "active_30d",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        mailing = Mailing.objects.get()
        rows = list(MailingGuest.objects.filter(mailing=mailing).order_by("guest_id"))
        self.assertEqual([row.guest_id for row in rows], [self.guest_1.id])

        snapshot = self.client.session["mailings_v2_workbench_snapshots"][str(mailing.id)]
        self.assertEqual(snapshot["selected_total"], 2)
        self.assertEqual(snapshot["delivery_available_guests"], 1)
        self.assertEqual(snapshot["delivery_blocked_without_bot_binding"], 1)

    def test_create_mailing_draft_includes_legacy_telegram_guest(self):
        """
        Черновик обычной рассылки может быть создан для legacy-гостя с Telegram-каналом.
        """
        self._legacy_telegram_channel(self.guest_2, external_id="legacy-tg-guest-2")

        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "create_mailing_draft",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "cooling_30_60d",
                "audience_channel_group": "legacy_no_new_bot",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        mailing = Mailing.objects.get()
        rows = list(MailingGuest.objects.filter(mailing=mailing).order_by("guest_id"))
        self.assertEqual([row.guest_id for row in rows], [self.guest_2.id])

        snapshot = self.client.session["mailings_v2_workbench_snapshots"][str(mailing.id)]
        self.assertEqual(snapshot["audience_channel_group"], "legacy_no_new_bot")
        self.assertEqual(snapshot["delivery_available_guests"], 1)
        self.assertEqual(snapshot["delivery_legacy_telegram_guests"], 1)

        mailing.refresh_from_db()
        self.assertEqual(mailing.source_filter_snapshot["audience_channel_group"], "legacy_no_new_bot")
        self.assertEqual(mailing.source_filter_snapshot["delivery_legacy_telegram_guests"], 1)

    def test_create_mailing_draft_without_audience_limit_uses_full_selection(self):
        """
        При отключенном лимите черновик должен создаваться по всей выборке.
        """
        department_id = "bulk-dep-full"
        self._create_bulk_window_metrics(department_id=department_id, total=205)

        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "create_mailing_draft",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": department_id,
                "audience_limit_enabled": "0",
                "audience_limit": "200",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        mailing = Mailing.objects.get()
        self.assertEqual(MailingGuest.objects.filter(mailing=mailing).count(), 205)

        snapshot = self.client.session["mailings_v2_workbench_snapshots"][str(mailing.id)]
        self.assertFalse(snapshot["audience_limit_enabled"])
        self.assertEqual(snapshot["audience_limit"], 200)
        self.assertEqual(snapshot["selected_total"], 205)
        self.assertEqual(snapshot["selected_rows_count"], 205)

    def test_create_mailing_draft_with_audience_limit_uses_limit_value(self):
        """
        При включенном лимите черновик создается только на указанное число гостей.
        """
        department_id = "bulk-dep-limited"
        self._create_bulk_window_metrics(department_id=department_id, total=7)

        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "create_mailing_draft",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": department_id,
                "audience_limit_enabled": "1",
                "audience_limit": "3",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        mailing = Mailing.objects.get()
        self.assertEqual(MailingGuest.objects.filter(mailing=mailing).count(), 3)

        snapshot = self.client.session["mailings_v2_workbench_snapshots"][str(mailing.id)]
        self.assertTrue(snapshot["audience_limit_enabled"])
        self.assertEqual(snapshot["audience_limit"], 3)
        self.assertEqual(snapshot["selected_total"], 7)
        self.assertEqual(snapshot["selected_rows_count"], 3)

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
                "venue_selection_mode": "favorite",
                "segment_code": "cooling_30_60d",
                "focus_category_code": "wine",
                "audience_channel_group": "legacy_no_new_bot",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("guests_workbench")))

        preset = GuestWorkbenchFilterPreset.objects.get(name="Остывшие + Вино")
        self.assertEqual(preset.window_days, 30)
        self.assertEqual(preset.department_id, self.department_id)
        self.assertEqual(preset.venue_selection_mode, "favorite")
        self.assertEqual(preset.segment_code, "cooling_30_60d")
        self.assertEqual(preset.focus_category_code, "wine")
        self.assertEqual(preset.audience_channel_group, "legacy_no_new_bot")

    def test_save_filter_preset_accepts_new_in_venue_segment(self):
        """
        Пресеты формы должны сохранять новый сегмент как валидный код.
        """
        response = self.client.post(
            reverse("guests_workbench_actions"),
            {
                "action": "save_filter_preset",
                "preset_name": "Новые в заведении",
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "venue_selection_mode": "visited_once",
                "segment_code": "new_in_venue",
                "audience_channel_group": "all",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        preset = GuestWorkbenchFilterPreset.objects.get(name="Новые в заведении")
        self.assertEqual(preset.department_id, self.department_id)
        self.assertEqual(preset.segment_code, "new_in_venue")

    def _create_bulk_window_metrics(self, *, department_id: str, total: int) -> None:
        """
        Создает набор гостей для проверки массового действия workbench.
        """
        for idx in range(total):
            guest = Guest.objects.create(
                phone=f"+7999888{idx:04d}",
                first_name=f"Гость {idx}",
            )
            self._create_bot_binding(guest, external_chat_id=f"tg-bulk-{idx}", is_primary=True)
            GuestRestaurantWindowMetrics.objects.create(
                as_of_date=self.as_of_date,
                guest=guest,
                department_id=department_id,
                window_days=30,
                orders_count=1,
                visits_count=1,
                avg_check_net=Decimal("100.00"),
                sum_net=Decimal("100.00"),
                bonus_in_sum=Decimal("0.00"),
                bonus_out_sum=Decimal("0.00"),
                rating_score=Decimal("1.00"),
                last_visit_at=self.as_of_date,
            )

    def _create_bot_binding(
        self,
        guest: Guest,
        *,
        external_chat_id: str,
        is_primary: bool,
        is_opt_in: bool = True,
    ) -> GuestBotBinding:
        return GuestBotBinding.objects.create(
            guest=guest,
            bot=self.bot_telegram,
            external_chat_id=external_chat_id,
            is_primary=is_primary,
            is_active=True,
            is_opt_in=is_opt_in,
            is_stop_sending=False,
        )

    @staticmethod
    def _legacy_telegram_channel(guest: Guest, *, external_id: str) -> VtelemaxRecipientChannel:
        return VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            phone_e164=guest.phone,
            external_id=external_id,
            notifications_allowed=True,
            is_registered=True,
            guest=guest,
        )

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

    def test_bot_active_no_visits_180d_segment_leaves_after_first_visit(self):
        """
        Гость из валидного bot-канала попадает в сегмент без визитов 180д,
        и автоматически выходит из него после первого зафиксированного визита.
        """
        bot_guest = Guest.objects.create(phone="+79990006666", first_name="Бот")
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            phone_e164="+79990006666",
            external_id="tg-bot-segment",
            notifications_allowed=True,
            is_registered=True,
            guest=bot_guest,
        )

        response_before = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "bot_active_no_visits_180d",
            },
            secure=True,
        )
        self.assertEqual(response_before.status_code, 200)
        payload_before = response_before.context["payload"]
        self.assertEqual(payload_before["segments"]["bot_active_no_visits_180d"], 1)
        self.assertEqual(payload_before["selected_guests"]["total"], 1)
        self.assertEqual(payload_before["selected_guests"]["rows"][0]["phone"], bot_guest.phone)
        self.assertEqual(payload_before["selected_guests"]["rows"][0]["orders_count"], 0)

        OrderFact.objects.create(
            guest=bot_guest,
            business_date=date(2026, 3, 22),
            department_id=self.department_id,
            department_name="Сами Сусами",
            order_number=777001,
            uniq_order_id="uniq-777001",
            net_sum=Decimal("850.00"),
            gross_sum=Decimal("850.00"),
        )

        response_after = self.client.get(
            reverse("guests_workbench"),
            {
                "as_of_date": self.as_of_date.isoformat(),
                "window_days": 30,
                "department_id": self.department_id,
                "segment_code": "bot_active_no_visits_180d",
            },
            secure=True,
        )
        self.assertEqual(response_after.status_code, 200)
        payload_after = response_after.context["payload"]
        self.assertEqual(payload_after["segments"]["bot_active_no_visits_180d"], 0)
        self.assertEqual(payload_after["selected_guests"]["total"], 0)

    def _create_window_metric(
        self,
        *,
        guest: Guest,
        department_id: str | None = None,
        window_days: int = 30,
        orders_count: int,
        visits_count: int,
        sum_net: str,
        avg_check_net: str,
        rating_score: str,
        last_visit_at: date | None,
    ) -> None:
        """
        Создаёт строку общей оконной витрины для проверок workbench.
        """
        GuestRestaurantWindowMetrics.objects.create(
            as_of_date=self.as_of_date,
            guest=guest,
            department_id=department_id or self.department_id,
            window_days=window_days,
            orders_count=orders_count,
            visits_count=visits_count,
            avg_check_net=Decimal(avg_check_net),
            sum_net=Decimal(sum_net),
            bonus_in_sum=Decimal("0.00"),
            bonus_out_sum=Decimal("0.00"),
            rating_score=Decimal(rating_score),
            last_visit_at=last_visit_at,
        )

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
