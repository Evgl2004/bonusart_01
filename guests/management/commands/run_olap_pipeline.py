import logging
import signal
import time
from datetime import date
from typing import Callable, Iterable

from django.conf import settings
from django.core.management.base import BaseCommand

from guests.services.daily_category_fact import rebuild_daily_category_fact_from_raw_lines
from guests.services.iiko_olap_client import build_iiko_olap_client_from_settings
from guests.services.olap_catalogs import (
    rebuild_focus_category_nomenclature_resolved,
    sync_olap_catalogs_from_raw_lines,
)
from guests.services.olap_check_sync import OlapCheckSyncWorkerService
from guests.services.order_fact import rebuild_order_fact_from_raw_lines
from guests.services.window_metrics import rebuild_window_metrics_from_daily_facts

logger = logging.getLogger(__name__)


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text)


def _normalize_focus_codes(raw_codes: Iterable[str]) -> list[str] | None:
    normalized = [str(code).strip() for code in raw_codes if str(code).strip()]
    return normalized or None


def _normalize_windows(raw_windows: Iterable[str]) -> list[int] | None:
    normalized: list[int] = []
    for raw_value in raw_windows:
        safe_value = str(raw_value).strip()
        if not safe_value:
            continue
        value = int(safe_value)
        if value <= 0:
            continue
        if value not in normalized:
            normalized.append(value)
    return normalized or None


