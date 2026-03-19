import logging
import signal
import time
from typing import Iterable

from django.core.management.base import BaseCommand

from guests.services.olap_catalogs import (
    FocusResolvedRebuildStats,
    OlapCatalogSyncStats,
    rebuild_focus_category_nomenclature_resolved,
    sync_olap_catalogs_from_raw_lines,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Синхронизирует OLAP-справочники и пересобирает resolved-состав фокусных категорий.

    Команда нужна как служебный мост после этапов S3/S3.1:
    1. наполняет `olap_category_dict` и `olap_nomenclature_dict` из `olap_sales_raw_line`;
    2. пересобирает `focus_category_nomenclature_resolved` для ускорения ночных расчётов.
    """

    help = (
        "Синхронизирует OLAP-справочники из сырых строк и пересобирает "
        "предрассчитанный состав фокусных категорий."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_stop = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Выполнить один проход и завершить процесс.",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=300.0,
            help="Пауза между проходами в циклическом режиме.",
        )
        parser.add_argument(
            "--raw-line-id-from",
            type=int,
            default=None,
            help="Нижняя граница id сырых строк OLAP для синхронизации.",
        )
        parser.add_argument(
            "--raw-line-id-to",
            type=int,
            default=None,
            help="Верхняя граница id сырых строк OLAP для синхронизации.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=2000,
            help="Размер порции чтения сырых строк OLAP.",
        )
        parser.add_argument(
            "--skip-rebuild-resolved",
            action="store_true",
            help="Пропустить пересборку focus_category_nomenclature_resolved.",
        )
        parser.add_argument(
            "--focus-code",
            dest="focus_codes",
            action="append",
            default=[],
            help="Код фокусной категории для точечной пересборки (можно передать несколько раз).",
        )

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        logger.info(
            "Получен сигнал %s, команда sync_olap_catalogs завершится после текущего прохода.",
            signum,
        )

    @staticmethod
    def _normalize_focus_codes(raw_codes: Iterable[str]) -> list[str]:
        return [str(code).strip() for code in raw_codes if str(code).strip()]

    def _print_catalog_stats(self, stats: OlapCatalogSyncStats) -> None:
        self.stdout.write(
            (
                "[catalog] scanned_raw_lines={scanned} categories(created={c_created}, updated={c_updated}) "
                "nomenclature(created={n_created}, updated={n_updated}) "
                "skipped_without_category={skip_cat} skipped_without_nomenclature={skip_nom}"
            ).format(
                scanned=stats.scanned_raw_lines,
                c_created=stats.categories_created,
                c_updated=stats.categories_updated,
                n_created=stats.nomenclatures_created,
                n_updated=stats.nomenclatures_updated,
                skip_cat=stats.skipped_without_category,
                skip_nom=stats.skipped_without_nomenclature,
            )
        )

    def _print_resolved_stats(self, stats: FocusResolvedRebuildStats) -> None:
        self.stdout.write(
            (
                "[resolved] scanned_focus={scanned} rebuilt={rebuilt} disabled_cleared={disabled} "
                "written_links={written} deleted_links={deleted} skipped_invalid={skipped}"
            ).format(
                scanned=stats.scanned_focus_categories,
                rebuilt=stats.rebuilt_focus_categories,
                disabled=stats.disabled_focus_categories_cleared,
                written=stats.written_links,
                deleted=stats.deleted_links,
                skipped=stats.skipped_invalid_focus_categories,
            )
        )

    def _run_single_iteration(self, *, options) -> None:
        catalog_stats = sync_olap_catalogs_from_raw_lines(
            raw_line_id_from=options["raw_line_id_from"],
            raw_line_id_to=options["raw_line_id_to"],
            batch_size=max(100, int(options["batch_size"])),
        )
        self._print_catalog_stats(catalog_stats)

        if options["skip_rebuild_resolved"]:
            self.stdout.write("[resolved] skipped by --skip-rebuild-resolved")
            return

        focus_codes = self._normalize_focus_codes(options["focus_codes"])
        resolved_stats = rebuild_focus_category_nomenclature_resolved(
            focus_codes=focus_codes or None,
        )
        self._print_resolved_stats(resolved_stats)

    def _sleep_with_stop(self, total_seconds: float) -> None:
        remaining = max(0.0, float(total_seconds))
        while remaining > 0 and not self.should_stop:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    def handle(self, *args, **options):
        self._setup_signal_handlers()

        once_mode = bool(options["once"])
        sleep_seconds = max(1.0, float(options["sleep_seconds"]))

        self.stdout.write(self.style.SUCCESS("Запущен sync_olap_catalogs"))
        self.stdout.write(f"mode={'once' if once_mode else 'loop'}")
        self.stdout.write(f"sleep_seconds={sleep_seconds}")
        self.stdout.write(
            f"raw_line_id_from={options['raw_line_id_from']} raw_line_id_to={options['raw_line_id_to']}"
        )
        self.stdout.write(f"batch_size={max(100, int(options['batch_size']))}")
        self.stdout.write(f"skip_rebuild_resolved={bool(options['skip_rebuild_resolved'])}")

        if once_mode:
            self._run_single_iteration(options=options)
            return

        while not self.should_stop:
            self._run_single_iteration(options=options)
            if self.should_stop:
                break
            self._sleep_with_stop(sleep_seconds)
