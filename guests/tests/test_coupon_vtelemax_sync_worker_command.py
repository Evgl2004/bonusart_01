from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from guests.services.vtelemax_coupon_sync import CouponVtelemaxSyncBatchStats


class RunCouponVtelemaxSyncWorkerCommandTests(SimpleTestCase):
    """
    Проверки команды run_coupon_vtelemax_sync_worker.
    """

    @override_settings(VTELEMAX_COUPON_SYNC_ENABLED=False)
    def test_once_raises_when_sync_disabled(self):
        with self.assertRaises(CommandError):
            call_command(
                "run_coupon_vtelemax_sync_worker",
                "--once",
                stdout=io.StringIO(),
            )

    @override_settings(VTELEMAX_COUPON_SYNC_ENABLED=True)
    @patch("guests.management.commands.run_coupon_vtelemax_sync_worker.signal.signal")
    @patch("guests.management.commands.run_coupon_vtelemax_sync_worker.VtelemaxCouponSyncService")
    def test_once_runs_single_batch(
        self,
        mocked_service_cls,
        mocked_signal,
    ):
        fake_service = MagicMock()
        fake_service.process_batch.return_value = CouponVtelemaxSyncBatchStats(
            scanned=7,
            processed=5,
            acked=4,
            failed=1,
            skipped_max_attempts=0,
            assignments_acked=3,
            status_updates_acked=1,
        )
        mocked_service_cls.from_settings.return_value = fake_service

        result = call_command(
            "run_coupon_vtelemax_sync_worker",
            "--once",
            "--batch-size=55",
            stdout=io.StringIO(),
        )

        self.assertIsNone(result)
        mocked_service_cls.from_settings.assert_called_once_with()
        fake_service.process_batch.assert_called_once_with(limit=55)
        self.assertGreaterEqual(mocked_signal.call_count, 2)
