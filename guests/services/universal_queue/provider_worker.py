import asyncio
import logging
import signal
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional, Tuple

from asgiref.sync import sync_to_async
from django.db.models import F
from django.utils import timezone

from guests.models import DispatchTask, GuestBotBinding
from guests.services.universal_queue.provider_clients import (
    ProviderBlockedError,
    ProviderPermanentError,
    ProviderRateLimitError,
    ProviderSendResult,
    ProviderTemporaryError,
    build_provider_sender,
)
from guests.services.universal_queue.rate_limiter import CentralizedRedisRateLimiter
from guests.services.universal_queue.redis_lanes import ProviderLaneQueue, QueueEnvelope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FairPolicy:
    """
    Квоты приоритетов для справедливой выборки задач.

    Пример `10:3:1` означает: после 10 задач `high` отдаём шанс
    трём задачам `normal` и одной задаче `bulk`.
    """

    high: int = 10
    normal: int = 3
    bulk: int = 1

    def to_cycle(self) -> List[str]:
        high = max(1, int(self.high))
        normal = max(1, int(self.normal))
        bulk = max(1, int(self.bulk))
        return (["high"] * high) + (["normal"] * normal) + (["bulk"] * bulk)


@dataclass(frozen=True)
class ProviderWorkerConfig:
    """
    Конфигурация async воркера отправки для одного провайдера.
    """

    provider_type: str
    block_timeout_seconds: int = 2
    idle_sleep_seconds: float = 0.2
    retry_base_seconds: float = 3.0
    retry_max_seconds: float = 300.0
    fair_policy: FairPolicy = FairPolicy()
    once: bool = False


