import io
import uuid
from datetime import datetime, timezone as dt_timezone

from django.core.management import call_command
from django.test import TestCase

from guests.models import VtelemaxRecipientChannel


class DiagnoseBotsDashboardCommandTests(TestCase):
    def test_outputs_backend_period_tail_and_growth_card(self):
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform="telegram",
            external_id="tg_1",
            is_registered=True,
            notifications_allowed=True,
            account_created_at=datetime(2026, 5, 2, 8, 0, tzinfo=dt_timezone.utc),
            registered_at=datetime(2026, 5, 2, 9, 0, tzinfo=dt_timezone.utc),
            source_payload={},
        )

        out = io.StringIO()
        call_command(
            "diagnose_bots_dashboard",
            "--period-days",
            "7",
            "--date-to",
            "2026-05-02",
            "--tail",
            "1",
            stdout=out,
        )

        text = out.getvalue()
        self.assertIn("=== Диагностика дашборда ботов (bots_dashboard) ===", text)
        self.assertIn(
            "Период backend (date_window): date_from=2026-04-26 date_to=2026-05-02 period_days=7",
            text,
        )
        self.assertIn("2026-05-02 | 1 | 1 | 1 | 1 | 1 | 1", text)
        self.assertIn('Карточка "Прирост за вчера" (yesterday_growth): date=2026-05-02', text)
        self.assertIn("ИИ-отчёт (ai_report):", text)
