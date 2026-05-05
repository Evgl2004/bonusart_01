import logging
import signal
import time
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections
from django.utils import timezone

from guests.models import DispatchTask
from guests.services.universal_queue import ProviderLaneQueue, UniversalTaskDispatcher

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Команда диспетчеризации задач в универсальные Redis-очереди.

    Команда не отправляет сообщения пользователям напрямую.
    Её ответственность: перенести задачи из таблицы DispatchTask
    в lane-очереди Redis по маршруту provider + priority.
    """

    help = "Постановка задач DispatchTask в Redis lane-очереди (universal queue)."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_stop = False
        self.exit_success = 0
        self.exit_failure = 1

    def add_arguments(self, parser):
        """
        Аргументы команды для гибкой эксплуатации.
        """
        parser.add_argument(
            "--once",
            action="store_true",
            help="Выполнить одну итерацию диспетчеризации и завершить процесс.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=getattr(settings, "UNIVERSAL_DISPATCH_BATCH_SIZE", 200),
            help="Размер пачки задач для одной итерации.",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=getattr(settings, "UNIVERSAL_DISPATCH_SLEEP_SECONDS", 2.0),
            help="Пауза между итерациями, если задач для постановки не найдено.",
        )
        parser.add_argument(
            "--redis-url",
            type=str,
            default=getattr(settings, "UNIVERSAL_QUEUE_REDIS_URL", getattr(settings, "REDIS_QUEUE_URL", "redis://localhost:6379/1")),
            help="URL подключения к Redis для универсальной очереди.",
        )
        parser.add_argument(
            "--provider",
            type=str,
            choices=["telegram", "max", "vk"],
            default=None,
            help="Ограничить диспетчеризацию конкретным провайдером.",
        )
        parser.add_argument(
            "--namespace",
            type=str,
            default=getattr(settings, "UNIVERSAL_QUEUE_NAMESPACE", "uq:v1"),
            help="Namespace префикс для ключей Redis очереди.",
        )

        parser.add_argument(
            "--health-check",
            action="store_true",
            help="Лёгкая проверка здоровья диспетчера без запуска цикла.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Печать расширенных показателей для health-check.",
        )

    def _setup_signal_handlers(self) -> None:
        """
        Регистрирует SIGINT/SIGTERM для корректной остановки цикла.
        """
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        """
        Обработчик системных сигналов.
        """
        self.should_stop = True
        logger.info("Получен сигнал %s, завершаем диспетчеризацию после текущей итерации.", signum)

    def _run_iteration(
        self,
        dispatcher: UniversalTaskDispatcher,
        batch_size: int,
    ) -> int:
        """
        Выполняет одну итерацию постановки задач.
        """
        result = dispatcher.enqueue_pending_tasks(batch_size=batch_size)
        logger.info(
            "Итерация диспетчера завершена: claimed=%s enqueued=%s failed=%s",
            result.claimed,
            result.enqueued,
            result.failed,
        )
        return result.claimed

    def _sleep_with_stop(self, total_seconds: float) -> None:
        """
        Пауза цикла с регулярной проверкой флага остановки.

        Нужна для быстрого graceful shutdown в окружении Docker/K8s:
        процесс не ждёт полный `time.sleep(...)`, а выходит из паузы
        почти сразу после получения SIGTERM/SIGINT.
        """
        remaining = max(0.0, float(total_seconds))
        while remaining > 0 and not self.should_stop:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    def _run_health_check(self, *, options: dict) -> None:
        """
        Лёгкий health-check:
        1. БД: SELECT 1;
        2. Redis ping;
        3. короткие exists-признаки по DispatchTask + длины lane.
        """
        redis_url: str = options["redis_url"]
        namespace: str = options["namespace"]
        provider_type: Optional[str] = options.get("provider")
        verbose: bool = bool(options.get("verbose"))
        queue: Optional[ProviderLaneQueue] = None
        try:
            connection = connections["default"]
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

            queue = ProviderLaneQueue(redis_url=redis_url, namespace=namespace)
            queue.ping()

            providers = [provider_type] if provider_type else list(ProviderLaneQueue.PROVIDERS)
            lane_lengths = {provider: queue.lane_lengths(provider) for provider in providers}

            pending_qs = DispatchTask.objects.filter(
                status=DispatchTask.Status.PENDING,
                available_at__lte=timezone.now(),
            )
            queued_qs = DispatchTask.objects.filter(status=DispatchTask.Status.QUEUED)
            if provider_type:
                pending_qs = pending_qs.filter(provider_type=provider_type)
                queued_qs = queued_qs.filter(provider_type=provider_type)

            pending_exists = pending_qs.exists()
            queued_exists = queued_qs.exists()

            self.stdout.write(
                self.style.SUCCESS("[health] status=healthy component=dispatch_universal_tasks")
            )
            if verbose:
                self.stdout.write("Статус: здоров (status=healthy)")
                self.stdout.write("Компонент: диспетчер очереди (component=dispatch_universal_tasks)")
                self.stdout.write("База данных: доступна (db=ok)")
                self.stdout.write("Redis: доступен (redis=ok)")
                self.stdout.write(
                    "Ожидающие задачи: %s (pending_exists=%s)"
                    % ("да" if pending_exists else "нет", pending_exists)
                )
                self.stdout.write(
                    "Задачи в очереди: %s (queued_exists=%s)"
                    % ("да" if queued_exists else "нет", queued_exists)
                )
                self.stdout.write(f"Очереди провайдера: {lane_lengths} (lane_lengths={lane_lengths})")
            raise SystemExit(self.exit_success)
        except SystemExit:
            raise
        except Exception as err:
            self.stdout.write(
                self.style.ERROR(
                    f"[health] status=unhealthy component=dispatch_universal_tasks error={err}"
                )
            )
            if verbose:
                self.stdout.write("Статус: нездоров (status=unhealthy)")
                self.stdout.write("Компонент: диспетчер очереди (component=dispatch_universal_tasks)")
                self.stdout.write(f"Ошибка: {err} (error={err})")
            raise SystemExit(self.exit_failure)
        finally:
            if queue is not None:
                queue.close()

    def handle(self, *args, **options):
        """
        Точка входа management command.
        """
        if options.get("health_check"):
            return self._run_health_check(options=options)
        self._setup_signal_handlers()

        once_mode: bool = options["once"]
        batch_size: int = max(1, options["batch_size"])
        sleep_seconds: float = max(0.1, options["sleep_seconds"])
        redis_url: str = options["redis_url"]
        provider_type: Optional[str] = options["provider"]
        namespace: str = options["namespace"]

        queue: Optional[ProviderLaneQueue] = None

        try:
            queue = ProviderLaneQueue(redis_url=redis_url, namespace=namespace)
            dispatcher = UniversalTaskDispatcher(lane_queue=queue, provider_type=provider_type)

            self.stdout.write(self.style.SUCCESS("Запущен диспетчер универсальной очереди"))
            self.stdout.write(f"Redis URL: {redis_url}")
            self.stdout.write(f"Namespace: {namespace}")
            self.stdout.write(f"Provider: {provider_type or 'all'}")
            self.stdout.write(f"Batch size: {batch_size}")
            self.stdout.write(f"Mode: {'once' if once_mode else 'loop'}")

            if once_mode:
                self._run_iteration(dispatcher=dispatcher, batch_size=batch_size)
                return

            while not self.should_stop:
                claimed = self._run_iteration(dispatcher=dispatcher, batch_size=batch_size)
                if claimed == 0:
                    self._sleep_with_stop(sleep_seconds)

        finally:
            if queue is not None:
                queue.close()
