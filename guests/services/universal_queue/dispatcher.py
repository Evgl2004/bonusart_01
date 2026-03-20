import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from guests.models import DispatchTask
from guests.services.universal_queue.redis_lanes import ProviderLaneQueue, QueueEnvelope

logger = logging.getLogger(__name__)


@dataclass
class DispatchBatchResult:
    """
    Результат пакетной постановки задач в Redis-очереди.
    """

    claimed: int = 0
    enqueued: int = 0
    failed: int = 0


class UniversalTaskDispatcher:
    """
    Диспетчер задач из БД в Redis lane-очереди.

    Алгоритм:
    1. Атомарно "захватывает" пачку pending-задач.
    2. Переводит их в queued + фиксирует enqueued_at.
    3. Публикует задачи в Redis по lane-ключам провайдера.
    4. При ошибке откатывает задачу обратно в pending.
    """

    def __init__(self, lane_queue: ProviderLaneQueue, provider_type: Optional[str] = None):
        self.lane_queue = lane_queue
        self.provider_type = str(provider_type).strip().lower() if provider_type else None

        if self.provider_type and self.provider_type not in ProviderLaneQueue.PROVIDERS:
            raise ValueError(f"Неподдерживаемый provider_type={provider_type}")

    @staticmethod
    def _priority_order_expression() -> Case:
        """
        SQL-выражение для сортировки приоритетов:
        high -> normal -> bulk.
        """
        return Case(
            When(priority=DispatchTask.Priority.HIGH, then=Value(0)),
            When(priority=DispatchTask.Priority.NORMAL, then=Value(1)),
            When(priority=DispatchTask.Priority.BULK, then=Value(2)),
            default=Value(99),
            output_field=IntegerField(),
        )

    def _claim_pending_tasks(self, batch_size: int) -> Tuple[List[DispatchTask], datetime]:
        """
        Захватывает задачи на диспетчеризацию с блокировкой строк.
        """
        now = timezone.now()
        with transaction.atomic():
            queryset = (
                DispatchTask.objects.select_for_update(skip_locked=True)
                .filter(
                    status=DispatchTask.Status.PENDING,
                    enqueued_at__isnull=True,
                    available_at__lte=now,
                )
            )

            # Если задан provider_type, диспетчер обрабатывает только свой провайдер.
            if self.provider_type:
                queryset = queryset.filter(provider_type=self.provider_type)

            # Важно: здесь нельзя делать `select_related("guest_binding")`,
            # т.к. `guest_binding` nullable и PostgreSQL не разрешает
            # `FOR UPDATE` на NULL-содержащей стороне OUTER JOIN.
            tasks = list(
                queryset
                .annotate(priority_rank=self._priority_order_expression())
                .order_by("priority_rank", "available_at", "id")[:batch_size]
            )

            if not tasks:
                return [], now

            task_ids = [task.id for task in tasks]
            claim_time = timezone.now()
            DispatchTask.objects.filter(id__in=task_ids).update(
                status=DispatchTask.Status.QUEUED,
                enqueued_at=claim_time,
                updated_at=claim_time,
                last_error=None,
            )
            # Подгружаем `guest_binding` отдельным запросом уже без lock-join.
            tasks_with_binding = list(
                DispatchTask.objects.select_related("guest_binding")
                .filter(id__in=task_ids)
                .annotate(priority_rank=self._priority_order_expression())
                .order_by("priority_rank", "available_at", "id")
            )
            return tasks_with_binding, claim_time

    @staticmethod
    def _resolve_external_chat_id(task: DispatchTask) -> str:
        """
        Вычисляет целевой chat/peer id для отправки.
        """
        if task.external_chat_id:
            return task.external_chat_id
        if task.guest_binding and task.guest_binding.external_chat_id:
            return task.guest_binding.external_chat_id
        return ""

    def _build_envelope(self, task: DispatchTask) -> QueueEnvelope:
        """
        Собирает транспортный конверт задачи для Redis.
        """
        payload = task.payload if isinstance(task.payload, dict) else {}
        return QueueEnvelope(
            task_id=task.id,
            task_uuid=str(task.uuid),
            source_type=task.source_type,
            provider_type=task.provider_type,
            priority=task.priority,
            message_text=task.message_text or "",
            payload=payload,
            guest_id=task.guest_id,
            guest_binding_id=task.guest_binding_id,
            external_chat_id=self._resolve_external_chat_id(task),
            idempotency_key=task.idempotency_key,
        )

    def enqueue_pending_tasks(self, batch_size: int = 200) -> DispatchBatchResult:
        """
        Переносит пачку pending-задач из БД в Redis lane-очереди.
        """
        result = DispatchBatchResult()
        tasks, _claim_time = self._claim_pending_tasks(batch_size=batch_size)
        if not tasks:
            return result

        result.claimed = len(tasks)

        for task in tasks:
            try:
                envelope = self._build_envelope(task)
                queue_name = self.lane_queue.push(envelope)
                DispatchTask.objects.filter(id=task.id).update(
                    queue_name=queue_name,
                    updated_at=timezone.now(),
                    last_error=None,
                )
                result.enqueued += 1
            except Exception as err:
                logger.exception(
                    "Ошибка постановки задачи в Redis: task_id=%s provider=%s priority=%s",
                    task.id,
                    task.provider_type,
                    task.priority,
                )
                DispatchTask.objects.filter(id=task.id).update(
                    status=DispatchTask.Status.PENDING,
                    enqueued_at=None,
                    queue_name=None,
                    last_error=str(err)[:2000],
                    updated_at=timezone.now(),
                )
                result.failed += 1

        return result
