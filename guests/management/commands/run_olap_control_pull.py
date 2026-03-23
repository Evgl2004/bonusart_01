import logging
import signal
import time
from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from guests.services.iiko_olap_client import build_iiko_olap_client_from_settings
from guests.services.olap_control_pull import (
    OlapControlPullOptions,
    OlapControlPullService,
)

logger = logging.getLogger(__name__)


def _parse_date(raw_value: str, *, arg_name: str) -> date:
    try:
        return date.fromisoformat(str(raw_value).strip())
    except ValueError as exc:
        raise CommandError(f"Некорректная дата в {arg_name}: {raw_value!r}. Ожидается YYYY-MM-DD.") from exc


def _normalize_department_ids(raw_values: list[str]) -> set[str]:
    result: set[str] = set()
    for raw_value in raw_values:
        safe_value = str(raw_value or "").strip()
        if safe_value:
            result.add(safe_value)
    return result


class Command(BaseCommand):
    """
    Контрольная дозагрузка задач в OLAP-журнал по прямому OLAP-срезу.

    Режим:
    1. one-shot: получить заказы по Department.Id за диапазон дат;
    2. поставить недостающие записи в `olap_check_sync_journal`;
    3. дальше штатный `run_olap_sync_worker` поднимет raw/facts.
    """

    help = (
        "Контрольная постановка OLAP-задач по данным прямого OLAP-среза "
        "(идемпотентно, с dry-run режимом)."
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
            default=900.0,
            help="Пауза между циклами в loop-режиме.",
        )
        parser.add_argument(
            "--business-date-from",
            type=str,
            default="",
            help="Нижняя граница business_date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--business-date-to",
            type=str,
            default="",
            help="Верхняя граница business_date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--tail-days",
            type=int,
            default=max(1, int(getattr(settings, "OLAP_CONTROL_PULL_SCHEDULE_TAIL_DAYS", 2))),
            help="Если даты не указаны явно, использовать хвост последних N дней (по localdate).",
        )
        parser.add_argument(
            "--department-id",
            action="append",
            default=[],
            help="Ограничить контрольную дозагрузку выбранным Department.Id (можно указывать несколько раз).",
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="Только расчёт статистики без записи в БД.",
        )
        parser.add_argument(
            "--write",
            dest="dry_run",
            action="store_false",
            help="Включить запись в БД (создание задач в журнале).",
        )
        parser.set_defaults(dry_run=None)

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        logger.info(
            "Получен сигнал %s, run_olap_control_pull завершится после текущего цикла.",
            signum,
        )

    def _sleep_with_stop(self, total_seconds: float) -> None:
        remaining = max(0.0, float(total_seconds))
        while remaining > 0 and not self.should_stop:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    @staticmethod
    def _resolve_date_window(*, options: dict) -> tuple[date, date]:
        raw_from = str(options["business_date_from"] or "").strip()
        raw_to = str(options["business_date_to"] or "").strip()

        if raw_from or raw_to:
            if not (raw_from and raw_to):
                raise CommandError("Для явного диапазона нужно передать оба параметра: --business-date-from и --business-date-to.")
            date_from = _parse_date(raw_from, arg_name="--business-date-from")
            date_to = _parse_date(raw_to, arg_name="--business-date-to")
        else:
            tail_days = max(1, int(options["tail_days"]))
            date_to = timezone.localdate()
            date_from = date_to - timedelta(days=tail_days - 1)

        if date_from > date_to:
            raise CommandError("--business-date-from не может быть больше --business-date-to.")
        return date_from, date_to

    def _build_service_options(self, *, options: dict) -> OlapControlPullOptions:
        date_from, date_to = self._resolve_date_window(options=options)
        department_ids = _normalize_department_ids(options["department_id"])
        if options["dry_run"] is None:
            dry_run = bool(getattr(settings, "OLAP_CONTROL_PULL_SCHEDULE_DRY_RUN", True))
        else:
            dry_run = bool(options["dry_run"])

        return OlapControlPullOptions(
            business_date_from=date_from,
            business_date_to=date_to,
            department_ids=(department_ids or None),
            dry_run=dry_run,
        )

    def _print_stats(self, *, stats) -> None:
        self.stdout.write(
            (
                "[control_pull] departments={departments} failed_departments={failed_departments} "
                "olap_rows={rows} with_phone={with_phone} without_phone={without_phone} "
                "unknown_guest_phone={unknown_guest_phone} phone_fields={phone_fields} "
                "distinct_orders={orders} skipped_invalid={skipped} "
                "would_create={would_create} created={created} duplicates={duplicates}"
            ).format(
                departments=stats.departments_scanned,
                failed_departments=stats.departments_failed,
                rows=stats.olap_rows_seen,
                with_phone=stats.olap_rows_with_phone,
                without_phone=stats.olap_rows_without_phone,
                unknown_guest_phone=stats.olap_rows_phone_without_guest,
                phone_fields=",".join(sorted(stats.phone_fields_used)) if stats.phone_fields_used else "-",
                orders=stats.distinct_order_keys_seen,
                skipped=stats.skipped_invalid_rows,
                would_create=stats.would_create_journal_rows,
                created=stats.created_journal_rows,
                duplicates=stats.duplicate_journal_rows,
            )
        )

    def handle(self, *args, **options):
        self._setup_signal_handlers()

        once_mode = bool(options["once"])
        sleep_seconds = max(1.0, float(options["sleep_seconds"]))
        service_options = self._build_service_options(options=options)

        self.stdout.write(self.style.SUCCESS("Запущен run_olap_control_pull"))
        self.stdout.write(f"mode={'once' if once_mode else 'loop'}")
        self.stdout.write(f"dry_run={service_options.dry_run}")
        self.stdout.write(
            f"business_date_from={service_options.business_date_from} "
            f"business_date_to={service_options.business_date_to}"
        )
        self.stdout.write(
            f"department_ids={sorted(service_options.department_ids) if service_options.department_ids else 'ALL_ACTIVE'}"
        )

        client = build_iiko_olap_client_from_settings()
        service = OlapControlPullService(client=client)
        try:
            if once_mode:
                stats = service.run_cycle(options=service_options)
                self._print_stats(stats=stats)
                return

            while not self.should_stop:
                stats = service.run_cycle(options=service_options)
                self._print_stats(stats=stats)
                if self.should_stop:
                    break
                self._sleep_with_stop(sleep_seconds)
        finally:
            client.close()
