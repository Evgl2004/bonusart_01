from __future__ import annotations

import csv
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from guests.models import CouponPoolBatch, CouponRegistryEntry
from guests.services.coupon_constants import COUPON_VENUE_GLOBAL_NAME, is_coupon_global_venue


class CouponPoolGenerationError(Exception):
    """Ошибка генерации или экспорта купонного пула."""


@dataclass(slots=True)
class CouponPoolGenerationResult:
    """Результат генерации партии купонов."""

    batch: CouponPoolBatch
    created_count: int
    collisions_count: int


class CouponPoolService:
    """
    Сервис генерации и экспорта купонного пула.

    Назначение:
    1. централизованно генерировать уникальные коды купонов;
    2. сохранять коды в локальный реестр SAGUR;
    3. формировать CSV в формате импорта iikoCard.
    """

    ALPHABETS = {
        CouponPoolBatch.AlphabetMode.DIGITS: "0123456789",
        CouponPoolBatch.AlphabetMode.LATIN_UPPER: "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER: "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    }

    def __init__(self) -> None:
        self._rng = secrets.SystemRandom()

    def generate_batch_code(self, *, series: str) -> str:
        """
        Генерирует технический batch-код.

        Формат:
        1. нормализованная серия (буквы/цифры/подчёркивание);
        2. дата/время UTC;
        3. короткий случайный суффикс.
        """
        series_token = "".join(ch if ch.isalnum() else "_" for ch in str(series or "").strip().upper())
        if not series_token:
            series_token = "BATCH"
        timestamp = datetime.now(dt_timezone.utc).strftime("%Y%m%d_%H%M%S")
        suffix = "".join(self._rng.choice("0123456789ABCDEF") for _ in range(6))
        return f"{series_token}_{timestamp}_{suffix}"

    def generate_pool(
        self,
        *,
        series: str,
        prefix: str,
        venue_code: str | None = None,
        venue_name: str | None = None,
        count: int,
        random_length: int,
        alphabet_mode: str,
        generated_by: str | None = None,
        batch_code: str | None = None,
        source: str = CouponRegistryEntry.SourceType.GENERATED,
    ) -> CouponPoolGenerationResult:
        """
        Генерирует пул купонов и сохраняет его в локальный реестр.

        Правила:
        1. серия обязательна;
        2. коды уникальны в рамках серии (учитываются существующие записи в БД);
        3. префикс автоматически приводится к верхнему регистру.
        """
        normalized_series = str(series or "").strip()
        normalized_prefix = str(prefix or "").strip().upper()
        normalized_venue_code = str(venue_code or "").strip() or None
        normalized_venue_name = str(venue_name or "").strip() or None
        if is_coupon_global_venue(normalized_venue_code):
            normalized_venue_name = normalized_venue_name or COUPON_VENUE_GLOBAL_NAME
        normalized_generated_by = str(generated_by or "").strip() or None
        safe_count = int(count)
        safe_random_length = int(random_length)
        normalized_alphabet_mode = str(alphabet_mode or "").strip()

        if not normalized_series:
            raise CouponPoolGenerationError("Серия купонов не может быть пустой.")
        if safe_count <= 0:
            raise CouponPoolGenerationError("Количество купонов должно быть больше нуля.")
        if safe_random_length <= 0:
            raise CouponPoolGenerationError("Длина случайной части должна быть больше нуля.")
        alphabet = self.ALPHABETS.get(normalized_alphabet_mode)
        if not alphabet:
            allowed = ", ".join(sorted(self.ALPHABETS.keys()))
            raise CouponPoolGenerationError(
                f"Неизвестный режим алфавита `{normalized_alphabet_mode}`. Допустимые значения: {allowed}."
            )

        resolved_batch_code = str(batch_code or "").strip() or self.generate_batch_code(series=normalized_series)
        collisions = 0

        with transaction.atomic():
            batch = CouponPoolBatch.objects.create(
                batch_code=resolved_batch_code,
                series=normalized_series,
                venue_code=normalized_venue_code,
                venue_name=normalized_venue_name,
                prefix=normalized_prefix or None,
                alphabet_mode=normalized_alphabet_mode,
                random_length=safe_random_length,
                count_requested=safe_count,
                count_generated=0,
                generated_by=normalized_generated_by,
                verification_status=CouponPoolBatch.VerificationStatus.NOT_CHECKED,
            )

            existing_codes = set(
                CouponRegistryEntry.objects.filter(series=normalized_series).values_list("code", flat=True)
            )
            fresh_codes: set[str] = set()
            rows: list[CouponRegistryEntry] = []

            # Жёсткий предохранитель, чтобы не зациклиться при коллизиях.
            max_attempts = max(safe_count * 40, 400)
            attempts = 0

            while len(rows) < safe_count:
                attempts += 1
                if attempts > max_attempts:
                    raise CouponPoolGenerationError(
                        "Превышен лимит попыток генерации уникальных купонов. "
                        "Проверьте длину случайной части и режим алфавита."
                    )

                random_part = "".join(self._rng.choice(alphabet) for _ in range(safe_random_length))
                candidate = f"{normalized_prefix}{random_part}"
                if candidate in existing_codes or candidate in fresh_codes:
                    collisions += 1
                    continue

                fresh_codes.add(candidate)
                rows.append(
                    CouponRegistryEntry(
                        series=normalized_series,
                        code=candidate,
                        venue_code=normalized_venue_code,
                        venue_name=normalized_venue_name,
                        source=source,
                        is_active=True,
                        batch=batch,
                        pool_status=CouponRegistryEntry.PoolStatus.GENERATED,
                        iiko_check_status=CouponRegistryEntry.IikoCheckStatus.NOT_CHECKED,
                    )
                )

            CouponRegistryEntry.objects.bulk_create(rows, batch_size=1000)
            batch.count_generated = len(rows)
            batch.save(update_fields=["count_generated", "updated_at"])

        return CouponPoolGenerationResult(
            batch=batch,
            created_count=batch.count_generated,
            collisions_count=collisions,
        )

    def export_batch_csv(
        self,
        *,
        batch: CouponPoolBatch,
        output_path: str,
        include_optional_fields: bool = False,
    ) -> Path:
        """
        Экспортирует купоны партии в CSV для импорта iikoCard.

        Базовый контракт (проверен на стенде):
        1. обязательные заголовки: `series`, `number`;
        2. расширенные заголовки (опционально): `activated`, `activation_date`, `multi_use`, `deleted`.
        """
        path = Path(output_path)
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        coupons = list(
            CouponRegistryEntry.objects.filter(batch=batch).order_by("id").values_list("series", "code")
        )
        if not coupons:
            raise CouponPoolGenerationError(
                f"У партии `{batch.batch_code}` нет купонов для экспорта. Сначала сгенерируйте пул."
            )

        header = ["series", "number"]
        if include_optional_fields:
            header.extend(["activated", "activation_date", "multi_use", "deleted"])

        with path.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.writer(csv_file, delimiter=";")
            writer.writerow(header)
            for series, code in coupons:
                row: list[str | int] = [series, code]
                if include_optional_fields:
                    # Значения по умолчанию: не активирован, одноразовый, не удалён.
                    row.extend([0, "", 0, 0])
                writer.writerow(row)

        batch.export_file_path = str(path)
        batch.updated_at = timezone.now()
        batch.save(update_fields=["export_file_path", "updated_at"])
        return path

    def iter_batch_coupons(self, *, batch: CouponPoolBatch) -> Iterable[CouponRegistryEntry]:
        """
        Возвращает queryset купонов партии для последующей обработки.
        """
        return CouponRegistryEntry.objects.filter(batch=batch).order_by("id")
