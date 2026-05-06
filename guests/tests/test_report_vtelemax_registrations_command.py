import io
import uuid
from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from guests.models import VtelemaxRecipientChannel


class ReportVtelemaxRegistrationsCommandTests(TestCase):
    def _create_channel(
        self,
        *,
        platform: str,
        account_created_at,
        person_id: uuid.UUID | None = None,
        is_registered: bool = True,
    ) -> None:
        VtelemaxRecipientChannel.objects.create(
            person_id=person_id or uuid.uuid4(),
            platform=platform,
            is_registered=is_registered,
            account_created_at=account_created_at,
            notifications_allowed=True,
            rules_accepted=True,
            source_payload={},
        )

    def test_reports_grouped_rows_by_day_and_platform(self):
        now = timezone.now()
        day0 = now.replace(hour=11, minute=0, second=0, microsecond=0)
        day1 = day0 - timedelta(days=1)
        self._create_channel(platform="telegram", account_created_at=day0)
        self._create_channel(platform="telegram", account_created_at=day0)
        self._create_channel(platform="max", account_created_at=day0)
        self._create_channel(platform="vk", account_created_at=day1)
        self._create_channel(platform="vk", account_created_at=day1, is_registered=False)

        out = io.StringIO()
        call_command(
            "report_vtelemax_registrations",
            "--date-from",
            day1.date().isoformat(),
            "--date-to",
            day0.date().isoformat(),
            stdout=out,
        )
        text = out.getvalue()

        self.assertIn("=== Динамика регистраций vtelemax ===", text)
        self.assertIn(f"{day0.date()} | telegram | 2 | 2", text)
        self.assertIn(f"{day0.date()} | max | 1 | 1", text)
        self.assertIn(f"{day1.date()} | vk | 1 | 1", text)

    def test_fails_on_invalid_date_range(self):
        with self.assertRaises(CommandError):
            call_command(
                "report_vtelemax_registrations",
                "--date-from",
                "2026-05-10",
                "--date-to",
                "2026-05-01",
                stdout=io.StringIO(),
            )
