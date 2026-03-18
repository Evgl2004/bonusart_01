"""
Тесты подсистемы universal queue.

Покрываем:
1. Redis lane-адаптер;
2. диспетчер из БД в Redis;
3. сервис восстановления stale-задач;
4. централизованный rate limiter;
5. async provider-worker;
6. helper-логику provider clients.
"""

from __future__ import annotations

import os
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from guests.models import BotProfile, DispatchTask, Guest, GuestBotBinding
from guests.services.universal_queue.dispatcher import UniversalTaskDispatcher
from guests.services.universal_queue.maintenance import UniversalQueueMaintenanceService
from guests.services.universal_queue.provider_clients import (
    MaxAsyncSender,
    ProviderBlockedError,
    ProviderRateLimitError,
    ProviderSendResult,
    ProviderTemporaryError,
    TelegramAsyncSender,
    VkAsyncSender,
    _resolve_bot_token,
    build_provider_sender,
)
from guests.services.universal_queue.provider_worker import (
    AsyncProviderWorker,
    FairPolicy,
    ProviderWorkerConfig,
)
from guests.services.universal_queue.rate_limiter import (
    CentralizedRedisRateLimiter,
    ProviderRatePolicy,
)
from guests.services.universal_queue.redis_lanes import ProviderLaneQueue, QueueEnvelope


class _InMemoryRedisForLanes:
    """
    Простейшая in-memory реализация Redis API для тестов lane-очередей.
    """

    def __init__(self):
        self._lists: dict[str, list[bytes]] = {}
        self.closed = False

    def rpush(self, key: str, value: bytes) -> None:
        self._lists.setdefault(key, []).append(value)

    def lpop(self, key: str):
        queue = self._lists.get(key, [])
        if not queue:
            return None
        return queue.pop(0)

    def blpop(self, keys: list[str], timeout: int = 2):
        for key in keys:
            queue = self._lists.get(key, [])
            if queue:
                return key.encode("utf-8"), queue.pop(0)
        return None

    def llen(self, key: str) -> int:
        return len(self._lists.get(key, []))

    @staticmethod
    def ping() -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class _FakeRedisForRateLimiter:
    """
    Тестовый Redis-двойник для проверки rate limiter без внешнего Redis.
    """

    def __init__(self):
        self.values: dict[str, str] = {}
        self.psetex_calls: list[tuple[str, int, str]] = []
        self.script_load_calls = 0
        self.evalsha_calls = 0
        self.eval_calls = 0
        self.evalsha_result = 0
        self.eval_result = 0
        self.raise_evalsha_once = False

    def script_load(self, script: str) -> str:
        self.script_load_calls += 1
        return "sha:test"

    def evalsha(self, sha: str, numkeys: int, key: str, now_ms: int, interval_ms: int) -> int:
        self.evalsha_calls += 1
        if self.raise_evalsha_once:
            self.raise_evalsha_once = False
            raise RuntimeError("NOSCRIPT")
        return self.evalsha_result

    def eval(self, script: str, numkeys: int, key: str, now_ms: int, interval_ms: int) -> int:
        self.eval_calls += 1
        return self.eval_result

    def get(self, key: str):
        return self.values.get(key)

    def psetex(self, key: str, ttl_ms: int, value: str) -> None:
        self.psetex_calls.append((key, int(ttl_ms), str(value)))
        self.values[key] = str(value)


class _FakeLaneQueueForDispatcher:
    """
    Lane queue-двойник для тестов диспетчера.
    """

    def __init__(self, fail_task_ids: set[int] | None = None):
        self.fail_task_ids = fail_task_ids or set()
        self.envelopes: list[QueueEnvelope] = []

    def push(self, envelope: QueueEnvelope) -> str:
        if envelope.task_id in self.fail_task_ids:
            raise RuntimeError(f"redis push failed for task={envelope.task_id}")
        self.envelopes.append(envelope)
        return f"uq:v1:{envelope.provider_type}:{envelope.priority}"


