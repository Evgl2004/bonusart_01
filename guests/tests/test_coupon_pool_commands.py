from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from guests.models import CouponPoolBatch, CouponRegistryEntry
from guests.services.coupon_constants import COUPON_VENUE_GLOBAL_CODE, COUPON_VENUE_GLOBAL_NAME
from guests.services.iiko_coupon_client import IikoCouponClient


class GenerateCouponPoolCommandTests(TestCase):
    def test_generate_command_creates_batch_and_csv(self):
        with TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "generated.csv"
            call_command(
                "generate_coupon_pool",
                series="TEST",
                venue_code="DEP_1",
                venue_name="Тестовый ресторан",
                prefix="TST-",
                count=3,
                random_length=10,
                alphabet_mode="digits_latin_upper",
                generated_by="qa",
                export_path=str(csv_path),
            )

            self.assertTrue(csv_path.exists())
            rows = [line.strip() for line in csv_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(rows[0], "series;number")
            self.assertEqual(len(rows), 4)

        self.assertEqual(CouponPoolBatch.objects.count(), 1)
        self.assertEqual(CouponRegistryEntry.objects.count(), 3)
        self.assertEqual(CouponPoolBatch.objects.get().venue_code, "DEP_1")

    def test_generate_command_sets_default_global_venue_name(self):
        with TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "generated_global.csv"
            call_command(
                "generate_coupon_pool",
                series="GLOBAL_TEST",
                venue_code=COUPON_VENUE_GLOBAL_CODE,
                prefix="GLB-",
                count=1,
                random_length=8,
                alphabet_mode="digits_latin_upper",
                export_path=str(csv_path),
            )

        batch = CouponPoolBatch.objects.get(series="GLOBAL_TEST")
        self.assertEqual(batch.venue_code, COUPON_VENUE_GLOBAL_CODE)
        self.assertEqual(batch.venue_name, COUPON_VENUE_GLOBAL_NAME)

    def test_generate_command_accepts_latin_letters_matching_cyrillic(self):
        call_command(
            "generate_coupon_pool",
            series="LOOKALIKE_CMD",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            prefix="REL-",
            count=5,
            random_length=8,
            alphabet_mode=CouponPoolBatch.AlphabetMode.LATIN_CYRILLIC_LOOKALIKE_UPPER,
            skip_export=True,
        )

        batch = CouponPoolBatch.objects.get(series="LOOKALIKE_CMD")
        allowed_letters = set("ABCEHKMPTXY")
        codes = list(CouponRegistryEntry.objects.filter(batch=batch).values_list("code", flat=True))

        self.assertEqual(batch.alphabet_mode, CouponPoolBatch.AlphabetMode.LATIN_CYRILLIC_LOOKALIKE_UPPER)
        self.assertEqual(len(codes), 5)
        for code in codes:
            self.assertTrue(code.startswith("REL-"))
            self.assertLessEqual(set(code.removeprefix("REL-")), allowed_letters)

    def test_generate_command_accepts_digits_and_latin_letters_matching_cyrillic(self):
        call_command(
            "generate_coupon_pool",
            series="LOOKALIKE_DIGITS_CMD",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            prefix="REL-",
            count=5,
            random_length=8,
            alphabet_mode=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_CYRILLIC_LOOKALIKE_UPPER,
            skip_export=True,
        )

        batch = CouponPoolBatch.objects.get(series="LOOKALIKE_DIGITS_CMD")
        allowed_symbols = set("0123456789ABCEHKMPTXY")
        codes = list(CouponRegistryEntry.objects.filter(batch=batch).values_list("code", flat=True))

        self.assertEqual(
            batch.alphabet_mode,
            CouponPoolBatch.AlphabetMode.DIGITS_LATIN_CYRILLIC_LOOKALIKE_UPPER,
        )
        self.assertEqual(len(codes), 5)
        for code in codes:
            self.assertTrue(code.startswith("REL-"))
            self.assertLessEqual(set(code.removeprefix("REL-")), allowed_symbols)


class IikoCouponClientUrlTests(TestCase):
    def test_normalizes_root_base_url_to_api_v1(self):
        client = IikoCouponClient(
            api_key="key",
            base_url="https://api-ru.iiko.services/",
            organization_id="org",
        )

        self.assertEqual(client.base_url, "https://api-ru.iiko.services/api/1")
        client.close()

    def test_keeps_existing_api_v1_base_url(self):
        client = IikoCouponClient(
            api_key="key",
            base_url="https://api-ru.iiko.services/api/1/",
            organization_id="org",
        )

        self.assertEqual(client.base_url, "https://api-ru.iiko.services/api/1")
        client.close()


class VerifyCouponPoolIikoCommandTests(TestCase):
    def setUp(self):
        self.batch = CouponPoolBatch.objects.create(
            batch_code="TEST_BATCH",
            series="TEST",
            venue_code="DEP_1",
            venue_name="Тестовый ресторан",
            prefix="TST-",
            alphabet_mode=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
            random_length=10,
            count_requested=3,
            count_generated=3,
        )
        self.coupon_1 = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-AAA111",
            source=CouponRegistryEntry.SourceType.GENERATED,
            batch=self.batch,
            pool_status=CouponRegistryEntry.PoolStatus.UPLOADED_PENDING_CHECK,
        )
        self.coupon_2 = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-BBB222",
            source=CouponRegistryEntry.SourceType.GENERATED,
            batch=self.batch,
            pool_status=CouponRegistryEntry.PoolStatus.UPLOADED_PENDING_CHECK,
        )
        self.coupon_3 = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-CCC333",
            source=CouponRegistryEntry.SourceType.GENERATED,
            batch=self.batch,
            pool_status=CouponRegistryEntry.PoolStatus.UPLOADED_PENDING_CHECK,
        )

    @patch("guests.management.commands.verify_coupon_pool_iiko.IikoCouponClient")
    def test_verify_command_updates_found_and_not_found_statuses(self, client_cls):
        client = client_cls.return_value
        client.api_key = "key"
        client.base_url = "https://example.com/api/1"
        client.organization_id = "org"
        client.get_coupon_series_with_non_activated.return_value = [{"number": "TEST"}]
        client.fetch_all_non_activated_numbers.return_value = {"TST-AAA111", "TST-BBB222"}
        client.get_coupon_info.return_value = [{"number": "TST-AAA111"}]

        call_command(
            "verify_coupon_pool_iiko",
            batch_code="TEST_BATCH",
            sample_info_check_limit=1,
        )

        self.coupon_1.refresh_from_db()
        self.coupon_2.refresh_from_db()
        self.coupon_3.refresh_from_db()
        self.batch.refresh_from_db()

        self.assertEqual(self.coupon_1.iiko_check_status, CouponRegistryEntry.IikoCheckStatus.FOUND)
        self.assertEqual(self.coupon_2.iiko_check_status, CouponRegistryEntry.IikoCheckStatus.FOUND)
        self.assertEqual(self.coupon_3.iiko_check_status, CouponRegistryEntry.IikoCheckStatus.NOT_FOUND)
        self.assertEqual(self.batch.verification_status, CouponPoolBatch.VerificationStatus.PARTIALLY_LOADED)
        self.assertEqual(self.batch.verified_found_count, 2)
        self.assertEqual(self.batch.verified_not_found_count, 1)

    @patch("guests.management.commands.verify_coupon_pool_iiko.IikoCouponClient")
    def test_verify_command_dry_run_does_not_persist_changes(self, client_cls):
        client = client_cls.return_value
        client.api_key = "key"
        client.base_url = "https://example.com/api/1"
        client.organization_id = "org"
        client.get_coupon_series_with_non_activated.return_value = [{"number": "TEST"}]
        client.fetch_all_non_activated_numbers.return_value = {"TST-AAA111", "TST-BBB222"}
        client.get_coupon_info.return_value = [{"number": "TST-AAA111"}]

        call_command(
            "verify_coupon_pool_iiko",
            batch_code="TEST_BATCH",
            sample_info_check_limit=1,
            dry_run=True,
        )

        self.coupon_1.refresh_from_db()
        self.coupon_2.refresh_from_db()
        self.coupon_3.refresh_from_db()
        self.batch.refresh_from_db()

        self.assertEqual(self.coupon_1.iiko_check_status, CouponRegistryEntry.IikoCheckStatus.NOT_CHECKED)
        self.assertEqual(self.coupon_2.iiko_check_status, CouponRegistryEntry.IikoCheckStatus.NOT_CHECKED)
        self.assertEqual(self.coupon_3.iiko_check_status, CouponRegistryEntry.IikoCheckStatus.NOT_CHECKED)
        self.assertEqual(self.batch.verification_status, CouponPoolBatch.VerificationStatus.NOT_CHECKED)
