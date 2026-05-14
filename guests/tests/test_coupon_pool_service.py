from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase

from guests.models import CouponPoolBatch, CouponRegistryEntry
from guests.services.coupon_pool import CouponPoolService


class CouponPoolServiceTests(TestCase):
    def setUp(self):
        self.service = CouponPoolService()

    def test_generate_pool_creates_batch_and_registry_entries(self):
        result = self.service.generate_pool(
            series="TEST",
            prefix="TST-",
            count=5,
            random_length=8,
            alphabet_mode=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
            generated_by="qa_user",
        )

        self.assertEqual(result.created_count, 5)
        self.assertEqual(CouponPoolBatch.objects.count(), 1)
        self.assertEqual(CouponRegistryEntry.objects.count(), 5)

        batch = CouponPoolBatch.objects.get()
        self.assertEqual(batch.series, "TEST")
        self.assertEqual(batch.prefix, "TST-")
        self.assertEqual(batch.count_generated, 5)

        codes = list(CouponRegistryEntry.objects.values_list("code", flat=True))
        self.assertEqual(len(codes), len(set(codes)))
        for code in codes:
            self.assertTrue(code.startswith("TST-"))

    def test_generate_pool_avoids_collisions_with_existing_series(self):
        existing_batch = CouponPoolBatch.objects.create(
            batch_code="TEST_OLD",
            series="TEST",
            prefix="TST-",
            alphabet_mode=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
            random_length=8,
            count_requested=1,
            count_generated=1,
        )
        CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-AAAA1111",
            source=CouponRegistryEntry.SourceType.GENERATED,
            batch=existing_batch,
        )

        result = self.service.generate_pool(
            series="TEST",
            prefix="TST-",
            count=5,
            random_length=8,
            alphabet_mode=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
        )

        self.assertEqual(result.created_count, 5)
        self.assertFalse(
            CouponRegistryEntry.objects.filter(batch=result.batch, code="TST-AAAA1111").exists()
        )

    def test_export_batch_csv_minimal_format(self):
        result = self.service.generate_pool(
            series="TEST",
            prefix="TST-",
            count=2,
            random_length=6,
            alphabet_mode=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
        )
        with TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "coupons.csv"
            exported_path = self.service.export_batch_csv(
                batch=result.batch,
                output_path=str(output_path),
                include_optional_fields=False,
            )
            content = exported_path.read_text(encoding="utf-8")

        rows = [line.strip() for line in content.splitlines() if line.strip()]
        self.assertEqual(rows[0], "series;number")
        self.assertEqual(len(rows), 3)

    def test_export_batch_csv_optional_format(self):
        result = self.service.generate_pool(
            series="TEST",
            prefix="TST-",
            count=2,
            random_length=6,
            alphabet_mode=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
        )
        with TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "coupons_optional.csv"
            exported_path = self.service.export_batch_csv(
                batch=result.batch,
                output_path=str(output_path),
                include_optional_fields=True,
            )
            content = exported_path.read_text(encoding="utf-8")

        rows = [line.strip() for line in content.splitlines() if line.strip()]
        self.assertEqual(
            rows[0],
            "series;number;activated;activation_date;multi_use;deleted",
        )
        # 2 купона + заголовок
        self.assertEqual(len(rows), 3)
