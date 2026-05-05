"""
Команда фонового воркера массовых рассылок.

Новая (целевая) архитектура:
1. `mailing_worker` больше НЕ отправляет сообщения напрямую в Telegram;
2. воркер только ставит строки `MailingGuest` в универсальную очередь `DispatchTask`;
3. фактическая доставка выполняется провайдерными async-воркерами.

Legacy direct-send путь удалён специально, чтобы исключить двойную логику
маршрутизации и отправки.
"""

import logging
import time
import traceback

from django.core.management.base import BaseCommand
from django.db import connections
from django.db import transaction
from django.utils import timezone

from guests.models import Mailing, MailingGuest
from guests.services.universal_queue import enqueue_mailing_rows_as_dispatch_tasks

logger = logging.getLogger(__name__)

# Пауза между итерациями воркера при отсутствии задач.
SLEEP_SECONDS = 3

# Сколько строк MailingGuest забираем за одну транзакцию из конкретной рассылки.
BATCH_SIZE = 10


class Command(BaseCommand):
    """
    Циклический producer массовых рассылок для универсальной очереди.
    """

    help = "Worker: enqueue active mailing rows to universal DispatchTask queue."

    EXIT_SUCCESS = 0
    EXIT_FAILURE = 1

    def add_arguments(self, parser):
        parser.add_argument(
            "--health-check",
            action="store_true",
            help="Лёгкая проверка здоровья воркера без запуска бесконечного цикла.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Печать расширенных показателей для health-check.",
        )

    def handle(self, *args, **options):
        if options.get("health_check"):
            return self._run_health_check(verbose=bool(options.get("verbose")))

        self.stdout.write("=== Mailing worker (dispatch-only) started ===")

        while True:
            try:
                processed = run_iteration()
                if processed == 0:
                    self.stdout.write(f"[worker] nothing to process -> sleep {SLEEP_SECONDS}s\n")
                    time.sleep(SLEEP_SECONDS)
                else:
                    self.stdout.write(f"[worker] processed rows this iteration: {processed}\n")
            except KeyboardInterrupt:
                self.stdout.write("\n=== Mailing worker stopped (KeyboardInterrupt) ===")
                return
            except Exception as err:
                self.stdout.write(f"[worker] CRASH in iteration: {err}")
                traceback.print_exc()
                time.sleep(SLEEP_SECONDS)

    def _run_health_check(self, *, verbose: bool = False) -> None:
        """
        Лёгкий health-check без тяжёлой нагрузки:
        1. БД: SELECT 1;
        2. короткие exists-проверки по ключевым статусам MailingGuest.
        """
        try:
            connection = connections["default"]
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

            active_mailings = Mailing.objects.filter(is_active=True).exists()
            planned_exists = MailingGuest.objects.filter(status=MailingGuest.Status.PLANNED).exists()
            in_progress_exists = MailingGuest.objects.filter(status=MailingGuest.Status.IN_PROGRESS).exists()
            error_exists = MailingGuest.objects.filter(status=MailingGuest.Status.ERROR).exists()

            self.stdout.write(self.style.SUCCESS("[health] status=healthy component=mailing_worker"))
            if verbose:
                self.stdout.write(
                    "[health] active_mailings=%s planned_exists=%s in_progress_exists=%s error_exists=%s"
                    % (active_mailings, planned_exists, in_progress_exists, error_exists)
                )
            raise SystemExit(self.EXIT_SUCCESS)
        except SystemExit:
            raise
        except Exception as err:
            self.stdout.write(
                self.style.ERROR(f"[health] status=unhealthy component=mailing_worker error={err}")
            )
            raise SystemExit(self.EXIT_FAILURE)


