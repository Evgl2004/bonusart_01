"""
Тесты сервиса дашборда «Динамика сегментов».
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    BotProfile,
    Guest,
    GuestRestaurantDailyOrderFact,
    GuestRestaurantWindowMetrics,
    OrderFact,
    VtelemaxRecipientChannel,
)
from guests.services.segment_dynamics_dashboard import (
    build_segment_dynamics_dashboard_payload,
)


class SegmentDynamicsDashboardServiceTests(TestCase):
    """
    Проверяем ключевые правила сегментов по рядам дат.
    """

    def setUp(self):
        self.department_id = "dep-1"
        self.other_department_id = "dep-2"
        self.as_of_date = date(2026, 6, 24)
        self._phone_counter = 0

    def test_unsupported_180_days_period_falls_back_to_default(self):
        payload = build_segment_dynamics_dashboard_payload(
            period_days=180,
            department_id="",
            segment_code="all",
            date_to=self.as_of_date,
        )

        self.assertEqual(payload["filters"]["period_days"], 30)
        self.assertEqual(payload["filters"]["date_from"], "2026-05-26")
        self.assertEqual(payload["filters"]["date_to"], "2026-06-24")
        self.assertNotIn(180, payload["filters"]["period_options"])

    def test_new_in_venue_counts_first_purchase_exact_day(self):
        first_on_23 = self._create_guest("Новый 23")
        first_on_24 = self._create_guest("Новый 24")
        other_department_guest = self._create_guest("Новый другое заведение")

        self._create_daily_order(first_on_23, date(2026, 6, 23), self.department_id)
        self._create_daily_order(first_on_23, date(2026, 6, 24), self.department_id)
        self._create_daily_order(first_on_24, date(2026, 6, 24), self.department_id)
        self._create_daily_order(other_department_guest, date(2026, 6, 24), self.other_department_id)

        payload = build_segment_dynamics_dashboard_payload(
            period_days=7,
            department_id=self.department_id,
            segment_code="new_in_venue",
            date_to=self.as_of_date,
        )
        rows_by_day = {row["day"]: row for row in payload["rows"]}

        self.assertEqual(payload["filters"]["period_days"], 7)
        self.assertEqual(rows_by_day["2026-06-23"]["new_in_venue"], 1)
        self.assertEqual(rows_by_day["2026-06-24"]["new_in_venue"], 1)

    def test_new_in_venue_without_department_returns_zero_and_hint(self):
        guest = self._create_guest("Новый без заведения")
        self._create_daily_order(guest, self.as_of_date, self.department_id)

        payload = build_segment_dynamics_dashboard_payload(
            period_days=7,
            department_id="",
            segment_code="new_in_venue",
            date_to=self.as_of_date,
        )

        self.assertTrue(payload["needs_department_hint"])
        self.assertTrue(all(row["new_in_venue"] == 0 for row in payload["rows"]))

    def test_activity_segments_are_counted_from_window_metrics(self):
        active = self._create_guest("Активный")
        single = self._create_guest("Один визит")
        cooling = self._create_guest("Остывший")
        lost = self._create_guest("Потерянный")

        self._create_window(active, self.as_of_date, 30, 2)
        self._create_window(active, self.as_of_date, 60, 2)
        self._create_window(active, self.as_of_date, 180, 2)
        self._create_window(single, self.as_of_date, 30, 1)
        self._create_window(single, self.as_of_date, 60, 1)
        self._create_window(single, self.as_of_date, 180, 1)
        self._create_window(cooling, self.as_of_date, 60, 1)
        self._create_window(cooling, self.as_of_date, 180, 1)
        self._create_window(lost, self.as_of_date, 180, 1)

        payload = build_segment_dynamics_dashboard_payload(
            period_days=7,
            department_id=self.department_id,
            segment_code="all",
            date_to=self.as_of_date,
        )
        row = payload["rows"][-1]

        self.assertEqual(row["active_30d"], 1)
        self.assertEqual(row["single_visit_30d"], 1)
        self.assertEqual(row["cooling_30_60d"], 1)
        self.assertEqual(row["lost_60d_plus"], 1)
        self.assertTrue(row["has_window_metrics"])

    def test_bot_active_no_visits_180d_uses_recent_visits_window(self):
        idle_guest = self._create_guest("Бот без визитов")
        recent_guest = self._create_guest("Бот с визитом")
        self._create_vtelemax_channel(idle_guest, "+79990000091")
        self._create_vtelemax_channel(recent_guest, "+79990000092")
        self._create_order_fact(recent_guest, date(2026, 6, 20), self.department_id)

        payload = build_segment_dynamics_dashboard_payload(
            period_days=7,
            department_id=self.department_id,
            segment_code="bot_active_no_visits_180d",
            date_to=self.as_of_date,
        )

        self.assertEqual(payload["rows"][-1]["bot_active_no_visits_180d"], 1)

    def test_rows_are_calculated_independently_for_each_date(self):
        guest = self._create_guest("Меняет сегмент")
        previous_date = date(2026, 6, 23)
        self._create_window(guest, previous_date, 30, 1)
        self._create_window(guest, previous_date, 60, 1)
        self._create_window(guest, previous_date, 180, 1)
        self._create_window(guest, self.as_of_date, 30, 2)
        self._create_window(guest, self.as_of_date, 60, 2)
        self._create_window(guest, self.as_of_date, 180, 2)

        payload = build_segment_dynamics_dashboard_payload(
            period_days=7,
            department_id=self.department_id,
            segment_code="all",
            date_to=self.as_of_date,
        )
        rows_by_day = {row["day"]: row for row in payload["rows"]}

        self.assertEqual(rows_by_day["2026-06-23"]["single_visit_30d"], 1)
        self.assertEqual(rows_by_day["2026-06-23"]["active_30d"], 0)
        self.assertEqual(rows_by_day["2026-06-24"]["single_visit_30d"], 0)
        self.assertEqual(rows_by_day["2026-06-24"]["active_30d"], 1)

    def _create_guest(self, first_name: str) -> Guest:
        self._phone_counter += 1
        return Guest.objects.create(
            phone=f"+7999000{self._phone_counter:04d}",
            first_name=first_name,
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )

    def _create_daily_order(self, guest: Guest, business_date: date, department_id: str):
        return GuestRestaurantDailyOrderFact.objects.create(
            business_date=business_date,
            guest=guest,
            department_id=department_id,
            orders_count=1,
            sum_net=Decimal("1000.00"),
        )

    def _create_window(
        self,
        guest: Guest,
        as_of_date: date,
        window_days: int,
        visits_count: int,
    ):
        return GuestRestaurantWindowMetrics.objects.create(
            as_of_date=as_of_date,
            guest=guest,
            department_id=self.department_id,
            window_days=window_days,
            visits_count=visits_count,
            orders_count=visits_count,
            sum_net=Decimal(visits_count * 1000),
            avg_check_net=Decimal("1000.00") if visits_count else Decimal("0.00"),
        )

    def _create_order_fact(self, guest: Guest, business_date: date, department_id: str):
        return OrderFact.objects.create(
            guest=guest,
            business_date=business_date,
            department_id=department_id,
            department_name=department_id,
            order_number=100000 + self._phone_counter,
            uniq_order_id=f"segment-dynamics-{guest.id}-{business_date.isoformat()}",
            gross_sum=Decimal("1000.00"),
            net_sum=Decimal("1000.00"),
        )

    def _create_vtelemax_channel(self, guest: Guest, phone: str):
        bot = BotProfile.objects.create(
            code=f"segment-dynamics-bot-{guest.id}",
            name=f"Segment Dynamics Bot {guest.id}",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        return VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            phone_e164=phone,
            external_id=f"chat-{guest.id}",
            is_registered=True,
            notifications_allowed=True,
            guest=guest,
            source_payload={},
            guest_binding=None,
        )
