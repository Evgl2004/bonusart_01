from __future__ import annotations

import uuid
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from guests.models import VtelemaxRecipientChannel


class BotsDashboardViewTests(TestCase):
    def test_dashboard_bots_page_renders(self):
        now = timezone.now()
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform="telegram",
            phone_e164="+79000000001",
            external_id="tg_1",
            is_registered=True,
            notifications_allowed=True,
            registered_at=now - timedelta(days=1),
            account_created_at=now - timedelta(days=2),
            source_payload={},
        )
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform="vk",
            phone_e164="+79000000002",
            external_id="vk_1",
            is_registered=True,
            notifications_allowed=False,
            registered_at=now - timedelta(days=1),
            account_created_at=now - timedelta(days=1),
            source_payload={},
        )

        response = self.client.get(reverse("dashboard_bots"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "analytics/dashboard_bots.html")
        self.assertContains(response, "bots-dashboard-data")
        payload = response.context["bots_dashboard_payload"]
        self.assertIn("rows", payload)
        self.assertGreaterEqual(len(payload["rows"]), 1)

    def test_invalid_date_range_falls_back_to_default_window(self):
        with patch("guests.views_analytics.timezone.localdate", return_value=date(2026, 5, 18)):
            response = self.client.get(
                reverse("dashboard_bots"),
                {"date_from": "2026-05-13", "date_to": "2026-05-01"},
                secure=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_date_to"], "2026-05-17")
        self.assertEqual(response.context["selected_date_from"], "2026-04-18")

    @patch("guests.views_analytics.timezone.localdate", return_value=date(2026, 5, 18))
    def test_period_days_switch_applies_window_from_current_closed_day(self, _localdate_mock):
        response = self.client.get(
            reverse("dashboard_bots"),
            {"period_days": "7", "date_to": "2026-05-13"},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_period_days"], 7)
        self.assertEqual(response.context["selected_date_to"], "2026-05-17")
        self.assertEqual(response.context["selected_date_from"], "2026-05-11")
        self.assertContains(response, "period-switch-btn is-active")
        self.assertContains(response, 'href="?period_days=7"')

    @patch("guests.views_analytics.timezone.localdate", return_value=date(2026, 5, 13))
    def test_default_and_today_date_to_are_clamped_to_yesterday(self, _localdate_mock):
        response_default = self.client.get(reverse("dashboard_bots"), secure=True)
        self.assertEqual(response_default.status_code, 200)
        self.assertEqual(response_default.context["selected_date_to"], "2026-05-12")

        response_today = self.client.get(
            reverse("dashboard_bots"),
            {"date_to": "2026-05-13", "period_days": "7"},
            secure=True,
        )
        self.assertEqual(response_today.status_code, 200)
        self.assertEqual(response_today.context["selected_date_to"], "2026-05-12")
        self.assertEqual(response_today.context["selected_date_from"], "2026-05-06")

    @patch("guests.views_analytics.timezone.localdate", return_value=date(2026, 5, 18))
    def test_stale_date_to_in_url_is_normalized_to_current_closed_day(self, _localdate_mock):
        response = self.client.get(
            reverse("dashboard_bots"),
            {"period_days": "30", "date_to": "2026-05-13"},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_date_to"], "2026-05-17")
        self.assertEqual(response.context["selected_date_from"], "2026-04-18")
        payload = response.context["bots_dashboard_payload"]
        self.assertEqual(payload["filters"]["date_to"], "2026-05-17")
        self.assertEqual(payload["filters"]["date_from"], "2026-04-18")

    @patch("guests.views_analytics.timezone.localdate", return_value=date(2026, 5, 18))
    def test_period_modes_use_the_same_date_window_rule(self, _localdate_mock):
        expected_windows = {
            7: "2026-05-11",
            14: "2026-05-04",
            30: "2026-04-18",
        }
        for period_days, expected_date_from in expected_windows.items():
            with self.subTest(period_days=period_days):
                response = self.client.get(
                    reverse("dashboard_bots"),
                    {"period_days": str(period_days)},
                    secure=True,
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["selected_date_to"], "2026-05-17")
                self.assertEqual(response.context["selected_date_from"], expected_date_from)
