from __future__ import annotations

import uuid
from datetime import timedelta
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
        response = self.client.get(
            reverse("dashboard_bots"),
            {"date_from": "2026-05-13", "date_to": "2026-05-01"},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(response.context["selected_date_from"], response.context["selected_date_to"])

    def test_period_days_switch_applies_window_from_date_to(self):
        response = self.client.get(
            reverse("dashboard_bots"),
            {"period_days": "7", "date_to": "2026-05-13"},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_period_days"], 7)
        self.assertEqual(response.context["selected_date_to"], "2026-05-12")
        self.assertEqual(response.context["selected_date_from"], "2026-05-06")
        self.assertContains(response, "period-switch-btn is-active")

    @patch("guests.views_analytics.timezone.localdate", return_value=timezone.datetime(2026, 5, 13).date())
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
