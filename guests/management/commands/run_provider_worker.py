import asyncio
import math

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from guests.services.universal_queue.provider_worker import (
    AsyncProviderWorker,
    FairPolicy,
    ProviderWorkerConfig,
)
from guests.services.universal_queue.rate_limiter import (
    CentralizedRedisRateLimiter,
    ProviderRatePolicy,
)
from guests.services.universal_queue.redis_lanes import ProviderLaneQueue


def _as_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    # Защита от NaN/Inf из окружения: такие значения ломают лимиты и таймеры.
    if not math.isfinite(parsed):
        return default
    return parsed


class Command(BaseCommand):
    """
    Асинхронный воркер отправки задач для одного провайдера (telegram/max/vk).
    """

    help = (
        "Запускает async provider-worker: читает Redis lane-очереди, "
        "применяет централизованный rate limiter и отправляет сообщения."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider",
            type=str,
            required=True,
            choices=["telegram", "max", "vk"],
            help="Тип провайдера, для которого запускается воркер.",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Обработать не более одной задачи и завершиться.",
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
            "--block-timeout",
            type=int,
            default=int(getattr(settings, "UNIVERSAL_PROVIDER_BLOCK_TIMEOUT_SECONDS", 2)),
            help="Таймаут blocking-pop из Redis в секундах.",
        )
        parser.add_argument(
            "--idle-sleep",
            type=float,
            default=_as_float(getattr(settings, "UNIVERSAL_PROVIDER_IDLE_SLEEP_SECONDS", 0.2), 0.2),
            help="Пауза цикла в секундах, когда задач нет.",
        )
        parser.add_argument(
            "--retry-base",
            type=float,
            default=_as_float(getattr(settings, "UNIVERSAL_PROVIDER_RETRY_BASE_SECONDS", 3.0), 3.0),
            help="Базовая задержка экспоненциального retry в секундах.",
        )
        parser.add_argument(
            "--retry-max",
            type=float,
            default=_as_float(getattr(settings, "UNIVERSAL_PROVIDER_RETRY_MAX_SECONDS", 300.0), 300.0),
            help="Максимальная задержка retry в секундах.",
        )
        parser.add_argument(
            "--fair-high",
            type=int,
            default=int(getattr(settings, "UNIVERSAL_FAIR_HIGH", 10)),
            help="Квота high для fair-policy.",
        )
        parser.add_argument(
            "--fair-normal",
            type=int,
            default=int(getattr(settings, "UNIVERSAL_FAIR_NORMAL", 3)),
            help="Квота normal для fair-policy.",
        )
        parser.add_argument(
            "--fair-bulk",
            type=int,
            default=int(getattr(settings, "UNIVERSAL_FAIR_BULK", 1)),
            help="Квота bulk для fair-policy.",
        )

    def handle(self, *args, **options):
        provider_type = str(options["provider"]).strip().lower()
        redis_url = str(options["redis_url"]).strip()
        namespace = str(options["namespace"]).strip()

        if not redis_url:
            raise CommandError("Пустой redis-url для provider-worker.")
        if not namespace:
            raise CommandError("Пустой namespace для provider-worker.")

        block_timeout = max(1, int(options["block_timeout"]))
        idle_sleep = max(0.05, float(options["idle_sleep"]))
        retry_base = max(1.0, float(options["retry_base"]))
        retry_max = max(retry_base, float(options["retry_max"]))

        fair_policy = FairPolicy(
            high=max(1, int(options["fair_high"])),
            normal=max(1, int(options["fair_normal"])),
            bulk=max(1, int(options["fair_bulk"])),
        )

        provider_policies = {
            "telegram": ProviderRatePolicy(
                rate_per_second=_as_float(getattr(settings, "UNIVERSAL_RATE_LIMIT_TELEGRAM_PER_SECOND", 28.0), 28.0),
            ),
            "max": ProviderRatePolicy(
                rate_per_second=_as_float(getattr(settings, "UNIVERSAL_RATE_LIMIT_MAX_PER_SECOND", 20.0), 20.0),
            ),
            "vk": ProviderRatePolicy(
                rate_per_second=_as_float(getattr(settings, "UNIVERSAL_RATE_LIMIT_VK_PER_SECOND", 20.0), 20.0),
            ),
        }

        if provider_type not in provider_policies:
            raise CommandError(f"Неподдерживаемый provider={provider_type}")

        lane_queue = ProviderLaneQueue(redis_url=redis_url, namespace=namespace)
        rate_limiter = CentralizedRedisRateLimiter(
            redis_client=lane_queue.redis,
            namespace=namespace,
            provider_policies=provider_policies,
        )
        worker_config = ProviderWorkerConfig(
            provider_type=provider_type,
            block_timeout_seconds=block_timeout,
            idle_sleep_seconds=idle_sleep,
            retry_base_seconds=retry_base,
            retry_max_seconds=retry_max,
            fair_policy=fair_policy,
            once=bool(options["once"]),
        )
        worker = AsyncProviderWorker(
            lane_queue=lane_queue,
            rate_limiter=rate_limiter,
            config=worker_config,
        )
        worker.bind_signal_handlers()

        self.stdout.write(self.style.SUCCESS(f"Запуск provider-worker: provider={provider_type}"))
        self.stdout.write(f"Redis: {redis_url}")
        self.stdout.write(f"Namespace: {namespace}")
        self.stdout.write(
            f"Fair policy: high={fair_policy.high}, normal={fair_policy.normal}, bulk={fair_policy.bulk}"
        )
        self.stdout.write(f"Rate limit rps: {provider_policies[provider_type].rate_per_second}")

        try:
            asyncio.run(worker.run())
        finally:
            lane_queue.close()
