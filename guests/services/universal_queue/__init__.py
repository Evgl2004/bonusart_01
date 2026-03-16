"""
Сервисы универсальной очереди сообщений.

Пакет содержит:
1. Контракт Redis lane-очередей.
2. Диспетчер постановки задач из БД в Redis.
"""

from .dispatcher import UniversalTaskDispatcher
from .maintenance import QueueHealthSnapshot, RecoverySummary, UniversalQueueMaintenanceService
from .mailing_producer import enqueue_mailing_rows_as_dispatch_tasks
from .provider_worker import AsyncProviderWorker, FairPolicy, ProviderWorkerConfig
from .rate_limiter import CentralizedRedisRateLimiter, ProviderRatePolicy
from .redis_lanes import ProviderLaneQueue, QueueEnvelope
from .webhook_producer import enqueue_high_priority_webhook_tasks

__all__ = [
    "AsyncProviderWorker",
    "CentralizedRedisRateLimiter",
    "FairPolicy",
    "ProviderLaneQueue",
    "ProviderRatePolicy",
    "ProviderWorkerConfig",
    "QueueHealthSnapshot",
    "QueueEnvelope",
    "RecoverySummary",
    "UniversalTaskDispatcher",
    "UniversalQueueMaintenanceService",
    "enqueue_mailing_rows_as_dispatch_tasks",
    "enqueue_high_priority_webhook_tasks",
]
