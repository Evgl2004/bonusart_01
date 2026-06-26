from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from guests.services.iiko_customer_category_sync import IikoCustomerCategorySyncBatchStats


class RunIikoCustomerCategorySyncWorkerCommandTests(SimpleTestCase):
    """
    Проверки команды run_iiko_customer_category_sync_worker.
    """

    @override_settings(IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=False)
    def test_once_raises_when_sync_disabled(self):
        with self.assertRaises(CommandError):
            call_command(
                "run_iiko_customer_category_sync_worker",
                "--once",
                stdout=io.StringIO(),
            )

    @override_settings(IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True)
    @patch("guests.management.commands.run_iiko_customer_category_sync_worker.signal.signal")
    @patch("guests.management.commands.run_iiko_customer_category_sync_worker.IikoCustomerCategorySyncService")
    def test_once_runs_single_batch(
        self,
        mocked_service_cls,
        mocked_signal,
    ):
        fake_service = MagicMock()
        fake_service.process_batch.return_value = IikoCustomerCategorySyncBatchStats(
            scanned=7,
            processed=5,
            acked=4,
            failed=1,
            skipped=1,
            skipped_max_attempts=0,
            add_acked=3,
            remove_acked=1,
        )
        mocked_service_cls.from_settings.return_value = fake_service

        result = call_command(
            "run_iiko_customer_category_sync_worker",
            "--once",
            "--batch-size=55",
            stdout=io.StringIO(),
        )

        self.assertIsNone(result)
        mocked_service_cls.from_settings.assert_called_once_with()
        fake_service.process_batch.assert_called_once_with(limit=55)
        self.assertGreaterEqual(mocked_signal.call_count, 2)


class DiagnoseIikoCustomerCategorySyncCommandTests(TestCase):
    """
    Smoke-тест диагностики очереди iikoCard.
    """

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="cat-active-coupon",
    )
    def test_diagnose_command_prints_empty_queue_summary(self):
        output = io.StringIO()

        call_command(
            "diagnose_iiko_customer_category_sync",
            "--limit=5",
            stdout=output,
        )

        text = output.getvalue()
        self.assertIn("Диагностика очереди iikoCard категорий гостей", text)
        self.assertIn("category_id=cat-active-coupon", text)
        self.assertIn("событий нет", text)