class _FakeRateLimiter:
    """
    Async rate-limiter двойник для тестов provider-worker.
    """

    def __init__(self):
        self.acquire_calls: list[tuple[str, float, str | None]] = []
        self.retry_after_calls: list[tuple[str, float]] = []
        self.scope_retry_after_calls: list[tuple[str, str, float]] = []

    async def acquire(self, provider_type: str, timeout_seconds: float = 30.0, scope_key: str | None = None) -> None:
        self.acquire_calls.append((provider_type, float(timeout_seconds), scope_key))

    async def register_retry_after(self, provider_type: str, retry_after_seconds: float) -> None:
        self.retry_after_calls.append((provider_type, float(retry_after_seconds)))

    async def register_scope_retry_after(
        self,
        provider_type: str,
        scope_key: str,
        retry_after_seconds: float,
    ) -> None:
        self.scope_retry_after_calls.append((provider_type, str(scope_key), float(retry_after_seconds)))


class _SuccessSender:
    """
    Успешный sender-двойник.
    """

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def send(self, task: DispatchTask, chat_id: str, text: str) -> ProviderSendResult:
        return ProviderSendResult(
            provider_message_id="provider_msg_1",
            sent_at=timezone.now(),
            raw_response={"ok": True, "chat_id": chat_id, "text": text},
        )


class _ErrorSender:
    """
    Sender-двойник, который всегда бросает заданную ошибку.
    """

    def __init__(self, error: Exception):
        self.error = error

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def send(self, task: DispatchTask, chat_id: str, text: str) -> ProviderSendResult:
        raise self.error


