import logging
import signal
import time
from datetime import date

from django.core.management.base import BaseCommand

from guests.services.daily_category_fact import rebuild_daily_category_fact_from_raw_lines

logger = logging.getLogger(__name__)


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text)


class Command(BaseCommand):
    """
    Синхронизирует дневной слой `guest_restaurant_daily_category_fact`.
    """

    help = (
        "Пересчитывает дневной слой категорий по гостям и заведениям. "
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
            default=1800.0,
            help="Пауза между проходами в циклическом режиме.",
        )
        parser.add_argument(
            "--raw-line-id-from",
            type=int,
            default=None,
            help="Нижняя граница id сырых строк для пересчёта.",
        )
        parser.add_argument(
            "--raw-line-id-to",
            type=int,
            default=None,
            help="Верхняя граница id сырых строк для пересчёта.",
        )
        parser.add_argument(
            "--business-date-from",
            type=str,
            default=None,
            help="Нижняя граница бизнес-даты (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--business-date-to",
            type=str,
            default=None,
            help="Верхняя граница бизнес-даты (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=2000,
            help="Размер порции чтения сырых строк.",
        )

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        logger.info(
            "Получен сигнал %s, sync_daily_category_fact завершится после текущего прохода.",
            signum,
        )

    def _sleep_with_stop(self, total_seconds: float) -> None:
        remaining = max(0.0, float(total_seconds))
        while remaining > 0 and not self.should_stop:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    def _run_once(self, *, options) -> None:
        business_date_from = _parse_date(options["business_date_from"])
        business_date_to = _parse_date(options["business_date_to"])
        stats = rebuild_daily_category_fact_from_raw_lines(
            raw_line_id_from=options["raw_line_id_from"],
            raw_line_id_to=options["raw_line_id_to"],
            business_date_from=business_date_from,
            business_date_to=business_date_to,
            batch_size=max(100, int(options["batch_size"])),
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

    def handle(self, *args, **options):
        self._setup_signal_handlers()
        once_mode = bool(options["once"])
        sleep_seconds = max(1.0, float(options["sleep_seconds"]))

        self.stdout.write(self.style.SUCCESS("Запущен sync_daily_category_fact"))
        self.stdout.write(f"mode={'once' if once_mode else 'loop'}")
        self.stdout.write(f"sleep_seconds={sleep_seconds}")
        self.stdout.write(
            f"raw_line_id_from={options['raw_line_id_from']} raw_line_id_to={options['raw_line_id_to']}"
        )
        self.stdout.write(
            f"business_date_from={options['business_date_from']} business_date_to={options['business_date_to']}"
        )
        self.stdout.write(f"batch_size={max(100, int(options['batch_size']))}")

        if once_mode:
            self._run_once(options=options)
            return

        while not self.should_stop:
            self._run_once(options=options)
            if self.should_stop:
                break
            self._sleep_with_stop(sleep_seconds)
