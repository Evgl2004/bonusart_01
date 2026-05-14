from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from guests.models import CouponPoolBatch, CouponRegistryEntry
from guests.services.iiko_coupon_client import IikoCouponApiError, IikoCouponClient


@dataclass(slots=True)
class VerifyStats:
    total: int = 0
    found: int = 0
    not_found: int = 0
    check_errors: int = 0


class Command(BaseCommand):
    help = (
        "Проверяет, что купоны из реестра SAGUR действительно загружены в iikoCard, "
        "и обновляет статусы проверки в локальной БД."
    )

    def add_arguments(self, parser):
        parser.add_argument("--series", default="", help="Серия купонов для проверки.")
        parser.add_argument("--batch-code", default="", help="Код партии для проверки.")
        parser.add_argument(
            "--page-size",
            type=int,
            default=500,
            help="Размер страницы запроса `coupons/by_series`.",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=200,
            help="Ограничение количества страниц для обхода `coupons/by_series`.",
        )
        parser.add_argument(
            "--sample-info-check-limit",
            type=int,
            default=2,
            help="Сколько найденных купонов дополнительно проверить через `coupons/info`.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Только показать итог проверки без записи в БД.",
        )

    def handle(self, *args, **options):
        series = str(options.get("series") or "").strip()
        batch_code = str(options.get("batch_code") or "").strip()
        page_size = max(1, int(options.get("page_size") or 500))
        max_pages = max(1, int(options.get("max_pages") or 200))
        sample_limit = max(0, int(options.get("sample_info_check_limit") or 0))
        dry_run = bool(options.get("dry_run", False))

        if not series and not batch_code:
            raise CommandError("Нужно указать --series или --batch-code.")

        coupon_qs = CouponRegistryEntry.objects.all().order_by("id")
        target_batch: CouponPoolBatch | None = None
        if batch_code:
            target_batch = CouponPoolBatch.objects.filter(batch_code=batch_code).first()
            if target_batch is None:
                raise CommandError(f"Партия `{batch_code}` не найдена.")
            coupon_qs = coupon_qs.filter(batch=target_batch)
            if not series:
                series = target_batch.series
        if series:
            coupon_qs = coupon_qs.filter(series=series)

        coupons = list(coupon_qs)
        if not coupons:
            raise CommandError("В выбранной области нет купонов для проверки.")

        client = IikoCouponClient(
            api_key=str(getattr(settings, "IIKO_API_KEY", "") or "").strip(),
            base_url=str(getattr(settings, "IIKO_API_BASE_URL", "") or "").strip(),
            organization_id=str(getattr(settings, "IIKO_ORGANIZATION_ID", "") or "").strip(),
            timeout_seconds=15.0,
        )

        if not client.api_key or not client.base_url or not client.organization_id:
            raise CommandError(
                "Не заполнены настройки IIKO_API_KEY / IIKO_API_BASE_URL / IIKO_ORGANIZATION_ID."
            )

        stats = VerifyStats(total=len(coupons))
        updated_at = timezone.now()
        found_numbers: set[str] = set()
        series_exists = False
        sample_checked = 0
        sample_info_errors = 0

        try:
            series_rows = client.get_coupon_series_with_non_activated()
            for row in series_rows:
                series_number = str(row.get("number") or "").strip()
                if series_number == series:
                    series_exists = True
                    break

            found_numbers = client.fetch_all_non_activated_numbers(
                series=series,
                page_size=page_size,
                max_pages=max_pages,
            )

            updates: list[CouponRegistryEntry] = []
            for coupon in coupons:
                if coupon.code in found_numbers:
                    stats.found += 1
                    coupon.iiko_check_status = CouponRegistryEntry.IikoCheckStatus.FOUND
                    coupon.iiko_check_error = None
                    if coupon.pool_status in (
                        CouponRegistryEntry.PoolStatus.GENERATED,
                        CouponRegistryEntry.PoolStatus.UPLOADED_PENDING_CHECK,
                        CouponRegistryEntry.PoolStatus.VERIFY_FAILED,
                    ):
                        coupon.pool_status = CouponRegistryEntry.PoolStatus.VERIFIED_LOADED
                else:
                    stats.not_found += 1
                    coupon.iiko_check_status = CouponRegistryEntry.IikoCheckStatus.NOT_FOUND
                    coupon.iiko_check_error = "Купон не найден в ответе iiko `coupons/by_series`."
                    if coupon.pool_status in (
                        CouponRegistryEntry.PoolStatus.GENERATED,
                        CouponRegistryEntry.PoolStatus.UPLOADED_PENDING_CHECK,
                        CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
                    ):
                        coupon.pool_status = CouponRegistryEntry.PoolStatus.VERIFY_FAILED
                coupon.iiko_checked_at = updated_at
                updates.append(coupon)

            if sample_limit > 0:
                for coupon in coupons:
                    if sample_checked >= sample_limit:
                        break
                    if coupon.code not in found_numbers:
                        continue
                    sample_checked += 1
                    try:
                        info_rows = client.get_coupon_info(number=coupon.code, series=series)
                        if not info_rows:
                            sample_info_errors += 1
                    except IikoCouponApiError:
                        sample_info_errors += 1

            if not dry_run:
                with transaction.atomic():
                    CouponRegistryEntry.objects.bulk_update(
                        updates,
                        fields=[
                            "iiko_check_status",
                            "iiko_check_error",
                            "pool_status",
                            "iiko_checked_at",
                            "updated_at",
                        ],
                        batch_size=1000,
                    )

                    if target_batch is not None:
                        if stats.not_found == 0 and stats.found > 0:
                            batch_status = CouponPoolBatch.VerificationStatus.LOADED
                        elif stats.found > 0:
                            batch_status = CouponPoolBatch.VerificationStatus.PARTIALLY_LOADED
                        else:
                            batch_status = CouponPoolBatch.VerificationStatus.FAILED
                        note = (
                            f"series_exists={series_exists}; sample_info_checked={sample_checked}; "
                            f"sample_info_errors={sample_info_errors}"
                        )
                        target_batch.verification_status = batch_status
                        target_batch.last_verified_at = updated_at
                        target_batch.verified_found_count = stats.found
                        target_batch.verified_not_found_count = stats.not_found
                        target_batch.verification_note = note
                        target_batch.save(
                            update_fields=[
                                "verification_status",
                                "last_verified_at",
                                "verified_found_count",
                                "verified_not_found_count",
                                "verification_note",
                                "updated_at",
                            ]
                        )

        except IikoCouponApiError as exc:
            stats.check_errors += 1
            raise CommandError(f"Ошибка API iiko: {exc}") from exc
        finally:
            client.close()

        self.stdout.write("=== Результат проверки загрузки купонов в iikoCard ===")
        self.stdout.write(f"series={series}")
        self.stdout.write(f"batch_code={target_batch.batch_code if target_batch else ''}")
        self.stdout.write(f"series_exists={series_exists}")
        self.stdout.write(f"rows_total={stats.total}")
        self.stdout.write(f"rows_found={stats.found}")
        self.stdout.write(f"rows_not_found={stats.not_found}")
        self.stdout.write(f"sample_info_checked={sample_checked}")
        self.stdout.write(f"sample_info_errors={sample_info_errors}")
        self.stdout.write(f"dry_run={dry_run}")
