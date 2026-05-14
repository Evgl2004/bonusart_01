from __future__ import annotations

import io

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase


class SyncCouponRedemptionsCommandTests(TestCase):
    def test_runs_and_prints_summary(self):
        out = io.StringIO()
        call_command("sync_coupon_redemptions", "--dry-run", stdout=out)
        text = out.getvalue()
        self.assertIn("=== Синхронизация статусов купонов из OLAP ===", text)
        self.assertIn("dry_run=True", text)
        self.assertIn("order_facts_total=", text)

    def test_validates_date_range(self):
        with self.assertRaises(CommandError):
            call_command(
                "sync_coupon_redemptions",
                "--business-date-from",
                "2026-05-12",
                "--business-date-to",
                "2026-05-01",
                stdout=io.StringIO(),
            )

    def test_validates_order_fact_id_range(self):
        with self.assertRaises(CommandError):
            call_command(
                "sync_coupon_redemptions",
                "--order-fact-id-from",
                "200",
                "--order-fact-id-to",
                "100",
                stdout=io.StringIO(),
            )
