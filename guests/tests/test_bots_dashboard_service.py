from __future__ import annotations

import uuid
from datetime import date, datetime, timezone as dt_timezone

from django.test import TestCase

from guests.models import VtelemaxRecipientChannel
from guests.services.bots_dashboard import build_bots_dashboard_payload


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
        )

        self.assertEqual(payload["filters"]["days"], 2)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["rows"][0]["channels_total_telegram"], 1)
        self.assertEqual(payload["rows"][1]["channels_total_max"], 1)
        self.assertEqual(payload["kpis"]["channels_total"], 2)
        self.assertEqual(payload["kpis"]["channels_registered_optin"], 1)
