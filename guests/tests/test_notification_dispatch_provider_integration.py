"""
Интеграционные тесты цепочки:
NotificationEvent -> DispatchTask -> dispatcher -> provider-worker.

Содержит:
1. «Хороший» сценарий сквозной доставки;
2. bug-seeking сценарий дедупликации по dedupe_key;
3. bug-seeking сценарий временной ошибки провайдера с переносом задачи.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    GuestBotBinding,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
    NotificationScenarioBotProfileLink,
)
from guests.services.notification_events import create_notification_event
from guests.services.universal_queue.dispatcher import UniversalTaskDispatcher
from guests.services.universal_queue.provider_clients import (
    ProviderSendResult,
    ProviderTemporaryError,
)
from guests.services.universal_queue.provider_worker import AsyncProviderWorker, ProviderWorkerConfig
from guests.services.universal_queue.redis_lanes import QueueEnvelope


class _InMemoryLaneQueueForNotifyPipeline:
    """
    In-memory lane-очередь для тестов notification-пайплайна.
    """

    PRIORITY_ORDER = ("high", "normal", "bulk")
    PROVIDERS = ("telegram", "max", "vk")

    def __init__(self, namespace: str = "uq:notify-test"):
        self.namespace = namespace
        self._lanes: dict[str, list[QueueEnvelope]] = {}

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
        return None


class _FakeRateLimiter:
    """
    Минимальный rate-limiter для запуска AsyncProviderWorker в тестах.
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