def run_iteration() -> int:
    """
    Выполняет один проход по активным рассылкам.

    Шаги:
    1. Реанимирует зависшие `in_progress` строки (если процесс ранее завершился аварийно).
    2. Берёт активные рассылки в рамках временного окна.
    3. Для каждой рассылки ставит готовые строки в `DispatchTask`.
    """
    now = timezone.now()
    print(f"[iter] start now={now.isoformat()}")

    # Возвращаем зависшие строки обратно в planned.
    stuck_qs = MailingGuest.objects.filter(status=MailingGuest.Status.IN_PROGRESS)
    stuck_count = stuck_qs.count()
    if stuck_count > 0:
        print(f"[requeue] Found {stuck_count} stuck IN_PROGRESS rows. Reverting to PLANNED.")
        stuck_qs.update(
            status=MailingGuest.Status.PLANNED,
            delivery_status="requeued",
        )

    mailings_qs = (
        Mailing.objects.filter(
            is_active=True,
            scheduled_time_begin__lte=now,
            scheduled_time_end__gte=now,
        )
        .order_by("id")
    )

    mailings_count = mailings_qs.count()
    print(f"[iter] active mailings in time window: {mailings_count}")

    total = 0
    for mailing in mailings_qs:
        print(f"\n[mailing] id={mailing.id} name={getattr(mailing, 'name', None)}")
        total += process_one_mailing(mailing=mailing, now=now)

    return total


def process_one_mailing(mailing: Mailing, now) -> int:
    """
    Обрабатывает одну рассылку и ставит её строки в универсальную очередь.

    Возвращает количество строк, которые были взяты в обработку в рамках итерации.
    """
    local_now = timezone.localtime(now)
    current_time = local_now.time()

    if not (mailing.send_window_begin <= current_time <= mailing.send_window_end):
        print(
            f"[mailing:{mailing.id}] outside send window "
            f"{mailing.send_window_begin}-{mailing.send_window_end}, skip"
        )
        return 0

    selected_bots = list(mailing.bot_profiles.filter(is_active=True).order_by("provider_type", "id"))
    print(f"[mailing:{mailing.id}] selected bot profiles: {len(selected_bots)}")
    for bot in selected_bots:
        print(f"  - bot id={bot.id} provider={bot.provider_type} code={bot.code}")

    if not selected_bots:
        updated = MailingGuest.objects.filter(
            mailing=mailing,
            status=MailingGuest.Status.PLANNED,
        ).update(
            status=MailingGuest.Status.ERROR,
            delivery_status="no_bot_profiles",
            error_description="В рассылке не выбраны активные профили ботов.",
        )
        print(f"[mailing:{mailing.id}] NO BOT PROFILES -> marked ERROR rows: {updated}")
        return 0

    with transaction.atomic():
        rows = list(
            MailingGuest.objects.select_for_update()
            .filter(
                mailing=mailing,
                status=MailingGuest.Status.PLANNED,
                scheduled_datetime__lte=now,
            )
            .order_by("id")[:BATCH_SIZE]
        )

        print(f"[mailing:{mailing.id}] ready planned rows (<=now): {len(rows)}")
        if not rows:
            return 0

        ids = [row.id for row in rows]
        MailingGuest.objects.filter(id__in=ids).update(status=MailingGuest.Status.IN_PROGRESS)
        print(f"[mailing:{mailing.id}] moved to IN_PROGRESS ids={ids}")

    try:
        summary = enqueue_mailing_rows_as_dispatch_tasks(
            mailing=mailing,
            rows=rows,
            now=now,
        )
        logger.info(
            "mailing enqueue: mailing_id=%s rows_total=%s rows_queued=%s rows_failed=%s tasks_created=%s tasks_duplicates=%s",
            mailing.id,
            summary.rows_total,
            summary.rows_queued,
            summary.rows_failed,
            summary.tasks_created,
            summary.tasks_duplicates,
        )
        print(
            f"[mailing:{mailing.id}] queued_to_dispatch="
            f"{summary.rows_queued}/{summary.rows_total} failed={summary.rows_failed}"
        )
        return summary.rows_total
    except Exception as enqueue_error:
        error_text = f"dispatch_enqueue_exception: {str(enqueue_error)[:1800]}"
        for row in rows:
            row.status = MailingGuest.Status.ERROR
            row.delivery_status = "dispatch_enqueue_exception"
            row.error_description = error_text
            row.save(update_fields=["status", "delivery_status", "error_description"])

        logger.exception(
            "Ошибка постановки dispatch-задач для mailing_id=%s: %s",
            mailing.id,
            enqueue_error,
        )
        print(f"[mailing:{mailing.id}] enqueue failed -> rows marked ERROR")
        return len(rows)
