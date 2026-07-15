from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    Mailing,
    MailingGuest,
    MessageTemplate,
    OlapCategoryDict,
    OlapCheckSyncJournal,
    OlapNomenclatureDict,
    OlapSalesRawLine,
    OrderFact,
    TerminalDepartmentMap,
)
from guests.services.simple_mailing_reporting import (
    DEFAULT_ORDER_PAGE_SIZE,
    MAX_ORDER_PAGE_SIZE,
    SimpleMailingReportError,
    build_simple_mailing_order_details_page,
    build_simple_mailing_report_snapshot,
    normalize_simple_mailing_department_id,
    normalize_order_page_number,
    normalize_order_page_size,
    normalize_simple_mailing_period_days,
    search_simple_mailings,
)


class SimpleMailingReportingServiceTests(TestCase):
    """Проверки расчётного контракта отчёта по простым рассылкам."""

    def setUp(self):
        super().setUp()
        self.start_date = date(2026, 6, 12)
        self.now = timezone.make_aware(datetime(2026, 6, 12, 10, 0))
        self.template = MessageTemplate.objects.create(
            name="Шаблон простой рассылки",
            description="",
            message_text="Тестовое сообщение",
            created_by="tester",
            is_active=True,
        )
        self.mailing = self._create_mailing(name="Летнее меню", coupon_series=None)
        self._guest_sequence = 0
        self._order_sequence = 0
        self._raw_sequence = 0

    def _create_mailing(
        self,
        *,
        name: str,
        coupon_series: str | None,
        scheduled_date: date | None = None,
        scheduled_begin=None,
    ) -> Mailing:
        report_date = scheduled_date or self.start_date
        begin = scheduled_begin or timezone.make_aware(
            datetime.combine(report_date, time(10, 0))
        )
        end = begin + timedelta(hours=4)
        return Mailing.objects.create(
            name=name,
            template=self.template,
            scheduled_date=report_date,
            scheduled_time_begin=begin,
            scheduled_time_end=end,
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=time(10, 0),
            send_window_end=time(14, 0),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.NORMAL,
            coupon_series=coupon_series,
        )

    def _create_guest(self) -> Guest:
        self._guest_sequence += 1
        return Guest.objects.create(
            phone=f"+7999000{self._guest_sequence:04d}",
            first_name=f"Гость {self._guest_sequence}",
            created_at=self.now,
            updated_at=self.now,
        )

    def _add_audience(
        self,
        guest: Guest,
        *,
        mailing: Mailing | None = None,
        status: str = MailingGuest.Status.PLANNED,
        delivery_status: str | None = None,
    ) -> MailingGuest:
        selected_mailing = mailing or self.mailing
        return MailingGuest.objects.create(
            mailing=selected_mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Тестовое сообщение",
            scheduled_datetime=selected_mailing.scheduled_time_begin,
            status=status,
            delivery_status=delivery_status,
            created_at=self.now,
        )

    def _add_task(
        self,
        mailing_guest: MailingGuest,
        *,
        provider: str = BotProfile.ProviderType.TELEGRAM,
        status: str = DispatchTask.Status.DONE,
        source_type: str = DispatchTask.SourceType.MAILING,
        guest: Guest | None = None,
        finished_at=None,
    ) -> DispatchTask:
        return DispatchTask.objects.create(
            source_type=source_type,
            provider_type=provider,
            priority=DispatchTask.Priority.BULK,
            status=status,
            mailing_guest=mailing_guest,
            guest=guest,
            message_text="Тестовое сообщение",
            finished_at=finished_at,
        )

    def _add_order(
        self,
        guest: Guest | None,
        *,
        business_date: date,
        net_sum: str,
        department_id: str = "DEP-1",
        department_name: str = "Заведение 1",
        order_number: int | None = None,
        uniq_order_id: str | None = None,
    ) -> OrderFact:
        self._order_sequence += 1
        sequence = self._order_sequence
        return OrderFact.objects.create(
            guest=guest,
            business_date=business_date,
            department_id=department_id,
            department_name=department_name,
            order_number=order_number or 1000 + sequence,
            uniq_order_id=uniq_order_id if uniq_order_id is not None else f"order-{sequence}",
            gross_sum=Decimal(net_sum) + Decimal("50"),
            net_sum=net_sum,
            discount_sum="50.00",
            bonus_sum="0.00",
            items_count=1,
            categories_count=1,
            first_seen_at=self.now + timedelta(days=20),
        )

    def _add_raw_line(
        self,
        order: OrderFact,
        *,
        dish_code: str,
        dish_name: str,
        amount: str = "1",
        before: str = "100",
        after: str | None = "80",
        department_id: str | None = None,
        uniq_order_id: str | None = None,
        order_number: int | None = None,
    ) -> OlapSalesRawLine:
        self._raw_sequence += 1
        sequence = self._raw_sequence
        journal = OlapCheckSyncJournal.objects.create(
            idempotency_key=f"simple-report-journal-{sequence}",
            status=OlapCheckSyncJournal.Status.LOADED,
            guest=order.guest,
            order_number=order.order_number,
            order_external_id=order.uniq_order_id,
            business_date=order.business_date,
            department_id=order.department_id,
            loaded_at=self.now,
        )
        return OlapSalesRawLine.objects.create(
            row_fingerprint=f"simple-report-raw-{sequence}",
            sync_journal=journal,
            guest=order.guest,
            business_date=order.business_date,
            department_id=order.department_id if department_id is None else department_id,
            department_name=order.department_name,
            order_number=order.order_number if order_number is None else order_number,
            uniq_order_id=order.uniq_order_id if uniq_order_id is None else uniq_order_id,
            dish_code=dish_code,
            dish_name=dish_name,
            dish_category_name="Категория из строки",
            dish_group_name="Группа из строки",
            dish_amount=amount,
            dish_sum_before_discount=before,
            dish_sum_after_discount=after,
            discount_sum="0.00",
            bonus_sum="0.00",
        )

    def _make_sent_guest(
        self,
        *,
        provider: str = BotProfile.ProviderType.TELEGRAM,
    ) -> tuple[Guest, MailingGuest]:
        guest = self._create_guest()
        mailing_guest = self._add_audience(guest)
        self._add_task(mailing_guest, provider=provider, guest=None)
        return guest, mailing_guest

    def test_simple_scope_and_period_normalization_follow_fixed_contract(self):
        empty_series = self._create_mailing(name="Пустая серия", coupon_series="")
        coupon = self._create_mailing(name="Купонная", coupon_series="PROMO")
        spaces = self._create_mailing(name="Пробельная серия", coupon_series="   ")

        found_ids = set(search_simple_mailings().values_list("id", flat=True))

        self.assertIn(self.mailing.id, found_ids)
        self.assertIn(empty_series.id, found_ids)
        self.assertNotIn(coupon.id, found_ids)
        self.assertNotIn(spaces.id, found_ids)
        self.assertEqual(normalize_simple_mailing_period_days(None), 7)
        self.assertEqual(normalize_simple_mailing_period_days("14"), 14)
        self.assertEqual(normalize_simple_mailing_period_days(30), 30)
        self.assertEqual(normalize_simple_mailing_period_days("8"), 7)
        with self.assertRaises(SimpleMailingReportError):
            build_simple_mailing_report_snapshot(mailing=coupon, period_days=7)
        with self.assertRaises(SimpleMailingReportError):
            build_simple_mailing_report_snapshot(mailing=spaces, period_days=7)

    def test_success_depends_only_on_authoritative_done_task_and_is_deduplicated(self):
        successful_guest = self._create_guest()
        successful_row = self._add_audience(successful_guest)
        self._add_task(successful_row, provider=BotProfile.ProviderType.TELEGRAM, guest=None)
        self._add_task(successful_row, provider=BotProfile.ProviderType.VK, guest=None)
        self._add_task(successful_row, status=DispatchTask.Status.FAILED)

        legacy_guest = self._create_guest()
        self._add_audience(
            legacy_guest,
            status=MailingGuest.Status.DONE,
            delivery_status="done",
        )

        unfinished_guest = self._create_guest()
        unfinished_row = self._add_audience(unfinished_guest)
        for status in (
            DispatchTask.Status.PENDING,
            DispatchTask.Status.QUEUED,
            DispatchTask.Status.IN_PROGRESS,
            DispatchTask.Status.FAILED,
            DispatchTask.Status.CANCELED,
        ):
            self._add_task(unfinished_row, status=status)

        wrong_source_guest = self._create_guest()
        wrong_source_row = self._add_audience(wrong_source_guest)
        self._add_task(
            wrong_source_row,
            status=DispatchTask.Status.DONE,
            source_type=DispatchTask.SourceType.SYSTEM,
        )

        snapshot = build_simple_mailing_report_snapshot(mailing=self.mailing).to_dict()

        self.assertEqual(snapshot["audience"]["recipients_total"], 4)
        self.assertEqual(snapshot["audience"]["sent_total"], 1)
        self.assertEqual(snapshot["audience"]["not_sent_total"], 3)
        self.assertEqual(snapshot["audience"]["send_share_percent"], Decimal("25.0"))
        channel_rows = {row["provider_type"]: row for row in snapshot["channel_rows"]}
        self.assertEqual(channel_rows[BotProfile.ProviderType.TELEGRAM]["recipients_count"], 2)
        self.assertEqual(channel_rows[BotProfile.ProviderType.TELEGRAM]["sent_count"], 1)
        self.assertEqual(channel_rows[BotProfile.ProviderType.VK]["sent_count"], 1)
        self.assertEqual(channel_rows["total"]["sent_count"], 1)

    def test_orders_use_guest_bridge_calendar_boundaries_net_sum_and_zero_days(self):
        first_guest, first_row = self._make_sent_guest()
        second_guest, _ = self._make_sent_guest(provider=BotProfile.ProviderType.MAX)
        self._add_task(
            first_row,
            provider=BotProfile.ProviderType.TELEGRAM,
            finished_at=self.now + timedelta(days=2),
        )

        self._add_order(
            first_guest,
            business_date=self.start_date - timedelta(days=1),
            net_sum="999.00",
        )
        self._add_order(first_guest, business_date=self.start_date, net_sum="100.00")
        self._add_order(first_guest, business_date=self.start_date, net_sum="200.00")
        self._add_order(
            second_guest,
            business_date=self.start_date + timedelta(days=6),
            net_sum="300.00",
        )
        self._add_order(
            first_guest,
            business_date=self.start_date + timedelta(days=7),
            net_sum="400.00",
        )
        self._add_order(
            None,
            business_date=self.start_date + timedelta(days=1),
            net_sum="777.00",
        )

        snapshot = build_simple_mailing_report_snapshot(
            mailing=self.mailing,
            period_days=7,
        ).to_dict()

        self.assertEqual(snapshot["period"]["start_date"], self.start_date)
        self.assertEqual(snapshot["period"]["end_date"], self.start_date + timedelta(days=6))
        self.assertEqual(snapshot["orders"]["guests_count"], 2)
        self.assertEqual(snapshot["orders"]["orders_count"], 3)
        self.assertEqual(snapshot["orders"]["net_sum"], Decimal("600"))
        self.assertEqual(snapshot["orders"]["average_check"], Decimal("200.00"))
        self.assertEqual(snapshot["orders"]["average_first_order_days"], Decimal("3.0"))
        self.assertEqual(len(snapshot["daily_rows"]), 7)
        self.assertEqual(snapshot["daily_rows"][0]["orders_count"], 2)
        self.assertEqual(snapshot["daily_rows"][1]["orders_count"], 0)
        self.assertEqual(snapshot["daily_rows"][-1]["orders_count"], 1)
        summaries = {row["period_days"]: row for row in snapshot["period_summary_rows"]}
        self.assertEqual(summaries[7]["end_date"], date(2026, 6, 18))
        self.assertEqual(summaries[7]["orders_count"], 3)
        self.assertEqual(summaries[14]["orders_count"], 4)
        self.assertEqual(summaries[30]["orders_count"], 4)

    def test_department_filter_limits_all_order_metrics_and_keeps_delivery_totals(self):
        first_guest, _ = self._make_sent_guest()
        second_guest, _ = self._make_sent_guest()
        first_order = self._add_order(
            first_guest,
            business_date=self.start_date,
            net_sum="100",
            department_id="DEP-A",
            department_name="Заведение A",
        )
        second_order = self._add_order(
            second_guest,
            business_date=self.start_date + timedelta(days=1),
            net_sum="300",
            department_id="DEP-B",
            department_name="Заведение B",
        )
        self._add_raw_line(first_order, dish_code="DISH-A", dish_name="Блюдо A")
        self._add_raw_line(second_order, dish_code="DISH-B", dish_name="Блюдо B")

        network_snapshot = build_simple_mailing_report_snapshot(
            mailing=self.mailing,
        ).to_dict()
        venue_snapshot = build_simple_mailing_report_snapshot(
            mailing=self.mailing,
            department_id=" DEP-A ",
        ).to_dict()

        self.assertEqual(network_snapshot["orders"]["orders_count"], 2)
        self.assertEqual(venue_snapshot["department_filter"]["selected_id"], "DEP-A")
        self.assertEqual(
            venue_snapshot["department_filter"]["selected_name"],
            "Заведение A",
        )
        self.assertEqual(
            {item["id"] for item in venue_snapshot["department_filter"]["options"]},
            {"DEP-A", "DEP-B"},
        )
        self.assertEqual(venue_snapshot["audience"]["sent_total"], 2)
        self.assertEqual(venue_snapshot["orders"]["guests_count"], 1)
        self.assertEqual(venue_snapshot["orders"]["orders_count"], 1)
        self.assertEqual(venue_snapshot["orders"]["net_sum"], Decimal("100"))
        self.assertEqual(venue_snapshot["orders"]["guest_share_percent"], Decimal("50.0"))
        self.assertEqual(venue_snapshot["daily_rows"][0]["orders_count"], 1)
        self.assertEqual(venue_snapshot["daily_rows"][1]["orders_count"], 0)
        self.assertTrue(
            all(row["orders_count"] == 1 for row in venue_snapshot["period_summary_rows"])
        )
        self.assertEqual(
            [row["venue_name"] for row in venue_snapshot["venue_rows"]],
            ["Заведение A"],
        )
        self.assertEqual(
            [row["dish_code"] for row in venue_snapshot["purchase_rows"]],
            ["DISH-A"],
        )

    def test_department_filter_with_no_orders_returns_zero_instead_of_network_totals(self):
        guest, _ = self._make_sent_guest()
        self._add_order(
            guest,
            business_date=self.start_date,
            net_sum="500",
            department_id="DEP-A",
            department_name="Заведение A",
        )
        TerminalDepartmentMap.objects.create(
            terminal_group_id="terminal-empty-venue",
            department_id="DEP-EMPTY",
            department_name="Заведение без заказов",
            is_active=True,
        )

        snapshot = build_simple_mailing_report_snapshot(
            mailing=self.mailing,
            department_id="DEP-EMPTY",
        ).to_dict()

        self.assertEqual(snapshot["audience"]["sent_total"], 1)
        self.assertEqual(snapshot["orders"]["orders_count"], 0)
        self.assertEqual(snapshot["orders"]["net_sum"], Decimal("0"))
        self.assertEqual(snapshot["purchase_rows"], [])
        self.assertEqual(snapshot["venue_rows"], [])
        self.assertEqual(
            snapshot["department_filter"]["selected_name"],
            "Заведение без заказов",
        )

        unknown = build_simple_mailing_report_snapshot(
            mailing=self.mailing,
            department_id="UNKNOWN",
        ).to_dict()
        self.assertEqual(unknown["orders"]["orders_count"], 0)
        self.assertEqual(unknown["department_filter"]["selected_name"], "UNKNOWN")
        self.assertEqual(normalize_simple_mailing_department_id(" DEP-A "), "DEP-A")
        with self.assertRaises(SimpleMailingReportError):
            normalize_simple_mailing_department_id("X" * 65)
        with self.assertRaises(SimpleMailingReportError):
            normalize_simple_mailing_department_id("DEP-A\x00")

    def test_venue_other_row_recomputes_distinct_guests_and_average_check(self):
        first_guest, _ = self._make_sent_guest()
        second_guest, _ = self._make_sent_guest()
        venues = [
            ("A", "Заведение A", first_guest, "500"),
            ("B", "Заведение B", first_guest, "400"),
            ("C", "Заведение C", second_guest, "300"),
            ("D", "Заведение D", first_guest, "100"),
            ("E", "Заведение E", first_guest, "60"),
            ("E", "Заведение E", second_guest, "40"),
        ]
        for department_id, name, guest, net_sum in venues:
            self._add_order(
                guest,
                business_date=self.start_date,
                net_sum=net_sum,
                department_id=department_id,
                department_name=name,
            )

        snapshot = build_simple_mailing_report_snapshot(mailing=self.mailing).to_dict()

        self.assertEqual([row["venue_name"] for row in snapshot["venue_rows"][:3]], [
            "Заведение A",
            "Заведение B",
            "Заведение C",
        ])
        other = snapshot["venue_rows"][3]
        self.assertTrue(other["is_other"])
        self.assertEqual(other["guests_count"], 2)
        self.assertEqual(other["orders_count"], 3)
        self.assertEqual(other["net_sum"], Decimal("200"))
        self.assertEqual(other["average_check"], Decimal("66.67"))

    def test_purchase_analysis_uses_full_identity_and_preserves_honest_zero_net(self):
        first_guest, _ = self._make_sent_guest()
        self._make_sent_guest()
        order = self._add_order(
            first_guest,
            business_date=self.start_date,
            net_sum="180",
            department_id="DEP-A",
            department_name="Заведение A",
            order_number=777,
            uniq_order_id="eligible-order",
        )
        self._add_raw_line(
            order,
            dish_code="DISH-ZERO",
            dish_name="Сырое название",
            amount="2",
            before="100",
            after="0",
        )
        self._add_raw_line(
            order,
            dish_code="DISH-FALLBACK",
            dish_name="Позиция без net",
            amount="1",
            before="50",
            after=None,
        )
        self._add_raw_line(
            order,
            dish_code="WRONG-IDENTITY",
            dish_name="Чужая позиция",
            amount="100",
            before="900",
            after="900",
            department_id="OTHER-DEP",
        )
        category = OlapCategoryDict.objects.create(
            iiko_category_external_id="CAT-1",
            category_name="Справочная категория",
        )
        OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id="DISH-ZERO",
            nomenclature_name="Название из справочника",
            olap_category=category,
            dish_group_name="Справочная группа",
        )

        snapshot = build_simple_mailing_report_snapshot(mailing=self.mailing).to_dict()
        rows = {row["dish_code"]: row for row in snapshot["purchase_rows"]}

        self.assertNotIn("WRONG-IDENTITY", rows)
        self.assertEqual(rows["DISH-ZERO"]["dish_name"], "Название из справочника")
        self.assertEqual(rows["DISH-ZERO"]["quantity"], Decimal("2"))
        self.assertEqual(rows["DISH-ZERO"]["net_sum"], Decimal("0"))
        self.assertEqual(rows["DISH-ZERO"]["guest_share_percent"], Decimal("50.0"))
        self.assertEqual(rows["DISH-FALLBACK"]["net_sum"], Decimal("50"))
        self.assertEqual(rows["DISH-FALLBACK"]["dish_name"], "Позиция без net")

    def test_purchase_analysis_returns_only_top_ten_items(self):
        guest, _ = self._make_sent_guest()
        order = self._add_order(
            guest,
            business_date=self.start_date,
            net_sum="1000",
        )
        for index in range(12):
            self._add_raw_line(
                order,
                dish_code=f"DISH-{index:02d}",
                dish_name=f"Позиция {index}",
                amount=str(12 - index),
                before="100",
                after="80",
            )

        snapshot = build_simple_mailing_report_snapshot(mailing=self.mailing).to_dict()

        self.assertEqual(len(snapshot["purchase_rows"]), 10)
        self.assertEqual(
            [row["dish_code"] for row in snapshot["purchase_rows"]],
            [f"DISH-{index:02d}" for index in range(10)],
        )

    def test_details_page_is_capped_stable_and_shows_only_unambiguous_channel(self):
        first_guest, _ = self._make_sent_guest()
        second_guest, second_row = self._make_sent_guest(provider=BotProfile.ProviderType.TELEGRAM)
        self._add_task(second_row, provider=BotProfile.ProviderType.VK)
        self._add_order(
            first_guest,
            business_date=self.start_date,
            net_sum="100",
            order_number=10,
        )
        self._add_order(
            second_guest,
            business_date=self.start_date + timedelta(days=1),
            net_sum="200",
            order_number=20,
        )
        self._add_order(
            first_guest,
            business_date=self.start_date + timedelta(days=2),
            net_sum="300",
            order_number=30,
        )

        first_page = build_simple_mailing_order_details_page(
            mailing=self.mailing,
            period_days=7,
            page_number=1,
            page_size=2,
        ).to_dict()
        ended_page = build_simple_mailing_order_details_page(
            mailing=self.mailing,
            period_days=7,
            page_number=5,
            page_size=2,
        ).to_dict()

        self.assertEqual(first_page["total"], 3)
        self.assertEqual([row["order_number"] for row in first_page["results"]], [10, 20])
        self.assertEqual(first_page["results"][0]["channel"], "Telegram")
        self.assertEqual(first_page["results"][0]["calendar_delay_label"], "день отправки")
        self.assertEqual(first_page["results"][1]["channel"], "")
        self.assertEqual(first_page["results"][1]["calendar_delay_label"], "через 1 день")
        self.assertTrue(first_page["has_next"])
        self.assertEqual(ended_page["results"], [])
        self.assertFalse(ended_page["has_next"])
        self.assertEqual(normalize_order_page_number("-7"), 1)
        self.assertEqual(normalize_order_page_size("0"), DEFAULT_ORDER_PAGE_SIZE)
        self.assertEqual(normalize_order_page_size("500"), MAX_ORDER_PAGE_SIZE)

    def test_details_page_applies_the_same_department_filter(self):
        guest, _ = self._make_sent_guest()
        self._add_order(
            guest,
            business_date=self.start_date,
            net_sum="100",
            department_id="DEP-A",
            department_name="Заведение A",
            order_number=10,
        )
        self._add_order(
            guest,
            business_date=self.start_date,
            net_sum="200",
            department_id="DEP-B",
            department_name="Заведение B",
            order_number=20,
        )

        page = build_simple_mailing_order_details_page(
            mailing=self.mailing,
            period_days=7,
            department_id="DEP-A",
        ).to_dict()
        unknown = build_simple_mailing_order_details_page(
            mailing=self.mailing,
            period_days=7,
            department_id="UNKNOWN",
        ).to_dict()

        self.assertEqual(page["total"], 1)
        self.assertEqual(page["results"][0]["order_number"], 10)
        self.assertEqual(page["results"][0]["venue_name"], "Заведение A")
        self.assertEqual(unknown["total"], 0)
        self.assertEqual(unknown["results"], [])

    def test_empty_and_historical_data_remain_zero_safe_without_status_fallback(self):
        guest = self._create_guest()
        self._add_audience(
            guest,
            status=MailingGuest.Status.DONE,
            delivery_status="done",
        )

        snapshot = build_simple_mailing_report_snapshot(mailing=self.mailing).to_dict()

        self.assertEqual(snapshot["audience"]["sent_total"], 0)
        self.assertEqual(snapshot["audience"]["send_share_percent"], Decimal("0"))
        self.assertEqual(snapshot["orders"]["orders_count"], 0)
        self.assertEqual(snapshot["orders"]["average_check"], Decimal("0"))
        self.assertEqual(snapshot["orders"]["guest_share_percent"], Decimal("0"))
        self.assertEqual(snapshot["orders"]["average_first_order_days"], Decimal("0"))
        self.assertEqual(snapshot["purchase_rows"], [])
        self.assertTrue(any("исторический статус" in item for item in snapshot["limitations"]))

    def test_scheduled_date_remains_anchor_when_planned_datetime_disagrees(self):
        mismatched = self._create_mailing(
            name="Несогласованное расписание",
            coupon_series=None,
            scheduled_date=self.start_date,
            scheduled_begin=timezone.make_aware(datetime(2026, 6, 13, 1, 0)),
        )
        guest = self._create_guest()
        mailing_guest = self._add_audience(guest, mailing=mismatched)
        self._add_task(
            mailing_guest,
            finished_at=timezone.make_aware(datetime(2026, 6, 14, 3, 0)),
        )
        self._add_order(guest, business_date=self.start_date, net_sum="120")

        snapshot = build_simple_mailing_report_snapshot(mailing=mismatched).to_dict()

        self.assertEqual(snapshot["period"]["start_date"], self.start_date)
        self.assertEqual(snapshot["orders"]["orders_count"], 1)
        self.assertTrue(any("не совпадает" in item for item in snapshot["limitations"]))

    def test_query_count_does_not_grow_with_audience_size(self):
        first_guest, _ = self._make_sent_guest()
        first_order = self._add_order(
            first_guest,
            business_date=self.start_date,
            net_sum="100",
        )
        self._add_raw_line(
            first_order,
            dish_code="DISH-QUERY",
            dish_name="Контрольная позиция",
        )

        with CaptureQueriesContext(connection) as small_scope_queries:
            build_simple_mailing_report_snapshot(mailing=self.mailing)

        for index in range(5):
            guest, _ = self._make_sent_guest()
            self._add_order(
                guest,
                business_date=self.start_date + timedelta(days=index),
                net_sum="50",
            )

        with CaptureQueriesContext(connection) as expanded_scope_queries:
            build_simple_mailing_report_snapshot(mailing=self.mailing)

        self.assertLessEqual(len(small_scope_queries), 14)
        self.assertEqual(len(expanded_scope_queries), len(small_scope_queries))