class _CollectingSender:
    """
    Успешный sender, собирающий task_id отправленных задач.
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
            provider_message_id=f"notify-msg-{task.id}",
            sent_at=timezone.now(),
            raw_response={"ok": True, "chat_id": chat_id, "text": text},
        )


class _TemporaryErrorSender:
    """
    Sender, который всегда отдаёт временную ошибку.
    """

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def send(self, task: DispatchTask, chat_id: str, text: str) -> ProviderSendResult:
        raise ProviderTemporaryError("temporary API outage")


class NotificationDispatchProviderIntegrationTests(TestCase):
    """
    Сквозные интеграционные тесты цепочки уведомлений до отправки провайдеру.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest = Guest.objects.create(
            phone="+79990007788",
            first_name="Уведомление",
            created_at=now,
            updated_at=now,
        )
        self.template = MessageTemplate.objects.create(
            name="NOTIFY_PIPELINE_TEMPLATE",
            description="Шаблон для тестов notification pipeline",
            message_text="Здравствуйте, {first_name}. {event_text}",
            created_by="tests",
            is_active=True,
        )
        self.bot = BotProfile.objects.create(
            code="tg_notify_pipeline_test",
            name="TG Notify Pipeline Test Bot",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot,
            external_chat_id="notify-chat-7788",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        self.scenario = NotificationScenario.objects.create(
            code="notify_pipeline_test",
            name="Notify Pipeline Test",
            description="Сценарий для интеграционных тестов цепочки уведомлений.",
            is_active=True,
            is_system=False,
            trigger_type=NotificationScenario.TriggerType.WEBHOOK,
            template=self.template,
            priority=NotificationScenario.Priority.HIGH,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            settings={},
        )
        NotificationScenarioBotProfileLink.objects.create(
            scenario=self.scenario,
            bot_profile=self.bot,
        )

    @staticmethod
    def _run_provider_once(*, lane_queue, sender, limiter) -> None:
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
                rate_limiter=limiter,
                config=config,
            )
            async_to_sync(worker.run)()

    def _create_event(self, *, dedupe_key: str, event_text: str = "Баланс изменился") -> int:
        return create_notification_event(
            scenario_code=self.scenario.code,
            guest=self.guest,
            dedupe_key=dedupe_key,
            source_ref="webhook-test",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "notify_pipeline"},
            template_context={
                "first_name": self.guest.first_name,
                "event_text": event_text,
            },
            fallback_message_text=event_text,
        )

    def test_notification_pipeline_happy_path(self):
        """
        Хороший сценарий: создаём событие, диспетчеризуем, отправляем, задача становится DONE.
        """
        created_count = self._create_event(dedupe_key="notify:ok:1")
        self.assertEqual(created_count, 1)

        event = NotificationEvent.objects.get(dedupe_key="notify:ok:1")
        task = DispatchTask.objects.get(notification_event=event)
        self.assertEqual(task.status, DispatchTask.Status.PENDING)

        queue = _InMemoryLaneQueueForNotifyPipeline()
        dispatcher = UniversalTaskDispatcher(lane_queue=queue)
        dispatch_result = dispatcher.enqueue_pending_tasks(batch_size=100)
        self.assertEqual(dispatch_result.enqueued, 1)
        self.assertEqual(queue.lane_lengths("telegram")["high"], 1)

        sender = _CollectingSender()
        limiter = _FakeRateLimiter()
        self._run_provider_once(lane_queue=queue, sender=sender, limiter=limiter)

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.DONE)
        self.assertEqual(task.attempt, 1)
        self.assertEqual(task.payload.get("provider_message_id"), f"notify-msg-{task.id}")
        self.assertEqual(sender.sent_task_ids, [task.id])
        self.assertEqual(len(limiter.acquire_calls), 1)

    def test_notification_pipeline_deduplicates_and_sends_once(self):
        """
        Bug-seeking: повторный вызов с тем же dedupe_key не должен создать вторую задачу отправки.
        """
        first = self._create_event(dedupe_key="notify:dup:1", event_text="Первый")
        second = self._create_event(dedupe_key="notify:dup:1", event_text="Повтор")
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

        event = NotificationEvent.objects.get(dedupe_key="notify:dup:1")
        self.assertEqual(event.duplicate_hits, 1)
        self.assertEqual(DispatchTask.objects.filter(notification_event=event).count(), 1)

        queue = _InMemoryLaneQueueForNotifyPipeline()
        dispatcher = UniversalTaskDispatcher(lane_queue=queue)
        dispatch_result = dispatcher.enqueue_pending_tasks(batch_size=100)
        self.assertEqual(dispatch_result.enqueued, 1)

        sender = _CollectingSender()
        limiter = _FakeRateLimiter()
        self._run_provider_once(lane_queue=queue, sender=sender, limiter=limiter)

        self.assertEqual(len(sender.sent_task_ids), 1)
        task = DispatchTask.objects.get(notification_event=event)
        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.DONE)

    def test_notification_pipeline_temporary_error_requeues_task(self):
        """
        Bug-seeking: при временной ошибке провайдера задача переносится в PENDING с future available_at.
        """
        created_count = self._create_event(dedupe_key="notify:tmp:1")
        self.assertEqual(created_count, 1)

        event = NotificationEvent.objects.get(dedupe_key="notify:tmp:1")
        task = DispatchTask.objects.get(notification_event=event)

        queue = _InMemoryLaneQueueForNotifyPipeline()
        dispatcher = UniversalTaskDispatcher(lane_queue=queue)
        dispatch_result = dispatcher.enqueue_pending_tasks(batch_size=100)
        self.assertEqual(dispatch_result.enqueued, 1)

        limiter = _FakeRateLimiter()
        self._run_provider_once(
            lane_queue=queue,
            sender=_TemporaryErrorSender(),
            limiter=limiter,
        )

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertEqual(task.attempt, 1)
        self.assertIn("temporary_error", task.last_error or "")
        self.assertGreater(task.available_at, timezone.now())
        self.assertEqual(task.notification_event_id, event.id)
        self.assertEqual(len(limiter.retry_after_calls), 1)
        self.assertEqual(queue.lane_lengths("telegram")["high"], 0)

        immediate_dispatch = dispatcher.enqueue_pending_tasks(batch_size=100)
        self.assertEqual(immediate_dispatch.claimed, 0)

        task.available_at = timezone.now() - timedelta(seconds=1)
        task.save(update_fields=["available_at", "updated_at"])
        second_dispatch = dispatcher.enqueue_pending_tasks(batch_size=100)
        self.assertEqual(second_dispatch.enqueued, 1)
