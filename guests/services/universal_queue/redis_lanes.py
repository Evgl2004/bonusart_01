import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

from redis import from_url as redis_from_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueEnvelope:
    """
    Транспортный конверт задачи для Redis-очереди.

    Конверт сериализуется в JSON и помещается в lane-очередь
    провайдера согласно полю `priority`.
    """

    task_id: int
    task_uuid: str
    source_type: str
    provider_type: str
    priority: str
    message_text: str
    payload: Dict[str, Any]
    guest_id: Optional[int]
    guest_binding_id: Optional[int]
    external_chat_id: Optional[str]
    idempotency_key: Optional[str] = None

    def to_bytes(self) -> bytes:
        """
        Сериализует конверт задачи в JSON-байты.
        """
        return json.dumps(asdict(self), ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw_value: bytes) -> "QueueEnvelope":
        """
        Десериализует JSON-байты обратно в структуру QueueEnvelope.
        """
        data = json.loads(raw_value.decode("utf-8"))
        return cls(**data)


class ProviderLaneQueue:
    """
    Низкоуровневый адаптер Redis lane-очередей.

    Логическая модель:
    1. У каждого провайдера одна очередь.
    2. Внутри очереди три lane-приоритета: high -> normal -> bulk.

    Физическая модель:
    ключи вида `<namespace>:<provider>:<priority>`.
    """

    PRIORITY_ORDER = ("high", "normal", "bulk")
    PROVIDERS = ("telegram", "max", "vk")

    def __init__(self, redis_url: str, namespace: str = "uq:v1"):
        self.redis_url = redis_url
        self.namespace = namespace
        self.redis = redis_from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=10,
            socket_timeout=30,
            health_check_interval=30,
        )

    def _validate_provider(self, provider_type: str) -> None:
        if provider_type not in self.PROVIDERS:
            raise ValueError(f"Неподдерживаемый провайдер: {provider_type}")

    def _validate_priority(self, priority: str) -> None:
        if priority not in self.PRIORITY_ORDER:
            raise ValueError(f"Неподдерживаемый приоритет: {priority}")

    def lane_key(self, provider_type: str, priority: str) -> str:
        """
        Возвращает физический ключ Redis для lane-очереди.
        """
        self._validate_provider(provider_type)
        self._validate_priority(priority)
        return f"{self.namespace}:{provider_type}:{priority}"

    def lane_keys_for_provider(self, provider_type: str) -> Tuple[str, str, str]:
        """
        Возвращает ключи lane-очередей в порядке вычитки:
        high -> normal -> bulk.
        """
        self._validate_provider(provider_type)
        return tuple(self.lane_key(provider_type, priority) for priority in self.PRIORITY_ORDER)

    def push(self, envelope: QueueEnvelope) -> str:
        """
        Кладёт задачу в lane-очередь и возвращает имя использованного ключа.
        """
        key = self.lane_key(envelope.provider_type, envelope.priority)
        self.redis.rpush(key, envelope.to_bytes())
        logger.debug(
            "Задача поставлена в Redis lane: key=%s task_id=%s priority=%s",
            key,
            envelope.task_id,
            envelope.priority,
        )
        return key

    def pop_for_provider(self, provider_type: str, timeout: int = 2) -> Optional[Tuple[str, QueueEnvelope]]:
        """
        Вытаскивает задачу для конкретного провайдера с учётом приоритетов.
        """
        lane_keys = self.lane_keys_for_provider(provider_type)
        result = self.redis.blpop(list(lane_keys), timeout=timeout)
        if result is None:
            return None

        raw_key, raw_payload = result
        key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
        envelope = QueueEnvelope.from_bytes(raw_payload)
        return key, envelope

    def lane_lengths(self, provider_type: str) -> Dict[str, int]:
        """
        Возвращает длину каждой lane-очереди провайдера.
        """
        lengths: Dict[str, int] = {}
        for priority in self.PRIORITY_ORDER:
            lane_key = self.lane_key(provider_type, priority)
            lengths[priority] = int(self.redis.llen(lane_key))
        return lengths

    def ping(self) -> bool:
        """
        Проверка доступности Redis.
        """
        return bool(self.redis.ping())

    def close(self) -> None:
        """
        Закрывает соединение с Redis.
        """
        self.redis.close()
