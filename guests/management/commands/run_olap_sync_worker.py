import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from guests.services.iiko_olap_client import build_iiko_olap_client_from_settings
from guests.services.olap_check_sync import OlapCheckSyncWorkerService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Запускает воркер дозагрузки чеков из `olap_check_sync_journal` в `olap_sales_raw_line`.

    Сценарий работы:
    1. Забирает задачи `new|retry` из журнала синхронизации.
    2. Запрашивает iiko OLAP порциями по `order_number`.
    3. Идемпотентно записывает строки чека в сырой слой.
    4. Обновляет статусы журнала (`loaded|retry|failed|skipped`).
    """

    help = (
        "Воркер дозагрузки чеков из журнала OLAP-синхронизации. "
        "Поддерживает режим одного прохода (--once) и циклический режим."
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
            default=15.0,
            help="Пауза между проходами в циклическом режиме.",
        )
        parser.add_argument(
            "--claim-limit",
            type=int,
            default=200,
            help="Максимум строк журнала, забираемых за один проход.",
        )
        parser.add_argument(
            "--portion-size",
            type=int,
            default=int(getattr(settings, "IIKO_OLAP_PORTION_SIZE", 200)),
            help="Размер порции order_number в одном OLAP-запросе.",
        )
        parser.add_argument(
            "--max-attempts",
            type=int,
            default=5,
            help="Максимальное количество попыток обработки одной строки журнала.",
        )
        parser.add_argument(
            "--retry-base-seconds",
            type=int,
            default=120,
            help="Базовая задержка retry (далее применяется экспоненциальное увеличение).",
        )
        parser.add_argument(
            "--lock-timeout-seconds",
            type=int,
            default=900,
            help="Тайм-аут блокировки для реанимации зависших in_progress строк.",
        )

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        logger.info(
            "Получен сигнал %s, run_olap_sync_worker завершится после текущего прохода.",
            signum,
        )

    def _sleep_with_stop(self, total_seconds: float) -> None:
        remaining = max(0.0, float(total_seconds))
        while remaining > 0 and not self.should_stop:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    def _print_iteration_stats(self, stats) -> None:
        self.stdout.write(
            (
                "[iteration] claimed={claimed} recovered_stale={recovered} groups={groups} "
                "loaded={loaded} retry={retry} failed={failed} skipped={skipped} "
                "raw(planned={raw_planned}, created={raw_created}, duplicates={raw_dup}) "
                "portions(requested={p_requested}, success={p_success}, failed={p_failed})"
            ).format(
                claimed=stats.claimed_rows,
                recovered=stats.recovered_stale_rows,
                groups=stats.processed_groups,
                loaded=stats.loaded_rows,
                retry=stats.retry_rows,
                failed=stats.failed_rows,
                skipped=stats.skipped_rows,
                raw_planned=stats.raw_rows_planned,
                raw_created=stats.raw_rows_created,
                raw_dup=stats.raw_rows_duplicates,
                p_requested=stats.requested_portions,
                p_success=stats.successful_portions,
                p_failed=stats.failed_portions,
            )
        )

    def handle(self, *args, **options):
        self._setup_signal_handlers()

        once_mode = bool(options["once"])
        sleep_seconds = max(1.0, float(options["sleep_seconds"]))
        claim_limit = max(1, int(options["claim_limit"]))
        portion_size = max(1, int(options["portion_size"]))
        max_attempts = max(1, int(options["max_attempts"]))
        retry_base_seconds = max(1, int(options["retry_base_seconds"]))
        lock_timeout_seconds = max(60, int(options["lock_timeout_seconds"]))

        self.stdout.write(self.style.SUCCESS("Запущен run_olap_sync_worker"))
        self.stdout.write(f"mode={'once' if once_mode else 'loop'}")
        self.stdout.write(f"sleep_seconds={sleep_seconds}")
        self.stdout.write(f"claim_limit={claim_limit}")
        self.stdout.write(f"portion_size={portion_size}")
        self.stdout.write(f"max_attempts={max_attempts}")
        self.stdout.write(f"retry_base_seconds={retry_base_seconds}")
        self.stdout.write(f"lock_timeout_seconds={lock_timeout_seconds}")

        client = build_iiko_olap_client_from_settings()
        worker_service = OlapCheckSyncWorkerService(
            client=client,
            claim_limit=claim_limit,
            portion_size=portion_size,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            lock_timeout_seconds=lock_timeout_seconds,
        )

        try:
            if once_mode:
                stats = worker_service.run_iteration()
                self._print_iteration_stats(stats)
                return

            while not self.should_stop:
                stats = worker_service.run_iteration()
                self._print_iteration_stats(stats)
                if self.should_stop:
                    break
                self._sleep_with_stop(sleep_seconds)
        finally:
            client.close()

