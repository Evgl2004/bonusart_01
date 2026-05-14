from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from guests.models import CouponPoolBatch
from guests.services.coupon_pool import CouponPoolGenerationError, CouponPoolService


def _safe_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() else "_" for ch in str(value or "").strip())
    return token or "NA"


class Command(BaseCommand):
    help = (
        "Генерирует пул купонов в локальном реестре SAGUR и, при необходимости, "
        "выгружает CSV для импорта в iikoCard."
    )

    def add_arguments(self, parser):
        parser.add_argument("--series", required=True, help="Серия купонов в iikoCard (например, TEST).")
        parser.add_argument(
            "--venue-code",
            required=True,
            help="Код заведения (department_id), для которого генерируется пул купонов.",
        )
        parser.add_argument(
            "--venue-name",
            default="",
            help="Человекочитаемое имя заведения (опционально, для удобства в реестре).",
        )
        parser.add_argument("--prefix", default="", help="Префикс купона (например, TST-).")
        parser.add_argument("--count", type=int, required=True, help="Количество купонов в партии.")
        parser.add_argument(
            "--random-length",
            type=int,
            default=12,
            help="Длина случайной части купона.",
        )
        parser.add_argument(
            "--alphabet-mode",
            choices=[
                CouponPoolBatch.AlphabetMode.DIGITS,
                CouponPoolBatch.AlphabetMode.LATIN_UPPER,
                CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
            ],
            default=CouponPoolBatch.AlphabetMode.DIGITS_LATIN_UPPER,
            help="Режим алфавита генерации случайной части.",
        )
        parser.add_argument("--batch-code", default="", help="Явный код партии (если нужно задать вручную).")
        parser.add_argument("--generated-by", default="", help="Оператор/пользователь, запустивший генерацию.")
        parser.add_argument(
            "--export-path",
            default="",
            help="Явный путь к CSV. Если не задан, путь формируется автоматически в каталоге tools/.",
        )
        parser.add_argument(
            "--include-optional-fields",
            action="store_true",
            help="Добавить в CSV опциональные столбцы iikoCard: activated/activation_date/multi_use/deleted.",
        )
        parser.add_argument(
            "--skip-export",
            action="store_true",
            help="Не формировать CSV-файл, только создать записи в реестре SAGUR.",
        )

    def handle(self, *args, **options):
        series = str(options["series"] or "").strip()
        venue_code = str(options["venue_code"] or "").strip()
        venue_name = str(options.get("venue_name") or "").strip() or None
        prefix = str(options["prefix"] or "")
        count = int(options["count"])
        random_length = int(options["random_length"])
        alphabet_mode = str(options["alphabet_mode"])
        batch_code = str(options.get("batch_code") or "").strip() or None
        generated_by = str(options.get("generated_by") or "").strip() or None
        export_path_raw = str(options.get("export_path") or "").strip()
        include_optional_fields = bool(options.get("include_optional_fields", False))
        skip_export = bool(options.get("skip_export", False))

        service = CouponPoolService()

        try:
            result = service.generate_pool(
                series=series,
                prefix=prefix,
                venue_code=venue_code,
                venue_name=venue_name,
                count=count,
                random_length=random_length,
                alphabet_mode=alphabet_mode,
                generated_by=generated_by,
                batch_code=batch_code,
            )
        except CouponPoolGenerationError as exc:
            raise CommandError(str(exc)) from exc

        csv_path: Path | None = None
        if not skip_export:
            if export_path_raw:
                export_path = Path(export_path_raw)
            else:
                suffix = "series_number_optional" if include_optional_fields else "series_number"
                default_name = (
                    f"iikocard_coupon_import_{_safe_token(series)}_{_safe_token(prefix)}_{count}_{suffix}.csv"
                )
                export_path = Path("tools") / default_name
            try:
                csv_path = service.export_batch_csv(
                    batch=result.batch,
                    output_path=str(export_path),
                    include_optional_fields=include_optional_fields,
                )
            except CouponPoolGenerationError as exc:
                raise CommandError(str(exc)) from exc

        self.stdout.write("=== Результат генерации купонного пула ===")
        self.stdout.write(f"batch_code={result.batch.batch_code}")
        self.stdout.write(f"series={result.batch.series}")
        self.stdout.write(f"venue_code={result.batch.venue_code or ''}")
        self.stdout.write(f"venue_name={result.batch.venue_name or ''}")
        self.stdout.write(f"prefix={result.batch.prefix or ''}")
        self.stdout.write(f"count_requested={result.batch.count_requested}")
        self.stdout.write(f"count_generated={result.created_count}")
        self.stdout.write(f"collisions={result.collisions_count}")
        self.stdout.write(f"alphabet_mode={result.batch.alphabet_mode}")
        self.stdout.write(f"random_length={result.batch.random_length}")
        self.stdout.write(f"generated_by={result.batch.generated_by or ''}")
        if csv_path is not None:
            self.stdout.write(f"csv_path={csv_path}")
            self.stdout.write(f"csv_optional_fields={include_optional_fields}")
        else:
            self.stdout.write("csv_path=")
            self.stdout.write("csv_optional_fields=False")
