import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderRatePolicy:
    """
    Политика ограничения скорости для одного провайдера.

    Параметр `rate_per_second` задаёт среднее количество сообщений в секунду,
    которое может быть выдано всеми воркерами совместно.
    """

    rate_per_second: float


class CentralizedRedisRateLimiter:
    """
    Централизованный limiter на Redis для координации всех воркеров.

    Механизм:
    1. Для каждого провайдера хранится "следующий допустимый момент отправки".
    2. Резервирование слота выполняется атомарно через Lua-скрипт.
    3. При `RetryAfter` можно глобально "поставить на паузу" провайдера.
    """

    _ACQUIRE_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local interval_ms = tonumber(ARGV[2])
local current = redis.call('GET', key)

if (not current) then
  redis.call('PSETEX', key, math.max(interval_ms * 2, 1000), now_ms + interval_ms)
  return 0
end

local current_ms = tonumber(current)
if current_ms <= now_ms then
  redis.call('PSETEX', key, math.max(interval_ms * 2, 1000), now_ms + interval_ms)
  return 0
end

local wait_ms = current_ms - now_ms
redis.call('PSETEX', key, math.max((current_ms + interval_ms) - now_ms, 1000), current_ms + interval_ms)
return wait_ms
"""

    def __init__(
        self,
        redis_client,
        namespace: str = "uq:v1",
        provider_policies: Dict[str, ProviderRatePolicy] | None = None,
    ):
        self.redis = redis_client
        self.namespace = namespace
        self.provider_policies = provider_policies or {
            "telegram": ProviderRatePolicy(rate_per_second=28.0),
            "max": ProviderRatePolicy(rate_per_second=20.0),
            "vk": ProviderRatePolicy(rate_per_second=20.0),
        }
        self._script_sha: str | None = None

    def _provider_key(self, provider_type: str) -> str:
        return f"{self.namespace}:rate:{provider_type}:next_ts_ms"

    def _pause_key(self, provider_type: str) -> str:
        return f"{self.namespace}:rate:{provider_type}:pause_until_ms"

    def _scope_pause_key(self, provider_type: str, scope_key: str) -> str:
        safe_scope_key = str(scope_key).strip()
        return f"{self.namespace}:rate:{provider_type}:scope:{safe_scope_key}:pause_until_ms"

    def _interval_ms(self, provider_type: str) -> int:
        policy = self.provider_policies.get(provider_type)
        if policy is None:
            policy = ProviderRatePolicy(rate_per_second=10.0)
        safe_rate = max(0.1, float(policy.rate_per_second))
        return max(1, int(1000.0 / safe_rate))

    def _run_acquire_script(self, provider_type: str, now_ms: int) -> int:
        key = self._provider_key(provider_type)
        interval_ms = self._interval_ms(provider_type)

        if self._script_sha is None:
            self._script_sha = self.redis.script_load(self._ACQUIRE_SCRIPT)

        try:
            result = self.redis.evalsha(self._script_sha, 1, key, now_ms, interval_ms)
        except Exception:
            # Fallback на прямой eval полезен после перезапуска Redis.
            result = self.redis.eval(self._ACQUIRE_SCRIPT, 1, key, now_ms, interval_ms)
            self._script_sha = None

        return int(result or 0)

    def _current_pause_delay_for_key_ms(self, redis_key: str, now_ms: int) -> int:
        raw_value = self.redis.get(redis_key)
        if not raw_value:
            return 0
        try:
            pause_until_ms = int(raw_value)
        except (TypeError, ValueError):
            return 0
        return max(0, pause_until_ms - now_ms)

    def _current_pause_delay_ms(self, provider_type: str, now_ms: int, scope_key: str | None = None) -> int:
        provider_delay = self._current_pause_delay_for_key_ms(self._pause_key(provider_type), now_ms)
        if not scope_key:
            return provider_delay

        scope_delay = self._current_pause_delay_for_key_ms(
            self._scope_pause_key(provider_type, scope_key),
            now_ms,
        )
        return max(provider_delay, scope_delay)

    async def acquire(
        self,
        provider_type: str,
        timeout_seconds: float = 30.0,
        scope_key: str | None = None,
    ) -> None:
        """
        Асинхронно ждёт глобально разрешённый слот отправки для провайдера.

        Важно: лимит общий на все процессы, использующие один Redis namespace.
        """
        started_at = time.monotonic()

        while True:
            now_ms = int(time.time() * 1000)
            pause_delay_ms = await asyncio.to_thread(
                self._current_pause_delay_ms,
                provider_type,
                now_ms,
                scope_key,
            )
            if pause_delay_ms > 0:
                await asyncio.sleep(pause_delay_ms / 1000.0)
                continue

            wait_ms = await asyncio.to_thread(self._run_acquire_script, provider_type, now_ms)
            if wait_ms <= 0:
                return

            elapsed = time.monotonic() - started_at
            if elapsed >= timeout_seconds:
                raise TimeoutError(
                    f"Не удалось получить слот rate limiter для provider={provider_type} за {timeout_seconds}s"
                )

            await asyncio.sleep(wait_ms / 1000.0)

    async def register_retry_after(self, provider_type: str, retry_after_seconds: float) -> None:
        """
        Регистрирует глобальную паузу провайдера после ответа `RetryAfter`.
        """
        safe_retry_after = max(0.0, float(retry_after_seconds))
        now_ms = int(time.time() * 1000)
        pause_until_ms = now_ms + int(safe_retry_after * 1000)
        pause_ttl_ms = max(1000, int(safe_retry_after * 2000))

        def _save_pause() -> None:
            self.redis.psetex(self._pause_key(provider_type), pause_ttl_ms, str(pause_until_ms))

        await asyncio.to_thread(_save_pause)
        logger.warning(
            "Rate limiter pause provider=%s retry_after=%.2fs",
            provider_type,
            safe_retry_after,
        )

    async def register_scope_retry_after(
        self,
        provider_type: str,
        scope_key: str,
        retry_after_seconds: float,
    ) -> None:
        """
        Регистрирует паузу по scope-ключу (например, по chat_id/peer_id).
        """
        safe_scope_key = str(scope_key).strip()
        if not safe_scope_key:
            return

        safe_retry_after = max(0.0, float(retry_after_seconds))
        now_ms = int(time.time() * 1000)
        pause_until_ms = now_ms + int(safe_retry_after * 1000)
        pause_ttl_ms = max(1000, int(safe_retry_after * 2000))

        def _save_pause() -> None:
            self.redis.psetex(
                self._scope_pause_key(provider_type, safe_scope_key),
                pause_ttl_ms,
                str(pause_until_ms),
            )

        await asyncio.to_thread(_save_pause)
        logger.warning(
            "Rate limiter scope pause provider=%s scope=%s retry_after=%.2fs",
            provider_type,
            safe_scope_key,
            safe_retry_after,
        )
