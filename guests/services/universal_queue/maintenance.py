import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, Optional

from django.db.models import Count
from django.utils import timezone

from guests.models import DispatchTask
from guests.services.universal_queue.redis_lanes import ProviderLaneQueue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoverySummary:
    """
    Сводка восстановления «зависших» задач universal queue.
    """

    recovered_queued: int = 0
    recovered_in_progress: int = 0
    failed_in_progress: int = 0


@dataclass(frozen=True)
class QueueHealthSnapshot:
    """
    Снимок состояния очереди в Redis и БД.
    """

    provider_type: str
    redis_lane_lengths: Dict[str, int]
    db_status_counts: Dict[str, int]


class UniversalQueueMaintenanceService:
    """
    Сервис регулярного обслуживания очереди dispatch-задач.

    Задачи сервиса:
    1. Возвращать stale `queued` задачи обратно в `pending`.
    2. Возвращать stale `in_progress` задачи в `pending` (или `failed`, если исчерпаны попытки).
    3. Собирать health-метрики Redis lane + статусов в БД.
    """

    PROVIDERS = ("telegram", "max", "vk")

    def __init__(self, lane_queue: ProviderLaneQueue):
        self.lane_queue = lane_queue

    def _with_provider_filter(self, queryset, provider_type: Optional[str]):
        if provider_type:
            return queryset.filter(provider_type=provider_type)
        return queryset.filter(provider_type__in=self.PROVIDERS)

    def recover_stale_queued(self, stale_seconds: int, provider_type: Optional[str] = None) -> int:
        """
        Возвращает в `pending` задачи, которые слишком долго находятся в `queued`.
        """
        safe_stale_seconds = max(1, int(stale_seconds))
        cutoff = timezone.now() - timedelta(seconds=safe_stale_seconds)
        now = timezone.now()

        queryset = DispatchTask.objects.filter(
            status=DispatchTask.Status.QUEUED,
            enqueued_at__isnull=False,
            enqueued_at__lt=cutoff,
        )
        queryset = self._with_provider_filter(queryset, provider_type)

        recovered = queryset.update(
            status=DispatchTask.Status.PENDING,
            enqueued_at=None,
            queue_name=None,
            started_at=None,
            finished_at=None,
            available_at=now,
            updated_at=now,
            last_error=f"auto_recovered_stale_queued: older_than={safe_stale_seconds}s",
        )
        return int(recovered)

    def recover_stale_in_progress(self, stale_seconds: int, provider_type: Optional[str] = None) -> RecoverySummary:
        """
        Восстанавливает stale `in_progress` задачи:
        1. Если попытки не исчерпаны -> возвращает в `pending`.
        2. Если `attempt >= max_attempts` -> переводит в `failed`.
        """
        safe_stale_seconds = max(1, int(stale_seconds))
        cutoff = timezone.now() - timedelta(seconds=safe_stale_seconds)
        now = timezone.now()

        stale_queryset = DispatchTask.objects.filter(
            status=DispatchTask.Status.IN_PROGRESS,
            started_at__isnull=False,
            started_at__lt=cutoff,
        )
        stale_queryset = self._with_provider_filter(stale_queryset, provider_type)

        # Сравнение `attempt >= max_attempts` делаем в Python по срезу,
        # чтобы логика была прозрачной и одинаковой для разных СУБД.
        stale_rows = list(stale_queryset.values_list("id", "attempt", "max_attempts"))
        failed_ids = [
            task_id
            for task_id, attempt, max_attempts in stale_rows
            if int(attempt or 0) >= int(max_attempts or 0)
        ]
        failed_ids_set = set(failed_ids)
        requeue_ids = [task_id for task_id, _, _ in stale_rows if task_id not in failed_ids_set]

        failed_count = 0
        recovered_count = 0

        if failed_ids:
            failed_count = DispatchTask.objects.filter(id__in=failed_ids).update(
                status=DispatchTask.Status.FAILED,
                finished_at=now,
                updated_at=now,
                last_error=f"auto_failed_stale_in_progress: attempts_exhausted older_than={safe_stale_seconds}s",
            )

        if requeue_ids:
            recovered_count = DispatchTask.objects.filter(id__in=requeue_ids).update(
                status=DispatchTask.Status.PENDING,
                enqueued_at=None,
                queue_name=None,
                started_at=None,
                finished_at=None,
                available_at=now,
                updated_at=now,
                last_error=f"auto_recovered_stale_in_progress: older_than={safe_stale_seconds}s",
            )

        return RecoverySummary(
            recovered_queued=0,
            recovered_in_progress=int(recovered_count),
            failed_in_progress=int(failed_count),
        )

    def recover_stale_tasks(
        self,
        queued_stale_seconds: int,
        in_progress_stale_seconds: int,
        provider_type: Optional[str] = None,
    ) -> RecoverySummary:
        """
        Выполняет полный проход восстановления stale задач.
        """
        recovered_queued = self.recover_stale_queued(
            stale_seconds=queued_stale_seconds,
            provider_type=provider_type,
        )
        in_progress_summary = self.recover_stale_in_progress(
            stale_seconds=in_progress_stale_seconds,
            provider_type=provider_type,
        )
        return RecoverySummary(
            recovered_queued=int(recovered_queued),
            recovered_in_progress=in_progress_summary.recovered_in_progress,
            failed_in_progress=in_progress_summary.failed_in_progress,
        )

    def collect_health_snapshot(self, provider_type: str) -> QueueHealthSnapshot:
        """
        Собирает health snapshot для одного провайдера.
        """
        redis_lane_lengths = self.lane_queue.lane_lengths(provider_type=provider_type)

        queryset = DispatchTask.objects.filter(provider_type=provider_type).values("status").annotate(total=Count("id"))
        db_status_counts = {row["status"]: int(row["total"]) for row in queryset}

        return QueueHealthSnapshot(
            provider_type=provider_type,
            redis_lane_lengths=redis_lane_lengths,
            db_status_counts=db_status_counts,
        )

    def collect_health_snapshots(self, provider_type: Optional[str] = None) -> Dict[str, QueueHealthSnapshot]:
        """
        Собирает health snapshots по всем провайдерам или по одному.
        """
        providers = [provider_type] if provider_type else list(self.PROVIDERS)
        snapshots: Dict[str, QueueHealthSnapshot] = {}
        for provider in providers:
            snapshots[provider] = self.collect_health_snapshot(provider_type=provider)
        return snapshots
