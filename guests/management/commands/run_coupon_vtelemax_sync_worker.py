from __future__ import annotations

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.utils import timezone

from guests.models import CouponVtelemaxSyncQueue
from guests.services.vtelemax_coupon_sync import (
    CouponVtelemaxSyncBatchStats,
    VtelemaxCouponSyncService,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Воркер отправки купонных событий в vtelemax из таблицы `CouponVtelemaxSyncQueue`.
    """

    help = (
        "Обрабатывает очередь синхронизации купонов в vtelemax: "
        "отправка событий, retry, обновление статусов назначений."
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
            default=int(getattr(settings, "VTELEMAX_COUPON_SYNC_BATCH_SIZE", 100) or 100),
            help="Максимальное количество событий очереди за один проход.",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=float(getattr(settings, "VTELEMAX_COUPON_SYNC_LOOP_SLEEP_SECONDS", 5.0) or 5.0),
            help="Пауза между проходами в loop-режиме, если очередь пуста.",
        )
        parser.add_argument(
            "--force-run",
            action="store_true",
            help="Разрешить запуск даже при VTELEMAX_COUPON_SYNC_ENABLED=False.",
        )
        parser.add_argument(
            "--health-check",
            action="store_true",
            help="Лёгкая проверка здоровья без отправки событий в API.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Расширенный человекочитаемый отчёт для health-check.",
        )

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        logger.info(
            "Coupon vtelemax sync worker: получен сигнал %s, остановка после текущего прохода.",
            signum,
        )

    def _collect_queue_metrics(self) -> dict[str, int]:
        now = timezone.now()
        max_attempts = max(1, int(getattr(settings, "VTELEMAX_COUPON_SYNC_MAX_ATTEMPTS", 8) or 8))
        return {
            "pending": int(
                CouponVtelemaxSyncQueue.objects.filter(
                    status=CouponVtelemaxSyncQueue.Status.PENDING
                ).count()
            ),
            "error": int(
                CouponVtelemaxSyncQueue.objects.filter(
                    status=CouponVtelemaxSyncQueue.Status.ERROR
                ).count()
            ),
            "sent": int(
                CouponVtelemaxSyncQueue.objects.filter(
                    status=CouponVtelemaxSyncQueue.Status.SENT
                ).count()
            ),
            "acked": int(
                CouponVtelemaxSyncQueue.objects.filter(
                    status=CouponVtelemaxSyncQueue.Status.ACKED
                ).count()
            ),
            "due": int(
                CouponVtelemaxSyncQueue.objects.filter(
                    status__in=[
                        CouponVtelemaxSyncQueue.Status.PENDING,
                        CouponVtelemaxSyncQueue.Status.ERROR,
                        CouponVtelemaxSyncQueue.Status.SENT,
                    ],
                    attempts__lt=max_attempts,
                    next_retry_at__lte=now,
                ).count()
            ),
        }

    def _run_health_check(self, *, verbose: bool) -> None:
        try:
            connection = connections["default"]
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

            config_error = None
            if bool(getattr(settings, "VTELEMAX_COUPON_SYNC_ENABLED", False)):
                try:
                    VtelemaxCouponSyncService.from_settings()
                except Exception as exc:  # pragma: no cover - defensive branch
                    config_error = str(exc)

            metrics = self._collect_queue_metrics()
            if config_error:
                self.stdout.write(
                    self.style.ERROR(
                        f"[health] status=unhealthy component=run_coupon_vtelemax_sync_worker error={config_error}"
                    )
                )
                if verbose:
                    self.stdout.write("Статус: нездоров (status=unhealthy)")
                    self.stdout.write(
                        "Компонент: синк купонов vtelemax (component=run_coupon_vtelemax_sync_worker)"
                    )
                    self.stdout.write(f"Ошибка конфигурации: {config_error} (config_error={config_error})")
                raise SystemExit(self.exit_failure)

            self.stdout.write(
                self.style.SUCCESS("[health] status=healthy component=run_coupon_vtelemax_sync_worker")
            )
            self.stdout.write(
                f"[health] sync_enabled={bool(getattr(settings, 'VTELEMAX_COUPON_SYNC_ENABLED', False))} "
                f"due={metrics['due']} pending={metrics['pending']} error={metrics['error']} "
                f"sent={metrics['sent']} acked={metrics['acked']}"
            )
            if verbose:
                self.stdout.write("Статус: здоров (status=healthy)")
                self.stdout.write(
                    "Компонент: синк купонов vtelemax (component=run_coupon_vtelemax_sync_worker)"
                )
                self.stdout.write("База данных: доступна (db=ok)")
                self.stdout.write(
                    "Синк включён: %s (sync_enabled=%s)"
                    % (
                        "да" if bool(getattr(settings, "VTELEMAX_COUPON_SYNC_ENABLED", False)) else "нет",
                        bool(getattr(settings, "VTELEMAX_COUPON_SYNC_ENABLED", False)),
                    )
                )
                self.stdout.write(f"Готово к отправке: {metrics['due']} (due={metrics['due']})")
                self.stdout.write(
                    "Очередь: pending=%s error=%s sent=%s acked=%s "
                    "(pending=%s error=%s sent=%s acked=%s)"
                    % (
                        metrics["pending"],
                        metrics["error"],
                        metrics["sent"],
                        metrics["acked"],
                        metrics["pending"],
                        metrics["error"],
                        metrics["sent"],
                        metrics["acked"],
                    )
                )
            raise SystemExit(self.exit_success)
        except SystemExit:
            raise
        except Exception as exc:
            self.stdout.write(
                self.style.ERROR(
                    f"[health] status=unhealthy component=run_coupon_vtelemax_sync_worker error={exc}"
                )
            )
            if verbose:
                self.stdout.write("Статус: нездоров (status=unhealthy)")
                self.stdout.write(
                    "Компонент: синк купонов vtelemax (component=run_coupon_vtelemax_sync_worker)"
                )
                self.stdout.write(f"Ошибка: {exc} (error={exc})")
            raise SystemExit(self.exit_failure)

    @staticmethod
    def _print_batch_stats(*, stats: CouponVtelemaxSyncBatchStats) -> None:
        summary = stats.to_dict()
        print(
            "[coupon_vtelemax_sync] "
            f"scanned={summary['scanned']} processed={summary['processed']} "
            f"acked={summary['acked']} failed={summary['failed']} "
            f"skipped_max_attempts={summary['skipped_max_attempts']} "
            f"assignments_acked={summary['assignments_acked']} "
            f"status_updates_acked={summary['status_updates_acked']}"
        )

    def handle(self, *args, **options):
        once_mode = bool(options.get("once"))
        batch_size = max(1, int(options.get("batch_size") or 1))
        sleep_seconds = max(0.1, float(options.get("sleep_seconds") or 0.1))
        force_run = bool(options.get("force_run"))
        health_check = bool(options.get("health_check"))
        verbose = bool(options.get("verbose"))

        if health_check:
            return self._run_health_check(verbose=verbose)

        if not force_run and not bool(getattr(settings, "VTELEMAX_COUPON_SYNC_ENABLED", False)):
            raise CommandError("Синхронизация отключена: VTELEMAX_COUPON_SYNC_ENABLED=false.")

        service = VtelemaxCouponSyncService.from_settings()
        self._setup_signal_handlers()
        self.stdout.write(self.style.SUCCESS("Запущен run_coupon_vtelemax_sync_worker"))
        self.stdout.write(f"mode={'once' if once_mode else 'loop'}")
        self.stdout.write(f"batch_size={batch_size}")
        self.stdout.write(f"sleep_seconds={sleep_seconds}")

        if once_mode:
            stats = service.process_batch(limit=batch_size)
            self._print_batch_stats(stats=stats)
            return

        while not self.should_stop:
            stats = service.process_batch(limit=batch_size)
            self._print_batch_stats(stats=stats)
            if stats.processed == 0:
                time.sleep(sleep_seconds)

        return
