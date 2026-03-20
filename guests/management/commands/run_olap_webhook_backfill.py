import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from guests.services.olap_webhook_backfill import (
    OlapWebhookBackfillOptions,
    OlapWebhookBackfillService,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Воркер исторического прогона webhook -> olap_check_sync_journal.

    Режимы:
    1. `--once` — один цикл загрузки страниц;
    2. цикл `loop` — фоновая обработка с паузой и graceful shutdown;
    3. `dry-run` — проверка без записи в БД.
    """

    help = (
        "Исторический прогон webhook из внутреннего API в журнал OLAP-синхронизации "
        "(порциями, с фильтрами и защитой backpressure)."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_stop = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Выполнить один цикл и завершить процесс.",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=float(getattr(settings, "OLAP_BACKFILL_SLEEP_BETWEEN_CYCLES_SECONDS", 20.0)),
            help="Пауза между циклами в loop-режиме.",
        )
        parser.add_argument(
            "--force-run",
            action="store_true",
            help="Запустить команду даже если OLAP_BACKFILL_ENABLE=False.",
        )
        parser.add_argument(
            "--date-from",
            type=str,
            default=str(getattr(settings, "OLAP_BACKFILL_DATE_FROM", "") or "").strip(),
            help="Нижняя граница выборки webhook (ISO-8601).",
        )
        parser.add_argument(
            "--date-to",
            type=str,
            default=str(getattr(settings, "OLAP_BACKFILL_DATE_TO", "") or "").strip(),
            help="Верхняя граница выборки webhook (ISO-8601).",
        )
        parser.add_argument(
            "--page-size",
            type=int,
            default=int(getattr(settings, "OLAP_BACKFILL_PAGE_SIZE", 100)),
            help="Размер страницы при запросе webhook из внутреннего API.",
        )
        parser.add_argument(
            "--max-pages-per-cycle",
            type=int,
            default=int(getattr(settings, "OLAP_BACKFILL_MAX_PAGES_PER_CYCLE", 5)),
            help="Максимум страниц за один цикл обработки.",
        )
        parser.add_argument(
            "--sleep-between-pages-seconds",
            type=float,
            default=float(getattr(settings, "OLAP_BACKFILL_SLEEP_BETWEEN_PAGES_SECONDS", 1.0)),
            help="Пауза между запросами соседних страниц webhook API.",
        )
        parser.add_argument(
            "--pause-queue-gt",
            type=int,
            default=int(getattr(settings, "OLAP_BACKFILL_PAUSE_QUEUE_GT", 5000)),
            help="Включить backpressure, если глубина new/retry больше этого порога.",
        )
        parser.add_argument(
            "--resume-queue-lt",
            type=int,
            default=int(getattr(settings, "OLAP_BACKFILL_RESUME_QUEUE_LT", 2000)),
            help="Снять backpressure, когда глубина очереди станет меньше этого порога.",
        )
        parser.add_argument(
            "--status",
            dest="statuses",
            action="append",
            default=[],
            help="Фильтр query-параметра status (можно передать несколько раз).",
        )
        parser.add_argument(
            "--business-status",
            dest="business_statuses",
            action="append",
            default=[],
            help="Фильтр query-параметра business_status (можно передать несколько раз).",
        )
        parser.add_argument(
            "--category-id-ext",
            dest="category_external_ids",
            action="append",
            default=[],
            help="Фильтр query-параметра category_id_ext (можно передать несколько раз).",
        )
        parser.add_argument(
            "--notification-type",
            dest="notification_types",
            action="append",
            default=[],
            help="Разрешенный notificationType (можно передать несколько раз).",
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="Не писать в БД, только считать статистику (принудительно).",
        )
        parser.add_argument(
            "--write",
            dest="dry_run",
            action="store_false",
            help="Включить запись в БД (принудительно).",
        )
        parser.set_defaults(dry_run=None)

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        logger.info(
            "Получен сигнал %s, run_olap_webhook_backfill завершится после текущего цикла.",
            signum,
        )

    @staticmethod
    def _normalize_notification_types(raw_values: list[str]) -> set[int]:
        parsed: set[int] = set()
        for raw_value in raw_values:
            try:
                parsed.add(int(str(raw_value).strip()))
            except (TypeError, ValueError):
                continue
        return parsed

    @staticmethod
    def _normalize_csv_items(raw_values: list[str]) -> list[str]:
        result: list[str] = []
        for value in raw_values:
            safe_value = str(value or "").strip()
            if safe_value:
                result.append(safe_value)
        return result

    def _build_backfill_options(self, *, options: dict) -> OlapWebhookBackfillOptions:
        if options["dry_run"] is None:
            dry_run = bool(getattr(settings, "OLAP_BACKFILL_DRY_RUN", True))
        else:
            dry_run = bool(options["dry_run"])

        cli_notification_types = self._normalize_notification_types(options["notification_types"])
        if cli_notification_types:
            allowed_notification_types = cli_notification_types
        else:
            allowed_notification_types = set(
                getattr(settings, "OLAP_BRIDGE_ALLOWED_NOTIFICATION_TYPES", {1}) or {1}
            )

        return OlapWebhookBackfillOptions(
            dry_run=dry_run,
            date_from=str(options["date_from"] or "").strip(),
            date_to=(str(options["date_to"] or "").strip() or None),
            page_size=max(1, int(options["page_size"])),
            max_pages_per_cycle=max(1, int(options["max_pages_per_cycle"])),
            sleep_between_pages_seconds=max(0.0, float(options["sleep_between_pages_seconds"])),
            pause_queue_gt=max(1, int(options["pause_queue_gt"])),
            resume_queue_lt=max(0, int(options["resume_queue_lt"])),
            statuses=self._normalize_csv_items(options["statuses"]),
            business_statuses=self._normalize_csv_items(options["business_statuses"]),
            category_external_ids=self._normalize_csv_items(options["category_external_ids"]),
            allowed_notification_types=allowed_notification_types,
        )

    def _print_iteration_stats(self, *, stats) -> None:
        self.stdout.write(
            (
                "[backfill] queue_depth={queue_depth} paused={paused} pages={pages} seen={seen} "
                "filtered_type={filtered} skipped_no_order={skip_no_order} "
                "dry_would_enqueue={would_enqueue} created={created} duplicates={duplicates} "
                "other_skipped={other_skipped} errors={errors}"
            ).format(
                queue_depth=stats.queue_depth,
                paused=stats.paused_by_backpressure,
                pages=stats.pages_fetched,
                seen=stats.webhooks_seen,
                filtered=stats.filtered_by_notification_type,
                skip_no_order=stats.skipped_without_order_number,
                would_enqueue=stats.would_enqueue,
                created=stats.created_rows,
                duplicates=stats.duplicate_rows,
                other_skipped=stats.other_skipped_rows,
                errors=stats.processing_errors,
            )
        )

    def _sleep_with_stop(self, total_seconds: float) -> None:
        remaining = max(0.0, float(total_seconds))
        while remaining > 0 and not self.should_stop:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    def handle(self, *args, **options):
        self._setup_signal_handlers()

        backfill_enabled = bool(getattr(settings, "OLAP_BACKFILL_ENABLE", False))
        if not backfill_enabled and not bool(options["force_run"]):
            raise CommandError(
                "OLAP backfill отключен (OLAP_BACKFILL_ENABLE=False). "
                "Для ручного запуска используйте --force-run."
            )

        base_url = str(getattr(settings, "SAGUR_BASE_URL", "") or "").strip()
        username = str(getattr(settings, "SAGUR_USERNAME", "") or "").strip()
        password = str(getattr(settings, "SAGUR_PASSWORD", "") or "").strip()
        if not base_url or not username or not password:
            raise CommandError(
                "Не заданы SAGUR_BASE_URL/SAGUR_USERNAME/SAGUR_PASSWORD в окружении."
            )

        backfill_options = self._build_backfill_options(options=options)
        once_mode = bool(options["once"])
        sleep_seconds = max(1.0, float(options["sleep_seconds"]))

        self.stdout.write(self.style.SUCCESS("Запущен run_olap_webhook_backfill"))
        self.stdout.write(f"mode={'once' if once_mode else 'loop'}")
        self.stdout.write(f"dry_run={backfill_options.dry_run}")
        self.stdout.write(f"date_from={backfill_options.date_from}")
        self.stdout.write(f"date_to={backfill_options.date_to}")
        self.stdout.write(f"page_size={backfill_options.page_size}")
        self.stdout.write(f"max_pages_per_cycle={backfill_options.max_pages_per_cycle}")
        self.stdout.write(
            f"allowed_notification_types={sorted(backfill_options.allowed_notification_types)}"
        )
        self.stdout.write(
            f"backpressure pause_gt={backfill_options.pause_queue_gt} "
            f"resume_lt={backfill_options.resume_queue_lt}"
        )

        service = OlapWebhookBackfillService(
            base_url=base_url,
            username=username,
            password=password,
            auth_timeout_seconds=float(getattr(settings, "OLAP_BACKFILL_AUTH_TIMEOUT_SECONDS", 10.0)),
            request_timeout_seconds=float(getattr(settings, "OLAP_BACKFILL_REQUEST_TIMEOUT_SECONDS", 20.0)),
        )

        try:
            if once_mode:
                stats = service.run_cycle(
                    options=backfill_options,
                    stop_requested=lambda: self.should_stop,
                )
                self._print_iteration_stats(stats=stats)
                return

            while not self.should_stop:
                stats = service.run_cycle(
                    options=backfill_options,
                    stop_requested=lambda: self.should_stop,
                )
                self._print_iteration_stats(stats=stats)
                if self.should_stop:
                    break
                self._sleep_with_stop(sleep_seconds)
        finally:
            service.close()
