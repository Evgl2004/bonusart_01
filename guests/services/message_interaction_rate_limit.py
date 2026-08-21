"""Глобальное ограничение частоты входящих пакетов взаимодействий."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from django.conf import settings
from redis import RedisError
from redis import from_url as redis_from_url


RATE_LIMIT_KEY_PREFIX = "sagur:message-interactions:rate:v1"
RATE_LIMIT_TTL_SECONDS = 70
REDIS_CONNECT_TIMEOUT_SECONDS = 2.0
REDIS_SOCKET_TIMEOUT_SECONDS = 2.0

_INCREMENT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return current
"""


class MessageInteractionRateLimitUnavailable(RuntimeError):
    """Общее хранилище ограничения частоты недоступно или настроено неверно."""


def _rate_limit_redis_url() -> str:
    """Возвращает существующий адрес Redis без создания новой настройки среды."""

    redis_url = str(
        getattr(settings, "UNIVERSAL_QUEUE_REDIS_URL", "")
        or getattr(settings, "REDIS_QUEUE_URL", "")
        or ""
    ).strip()
    if not redis_url:
        raise MessageInteractionRateLimitUnavailable(
            "Не задано общее хранилище ограничения частоты."
        )
    return redis_url


@lru_cache(maxsize=4)
def _build_rate_limit_redis_client(redis_url: str) -> Any:
    """Создаёт переиспользуемый клиент и пул соединений для одного адреса."""

    try:
        return redis_from_url(
            redis_url,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            retry_on_timeout=False,
        )
    except (RedisError, TypeError, ValueError) as error:
        raise MessageInteractionRateLimitUnavailable(
            "Не удалось настроить общее хранилище ограничения частоты."
        ) from error


def get_message_interaction_rate_limit_redis_client() -> Any:
    """Возвращает общий Redis-клиент входящей интеграции."""

    return _build_rate_limit_redis_client(_rate_limit_redis_url())


def increment_message_interaction_rate_limit(*, minute_bucket: int) -> int:
    """Атомарно увеличивает общий минутный счётчик всех процессов SAGUR."""

    key = f"{RATE_LIMIT_KEY_PREFIX}:{int(minute_bucket)}"
    try:
        value = get_message_interaction_rate_limit_redis_client().eval(
            _INCREMENT_SCRIPT,
            1,
            key,
            RATE_LIMIT_TTL_SECONDS,
        )
        current = int(value)
    except (RedisError, OSError, TypeError, ValueError) as error:
        raise MessageInteractionRateLimitUnavailable(
            "Общее хранилище ограничения частоты временно недоступно."
        ) from error
    if current < 1:
        raise MessageInteractionRateLimitUnavailable(
            "Общее хранилище вернуло неверное значение счётчика."
        )
    return current


def check_message_interaction_rate_limit_redis() -> None:
    """Проверяет доступность общего Redis без чтения и изменения счётчика."""

    try:
        available = get_message_interaction_rate_limit_redis_client().ping()
    except (RedisError, OSError, TypeError, ValueError) as error:
        raise MessageInteractionRateLimitUnavailable(
            "Общее хранилище ограничения частоты временно недоступно."
        ) from error
    if not available:
        raise MessageInteractionRateLimitUnavailable(
            "Общее хранилище не подтвердило готовность."
        )
