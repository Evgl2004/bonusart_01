"""
Сервисы универсальной очереди сообщений.

Пакет содержит:
1. Контракт Redis lane-очередей.
2. Диспетчер постановки задач из БД в Redis.
"""

from .dispatcher import UniversalTaskDispatcher
from .mailing_producer import enqueue_mailing_rows_as_dispatch_tasks
from .redis_lanes import ProviderLaneQueue, QueueEnvelope
from .webhook_producer import enqueue_high_priority_webhook_tasks

__all__ = [
    "ProviderLaneQueue",
    "QueueEnvelope",
    "UniversalTaskDispatcher",
    "enqueue_mailing_rows_as_dispatch_tasks",
    "enqueue_high_priority_webhook_tasks",
]
