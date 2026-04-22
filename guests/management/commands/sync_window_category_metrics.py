import logging
import signal
import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from guests.services.window_category_metrics import (
    rebuild_window_category_metrics_from_order_facts,
)

logger = logging.getLogger(__name__)


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text)


def _iter_date_range(date_from: date, date_to: date) -> list[date]:
    cursor = date_from
    result: list[date] = []
    while cursor <= date_to:
        result.append(cursor)
        cursor += timedelta(days=1)
    return result


class Command(BaseCommand):
    """
    Синхронизирует таблицу `guest_restaurant_window_category_metrics`.
    """

    help = (
        "Пересчитывает category-window метрики по заказам с выбранными фокусными категориями. "
        "Поддерживает один проход (--once) и циклический режим."
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
            default=3600.0,
            help="Пауза между проходами в циклическом режиме.",
        )
        parser.add_argument(
            "--as-of-date",
            type=str,
            default=None,
            help="Дата среза (YYYY-MM-DD). По умолчанию сегодняшняя локальная дата.",
        )
        parser.add_argument(
            "--window-days",
            action="append",
            default=[],
            help="Размер окна в днях (можно передавать несколько раз).",
        )
        parser.add_argument(
            "--department-id",
            type=str,
            default=None,
            help="Фильтр на одно заведение (Department.Id).",
        )
        parser.add_argument(
            "--business-date-from",
            type=str,
            default=None,
            help="Нижняя граница периода as_of_date для backfill (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--business-date-to",
            type=str,
            default=None,
            help="Верхняя граница периода as_of_date для backfill (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=2000,
            help="Размер порции чтения для сырого и order-level слоя.",
        )

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        logger.info(
            "Получен сигнал %s, sync_window_category_metrics завершится после текущего прохода.",
            signum,
        )

    def _sleep_with_stop(self, total_seconds: float) -> None:
        remaining = max(0.0, float(total_seconds))
        while remaining > 0 and not self.should_stop:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    @staticmethod
    def _normalize_windows(raw_windows: list[str]) -> list[int] | None:
        normalized: list[int] = []
        for raw in raw_windows:
            text = str(raw).strip()
            if not text:
                continue
            value = int(text)
            if value <= 0:
                continue
            if value not in normalized:
                normalized.append(value)
        return normalized or None

    def _resolve_dates(self, *, options: dict) -> list[date]:
        explicit_from = _parse_date(options.get("business_date_from"))
        explicit_to = _parse_date(options.get("business_date_to"))

        if explicit_from is not None or explicit_to is not None:
            if explicit_from is None or explicit_to is None:
                raise CommandError("Для backfill-режима нужно передать оба параметра: --business-date-from и --business-date-to.")
            if explicit_from > explicit_to:
                raise CommandError("--business-date-from не может быть больше --business-date-to.")
            return _iter_date_range(explicit_from, explicit_to)

        as_of_value = _parse_date(options.get("as_of_date")) or timezone.localdate()
        return [as_of_value]

    def _run_once(self, *, options) -> None:
        windows = self._normalize_windows(options["window_days"])
        dates_to_process = self._resolve_dates(options=options)
        safe_batch_size = max(100, int(options["batch_size"]))

        self.stdout.write(
            f"[window_category] dates={len(dates_to_process)} windows={windows or 'DEFAULT'}"
        )
        for target_date in dates_to_process:
            stats = rebuild_window_category_metrics_from_order_facts(
                as_of_date=target_date,
                window_days=windows,
                department_id=options["department_id"],
                batch_size=safe_batch_size,
            )
            self.stdout.write(
                (
                    "[window_category] as_of={as_of} windows={windows} scanned={scanned} grouped={grouped} "
                    "created={created} updated={updated} deleted={deleted} missing_order_facts={missing}"
                ).format(
                    as_of=stats.as_of_date,
                    windows=stats.windows_processed,
                    scanned=stats.scanned_raw_lines,
                    grouped=stats.grouped_rows,
                    created=stats.created_rows,
                    updated=stats.updated_rows,
                    deleted=stats.deleted_rows,
                    missing=stats.missing_order_facts,
                )
            )
            if self.should_stop:
                break

    def handle(self, *args, **options):
        self._setup_signal_handlers()
        once_mode = bool(options["once"])
        sleep_seconds = max(1.0, float(options["sleep_seconds"]))

        self.stdout.write(self.style.SUCCESS("Запущен sync_window_category_metrics"))
        self.stdout.write(f"mode={'once' if once_mode else 'loop'}")
        self.stdout.write(f"sleep_seconds={sleep_seconds}")
        self.stdout.write(f"as_of_date={options['as_of_date']}")
        self.stdout.write(f"business_date_from={options['business_date_from']}")
        self.stdout.write(f"business_date_to={options['business_date_to']}")
        self.stdout.write(f"window_days={options['window_days']}")
        self.stdout.write(f"department_id={options['department_id']}")
        self.stdout.write(f"batch_size={max(100, int(options['batch_size']))}")

        if once_mode:
            self._run_once(options=options)
            return

        while not self.should_stop:
            self._run_once(options=options)
            if self.should_stop:
                break
            self._sleep_with_stop(sleep_seconds)