class AsyncProviderWorker:
    """
    Асинхронный воркер отправки сообщений для конкретного провайдера.

    Ответственность воркера:
    1. Читать задачи из Redis lane-очередей провайдера с fair-policy.
    2. Ограничивать скорость через централизованный Redis rate limiter.
    3. Обновлять статус DispatchTask в БД.
    4. Корректно завершаться по сигналам ОС (graceful shutdown).
    """

    def __init__(
        self,
        lane_queue: ProviderLaneQueue,
        rate_limiter: CentralizedRedisRateLimiter,
        config: ProviderWorkerConfig,
    ):
        self.lane_queue = lane_queue
        self.rate_limiter = rate_limiter
        self.config = config
        self.sender = build_provider_sender(config.provider_type)

        self._fair_cycle = config.fair_policy.to_cycle()
        self._fair_index = 0
        self.should_stop = False

    def request_stop(self) -> None:
        """
        Переводит воркер в режим мягкой остановки.
        """
        self.should_stop = True

    def bind_signal_handlers(self) -> None:
        """
        Подключает обработчики SIGINT/SIGTERM для graceful shutdown.
        """
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        logger.info("Получен сигнал %s, начинаем мягкую остановку provider-worker.", signum)
        self.request_stop()

    async def run(self) -> None:
        """
        Основной цикл работы воркера.
        """
        await self.sender.startup()
        logger.info(
            "Provider worker started: provider=%s fair_policy=%s",
            self.config.provider_type,
            self.config.fair_policy,
        )

        processed = 0
        try:
            while not self.should_stop:
                item = await self._pop_next_envelope()
                if item is None:
                    await asyncio.sleep(self.config.idle_sleep_seconds)
                    if self.config.once:
                        break
                    continue

                priority, envelope = item
                await self._process_envelope(priority=priority, envelope=envelope)
                processed += 1

                if self.config.once:
                    break
        finally:
            await self.sender.shutdown()
            logger.info(
                "Provider worker stopped: provider=%s processed=%s",
                self.config.provider_type,
                processed,
            )

    def _next_fair_priority(self) -> str:
        priority = self._fair_cycle[self._fair_index]
        self._fair_index = (self._fair_index + 1) % len(self._fair_cycle)
        return priority

    async def _pop_next_envelope(self) -> Optional[Tuple[str, QueueEnvelope]]:
        """
        Возвращает следующую задачу с учётом fair-policy.
        """
        provider_type = self.config.provider_type
        preferred_priority = self._next_fair_priority()

        envelope = await asyncio.to_thread(
            self.lane_queue.pop_from_lane,
            provider_type,
            preferred_priority,
        )
        if envelope is not None:
            return preferred_priority, envelope

        for priority in ("high", "normal", "bulk"):
            if priority == preferred_priority:
                continue
            envelope = await asyncio.to_thread(
                self.lane_queue.pop_from_lane,
                provider_type,
                priority,
            )
            if envelope is not None:
                return priority, envelope

        result = await asyncio.to_thread(
            self.lane_queue.pop_for_provider,
            provider_type,
            self.config.block_timeout_seconds,
        )
        if result is None:
            return None

        lane_key, envelope = result
        priority = lane_key.rsplit(":", maxsplit=1)[-1]
        return priority, envelope

    async def _process_envelope(self, priority: str, envelope: QueueEnvelope) -> None:
        """
        Обрабатывает одну задачу отправки.
        """
        task = await sync_to_async(self._claim_task_sync, thread_sensitive=True)(envelope.task_id)
        if task is None:
            logger.debug("Task already claimed or missing: task_id=%s", envelope.task_id)
            return

        chat_id = str(envelope.external_chat_id or task.external_chat_id or "").strip()
        if not chat_id:
            await sync_to_async(self._fail_task_sync, thread_sensitive=True)(
                task.id,
                "Не найден external_chat_id для отправки.",
            )
            return

        try:
            await self.rate_limiter.acquire(provider_type=self.config.provider_type, timeout_seconds=30.0)
            result = await self.sender.send(task=task, chat_id=chat_id, text=task.message_text or "")
            await sync_to_async(self._mark_done_sync, thread_sensitive=True)(task.id, result)
            logger.info(
                "Task sent: task_id=%s provider=%s priority=%s message_id=%s",
                task.id,
                self.config.provider_type,
                priority,
                result.provider_message_id,
            )
        except ProviderRateLimitError as err:
            await self.rate_limiter.register_retry_after(
                provider_type=self.config.provider_type,
                retry_after_seconds=err.retry_after_seconds,
            )
            await sync_to_async(self._requeue_task_sync, thread_sensitive=True)(
                task.id,
                max(1.0, err.retry_after_seconds),
                f"rate_limited: {err}",
            )
        except ProviderBlockedError as err:
            await sync_to_async(self._mark_binding_blocked_sync, thread_sensitive=True)(task)
            await sync_to_async(self._fail_task_sync, thread_sensitive=True)(task.id, f"blocked: {err}")
        except ProviderTemporaryError as err:
            next_delay = self._temporary_retry_delay_seconds(task.attempt)
            if task.attempt >= task.max_attempts:
                await sync_to_async(self._fail_task_sync, thread_sensitive=True)(
                    task.id,
                    f"temporary_error_exhausted: {err}",
                )
            else:
                await sync_to_async(self._requeue_task_sync, thread_sensitive=True)(
                    task.id,
                    next_delay,
                    f"temporary_error: {err}",
                )
        except ProviderPermanentError as err:
            await sync_to_async(self._fail_task_sync, thread_sensitive=True)(task.id, f"permanent_error: {err}")
        except Exception as err:
            logger.exception("Unexpected provider worker error task_id=%s: %s", task.id, err)
            next_delay = self._temporary_retry_delay_seconds(task.attempt)
            if task.attempt >= task.max_attempts:
                await sync_to_async(self._fail_task_sync, thread_sensitive=True)(
                    task.id,
                    f"unexpected_error_exhausted: {err}",
                )
            else:
                await sync_to_async(self._requeue_task_sync, thread_sensitive=True)(
                    task.id,
                    next_delay,
                    f"unexpected_error: {err}",
                )

    def _temporary_retry_delay_seconds(self, attempt: int) -> float:
        exponent = max(0, int(attempt) - 1)
        delay = self.config.retry_base_seconds * (2 ** exponent)
        return min(self.config.retry_max_seconds, max(1.0, delay))

    @staticmethod
    def _claim_task_sync(task_id: int) -> DispatchTask | None:
        now = timezone.now()
        updated = DispatchTask.objects.filter(
            id=task_id,
            status=DispatchTask.Status.QUEUED,
        ).update(
            status=DispatchTask.Status.IN_PROGRESS,
            started_at=now,
            updated_at=now,
            attempt=F("attempt") + 1,
            last_error=None,
        )
        if updated == 0:
            return None

        return DispatchTask.objects.select_related("bot_profile", "guest_binding").get(id=task_id)

    @staticmethod
    def _mark_done_sync(task_id: int, result: ProviderSendResult) -> None:
        task = DispatchTask.objects.filter(id=task_id).only("payload").first()
        payload = task.payload if task and isinstance(task.payload, dict) else {}
        payload.update(
            {
                "provider_message_id": result.provider_message_id,
                "sent_at": result.sent_at.isoformat(),
                "provider_response": result.raw_response,
            }
        )

        now = timezone.now()
        DispatchTask.objects.filter(id=task_id).update(
            status=DispatchTask.Status.DONE,
            finished_at=now,
            updated_at=now,
            last_error=None,
            payload=payload,
        )

    @staticmethod
    def _requeue_task_sync(task_id: int, delay_seconds: float, reason: str) -> None:
        now = timezone.now()
        next_available = now + timedelta(seconds=max(1.0, delay_seconds))
        DispatchTask.objects.filter(id=task_id).update(
            status=DispatchTask.Status.PENDING,
            enqueued_at=None,
            queue_name=None,
            started_at=None,
            finished_at=None,
            available_at=next_available,
            updated_at=now,
            last_error=str(reason)[:2000],
        )

    @staticmethod
    def _fail_task_sync(task_id: int, reason: str) -> None:
        now = timezone.now()
        DispatchTask.objects.filter(id=task_id).update(
            status=DispatchTask.Status.FAILED,
            finished_at=now,
            updated_at=now,
            last_error=str(reason)[:2000],
        )

    @staticmethod
    def _mark_binding_blocked_sync(task: DispatchTask) -> None:
        if not task.guest_binding_id:
            return
        GuestBotBinding.objects.filter(id=task.guest_binding_id).update(
            is_stop_sending=True,
            is_active=False,
            last_error="Провайдер вернул blocked/forbidden при отправке.",
            updated_at=timezone.now(),
        )
