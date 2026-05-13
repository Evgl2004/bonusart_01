from __future__ import annotations

import uuid
from datetime import date, datetime, timezone as dt_timezone

from django.test import TestCase

from guests.models import VtelemaxRecipientChannel
from guests.services.bots_dashboard import (
    build_bots_dashboard_payload,
    normalize_bots_period_days,
)


class BotsDashboardServiceTests(TestCase):
    def test_builds_daily_rows_and_kpis(self):
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform="telegram",
            external_id="tg_1",
            is_registered=True,
            notifications_allowed=True,
            account_created_at=datetime(2026, 5, 1, 8, 0, tzinfo=dt_timezone.utc),
            registered_at=datetime(2026, 5, 1, 9, 0, tzinfo=dt_timezone.utc),
            source_payload={},
        )
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform="max",
            external_id="max_1",
            is_registered=True,
            notifications_allowed=False,
            account_created_at=datetime(2026, 5, 2, 8, 0, tzinfo=dt_timezone.utc),
            registered_at=datetime(2026, 5, 2, 9, 0, tzinfo=dt_timezone.utc),
            source_payload={},
        )

        payload = build_bots_dashboard_payload(
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 2),
            period_days=14,
        )

        self.assertEqual(payload["filters"]["days"], 2)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["rows"][0]["channels_total_telegram"], 1)
        self.assertEqual(payload["rows"][1]["channels_total_max"], 1)
        self.assertEqual(payload["kpis"]["channels_total"], 2)
        self.assertEqual(payload["kpis"]["channels_registered_optin"], 1)
        self.assertEqual(payload["filters"]["period_options"], [7, 14, 30])
        self.assertEqual(payload["filters"]["period_days"], 14)
        self.assertEqual(payload["header_totals"]["channels_total"], 2)
        self.assertEqual(payload["header_totals"]["channels_registered_optin"], 1)
        self.assertEqual(payload["header_totals"]["unique_persons_total"], 2)
        self.assertEqual(payload["header_totals"]["unique_persons_registered_optin"], 1)
        self.assertEqual(payload["yesterday_growth"]["channels_total_delta"], 1)
        self.assertEqual(payload["yesterday_growth"]["channels_total_delta_display"], "+1")
        self.assertEqual(payload["yesterday_growth"]["channels_registered_optin_delta"], 1)
        self.assertEqual(payload["yesterday_growth"]["channels_registered_optin_delta_display"], "+1")
        self.assertEqual(len(payload["quick_growth"]), 3)
        self.assertEqual(payload["quick_growth"][0]["days"], 7)
        self.assertEqual(payload["quick_growth"][0]["channels_total_delta"], 2)
        self.assertEqual(payload["quick_growth"][0]["channels_total_delta_display"], "+2")
        self.assertEqual(payload["quick_growth"][0]["channels_registered_optin_delta"], 1)
        self.assertEqual(payload["quick_growth"][0]["channels_registered_optin_delta_display"], "+1")
        self.assertEqual(payload["quick_growth"][0]["unique_persons_total_delta"], 2)
        self.assertEqual(payload["quick_growth"][0]["unique_persons_total_delta_display"], "+2")
        self.assertEqual(payload["quick_growth"][0]["unique_persons_registered_optin_delta"], 1)
        self.assertEqual(payload["quick_growth"][0]["unique_persons_registered_optin_delta_display"], "+1")

    def test_normalize_period_days_for_bots_dashboard(self):
        self.assertEqual(normalize_bots_period_days(None), 30)
        self.assertEqual(normalize_bots_period_days("abc"), 30)
        self.assertEqual(normalize_bots_period_days("999"), 30)
        self.assertEqual(normalize_bots_period_days(7), 7)
