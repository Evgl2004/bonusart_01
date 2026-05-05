from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from guests.models import Guest


class AuditGuestDuplicatesCommandTests(TestCase):
    def setUp(self):
        now_value = timezone.now()
        Guest.objects.create(
            phone="+79991110000",
            iiko_id="iiko-1",
            created_at=now_value,
            updated_at=now_value,
        )
        Guest.objects.create(
            phone="8 (999) 111-00-00",
            iiko_id="iiko-2",
            created_at=now_value,
            updated_at=now_value,
        )
        Guest.objects.create(
            phone="+79992223344",
            iiko_id="dup-iiko",
            created_at=now_value,
            updated_at=now_value,
        )
        Guest.objects.create(
            phone="+79993334455",
            iiko_id="dup-iiko",
            created_at=now_value,
            updated_at=now_value,
        )

    def test_command_prints_duplicate_summary(self):
        stdout = io.StringIO()
        call_command(
            "audit_guest_duplicates",
            "--show-groups",
            "--limit",
            "10",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("total_guests: 4", output)
        self.assertIn("phone10_duplicate_groups: 1", output)
        self.assertIn("iiko_id_duplicate_groups: 1", output)
        self.assertIn("phone10=9991110000", output)
        self.assertIn("iiko_id=dup-iiko", output)

    def test_command_writes_json_report(self):
        work_dir = Path(".tmp") / f"audit_guest_duplicates_{uuid.uuid4().hex}"
        report_path = work_dir / "duplicates.json"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            stdout = io.StringIO()
            call_command(
                "audit_guest_duplicates",
                "--output-json",
                str(report_path),
                stdout=stdout,
            )

            self.assertTrue(report_path.exists())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["total_guests"], 4)
            self.assertEqual(payload["summary"]["phone10_duplicate_groups"], 1)
            self.assertEqual(payload["summary"]["iiko_id_duplicate_groups"], 1)
        finally:
            if report_path.exists():
                report_path.unlink()
            if work_dir.exists():
                work_dir.rmdir()
