from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from guests.models import CouponPoolBatch, CouponRegistryEntry, TerminalDepartmentMap


class CouponRegistryOpsViewTests(TestCase):
    """
    Проверки POST-операций реестра купонов:
    1. генерация пула + экспорт CSV;
    2. запуск проверки загрузки в iikoCard;
    3. скачивание CSV по batch.
    """

    def test_generate_pool_action_creates_batch_and_csv(self):
        TerminalDepartmentMap.objects.create(
            terminal_group_id="terminal-dep-1",
            department_id="DEP_1",
            department_name="Тестовое заведение",
            is_active=True,
        )
        with TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "generated.csv"
            response = self.client.post(
                reverse("coupon_registry_ops"),
                {
                    "action": "generate_pool",
                    "series": "TEST_OPS",
                    "venue_code": "DEP_1",
                    "prefix": "TST-",
                    "count": "2",
                    "random_length": "8",
                    "alphabet_mode": CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
                    "generated_by": "tester",
                    "export_path": str(csv_path),
                },
                secure=True,
            )

            self.assertEqual(response.status_code, 302)
            batch = CouponPoolBatch.objects.get(series="TEST_OPS")
            self.assertTrue(response.url.startswith(reverse("coupon_generation")))
            self.assertIn("batch_code=", response.url)
            self.assertEqual(batch.count_generated, 2)
            self.assertEqual(batch.generated_by, "tester")
            self.assertEqual(batch.venue_code, "DEP_1")
            self.assertEqual(batch.venue_name, "Тестовое заведение")
            self.assertEqual(Path(batch.export_file_path), csv_path)
            self.assertTrue(csv_path.exists())
            self.assertEqual(
                CouponRegistryEntry.objects.filter(batch=batch).count(),
                2,
            )

            lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreaterEqual(len(lines), 3)
            self.assertEqual(lines[0], "series;number")

    def test_generate_pool_rejects_unknown_venue_code(self):
        response = self.client.post(
            reverse("coupon_registry_ops"),
            {
                "action": "generate_pool",
                "series": "TEST_OPS_UNKNOWN",
                "venue_code": "UNKNOWN_DEP",
                "prefix": "TST-",
                "count": "2",
                "random_length": "8",
                "alphabet_mode": CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
                "generated_by": "tester",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CouponPoolBatch.objects.filter(series="TEST_OPS_UNKNOWN").exists())

    @patch("guests.views_reports.call_command")
    def test_verify_pool_action_invokes_command(self, call_command_mock):
        batch = CouponPoolBatch.objects.create(
            batch_code="TEST_VERIFY_001",
            series="TEST_VERIFY",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            prefix="TST-",
            random_length=8,
            alphabet_mode=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
            count_requested=2,
            count_generated=2,
            generated_by="tester",
        )

        response = self.client.post(
            reverse("coupon_registry_ops"),
            {
                "action": "verify_pool",
                "batch_code": batch.batch_code,
                "sample_info_check_limit": "3",
                "page_size": "250",
                "max_pages": "40",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("coupon_generation")))
        self.assertIn("batch_code=TEST_VERIFY_001", response.url)
        call_command_mock.assert_called_once_with(
            "verify_coupon_pool_iiko",
            series="",
            batch_code="TEST_VERIFY_001",
            sample_info_check_limit=3,
            page_size=250,
            max_pages=40,
            dry_run=False,
        )

    def test_download_csv_action_returns_file(self):
        with TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "download.csv"
            csv_path.write_text("series;number\nTEST;TST-1\n", encoding="utf-8")
            batch = CouponPoolBatch.objects.create(
                batch_code="TEST_DOWNLOAD_001",
                series="TEST",
                venue_code="DEP_1",
                venue_name="Тестовое заведение",
                prefix="TST-",
                random_length=8,
                alphabet_mode=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
                count_requested=1,
                count_generated=1,
                generated_by="tester",
                export_file_path=str(csv_path),
            )

            response = self.client.post(
                reverse("coupon_registry_ops"),
                {
                    "action": "download_csv",
                    "batch_code": batch.batch_code,
                },
                secure=True,
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment;", response["Content-Disposition"])
            self.assertIn("download.csv", response["Content-Disposition"])
            response.close()

    def test_download_csv_action_guides_when_series_entered_instead_of_batch(self):
        CouponPoolBatch.objects.create(
            batch_code="TEST_SERIES_BATCH_001",
            series="TEST_SERIES",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            prefix="TST-",
            random_length=8,
            alphabet_mode=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
            count_requested=1,
            count_generated=1,
            generated_by="tester",
            export_file_path="tools/test_series.csv",
        )

        response = self.client.post(
            reverse("coupon_registry_ops"),
            {
                "action": "download_csv",
                "batch_code": "TEST_SERIES",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("coupon_generation")))
        self.assertIn("series_hint=TEST_SERIES", response.url)
