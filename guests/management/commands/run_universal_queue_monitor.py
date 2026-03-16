import signal
import time
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from guests.services.universal_queue.maintenance import UniversalQueueMaintenanceService
from guests.services.universal_queue.redis_lanes import ProviderLaneQueue


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Command(BaseCommand):
    """
    Монитор universal queue:
    1. Восстанавливает stale queued/in_progress задачи.
    2. Логирует метрики lane-очередей и статусов DispatchTask.
    """

    help = (
        "Запускает монитор universal queue: восстановление зависших задач "
        "и health-метрики Redis/DB."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_stop = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Выполнить один проход мониторинга и завершиться.",
        )
        parser.add_argument(
            "--provider",
            type=str,
            choices=["telegram", "max", "vk"],
            default=None,
            help="Ограничить мониторинг конкретным провайдером.",
        )
        parser.add_argument(
            "--redis-url",
            type=str,
            default=getattr(settings, "UNIVERSAL_QUEUE_REDIS_URL", getattr(settings, "REDIS_QUEUE_URL", "redis://localhost:6379/1")),
            help="URL подключения к Redis universal queue.",
        )
        parser.add_argument(
            "--namespace",
            type=str,
            default=getattr(settings, "UNIVERSAL_QUEUE_NAMESPACE", "uq:v1"),
            help="Namespace префикс ключей universal queue.",
        )
        parser.add_argument(
            "--interval-seconds",
            type=float,
            default=_as_float(getattr(settings, "UNIVERSAL_MONITOR_INTERVAL_SECONDS", 60.0), 60.0),
            help="Интервал между проходами мониторинга.",
        )
        parser.add_argument(
            "--stale-queued-seconds",
            type=int,
            default=_as_int(getattr(settings, "UNIVERSAL_STALE_QUEUED_SECONDS", 180), 180),
            help="TTL для queued задач, после которого они считаются stale.",
        )
        parser.add_argument(
            "--stale-in-progress-seconds",
            type=int,
            default=_as_int(getattr(settings, "UNIVERSAL_STALE_IN_PROGRESS_SECONDS", 600), 600),
            help="TTL для in_progress задач, после которого они считаются stale.",
        )

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        self.stdout.write(self.style.WARNING(f"Получен сигнал {signum}, завершаем monitor после текущего цикла."))

    def _bind_signals(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _print_snapshots(self, snapshots) -> None:
        for provider, snapshot in snapshots.items():
            self.stdout.write(
                f"[health:{provider}] redis={snapshot.redis_lane_lengths} db={snapshot.db_status_counts}"
            )

    def handle(self, *args, **options):
        self._bind_signals()

        once = bool(options["once"])
        provider_type: Optional[str] = options["provider"]
        redis_url = str(options["redis_url"]).strip()
        namespace = str(options["namespace"]).strip()
        interval_seconds = max(1.0, float(options["interval_seconds"]))
        stale_queued_seconds = max(1, int(options["stale_queued_seconds"]))
        stale_in_progress_seconds = max(1, int(options["stale_in_progress_seconds"]))

        if not redis_url:
            raise CommandError("Пустой redis-url для universal queue monitor.")

        queue = ProviderLaneQueue(redis_url=redis_url, namespace=namespace)
        maintenance = UniversalQueueMaintenanceService(lane_queue=queue)

        self.stdout.write(self.style.SUCCESS("Запущен universal queue monitor"))
        self.stdout.write(f"Redis URL: {redis_url}")
        self.stdout.write(f"Namespace: {namespace}")
        self.stdout.write(f"Provider: {provider_type or 'all'}")
        self.stdout.write(f"Stale queued: {stale_queued_seconds}s")
        self.stdout.write(f"Stale in_progress: {stale_in_progress_seconds}s")
        self.stdout.write(f"Interval: {interval_seconds}s")

        try:
            while not self.should_stop:
                summary = maintenance.recover_stale_tasks(
                    queued_stale_seconds=stale_queued_seconds,
                    in_progress_stale_seconds=stale_in_progress_seconds,
                    provider_type=provider_type,
                )
                self.stdout.write(
                    "[recover] queued=%s in_progress=%s failed=%s"
                    % (
                        summary.recovered_queued,
                        summary.recovered_in_progress,
                        summary.failed_in_progress,
                    )
                )

                snapshots = maintenance.collect_health_snapshots(provider_type=provider_type)
                self._print_snapshots(snapshots)

                if once:
                    break
                time.sleep(interval_seconds)
        finally:
            queue.close()
