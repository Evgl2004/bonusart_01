from __future__ import annotations

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.models import Q
from django.utils import timezone

from guests.models import OlapLivePipelineQueue
from guests.services.iiko_olap_client import build_iiko_olap_client_from_settings
from guests.services.olap_live_pipeline import OlapLivePipelineService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Воркер оперативного OLAP-конвейера по очереди `olap_live_pipeline_queue`.
    """

    help = (
        "Обрабатывает оперативную очередь OLAP: загрузка чека, сборка OrderFact, "
        "синхронизация применённого купона."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_stop = False
        self.exit_success = 0
        self.exit_failure = 1

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Выполнить один проход обработки очереди и завершиться.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=int(getattr(settings, "OLAP_LIVE_PIPELINE_BATCH_SIZE", 20) or 20),
            help="Сколько задач оперативного конвейера брать за один проход.",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=5.0,
            help="Пауза между проходами в циклическом режиме.",
        )
        parser.add_argument(
            "--force-run",
            action="store_true",
            help="Разрешить запуск даже при OLAP_LIVE_PIPELINE_ENABLED=False.",
        )
        parser.add_argument(
            "--health-check",
            action="store_true",
            help="Проверить состояние очереди без обработки задач.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Показать подробный человекочитаемый отчёт для health-check.",
        )

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        logger.info(
            "Оперативный OLAP-конвейер: получен сигнал %s, остановка после текущего прохода.",
            signum,
        )

    def _collect_queue_metrics(self) -> dict[str, int]:
        now = timezone.now()
        active_statuses = [
            OlapLivePipelineQueue.Status.NEW,
            OlapLivePipelineQueue.Status.WAITING_OLAP,
            OlapLivePipelineQueue.Status.OLAP_LOADED,
            OlapLivePipelineQueue.Status.FACT_BUILT,
            OlapLivePipelineQueue.Status.RETRY,
        ]
        metrics = {
            "due": int(
                OlapLivePipelineQueue.objects.filter(
                    status__in=active_statuses,
                )
                .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
                .count()
            ),
            "new": int(OlapLivePipelineQueue.objects.filter(status=OlapLivePipelineQueue.Status.NEW).count()),
            "waiting_olap": int(
                OlapLivePipelineQueue.objects.filter(status=OlapLivePipelineQueue.Status.WAITING_OLAP).count()
            ),
            "olap_loaded": int(
                OlapLivePipelineQueue.objects.filter(status=OlapLivePipelineQueue.Status.OLAP_LOADED).count()
            ),
            "fact_built": int(
                OlapLivePipelineQueue.objects.filter(status=OlapLivePipelineQueue.Status.FACT_BUILT).count()
            ),
            "retry": int(OlapLivePipelineQueue.objects.filter(status=OlapLivePipelineQueue.Status.RETRY).count()),
            "in_progress": int(
                OlapLivePipelineQueue.objects.filter(status=OlapLivePipelineQueue.Status.IN_PROGRESS).count()
            ),
            "done": int(OlapLivePipelineQueue.objects.filter(status=OlapLivePipelineQueue.Status.DONE).count()),
            "skipped": int(
                OlapLivePipelineQueue.objects.filter(status=OlapLivePipelineQueue.Status.SKIPPED).count()
            ),
            "failed": int(OlapLivePipelineQueue.objects.filter(status=OlapLivePipelineQueue.Status.FAILED).count()),
        }
        return metrics

    def _run_health_check(self, *, verbose: bool) -> None:
        try:
            connection = connections["default"]
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

            metrics = self._collect_queue_metrics()
            self.stdout.write(
                self.style.SUCCESS("[health] status=healthy component=run_olap_live_pipeline_worker")
            )
            self.stdout.write(
                f"[health] live_pipeline_enabled={bool(getattr(settings, 'OLAP_LIVE_PIPELINE_ENABLED', False))} "
                f"due={metrics['due']} new={metrics['new']} waiting_olap={metrics['waiting_olap']} "
                f"retry={metrics['retry']} done={metrics['done']} failed={metrics['failed']}"
            )
            if verbose:
                self.stdout.write("Статус: здоров (status=healthy)")
                self.stdout.write(
                    "Компонент: оперативный OLAP-конвейер (component=run_olap_live_pipeline_worker)"
                )
                self.stdout.write("База данных: доступна (db=ok)")
                self.stdout.write(
                    "Контур включён: %s (live_pipeline_enabled=%s)"
                    % (
                        "да" if bool(getattr(settings, "OLAP_LIVE_PIPELINE_ENABLED", False)) else "нет",
                        bool(getattr(settings, "OLAP_LIVE_PIPELINE_ENABLED", False)),
                    )
                )
                self.stdout.write(f"Готово к обработке: {metrics['due']} (due={metrics['due']})")
                self.stdout.write(
                    "Очередь: new=%s waiting_olap=%s olap_loaded=%s fact_built=%s retry=%s "
                    "in_progress=%s done=%s skipped=%s failed=%s "
                    "(new=%s waiting_olap=%s olap_loaded=%s fact_built=%s retry=%s "
                    "in_progress=%s done=%s skipped=%s failed=%s)"
                    % (
                        metrics["new"],
                        metrics["waiting_olap"],
                        metrics["olap_loaded"],
                        metrics["fact_built"],
                        metrics["retry"],
                        metrics["in_progress"],
                        metrics["done"],
                        metrics["skipped"],
                        metrics["failed"],
                        metrics["new"],
                        metrics["waiting_olap"],
                        metrics["olap_loaded"],
                        metrics["fact_built"],
                        metrics["retry"],
                        metrics["in_progress"],
                        metrics["done"],
                        metrics["skipped"],
                        metrics["failed"],
                    )
                )
            raise SystemExit(self.exit_success)
        except SystemExit:
            raise
        except Exception as exc:
            self.stdout.write(
                self.style.ERROR(
                    f"[health] status=unhealthy component=run_olap_live_pipeline_worker error={exc}"
                )
            )
            if verbose:
                self.stdout.write("Статус: нездоров (status=unhealthy)")
                self.stdout.write(f"Ошибка: {exc} (error={exc})")
            raise SystemExit(self.exit_failure)

    @staticmethod
    def _print_stats(stats) -> None:
        summary = stats.to_dict()
        print(
            "[olap_live_pipeline] "
            f"взято={summary['claimed']} (claimed={summary['claimed']}) "
            f"обработано={summary['processed']} (processed={summary['processed']}) "
            f"ожидает_olap={summary['waiting_olap']} (waiting_olap={summary['waiting_olap']}) "
            f"фактов_собрано={summary['facts_built']} (facts_built={summary['facts_built']}) "
            f"купонов_синхронизировано={summary['coupon_synced']} (coupon_synced={summary['coupon_synced']}) "
            f"завершено={summary['done']} (done={summary['done']}) "
            f"повтор={summary['retried']} (retried={summary['retried']}) "
            f"ошибок={summary['failed']} (failed={summary['failed']})"
        )

    def handle(self, *args, **options):
        once_mode = bool(options.get("once"))
        force_run = bool(options.get("force_run"))
        health_check = bool(options.get("health_check"))
        verbose = bool(options.get("verbose"))
        batch_size = max(1, int(options.get("batch_size") or 1))
        sleep_seconds = max(0.1, float(options.get("sleep_seconds") or 0.1))

        if health_check:
            return self._run_health_check(verbose=verbose)

        if not force_run and not bool(getattr(settings, "OLAP_LIVE_PIPELINE_ENABLED", False)):
            raise CommandError("Оперативный OLAP-конвейер выключен: OLAP_LIVE_PIPELINE_ENABLED=False.")

        self._setup_signal_handlers()
        self.stdout.write(self.style.SUCCESS("Запущен run_olap_live_pipeline_worker"))
        self.stdout.write(f"mode={'once' if once_mode else 'loop'}")
        self.stdout.write(f"batch_size={batch_size}")
        self.stdout.write(f"sleep_seconds={sleep_seconds}")

        while not self.should_stop:
            client = build_iiko_olap_client_from_settings()
            try:
                service = OlapLivePipelineService.from_settings(client=client)
                service.batch_size = batch_size
                stats = service.process_batch()
                self._print_stats(stats)
            finally:
                client.close()

            if once_mode or self.should_stop:
                break
            time.sleep(sleep_seconds)
