from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    Mailing,
    MailingGuest,
    MessageTemplate,
    OrderFact,
)


class SimpleMailingReportsViewTests(TestCase):
    """Проверки маршрутов, интерфейсного контракта, поиска и пагинации."""

    def setUp(self):
        super().setUp()
        self.start_date = date(2026, 6, 12)
        self.now = timezone.make_aware(datetime(2026, 6, 12, 10, 0))
        self.template = MessageTemplate.objects.create(
            name="Шаблон отчёта",
            description="",
            message_text="Тестовое сообщение",
            created_by="tester",
            is_active=True,
        )
        self.mailing = self._create_mailing(
            name="Летнее меню",
            scheduled_date=self.start_date,
            coupon_series=None,
        )

    def _create_mailing(
        self,
        *,
        name: str,
        scheduled_date: date,
        coupon_series: str | None,
    ) -> Mailing:
        scheduled_begin = timezone.make_aware(
            datetime.combine(scheduled_date, time(10, 0))
        )
        return Mailing.objects.create(
            name=name,
            template=self.template,
            scheduled_date=scheduled_date,
            scheduled_time_begin=scheduled_begin,
            scheduled_time_end=scheduled_begin + timedelta(hours=4),
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=time(10, 0),
            send_window_end=time(14, 0),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.NORMAL,
            coupon_series=coupon_series,
        )

    def _create_sent_order(self, *, order_number: int = 4242) -> OrderFact:
        guest = Guest.objects.create(
            phone="+79990000001",
            first_name="Тестовый гость",
            created_at=self.now,
            updated_at=self.now,
        )
        mailing_guest = MailingGuest.objects.create(
            mailing=self.mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Тестовое сообщение",
            scheduled_datetime=self.mailing.scheduled_time_begin,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            priority=DispatchTask.Priority.BULK,
            status=DispatchTask.Status.DONE,
            mailing_guest=mailing_guest,
            guest=None,
            message_text="Тестовое сообщение",
        )
        return OrderFact.objects.create(
            guest=guest,
            business_date=self.start_date,
            department_id="DEP-1",
            department_name="Заведение 1",
            order_number=order_number,
            uniq_order_id=f"order-{order_number}",
            gross_sum="550.00",
            net_sum="500.00",
            discount_sum="50.00",
            bonus_sum="0.00",
            items_count=1,
            categories_count=1,
        )

    def test_routes_are_named_and_reports_hub_contains_simple_report_link(self):
        self.assertEqual(reverse("reports_simple_mailings"), "/reports/simple-mailings/")
        self.assertEqual(
            reverse("reports_simple_mailings_search"),
            "/reports/simple-mailings/search/",
        )
        self.assertEqual(
            reverse("reports_simple_mailings_orders", kwargs={"mailing_id": self.mailing.id}),
            f"/reports/simple-mailings/{self.mailing.id}/orders/",
        )

        response = self.client.get(reverse("reports"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("reports_simple_mailings"))
        self.assertContains(response, "Простые рассылки")

    def test_initial_page_uses_seven_days_and_exact_compact_ui_contract(self):
        self._create_sent_order()

        response = self.client.get(reverse("reports_simple_mailings"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_mailing"].id, self.mailing.id)
        self.assertEqual(response.context["selected_period_days"], 7)
        self.assertContains(response, "Получатели")
        self.assertContains(response, "Отправлено")
        self.assertContains(response, "С заказом за 7 дней")
        self.assertContains(response, "Сумма за 7 дней")
        self.assertContains(response, "Главный вывод")
        self.assertContains(response, "Основные метрики")
        self.assertContains(response, "Заказы и гости за 7 дней")
        self.assertContains(response, 'role="combobox"')
        self.assertContains(response, 'aria-label="Раскрыть список рассылок"')
        self.assertContains(response, "Выберите рассылку")
        self.assertNotContains(response, "D0")
        self.assertNotContains(response, "D+")
        self.assertContains(
            response,
            "Заказ в первый день мог быть совершён как до, так и после сообщения.",
        )
        self.assertContains(response, "Отчёт не доказывает влияние сообщения на заказ.")

    def test_main_page_accepts_only_fixed_periods(self):
        fourteen_days = self.client.get(
            reverse("reports_simple_mailings"),
            {"mailing_id": self.mailing.id, "period_days": 14},
            secure=True,
        )
        arbitrary_period = self.client.get(
            reverse("reports_simple_mailings"),
            {"mailing_id": self.mailing.id, "period_days": 9},
            secure=True,
        )

        self.assertEqual(fourteen_days.status_code, 200)
        self.assertEqual(fourteen_days.context["selected_period_days"], 14)
        self.assertEqual(
            fourteen_days.context["simple_mailing_report"]["period"]["end_date"],
            self.start_date + timedelta(days=13),
        )
        self.assertEqual(arbitrary_period.status_code, 200)
        self.assertEqual(arbitrary_period.context["selected_period_days"], 7)

    def test_all_six_lower_sections_are_collapsible_and_details_are_closed(self):
        response = self.client.get(
            reverse("reports_simple_mailings"),
            {"mailing_id": self.mailing.id},
            secure=True,
        )
        content = response.content.decode("utf-8")

        self.assertEqual(content.count('<details class="simple-report-section"'), 5)
        self.assertEqual(content.count("<details\n      class=\"simple-report-section\""), 1)
        self.assertContains(response, "Заказы после рассылки по дням")
        self.assertContains(response, "Итоги по периодам 7, 14 и 30 дней")
        self.assertContains(response, "Отправка по каналам")
        self.assertContains(response, "Заказы по заведениям")
        self.assertContains(response, "Анализ покупок")
        self.assertContains(response, "Заказы гостей за выбранный период")
        details_marker = 'id="simpleMailingOrdersSection"'
        details_start = content.rfind("<details", 0, content.index(details_marker))
        details_tag_end = content.index(">", content.index(details_marker))
        self.assertNotIn(" open", content[details_start:details_tag_end])
        self.assertNotIn("Заказ: 4242", content)

    def test_status_and_audience_buttons_link_to_existing_screens(self):
        response = self.client.get(
            reverse("reports_simple_mailings"),
            {"mailing_id": self.mailing.id},
            secure=True,
        )

        self.assertContains(
            response,
            reverse("mailings_v2_campaigns_status", kwargs={"pk": self.mailing.id}),
        )
        self.assertContains(
            response,
            reverse("mailings_v2_campaigns_audience", kwargs={"pk": self.mailing.id}),
        )
        self.assertContains(response, "Карточка рассылки")
        self.assertContains(response, "Получатели")

    def test_initial_options_are_limited_to_ten_latest_simple_mailings(self):
        for index in range(12):
            self._create_mailing(
                name=f"Рассылка {index}",
                scheduled_date=self.start_date + timedelta(days=index + 1),
                coupon_series="",
            )
        coupon = self._create_mailing(
            name="Самая новая купонная",
            scheduled_date=self.start_date + timedelta(days=50),
            coupon_series="PROMO",
        )

        response = self.client.get(reverse("reports_simple_mailings"), secure=True)

        self.assertEqual(len(response.context["mailing_options"]), 10)
        option_ids = {item["id"] for item in response.context["mailing_options"]}
        self.assertNotIn(coupon.id, option_ids)
        self.assertEqual(
            response.context["selected_mailing"].scheduled_date,
            self.start_date + timedelta(days=12),
        )

    def test_search_is_limited_and_numeric_query_uses_exact_id(self):
        for index in range(12):
            self._create_mailing(
                name=f"Еженедельное меню {index}",
                scheduled_date=self.start_date + timedelta(days=index + 1),
                coupon_series=None,
            )
        coupon = self._create_mailing(
            name="Еженедельное меню с купоном",
            scheduled_date=self.start_date + timedelta(days=30),
            coupon_series="PROMO",
        )

        text_response = self.client.get(
            reverse("reports_simple_mailings_search"),
            {"q": "Еженедельное меню"},
            secure=True,
        )
        numeric_response = self.client.get(
            reverse("reports_simple_mailings_search"),
            {"q": str(self.mailing.id)},
            secure=True,
        )
        partial_numeric_response = self.client.get(
            reverse("reports_simple_mailings_search"),
            {"q": str(self.mailing.id)[-1:] + "999999999999999999999999"},
            secure=True,
        )

        self.assertEqual(text_response.status_code, 200)
        self.assertLessEqual(len(text_response.json()["results"]), 10)
        self.assertNotIn(coupon.id, {item["id"] for item in text_response.json()["results"]})
        self.assertEqual([item["id"] for item in numeric_response.json()["results"]], [self.mailing.id])
        self.assertEqual(partial_numeric_response.json()["results"], [])
        result = numeric_response.json()["results"][0]
        self.assertEqual(
            result["status_url"],
            reverse("mailings_v2_campaigns_status", kwargs={"pk": self.mailing.id}),
        )
        self.assertEqual(text_response["Cache-Control"], "no-store")

    def test_search_empty_state_and_non_get_method_are_safe(self):
        empty_response = self.client.get(
            reverse("reports_simple_mailings_search"),
            {"q": "несуществующее название"},
            secure=True,
        )
        post_response = self.client.post(
            reverse("reports_simple_mailings_search"),
            secure=True,
        )

        self.assertEqual(empty_response.status_code, 200)
        self.assertEqual(empty_response.json(), {"results": []})
        self.assertEqual(post_response.status_code, 405)

    def test_order_endpoint_returns_only_agreed_fields_and_no_store_header(self):
        self._create_sent_order(order_number=4242)

        response = self.client.get(
            reverse("reports_simple_mailings_orders", kwargs={"mailing_id": self.mailing.id}),
            {"period_days": 7, "page": 1, "page_size": 50},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["page_size"], 50)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(
            set(payload["results"][0]),
            {
                "business_date",
                "order_number",
                "guest_id",
                "net_sum",
                "venue_name",
                "calendar_delay_days",
                "calendar_delay_label",
                "channel",
            },
        )
        self.assertEqual(payload["results"][0]["order_number"], 4242)
        self.assertEqual(payload["results"][0]["channel"], "Telegram")
        self.assertNotIn("gross_sum", response.content.decode("utf-8"))
        self.assertNotIn("discount_sum", response.content.decode("utf-8"))
        self.assertNotIn("phone", response.content.decode("utf-8"))

    def test_order_endpoint_validates_period_caps_page_and_does_not_repeat_last_page(self):
        self._create_sent_order()
        url = reverse(
            "reports_simple_mailings_orders",
            kwargs={"mailing_id": self.mailing.id},
        )

        invalid_period = self.client.get(url, {"period_days": 8}, secure=True)
        capped = self.client.get(
            url,
            {"period_days": 7, "page": -3, "page_size": 1000},
            secure=True,
        )
        ended = self.client.get(
            url,
            {"period_days": 7, "page": 5, "page_size": 1},
            secure=True,
        )

        self.assertEqual(invalid_period.status_code, 400)
        self.assertEqual(capped.status_code, 200)
        self.assertEqual(capped.json()["page"], 1)
        self.assertEqual(capped.json()["page_size"], 100)
        self.assertEqual(ended.status_code, 200)
        self.assertEqual(ended.json()["results"], [])
        self.assertFalse(ended.json()["has_next"])

    def test_coupon_mailing_is_hidden_by_page_search_and_orders_endpoints(self):
        coupon = self._create_mailing(
            name="Скрытая купонная",
            scheduled_date=self.start_date,
            coupon_series="PROMO",
        )

        page_response = self.client.get(
            reverse("reports_simple_mailings"),
            {"mailing_id": coupon.id},
            secure=True,
        )
        search_response = self.client.get(
            reverse("reports_simple_mailings_search"),
            {"q": str(coupon.id)},
            secure=True,
        )
        orders_response = self.client.get(
            reverse("reports_simple_mailings_orders", kwargs={"mailing_id": coupon.id}),
            {"period_days": 7},
            secure=True,
        )

        self.assertEqual(page_response.status_code, 404)
        self.assertEqual(search_response.json()["results"], [])
        self.assertEqual(orders_response.status_code, 404)

    def test_mailing_name_is_escaped_in_html_and_json_remains_valid(self):
        dangerous = self._create_mailing(
            name='<script>alert("x")</script>',
            scheduled_date=self.start_date + timedelta(days=1),
            coupon_series=None,
        )

        page_response = self.client.get(
            reverse("reports_simple_mailings"),
            {"mailing_id": dangerous.id},
            secure=True,
        )
        search_response = self.client.get(
            reverse("reports_simple_mailings_search"),
            {"q": str(dangerous.id)},
            secure=True,
        )

        content = page_response.content.decode("utf-8")
        self.assertNotIn('<script>alert("x")</script>', content)
        self.assertIn("&lt;script&gt;", content)
        self.assertEqual(search_response.json()["results"][0]["name"], dangerous.name)

    def test_order_endpoint_rejects_post(self):
        response = self.client.post(
            reverse("reports_simple_mailings_orders", kwargs={"mailing_id": self.mailing.id}),
            secure=True,
        )

        self.assertEqual(response.status_code, 405)