class ProviderLaneQueueTests(SimpleTestCase):
    """
    Тесты Redis lane-адаптера.
    """

    def setUp(self):
        super().setUp()
        self.fake_redis = _InMemoryRedisForLanes()
        patcher = patch(
            "guests.services.universal_queue.redis_lanes.redis_from_url",
            return_value=self.fake_redis,
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        self.queue = ProviderLaneQueue(redis_url="redis://fake", namespace="uq:test")

    def _make_envelope(self, *, task_id: int, provider: str = "telegram", priority: str = "normal") -> QueueEnvelope:
        return QueueEnvelope(
            task_id=task_id,
            task_uuid=f"uuid-{task_id}",
            source_type="system",
            provider_type=provider,
            priority=priority,
            message_text=f"msg-{task_id}",
            payload={"task_id": task_id},
            guest_id=None,
            guest_binding_id=None,
            external_chat_id="100500",
            idempotency_key=f"idem-{task_id}",
        )

    def test_queue_envelope_serialization_roundtrip(self):
        """
        Конверт корректно сериализуется и десериализуется.
        """
        envelope = self._make_envelope(task_id=1, priority="high")
        restored = QueueEnvelope.from_bytes(envelope.to_bytes())
        self.assertEqual(restored.task_id, envelope.task_id)
        self.assertEqual(restored.provider_type, envelope.provider_type)
        self.assertEqual(restored.priority, envelope.priority)
        self.assertEqual(restored.payload["task_id"], 1)

    def test_push_and_pop_for_provider_respects_priority_order(self):
        """
        pop_for_provider должен забирать `high` раньше `normal`/`bulk`.
        """
        bulk = self._make_envelope(task_id=1, priority="bulk")
        high = self._make_envelope(task_id=2, priority="high")
        normal = self._make_envelope(task_id=3, priority="normal")

        self.queue.push(bulk)
        self.queue.push(high)
        self.queue.push(normal)

        key_1, env_1 = self.queue.pop_for_provider("telegram", timeout=1)
        key_2, env_2 = self.queue.pop_for_provider("telegram", timeout=1)
        key_3, env_3 = self.queue.pop_for_provider("telegram", timeout=1)

        self.assertTrue(key_1.endswith(":high"))
        self.assertTrue(key_2.endswith(":normal"))
        self.assertTrue(key_3.endswith(":bulk"))
        self.assertEqual((env_1.task_id, env_2.task_id, env_3.task_id), (2, 3, 1))

    def test_pop_from_lane_reads_only_selected_lane(self):
        """
        Неблокирующая вычитка должна работать только в выбранном lane.
        """
        high = self._make_envelope(task_id=10, priority="high")
        bulk = self._make_envelope(task_id=11, priority="bulk")
        self.queue.push(high)
        self.queue.push(bulk)

        from_bulk = self.queue.pop_from_lane("telegram", "bulk")
        from_normal = self.queue.pop_from_lane("telegram", "normal")
        from_high = self.queue.pop_from_lane("telegram", "high")

        self.assertIsNotNone(from_bulk)
        self.assertEqual(from_bulk.task_id, 11)
        self.assertIsNone(from_normal)
        self.assertIsNotNone(from_high)
        self.assertEqual(from_high.task_id, 10)

    def test_lane_lengths_and_validation(self):
        """
        Проверяем длины lane и валидацию provider/priority.
        """
        self.queue.push(self._make_envelope(task_id=21, priority="high"))
        self.queue.push(self._make_envelope(task_id=22, priority="high"))
        self.queue.push(self._make_envelope(task_id=23, priority="bulk"))

        lengths = self.queue.lane_lengths("telegram")
        self.assertEqual(lengths["high"], 2)
        self.assertEqual(lengths["normal"], 0)
        self.assertEqual(lengths["bulk"], 1)

        with self.assertRaises(ValueError):
            self.queue.lane_key("unknown", "high")
        with self.assertRaises(ValueError):
            self.queue.lane_key("telegram", "super_high")


class UniversalTaskDispatcherTests(TestCase):
    """
    Тесты диспетчеризации задач из БД в Redis lane-очереди.
    """

    def setUp(self):
        super().setUp()
        self.guest = Guest.objects.create(
            phone="+79990001001",
            first_name="Тест",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        self.bot_tg = BotProfile.objects.create(
            code="tg_dispatch_test",
            name="TG Dispatch Test",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_tg,
            external_chat_id="123456",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

    def _create_task(
        self,
        *,
        provider_type: str,
        priority: str,
        status: str = DispatchTask.Status.PENDING,
        available_at=None,
        external_chat_id: str = "",
    ) -> DispatchTask:
        return DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=provider_type,
            priority=priority,
            status=status,
            guest=self.guest,
            guest_binding=self.binding,
            external_chat_id=external_chat_id,
            message_text="Тестовое сообщение",
            payload={"kind": "dispatch_test"},
            available_at=available_at or (timezone.now() - timedelta(minutes=1)),
            max_attempts=3,
            attempt=0,
        )

    def test_claim_pending_tasks_orders_high_normal_bulk(self):
        """
        Захват pending-задач должен идти в порядке приоритетов.
        """
        task_bulk = self._create_task(provider_type="telegram", priority=DispatchTask.Priority.BULK)
        task_high = self._create_task(provider_type="telegram", priority=DispatchTask.Priority.HIGH)
        task_normal = self._create_task(provider_type="telegram", priority=DispatchTask.Priority.NORMAL)
        self._create_task(
            provider_type="telegram",
            priority=DispatchTask.Priority.HIGH,
            available_at=timezone.now() + timedelta(hours=1),
        )

        dispatcher = UniversalTaskDispatcher(lane_queue=_FakeLaneQueueForDispatcher())
        tasks, _ = dispatcher._claim_pending_tasks(batch_size=10)

        self.assertEqual([task.id for task in tasks], [task_high.id, task_normal.id, task_bulk.id])
        task_high.refresh_from_db()
        task_normal.refresh_from_db()
        task_bulk.refresh_from_db()
        self.assertEqual(task_high.status, DispatchTask.Status.QUEUED)
        self.assertEqual(task_normal.status, DispatchTask.Status.QUEUED)
        self.assertEqual(task_bulk.status, DispatchTask.Status.QUEUED)

    def test_enqueue_pending_tasks_success(self):
        """
        При успешной постановке задачи получают queue_name и статус queued.
        """
        task = self._create_task(
            provider_type="telegram",
            priority=DispatchTask.Priority.HIGH,
            external_chat_id="",
        )
        lane_queue = _FakeLaneQueueForDispatcher()
        dispatcher = UniversalTaskDispatcher(lane_queue=lane_queue)

        result = dispatcher.enqueue_pending_tasks(batch_size=100)

        self.assertEqual(result.claimed, 1)
        self.assertEqual(result.enqueued, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(len(lane_queue.envelopes), 1)
        self.assertEqual(lane_queue.envelopes[0].external_chat_id, "123456")

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.QUEUED)
        self.assertTrue((task.queue_name or "").endswith(":high"))
        self.assertIsNotNone(task.enqueued_at)
        self.assertEqual(task.last_error, None)

    def test_enqueue_pending_tasks_rollback_on_redis_error(self):
        """
        Ошибка публикации в Redis должна вернуть задачу обратно в pending.
        """
        task = self._create_task(
            provider_type="telegram",
            priority=DispatchTask.Priority.NORMAL,
        )
        lane_queue = _FakeLaneQueueForDispatcher(fail_task_ids={task.id})
        dispatcher = UniversalTaskDispatcher(lane_queue=lane_queue)

        result = dispatcher.enqueue_pending_tasks(batch_size=100)

        self.assertEqual(result.claimed, 1)
        self.assertEqual(result.enqueued, 0)
        self.assertEqual(result.failed, 1)

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertIsNone(task.enqueued_at)
        self.assertIsNone(task.queue_name)
        self.assertIn("redis push failed", task.last_error or "")

    def test_dispatcher_provider_filter(self):
        """
        Диспетчер с provider_type должен обрабатывать только свой провайдер.
        """
        task_tg = self._create_task(provider_type="telegram", priority=DispatchTask.Priority.HIGH)
        task_vk = self._create_task(provider_type="vk", priority=DispatchTask.Priority.HIGH)

        lane_queue = _FakeLaneQueueForDispatcher()
        dispatcher = UniversalTaskDispatcher(lane_queue=lane_queue, provider_type="telegram")
        result = dispatcher.enqueue_pending_tasks(batch_size=100)

        self.assertEqual(result.claimed, 1)
        self.assertEqual(result.enqueued, 1)
        self.assertEqual(len(lane_queue.envelopes), 1)
        self.assertEqual(lane_queue.envelopes[0].task_id, task_tg.id)

        task_tg.refresh_from_db()
        task_vk.refresh_from_db()
        self.assertEqual(task_tg.status, DispatchTask.Status.QUEUED)
        self.assertEqual(task_vk.status, DispatchTask.Status.PENDING)

class UniversalQueueMaintenanceTests(TestCase):
    """
    Тесты сервиса обслуживания/восстановления stale-задач.
    """

    class _FakeLaneQueue:
        @staticmethod
        def lane_lengths(provider_type: str):
            return {"high": 2, "normal": 1, "bulk": 0}

    def _create_task(
        self,
        *,
        provider_type: str,
        status: str,
        enqueued_at=None,
        started_at=None,
        attempt: int = 0,
        max_attempts: int = 3,
    ) -> DispatchTask:
        return DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=provider_type,
            priority=DispatchTask.Priority.NORMAL,
            status=status,
            message_text="maintenance",
            payload={},
            available_at=timezone.now() - timedelta(minutes=1),
            enqueued_at=enqueued_at,
            started_at=started_at,
            attempt=attempt,
            max_attempts=max_attempts,
        )

    def test_recover_stale_queued(self):
        """
        stale queued-задачи должны возвращаться в pending.
        """
        now = timezone.now()
        stale = self._create_task(
            provider_type="telegram",
            status=DispatchTask.Status.QUEUED,
            enqueued_at=now - timedelta(minutes=10),
        )
        fresh = self._create_task(
            provider_type="telegram",
            status=DispatchTask.Status.QUEUED,
            enqueued_at=now - timedelta(seconds=30),
        )
        self._create_task(
            provider_type="vk",
            status=DispatchTask.Status.QUEUED,
            enqueued_at=now - timedelta(minutes=10),
        )

        service = UniversalQueueMaintenanceService(lane_queue=self._FakeLaneQueue())
        recovered = service.recover_stale_queued(stale_seconds=180, provider_type="telegram")

        self.assertEqual(recovered, 1)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(stale.status, DispatchTask.Status.PENDING)
        self.assertIsNone(stale.enqueued_at)
        self.assertIn("auto_recovered_stale_queued", stale.last_error or "")
        self.assertEqual(fresh.status, DispatchTask.Status.QUEUED)

    def test_recover_stale_in_progress_requeues_or_fails(self):
        """
        stale in_progress: часть задач requeue, часть fail при исчерпании попыток.
        """
        now = timezone.now()
        task_requeue = self._create_task(
            provider_type="telegram",
            status=DispatchTask.Status.IN_PROGRESS,
            started_at=now - timedelta(minutes=10),
            attempt=1,
            max_attempts=3,
        )
        task_fail = self._create_task(
            provider_type="telegram",
            status=DispatchTask.Status.IN_PROGRESS,
            started_at=now - timedelta(minutes=10),
            attempt=3,
            max_attempts=3,
        )
        task_fresh = self._create_task(
            provider_type="telegram",
            status=DispatchTask.Status.IN_PROGRESS,
            started_at=now - timedelta(seconds=20),
            attempt=1,
            max_attempts=3,
        )

        service = UniversalQueueMaintenanceService(lane_queue=self._FakeLaneQueue())
        summary = service.recover_stale_in_progress(stale_seconds=180, provider_type="telegram")

        self.assertEqual(summary.recovered_in_progress, 1)
        self.assertEqual(summary.failed_in_progress, 1)

        task_requeue.refresh_from_db()
        task_fail.refresh_from_db()
        task_fresh.refresh_from_db()

        self.assertEqual(task_requeue.status, DispatchTask.Status.PENDING)
        self.assertIn("auto_recovered_stale_in_progress", task_requeue.last_error or "")

        self.assertEqual(task_fail.status, DispatchTask.Status.FAILED)
        self.assertIn("auto_failed_stale_in_progress", task_fail.last_error or "")

        self.assertEqual(task_fresh.status, DispatchTask.Status.IN_PROGRESS)

    def test_collect_health_snapshot(self):
        """
        Снимок здоровья должен включать Redis lane и статусы БД.
        """
        self._create_task(provider_type="telegram", status=DispatchTask.Status.PENDING)
        self._create_task(provider_type="telegram", status=DispatchTask.Status.QUEUED)
        self._create_task(provider_type="telegram", status=DispatchTask.Status.DONE)

        service = UniversalQueueMaintenanceService(lane_queue=self._FakeLaneQueue())
        snapshot = service.collect_health_snapshot(provider_type="telegram")

        self.assertEqual(snapshot.provider_type, "telegram")
        self.assertEqual(snapshot.redis_lane_lengths["high"], 2)
        self.assertEqual(snapshot.db_status_counts[DispatchTask.Status.PENDING], 1)
        self.assertEqual(snapshot.db_status_counts[DispatchTask.Status.QUEUED], 1)
        self.assertEqual(snapshot.db_status_counts[DispatchTask.Status.DONE], 1)


class RateLimiterSyncTests(SimpleTestCase):
    """
    Синхронная часть тестов централизованного rate limiter.
    """

    def test_interval_ms_uses_policy_and_default(self):
        """
        Интервал должен считаться по policy, для неизвестного провайдера — default.
        """
        redis = _FakeRedisForRateLimiter()
        limiter = CentralizedRedisRateLimiter(
            redis_client=redis,
            provider_policies={"telegram": ProviderRatePolicy(rate_per_second=2.0)},
        )
        self.assertEqual(limiter._interval_ms("telegram"), 500)
        self.assertEqual(limiter._interval_ms("unknown"), 100)

    def test_run_acquire_script_fallback_to_eval_on_evalsha_error(self):
        """
        После ошибки evalsha limiter должен сделать fallback на eval.
        """
        redis = _FakeRedisForRateLimiter()
        redis.raise_evalsha_once = True
        redis.eval_result = 17
        limiter = CentralizedRedisRateLimiter(redis_client=redis)

        result = limiter._run_acquire_script(provider_type="telegram", now_ms=1000)

        self.assertEqual(result, 17)
        self.assertEqual(redis.script_load_calls, 1)
        self.assertEqual(redis.evalsha_calls, 1)
        self.assertEqual(redis.eval_calls, 1)
        self.assertIsNone(limiter._script_sha)

    def test_pause_delay_uses_max_provider_and_scope(self):
        """
        При наличии scope-паузы итоговая задержка должна быть max(provider, scope).
        """
        redis = _FakeRedisForRateLimiter()
        limiter = CentralizedRedisRateLimiter(redis_client=redis, namespace="uq:test")
        now_ms = 1000

        redis.values[limiter._pause_key("telegram")] = "1500"
        redis.values[limiter._scope_pause_key("telegram", "chat_1")] = "1900"

        delay = limiter._current_pause_delay_ms("telegram", now_ms, scope_key="chat_1")
        self.assertEqual(delay, 900)

    def test_register_retry_after_sets_provider_pause(self):
        """
        register_retry_after должен писать pause-ключ провайдера в Redis.
        """
        redis = _FakeRedisForRateLimiter()
        limiter = CentralizedRedisRateLimiter(redis_client=redis, namespace="uq:test")

        with patch("guests.services.universal_queue.rate_limiter.time.time", return_value=1000.0):
            async_to_sync(limiter.register_retry_after)("telegram", 3.0)

        self.assertEqual(len(redis.psetex_calls), 1)
        key, ttl_ms, value = redis.psetex_calls[0]
        self.assertEqual(key, "uq:test:rate:telegram:pause_until_ms")
        self.assertGreaterEqual(ttl_ms, 1000)
        self.assertEqual(value, "1003000")

    def test_register_scope_retry_after_ignores_empty_scope(self):
        """
        Для пустого scope ключ не должен записываться.
        """
        redis = _FakeRedisForRateLimiter()
        limiter = CentralizedRedisRateLimiter(redis_client=redis, namespace="uq:test")

        async_to_sync(limiter.register_scope_retry_after)("telegram", "", 4.0)
        self.assertEqual(redis.psetex_calls, [])


class RateLimiterAsyncTests(SimpleTestCase):
    """
    Асинхронная часть тестов централизованного rate limiter.
    """

    def test_acquire_waits_then_grants_slot(self):
        """
        acquire должен ждать и завершаться после получения слота.
        """
        redis = _FakeRedisForRateLimiter()
        limiter = CentralizedRedisRateLimiter(redis_client=redis)

        with (
            patch.object(limiter, "_current_pause_delay_ms", side_effect=[0, 0]),
            patch.object(limiter, "_run_acquire_script", side_effect=[50, 0]),
            patch("guests.services.universal_queue.rate_limiter.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            async_to_sync(limiter.acquire)(provider_type="telegram", timeout_seconds=5.0, scope_key="chat_1")

        sleep_mock.assert_awaited_once_with(0.05)

    def test_acquire_raises_timeout(self):
        """
        При постоянном ожидании acquire должен завершаться TimeoutError.
        """
        redis = _FakeRedisForRateLimiter()
        limiter = CentralizedRedisRateLimiter(redis_client=redis)

        with (
            patch.object(limiter, "_current_pause_delay_ms", return_value=0),
            patch.object(limiter, "_run_acquire_script", return_value=100),
            patch("guests.services.universal_queue.rate_limiter.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaises(TimeoutError):
                async_to_sync(limiter.acquire)(provider_type="telegram", timeout_seconds=0.0)

class ProviderClientHelpersTests(SimpleTestCase):
    """
    Тесты helper-части provider clients.
    """

    class _TaskStub:
        def __init__(self, bot_profile=None, payload=None):
            self.bot_profile = bot_profile
            self.payload = payload or {}

    class _BotProfileStub:
        def __init__(self, token: str):
            self._token = token

        def resolve_token(self) -> str:
            return self._token

    def test_build_provider_sender_factory(self):
        """
        Фабрика sender должна возвращать корректные классы.
        """
        self.assertIsInstance(build_provider_sender("telegram"), TelegramAsyncSender)
        self.assertIsInstance(build_provider_sender("max"), MaxAsyncSender)
        self.assertIsInstance(build_provider_sender("vk"), VkAsyncSender)
        with self.assertRaises(ValueError):
            build_provider_sender("unknown")

    def test_resolve_bot_token_priority(self):
        """
        Приоритет токенов: bot_profile -> payload.bot_token -> payload.bot_token_ref -> env fallback.
        """
        with patch.dict(os.environ, {"BOT_REF_TOKEN": "from-ref", "UNIVERSAL_QUEUE_TELEGRAM_FALLBACK_TOKEN": "from-env"}):
            task_1 = self._TaskStub(
                bot_profile=self._BotProfileStub("from-profile"),
                payload={"bot_token": "from-payload"},
            )
            token_1 = async_to_sync(_resolve_bot_token)(task_1, "UNIVERSAL_QUEUE_TELEGRAM_FALLBACK_TOKEN")
            self.assertEqual(token_1, "from-profile")

            task_2 = self._TaskStub(
                bot_profile=self._BotProfileStub(""),
                payload={"bot_token": "from-payload"},
            )
            token_2 = async_to_sync(_resolve_bot_token)(task_2, "UNIVERSAL_QUEUE_TELEGRAM_FALLBACK_TOKEN")
            self.assertEqual(token_2, "from-payload")

            task_3 = self._TaskStub(
                bot_profile=self._BotProfileStub(""),
                payload={"bot_token_ref": "BOT_REF_TOKEN"},
            )
            token_3 = async_to_sync(_resolve_bot_token)(task_3, "UNIVERSAL_QUEUE_TELEGRAM_FALLBACK_TOKEN")
            self.assertEqual(token_3, "from-ref")

            task_4 = self._TaskStub(bot_profile=self._BotProfileStub(""), payload={})
            token_4 = async_to_sync(_resolve_bot_token)(task_4, "UNIVERSAL_QUEUE_TELEGRAM_FALLBACK_TOKEN")
            self.assertEqual(token_4, "from-env")


class ProviderWorkerTests(TestCase):
    """
    Тесты ключевых веток async provider-worker.
    """

    def setUp(self):
        super().setUp()
        self.guest = Guest.objects.create(
            phone="+79990001199",
            first_name="Воркер",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        self.bot = BotProfile.objects.create(
            code="tg_worker_test",
            name="TG Worker Test",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot,
            external_chat_id="321654",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

    def _create_queued_task(self, *, attempt: int = 0, max_attempts: int = 3, external_chat_id: str = "321654") -> DispatchTask:
        return DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type="telegram",
            priority=DispatchTask.Priority.HIGH,
            status=DispatchTask.Status.QUEUED,
            guest=self.guest,
            guest_binding=self.binding,
            external_chat_id=external_chat_id,
            message_text="worker test",
            payload={"kind": "worker_test"},
            available_at=timezone.now() - timedelta(minutes=1),
            max_attempts=max_attempts,
            attempt=attempt,
        )

    @staticmethod
    def _envelope_for_task(task: DispatchTask) -> QueueEnvelope:
        return QueueEnvelope(
            task_id=task.id,
            task_uuid=str(task.uuid),
            source_type=task.source_type,
            provider_type=task.provider_type,
            priority=task.priority,
            message_text=task.message_text,
            payload=task.payload if isinstance(task.payload, dict) else {},
            guest_id=task.guest_id,
            guest_binding_id=task.guest_binding_id,
            external_chat_id=task.external_chat_id or "",
            idempotency_key=task.idempotency_key,
        )

    def _build_worker(self, sender, rate_limiter: _FakeRateLimiter | None = None) -> tuple[AsyncProviderWorker, _FakeRateLimiter]:
        limiter = rate_limiter or _FakeRateLimiter()
        config = ProviderWorkerConfig(provider_type="telegram", retry_base_seconds=3.0, retry_max_seconds=300.0)
        with patch("guests.services.universal_queue.provider_worker.build_provider_sender", return_value=sender):
            worker = AsyncProviderWorker(lane_queue=Mock(), rate_limiter=limiter, config=config)
        return worker, limiter

    def test_fair_policy_cycle(self):
        """
        Проверяем формирование fair-cycle по квотам.
        """
        cycle = FairPolicy(high=2, normal=1, bulk=1).to_cycle()
        self.assertEqual(cycle, ["high", "high", "normal", "bulk"])

    def test_process_envelope_success_marks_done(self):
        """
        Успешная отправка переводит задачу в done и сохраняет метаданные ответа.
        """
        task = self._create_queued_task()
        envelope = self._envelope_for_task(task)
        worker, limiter = self._build_worker(sender=_SuccessSender())

        async_to_sync(worker._process_envelope)("high", envelope)

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.DONE)
        self.assertEqual(task.attempt, 1)
        self.assertEqual(task.last_error, None)
        self.assertEqual(task.payload.get("provider_message_id"), "provider_msg_1")
        self.assertEqual(len(limiter.acquire_calls), 1)
        self.assertEqual(limiter.acquire_calls[0][2], "321654")

    def test_process_envelope_rate_limit_requeues_and_registers_pause(self):
        """
        Rate-limit ошибка должна вернуть задачу в pending и зарегистрировать паузы.
        """
        task = self._create_queued_task()
        envelope = self._envelope_for_task(task)
        sender = _ErrorSender(ProviderRateLimitError(retry_after_seconds=5, message="limited"))
        worker, limiter = self._build_worker(sender=sender)

        async_to_sync(worker._process_envelope)("high", envelope)

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertIn("rate_limited", task.last_error or "")
        self.assertEqual(len(limiter.retry_after_calls), 1)
        self.assertEqual(limiter.retry_after_calls[0][0], "telegram")
        self.assertEqual(len(limiter.scope_retry_after_calls), 1)
        self.assertEqual(limiter.scope_retry_after_calls[0][1], "321654")

    def test_process_envelope_temporary_error_requeues(self):
        """
        Временная ошибка должна отложить задачу и оставить её в pending.
        """
        task = self._create_queued_task(attempt=0, max_attempts=3)
        envelope = self._envelope_for_task(task)
        sender = _ErrorSender(ProviderTemporaryError("temp failure"))
        worker, limiter = self._build_worker(sender=sender)

        before = timezone.now()
        async_to_sync(worker._process_envelope)("high", envelope)
        after = timezone.now()

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertIn("temporary_error", task.last_error or "")
        self.assertGreaterEqual(task.available_at, before + timedelta(seconds=2))
        self.assertLessEqual(task.available_at, after + timedelta(seconds=5))
        self.assertEqual(len(limiter.retry_after_calls), 1)
        self.assertEqual(limiter.retry_after_calls[0][0], "telegram")

    def test_process_envelope_temporary_error_exhausted_to_failed(self):
        """
        При исчерпании попыток временная ошибка переводит задачу в failed.
        """
        task = self._create_queued_task(attempt=2, max_attempts=3)
        envelope = self._envelope_for_task(task)
        sender = _ErrorSender(ProviderTemporaryError("temp exhausted"))
        worker, _ = self._build_worker(sender=sender)

        async_to_sync(worker._process_envelope)("high", envelope)

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.FAILED)
        self.assertIn("temporary_error_exhausted", task.last_error or "")

    def test_process_envelope_blocked_marks_binding_and_task_failed(self):
        """
        blocked-ошибка должна отключить binding и перевести задачу в failed.
        """
        task = self._create_queued_task()
        envelope = self._envelope_for_task(task)
        sender = _ErrorSender(ProviderBlockedError("blocked"))
        worker, _ = self._build_worker(sender=sender)

        async_to_sync(worker._process_envelope)("high", envelope)

        task.refresh_from_db()
        self.binding.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.FAILED)
        self.assertIn("blocked", task.last_error or "")
        self.assertTrue(self.binding.is_stop_sending)
        self.assertFalse(self.binding.is_active)
