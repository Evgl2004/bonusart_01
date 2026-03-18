"""
Интеграционные тесты связки universal queue:
dispatch -> monitor -> provider.

Тестируем как «хорошие» сценарии, так и bug-seeking ветки:
1. штатная доставка через dispatcher и provider-worker;
2. временная ошибка провайдера с переносом задачи (retry);
3. stale-конверт в Redis после recovery в БД (защита от двойной отправки).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone

from guests.models import BotProfile, DispatchTask, Guest, GuestBotBinding
from guests.services.universal_queue.dispatcher import UniversalTaskDispatcher
from guests.services.universal_queue.maintenance import UniversalQueueMaintenanceService
from guests.services.universal_queue.provider_clients import (
    ProviderSendResult,
    ProviderTemporaryError,
)
from guests.services.universal_queue.provider_worker import AsyncProviderWorker, ProviderWorkerConfig
from guests.services.universal_queue.redis_lanes import QueueEnvelope


class _InMemoryPipelineLaneQueue:
    """
    In-memory lane-очередь для интеграционных тестов пайплайна.

    Реализует минимальный контракт ProviderLaneQueue, необходимый для:
    - UniversalTaskDispatcher;
    - AsyncProviderWorker;
    - UniversalQueueMaintenanceService.
    """

    PRIORITY_ORDER = ("high", "normal", "bulk")
    PROVIDERS = ("telegram", "max", "vk")

    def __init__(self, namespace: str = "uq:test"):
        self.namespace = namespace
        self._lanes: dict[str, list[QueueEnvelope]] = {}
        self.closed = False

    def lane_key(self, provider_type: str, priority: str) -> str:
        if provider_type not in self.PROVIDERS:
            raise ValueError(f"Неподдерживаемый provider_type={provider_type}")
        if priority not in self.PRIORITY_ORDER:
            raise ValueError(f"Неподдерживаемый priority={priority}")
        return f"{self.namespace}:{provider_type}:{priority}"

    def push(self, envelope: QueueEnvelope) -> str:
        key = self.lane_key(envelope.provider_type, envelope.priority)
        self._lanes.setdefault(key, []).append(envelope)
        return key

    def pop_from_lane(self, provider_type: str, priority: str) -> Optional[QueueEnvelope]:
        key = self.lane_key(provider_type, priority)
        queue = self._lanes.get(key, [])
        if not queue:
            return None
        return queue.pop(0)

    def pop_for_provider(self, provider_type: str, timeout: int = 2):
        for priority in self.PRIORITY_ORDER:
            envelope = self.pop_from_lane(provider_type, priority)
            if envelope is not None:
                return self.lane_key(provider_type, priority), envelope
        return None

    def lane_lengths(self, provider_type: str) -> dict[str, int]:
        return {
            priority: len(self._lanes.get(self.lane_key(provider_type, priority), []))
            for priority in self.PRIORITY_ORDER
        }

    def close(self) -> None:
        self.closed = True


class _FakeRateLimiterForPipeline:
    """
    Минимальный async rate-limiter двойник для проверки вызовов worker.
    """

    def __init__(self):
        self.acquire_calls: list[tuple[str, float, str | None]] = []
        self.retry_after_calls: list[tuple[str, float]] = []
        self.scope_retry_after_calls: list[tuple[str, str, float]] = []

    async def acquire(self, provider_type: str, timeout_seconds: float = 30.0, scope_key: str | None = None) -> None:
        self.acquire_calls.append((provider_type, float(timeout_seconds), scope_key))

    async def register_retry_after(self, provider_type: str, retry_after_seconds: float) -> None:
        self.retry_after_calls.append((provider_type, float(retry_after_seconds)))

    async def register_scope_retry_after(self, provider_type: str, scope_key: str, retry_after_seconds: float) -> None:
        self.scope_retry_after_calls.append((provider_type, str(scope_key), float(retry_after_seconds)))


class _CollectingSuccessSender:
    """
    Успешный sender, который фиксирует отправленные task_id.
    """

    def __init__(self):
        self.sent_task_ids: list[int] = []

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def send(self, task: DispatchTask, chat_id: str, text: str) -> ProviderSendResult:
        self.sent_task_ids.append(int(task.id))
        return ProviderSendResult(
            provider_message_id=f"msg-{task.id}",
            sent_at=timezone.now(),
            raw_response={"ok": True, "chat_id": chat_id, "text": text},
        )


class _TemporaryErrorSender:
    """
    Sender, который всегда возвращает временную ошибку провайдера.
    """

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def send(self, task: DispatchTask, chat_id: str, text: str) -> ProviderSendResult:
        raise ProviderTemporaryError("provider is temporarily unavailable")


class UniversalQueuePipelineIntegrationTests(TestCase):
    """
    Интеграция цепочки dispatch -> monitor -> provider на реальной БД.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest = Guest.objects.create(
            phone="+79990004455",
            first_name="Интеграция",
            created_at=now,
            updated_at=now,
        )
        self.bot = BotProfile.objects.create(
            code="tg_pipeline_test",
            name="Telegram Pipeline Test Bot",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot,
            external_chat_id="chat-4455",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

    def _create_pending_task(self, *, priority: str = DispatchTask.Priority.HIGH) -> DispatchTask:
        return DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            priority=priority,
            status=DispatchTask.Status.PENDING,
            guest=self.guest,
            guest_binding=self.binding,
            bot_profile=self.bot,
            external_chat_id="",
            message_text="Тест цепочки universal queue",
            payload={"kind": "pipeline_test"},
            available_at=timezone.now() - timedelta(seconds=5),
            max_attempts=3,
            attempt=0,
        )

    @staticmethod
    def _run_worker_once(*, lane_queue, rate_limiter, sender) -> None:
        config = ProviderWorkerConfig(
            provider_type=BotProfile.ProviderType.TELEGRAM,
            once=True,
            block_timeout_seconds=1,
            idle_sleep_seconds=0.01,
            retry_base_seconds=3.0,
            retry_max_seconds=300.0,
        )
        with patch("guests.services.universal_queue.provider_worker.build_provider_sender", return_value=sender):
            worker = AsyncProviderWorker(
                lane_queue=lane_queue,
                rate_limiter=rate_limiter,
                config=config,
            )
            async_to_sync(worker.run)()

    def test_pipeline_happy_path_dispatch_then_provider_marks_done(self):
        """
        Хороший сценарий: dispatcher ставит задачу в lane, provider-worker отправляет и закрывает её в DONE.
        """
        queue = _InMemoryPipelineLaneQueue()
        task = self._create_pending_task(priority=DispatchTask.Priority.HIGH)

        dispatcher = UniversalTaskDispatcher(lane_queue=queue)
        dispatch_result = dispatcher.enqueue_pending_tasks(batch_size=100)
        self.assertEqual(dispatch_result.claimed, 1)
        self.assertEqual(dispatch_result.enqueued, 1)
        self.assertEqual(dispatch_result.failed, 0)
        self.assertEqual(queue.lane_lengths("telegram")["high"], 1)

        sender = _CollectingSuccessSender()
        limiter = _FakeRateLimiterForPipeline()
        self._run_worker_once(lane_queue=queue, rate_limiter=limiter, sender=sender)

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.DONE)
        self.assertEqual(task.attempt, 1)
        self.assertEqual(task.payload.get("provider_message_id"), f"msg-{task.id}")
        self.assertEqual(sender.sent_task_ids, [task.id])
        self.assertEqual(len(limiter.acquire_calls), 1)
        self.assertEqual(queue.lane_lengths("telegram")["high"], 0)

    def test_pipeline_temporary_error_requeues_in_db_and_requires_redis_redispatch(self):
        """
        Bug-seeking: при временной ошибке задача уходит в PENDING с future available_at
        и не должна повторно вставать в Redis до нового прохода dispatcher.
        """
        queue = _InMemoryPipelineLaneQueue()
        task = self._create_pending_task(priority=DispatchTask.Priority.NORMAL)

        dispatcher = UniversalTaskDispatcher(lane_queue=queue)
        first_dispatch = dispatcher.enqueue_pending_tasks(batch_size=100)
        self.assertEqual(first_dispatch.enqueued, 1)
        self.assertEqual(queue.lane_lengths("telegram")["normal"], 1)

        limiter = _FakeRateLimiterForPipeline()
        self._run_worker_once(
            lane_queue=queue,
            rate_limiter=limiter,
            sender=_TemporaryErrorSender(),
        )

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertEqual(task.attempt, 1)
        self.assertIn("temporary_error", task.last_error or "")
        self.assertGreater(task.available_at, timezone.now())
        self.assertEqual(queue.lane_lengths("telegram")["normal"], 0)

        immediate_dispatch = dispatcher.enqueue_pending_tasks(batch_size=100)
        self.assertEqual(immediate_dispatch.claimed, 0)
        self.assertEqual(immediate_dispatch.enqueued, 0)

        task.available_at = timezone.now() - timedelta(seconds=1)
        task.save(update_fields=["available_at", "updated_at"])

        second_dispatch = dispatcher.enqueue_pending_tasks(batch_size=100)
        self.assertEqual(second_dispatch.claimed, 1)
        self.assertEqual(second_dispatch.enqueued, 1)
        self.assertEqual(queue.lane_lengths("telegram")["normal"], 1)

    def test_pipeline_monitor_recovers_stale_queued_and_stale_envelope_is_not_sent(self):
        """
        Bug-seeking: stale-конверт в Redis не должен приводить к отправке,
        если monitor уже вернул задачу из QUEUED обратно в PENDING.
        """
        queue = _InMemoryPipelineLaneQueue()
        task = self._create_pending_task(priority=DispatchTask.Priority.HIGH)

        dispatcher = UniversalTaskDispatcher(lane_queue=queue)
        dispatch_result = dispatcher.enqueue_pending_tasks(batch_size=100)
        self.assertEqual(dispatch_result.enqueued, 1)
        self.assertEqual(queue.lane_lengths("telegram")["high"], 1)

        stale_time = timezone.now() - timedelta(minutes=30)
        DispatchTask.objects.filter(id=task.id).update(enqueued_at=stale_time, updated_at=stale_time)

        maintenance = UniversalQueueMaintenanceService(lane_queue=queue)
        summary = maintenance.recover_stale_tasks(
            queued_stale_seconds=60,
            in_progress_stale_seconds=600,
            provider_type=BotProfile.ProviderType.TELEGRAM,
        )
        self.assertEqual(summary.recovered_queued, 1)

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertEqual(task.attempt, 0)
        self.assertEqual(queue.lane_lengths("telegram")["high"], 1)

        sender = _CollectingSuccessSender()
        limiter = _FakeRateLimiterForPipeline()
        self._run_worker_once(lane_queue=queue, rate_limiter=limiter, sender=sender)

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertEqual(task.attempt, 0)
        self.assertEqual(sender.sent_task_ids, [])
        self.assertEqual(limiter.acquire_calls, [])
        self.assertEqual(queue.lane_lengths("telegram")["high"], 0)