class Command(BaseCommand):
    """
    Оркестратор полного OLAP-контура после backfill.

    Порядок шагов:
    1. `olap_check_sync_journal -> olap_sales_raw_line` (опционально);
    2. синхронизация справочников OLAP;
    3. пересборка resolved-связей фокусных категорий;
    4. пересборка `order_fact`;
    5. пересборка `guest_restaurant_daily_category_fact`;
    6. пересборка `guest_restaurant_window_metrics`.
    """

    help = (
        "Оркестратор полного OLAP-контура: "
        "olap_sync -> catalogs -> resolved -> order_fact -> daily_category -> window_metrics."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_stop = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Выполнить один проход контура и завершить процесс.",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=900.0,
            help="Пауза между проходами в loop-режиме.",
        )
        parser.add_argument(
            "--continue-on-step-error",
            action="store_true",
            help="Не останавливать весь контур при ошибке шага, переходить к следующему шагу.",
        )

        # Шаг 1: journal -> raw (OLAP sync)
        parser.add_argument("--skip-olap-sync", action="store_true")
        parser.add_argument("--olap-claim-limit", type=int, default=200)
        parser.add_argument(
            "--olap-portion-size",
            type=int,
            default=int(getattr(settings, "IIKO_OLAP_PORTION_SIZE", 200)),
        )
        parser.add_argument("--olap-max-attempts", type=int, default=5)
        parser.add_argument("--olap-retry-base-seconds", type=int, default=120)
        parser.add_argument("--olap-lock-timeout-seconds", type=int, default=900)

        # Шаг 2-5: common filters by raw/data window
        parser.add_argument("--raw-line-id-from", type=int, default=None)
        parser.add_argument("--raw-line-id-to", type=int, default=None)
        parser.add_argument("--business-date-from", type=str, default=None)
        parser.add_argument("--business-date-to", type=str, default=None)
        parser.add_argument("--batch-size", type=int, default=2000)

        # Шаг 2-3: catalogs + resolved
        parser.add_argument("--skip-catalog-sync", action="store_true")
        parser.add_argument("--skip-resolved-rebuild", action="store_true")
        parser.add_argument("--focus-code", dest="focus_codes", action="append", default=[])

        # Шаг 4: order_fact
        parser.add_argument("--skip-order-fact", action="store_true")

        # Шаг 5: daily facts
        parser.add_argument("--skip-daily-fact", action="store_true")

        # Шаг 6: window metrics
        parser.add_argument("--skip-window-metrics", action="store_true")
        parser.add_argument("--as-of-date", type=str, default=None)
        parser.add_argument("--window-days", action="append", default=[])
        parser.add_argument("--department-id", type=str, default=None)

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        logger.info(
            "Получен сигнал %s, run_olap_pipeline завершится после текущего прохода.",
            signum,
        )

    def _sleep_with_stop(self, total_seconds: float) -> None:
        remaining = max(0.0, float(total_seconds))
        while remaining > 0 and not self.should_stop:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    def _run_step(
        self,
        *,
        title: str,
        run_callable: Callable[[], None],
        continue_on_error: bool,
    ) -> bool:
        try:
            run_callable()
            return True
        except Exception:
            logger.exception("OLAP pipeline: ошибка шага '%s'", title)
            if continue_on_error:
                self.stdout.write(self.style.WARNING(f"[pipeline] step={title} failed, continue=true"))
                return False
            raise

    def handle(self, *args, **options):
        self._setup_signal_handlers()

        once_mode = bool(options["once"])
        sleep_seconds = max(1.0, float(options["sleep_seconds"]))
        continue_on_error = bool(options["continue_on_step_error"])
        batch_size = max(100, int(options["batch_size"]))
        business_date_from = _parse_date(options["business_date_from"])
        business_date_to = _parse_date(options["business_date_to"])
        focus_codes = _normalize_focus_codes(options["focus_codes"])
        window_days = _normalize_windows(options["window_days"])
        target_as_of_date = _parse_date(options["as_of_date"])

        self.stdout.write(self.style.SUCCESS("Запущен run_olap_pipeline"))
        self.stdout.write(f"mode={'once' if once_mode else 'loop'}")
        self.stdout.write(f"continue_on_step_error={continue_on_error}")
        self.stdout.write(f"sleep_seconds={sleep_seconds}")
        self.stdout.write(
            f"raw_line_id_from={options['raw_line_id_from']} raw_line_id_to={options['raw_line_id_to']}"
        )
        self.stdout.write(
            f"business_date_from={options['business_date_from']} business_date_to={options['business_date_to']}"
        )
        self.stdout.write(f"batch_size={batch_size}")

        olap_client = None
        olap_worker = None
        if not bool(options["skip_olap_sync"]):
            olap_client = build_iiko_olap_client_from_settings()
            olap_worker = OlapCheckSyncWorkerService(
                client=olap_client,
                claim_limit=max(1, int(options["olap_claim_limit"])),
                portion_size=max(1, int(options["olap_portion_size"])),
                max_attempts=max(1, int(options["olap_max_attempts"])),
                retry_base_seconds=max(1, int(options["olap_retry_base_seconds"])),
                lock_timeout_seconds=max(60, int(options["olap_lock_timeout_seconds"])),
            )

        try:
            while not self.should_stop:
                self.stdout.write("[pipeline] iteration-start")

                if olap_worker is not None and not bool(options["skip_olap_sync"]):
                    def _run_olap_sync_step() -> None:
                        stats = olap_worker.run_iteration()
                        self.stdout.write(
                            (
                                "[olap_sync] claimed={claimed} recovered={recovered} groups={groups} "
                                "loaded={loaded} retry={retry} failed={failed} skipped={skipped} "
                                "raw_created={raw_created} raw_dup={raw_dup}"
                            ).format(
                                claimed=stats.claimed_rows,
                                recovered=stats.recovered_stale_rows,
                                groups=stats.processed_groups,
                                loaded=stats.loaded_rows,
                                retry=stats.retry_rows,
                                failed=stats.failed_rows,
                                skipped=stats.skipped_rows,
                                raw_created=stats.raw_rows_created,
                                raw_dup=stats.raw_rows_duplicates,
                            )
                        )

                    self._run_step(
                        title="olap_sync",
                        run_callable=_run_olap_sync_step,
                        continue_on_error=continue_on_error,
                    )

                if not bool(options["skip_catalog_sync"]):
                    def _run_catalog_step() -> None:
                        stats = sync_olap_catalogs_from_raw_lines(
                            raw_line_id_from=options["raw_line_id_from"],
                            raw_line_id_to=options["raw_line_id_to"],
                            batch_size=batch_size,
                        )
                        self.stdout.write(
                            (
                                "[catalog] scanned={scanned} categories(created={c_created}, updated={c_updated}) "
                                "nomenclature(created={n_created}, updated={n_updated}) "
                                "skip_cat={skip_cat} skip_nom={skip_nom}"
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

                    self._run_step(
                        title="catalog_sync",
                        run_callable=_run_catalog_step,
                        continue_on_error=continue_on_error,
                    )

                if not bool(options["skip_resolved_rebuild"]):
                    def _run_resolved_step() -> None:
                        stats = rebuild_focus_category_nomenclature_resolved(
                            focus_codes=focus_codes,
                        )
                        self.stdout.write(
                            (
                                "[resolved] scanned={scanned} rebuilt={rebuilt} disabled_cleared={disabled} "
                                "written={written} deleted={deleted} skipped_invalid={skipped}"
                            ).format(
                                scanned=stats.scanned_focus_categories,
                                rebuilt=stats.rebuilt_focus_categories,
                                disabled=stats.disabled_focus_categories_cleared,
                                written=stats.written_links,
                                deleted=stats.deleted_links,
                                skipped=stats.skipped_invalid_focus_categories,
                            )
                        )

                    self._run_step(
                        title="resolved_rebuild",
                        run_callable=_run_resolved_step,
                        continue_on_error=continue_on_error,
                    )

                if not bool(options["skip_order_fact"]):
                    def _run_order_fact_step() -> None:
                        stats = rebuild_order_fact_from_raw_lines(
                            raw_line_id_from=options["raw_line_id_from"],
                            raw_line_id_to=options["raw_line_id_to"],
                            business_date_from=business_date_from,
                            business_date_to=business_date_to,
                            batch_size=batch_size,
                        )
                        self.stdout.write(
                            (
                                "[order_fact] scanned={scanned} grouped={grouped} skipped={skipped} "
                                "created={created} updated={updated}"
                            ).format(
                                scanned=stats.scanned_raw_lines,
                                grouped=stats.grouped_orders,
                                skipped=stats.skipped_invalid_lines,
                                created=stats.created_facts,
                                updated=stats.updated_facts,
                            )
                        )

                    self._run_step(
                        title="order_fact",
                        run_callable=_run_order_fact_step,
                        continue_on_error=continue_on_error,
                    )

                if not bool(options["skip_daily_fact"]):
                    def _run_daily_fact_step() -> None:
                        stats = rebuild_daily_category_fact_from_raw_lines(
                            raw_line_id_from=options["raw_line_id_from"],
                            raw_line_id_to=options["raw_line_id_to"],
                            business_date_from=business_date_from,
                            business_date_to=business_date_to,
                            batch_size=batch_size,
                        )
                        self.stdout.write(
                            (
                                "[daily_category] scanned={scanned} grouped={grouped} without_mapping={without_mapping} "
                                "created={created} updated={updated} deleted={deleted}"
                            ).format(
                                scanned=stats.scanned_raw_lines,
                                grouped=stats.grouped_rows,
                                without_mapping=stats.lines_without_focus_mapping,
                                created=stats.created_rows,
                                updated=stats.updated_rows,
                                deleted=getattr(stats, "deleted_rows", 0),
                            )
                        )

                    self._run_step(
                        title="daily_fact",
                        run_callable=_run_daily_fact_step,
                        continue_on_error=continue_on_error,
                    )

                if not bool(options["skip_window_metrics"]):
                    def _run_window_step() -> None:
                        stats = rebuild_window_metrics_from_daily_facts(
                            as_of_date=target_as_of_date,
                            window_days=window_days,
                            department_id=options["department_id"],
                            batch_size=batch_size,
                        )
                        self.stdout.write(
                            (
                                "[window_metrics] as_of={as_of} windows={windows} scanned={scanned} grouped={grouped} "
                                "created={created} updated={updated}"
                            ).format(
                                as_of=stats.as_of_date,
                                windows=stats.windows_processed,
                                scanned=stats.scanned_daily_rows,
                                grouped=stats.grouped_rows,
                                created=stats.created_rows,
                                updated=stats.updated_rows,
                            )
                        )

                    self._run_step(
                        title="window_metrics",
                        run_callable=_run_window_step,
                        continue_on_error=continue_on_error,
                    )

                self.stdout.write("[pipeline] iteration-done")

                if once_mode or self.should_stop:
                    break
                self._sleep_with_stop(sleep_seconds)
        finally:
            if olap_client is not None:
                olap_client.close()
