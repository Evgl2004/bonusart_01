"""
Сервисы универсальной очереди сообщений.

Пакет содержит:
1. Контракт Redis lane-очередей.
2. Диспетчер постановки задач из БД в Redis.
"""

from .dispatcher import UniversalTaskDispatcher
from .redis_lanes import ProviderLaneQueue, QueueEnvelope

__all__ = [
    "ProviderLaneQueue",
    "QueueEnvelope",
    "UniversalTaskDispatcher",
]
