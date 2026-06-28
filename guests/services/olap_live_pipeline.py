"""
Оперативный OLAP-конвейер для быстрых действий после входящих webhook iikoCard.

Назначение:
1. взять связанную запись `OlapCheckSyncJournal`;
2. точечно дозагрузить чек из OLAP, если он ещё не загружен;
3. пересобрать `OrderFact` только по ключу этого чека;
4. обработать применение купона и создать штатные очереди vtelemax/iikoCard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import logging
from typing import Iterable

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from guests.models import OlapCheckSyncJournal, OlapLivePipelineQueue, OlapSalesRawLine, OrderFact
from guests.services.coupon_redemption_sync import CouponRedemptionSyncService
from guests.services.iiko_olap_client import IikoOlapClient
from guests.services.olap_check_sync import OlapCheckSyncWorkerService
from guests.services.order_fact import rebuild_order_fact_for_order_keys

logger = logging.getLogger(__name__)


@dataclass
class OlapLivePipelineBatchStats:
    """
    Сводная статистика одного прохода оперативного OLAP-конвейера.
    """

    recovered_stale: int = 0
    claimed: int = 0
    processed: int = 0
    waiting_olap: int = 0
    olap_loaded: int = 0
    facts_built: int = 0
    coupon_synced: int = 0
    done: int = 0
    retried: int = 0
    skipped: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "recovered_stale": self.recovered_stale,
            "claimed": self.claimed,
            "processed": self.processed,
            "waiting_olap": self.waiting_olap,
            "olap_loaded": self.olap_loaded,
            "facts_built": self.facts_built,
            "coupon_synced": self.coupon_synced,
            "done": self.done,
            "retried": self.retried,
            "skipped": self.skipped,
            "failed": self.failed,
        }


def ensure_live_pipeline_task_for_journal(
    *,
    journal: OlapCheckSyncJournal,
) -> tuple[OlapLivePipelineQueue | None, bool]:
    """
    Идемпотентно создаёт задачу оперативного конвейера для OLAP-журнала.
    """

    if not bool(getattr(settings, "OLAP_LIVE_PIPELINE_ENABLED", False)):
        return None, False

    task, created = OlapLivePipelineQueue.objects.get_or_create(
        sync_journal=journal,
        defaults={
            "source_webhook_id": journal.source_webhook_id,
            "business_date": journal.business_date,
            "department_id": journal.department_id,
            "order_number": journal.order_number,
            "order_external_id": journal.order_external_id,
            "status": OlapLivePipelineQueue.Status.NEW,
            "next_retry_at": timezone.now(),
        },
    )
    return task, created


class OlapLivePipelineService:
    """
    Сервис короткого оперативного конвейера по свежим OLAP-задачам.
    """

    def __init__(
        self,
        *,
        client: IikoOlapClient,
        batch_size: int = 20,
        order_fact_batch_size: int = 2000,
        max_attempts: int = 10,
        retry_base_seconds: int = 30,
        retry_max_seconds: int = 600,
        lock_timeout_seconds: int = 300,
        olap_portion_size: int = 20,
        olap_max_attempts: int = 5,
        olap_retry_base_seconds: int = 120,
        olap_lock_timeout_seconds: int = 900,
    ) -> None:
        self.client = client
        self.batch_size = max(1, int(batch_size))
        self.order_fact_batch_size = max(100, int(order_fact_batch_size))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))
        self.lock_timeout_seconds = max(60, int(lock_timeout_seconds))
        self.olap_portion_size = max(1, int(olap_portion_size))
        self.olap_max_attempts = max(1, int(olap_max_attempts))
        self.olap_retry_base_seconds = max(1, int(olap_retry_base_seconds))
        self.olap_lock_timeout_seconds = max(60, int(olap_lock_timeout_seconds))

    @classmethod
    def from_settings(cls, *, client: IikoOlapClient) -> "OlapLivePipelineService":
        return cls(
            client=client,
            batch_size=int(getattr(settings, "OLAP_LIVE_PIPELINE_BATCH_SIZE", 20) or 20),
            order_fact_batch_size=int(
                getattr(settings, "OLAP_LIVE_PIPELINE_ORDER_FACT_BATCH_SIZE", 2000) or 2000
            ),
            max_attempts=int(getattr(settings, "OLAP_LIVE_PIPELINE_MAX_ATTEMPTS", 10) or 10),
            retry_base_seconds=int(
                getattr(settings, "OLAP_LIVE_PIPELINE_RETRY_BASE_SECONDS", 30) or 30
            ),
            retry_max_seconds=int(
                getattr(settings, "OLAP_LIVE_PIPELINE_RETRY_MAX_SECONDS", 600) or 600
            ),
            lock_timeout_seconds=int(
                getattr(settings, "OLAP_LIVE_PIPELINE_LOCK_TIMEOUT_SECONDS", 300) or 300
            ),
            olap_portion_size=int(getattr(settings, "OLAP_LIVE_PIPELINE_OLAP_PORTION_SIZE", 20) or 20),
            olap_max_attempts=int(
                getattr(settings, "OLAP_SYNC_SCHEDULE_MAX_ATTEMPTS", 5) or 5
            ),
            olap_retry_base_seconds=int(
                getattr(settings, "OLAP_SYNC_SCHEDULE_RETRY_BASE_SECONDS", 120) or 120
            ),
            olap_lock_timeout_seconds=int(
                getattr(settings, "OLAP_SYNC_SCHEDULE_LOCK_TIMEOUT_SECONDS", 900) or 900
            ),
        )

    def process_batch(self) -> OlapLivePipelineBatchStats:
        """
        Выполняет один короткий проход по очереди оперативного конвейера.
        """

        stats = OlapLivePipelineBatchStats()
        now = timezone.now()
        stats.recovered_stale = self._recover_stale(now=now)
        tasks = self._claim_due_tasks(now=now)
        stats.claimed = len(tasks)

        for task in tasks:
            try:
                self._process_one(task=task, stats=stats)
                stats.processed += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Оперативный OLAP-конвейер: ошибка обработки task_id=%s: %s",
                    task.id,
                    exc,
                )
                self._mark_retry(
                    task=task,
                    status=OlapLivePipelineQueue.Status.RETRY,
                    error_text=f"Ошибка оперативного конвейера: {exc}",
                    result={"exception": str(exc)},
                )
                stats.retried += 1

        return stats

    def _recover_stale(self, *, now) -> int:
        stale_before = now - timedelta(seconds=self.lock_timeout_seconds)
        return OlapLivePipelineQueue.objects.filter(
            status=OlapLivePipelineQueue.Status.IN_PROGRESS,
            locked_at__lt=stale_before,
        ).update(
            status=OlapLivePipelineQueue.Status.RETRY,
            next_retry_at=now,
            locked_at=None,
            last_error="Задача возвращена в повтор после тайм-аута блокировки.",
            updated_at=now,
        )

    def _claim_due_tasks(self, *, now) -> list[OlapLivePipelineQueue]:
        due_filter = (
            Q(status=OlapLivePipelineQueue.Status.NEW)
            | Q(status=OlapLivePipelineQueue.Status.OLAP_LOADED)
            | Q(status=OlapLivePipelineQueue.Status.FACT_BUILT)
            | Q(
                status__in=[
                    OlapLivePipelineQueue.Status.WAITING_OLAP,
                    OlapLivePipelineQueue.Status.RETRY,
                ],
                next_retry_at__isnull=True,
            )
            | Q(
                status__in=[
                    OlapLivePipelineQueue.Status.WAITING_OLAP,
                    OlapLivePipelineQueue.Status.RETRY,
                ],
                next_retry_at__lte=now,
            )
        )

        with transaction.atomic():
            rows = list(
                OlapLivePipelineQueue.objects.select_for_update(skip_locked=True)
                .select_related("sync_journal")
                .filter(due_filter)
                .order_by("created_at", "id")[: self.batch_size]
            )
            for row in rows:
                row.status = OlapLivePipelineQueue.Status.IN_PROGRESS
                row.locked_at = now
                row.attempt_count = int(row.attempt_count or 0) + 1
                row.last_error = None
                row.save(
                    update_fields=[
                        "status",
                        "locked_at",
                        "attempt_count",
                        "last_error",
                        "updated_at",
                    ]
                )
            return rows

    def _process_one(
        self,
        *,
        task: OlapLivePipelineQueue,
        stats: OlapLivePipelineBatchStats,
    ) -> None:
        task.refresh_from_db()
        journal = task.sync_journal
        journal.refresh_from_db()

        if not self._ensure_olap_loaded(task=task, journal=journal, stats=stats):
            return

        self._save_stage(
            task=task,
            status=OlapLivePipelineQueue.Status.OLAP_LOADED,
            result={"journal_status": journal.status},
        )
        stats.olap_loaded += 1

        order_keys = self._load_order_keys(journal=journal)
        if not order_keys:
            self._mark_retry(
                task=task,
                status=OlapLivePipelineQueue.Status.RETRY,
                error_text="OLAP загружен, но сырые строки для ключа чека не найдены.",
                result={"journal_id": journal.id, "order_number": journal.order_number},
            )
            stats.retried += 1
            return

        fact_stats = rebuild_order_fact_for_order_keys(
            order_keys=order_keys,
            batch_size=self.order_fact_batch_size,
        )
        order_facts = self._load_order_facts(order_keys=order_keys)
        if not order_facts:
            self._mark_retry(
                task=task,
                status=OlapLivePipelineQueue.Status.RETRY,
                error_text="После пересборки не найден `OrderFact` по ключу чека.",
                result={"order_keys": self._serialize_order_keys(order_keys)},
            )
            stats.retried += 1
            return

        order_fact_ids = [int(item.id) for item in order_facts]
        self._save_stage(
            task=task,
            status=OlapLivePipelineQueue.Status.FACT_BUILT,
            result={
                "order_keys": self._serialize_order_keys(order_keys),
                "order_fact_ids": order_fact_ids,
                "fact_stats": fact_stats.__dict__,
            },
        )
        stats.facts_built += 1

        if not bool(getattr(settings, "COUPON_REDEMPTION_SYNC_ENABLED", True)):
            self._mark_skipped(
                task=task,
                error_text="Синхронизация применений купонов отключена: COUPON_REDEMPTION_SYNC_ENABLED=False.",
                result={"order_fact_ids": order_fact_ids},
            )
            stats.skipped += 1
            return

        coupon_stats = CouponRedemptionSyncService().sync_from_order_facts(
            order_fact_ids=order_fact_ids,
            dry_run=False,
        )
        self._mark_done(
            task=task,
            result={
                "order_fact_ids": order_fact_ids,
                "coupon_stats": coupon_stats.to_dict(),
            },
        )
        stats.coupon_synced += 1
        stats.done += 1

    def _ensure_olap_loaded(
        self,
        *,
        task: OlapLivePipelineQueue,
        journal: OlapCheckSyncJournal,
        stats: OlapLivePipelineBatchStats,
    ) -> bool:
        if journal.status == OlapCheckSyncJournal.Status.LOADED:
            return True

        now = timezone.now()
        if journal.status == OlapCheckSyncJournal.Status.RETRY and journal.next_try_at and journal.next_try_at > now:
            self._mark_retry(
                task=task,
                status=OlapLivePipelineQueue.Status.WAITING_OLAP,
                error_text="OLAP-журнал ожидает своего времени повтора.",
                result={"journal_next_try_at": journal.next_try_at.isoformat()},
                next_retry_at=journal.next_try_at,
            )
            stats.waiting_olap += 1
            return False

        if journal.status in {
            OlapCheckSyncJournal.Status.NEW,
            OlapCheckSyncJournal.Status.RETRY,
        }:
            worker = OlapCheckSyncWorkerService(
                client=self.client,
                claim_limit=1,
                portion_size=self.olap_portion_size,
                max_attempts=self.olap_max_attempts,
                retry_base_seconds=self.olap_retry_base_seconds,
                lock_timeout_seconds=self.olap_lock_timeout_seconds,
            )
            worker.run_for_journal_ids(journal_ids=[int(journal.id)])
            journal.refresh_from_db()
            if journal.status == OlapCheckSyncJournal.Status.LOADED:
                return True

        if journal.status == OlapCheckSyncJournal.Status.IN_PROGRESS:
            self._mark_retry(
                task=task,
                status=OlapLivePipelineQueue.Status.WAITING_OLAP,
                error_text="OLAP-журнал уже обрабатывается другим воркером.",
                result={"journal_status": journal.status},
            )
            stats.waiting_olap += 1
            return False

        if journal.status == OlapCheckSyncJournal.Status.RETRY:
            self._mark_retry(
                task=task,
                status=OlapLivePipelineQueue.Status.WAITING_OLAP,
                error_text=journal.last_error or "OLAP пока не вернул чек, повторим позже.",
                result={"journal_status": journal.status},
                next_retry_at=journal.next_try_at,
            )
            stats.waiting_olap += 1
            return False

        if journal.status == OlapCheckSyncJournal.Status.SKIPPED:
            self._mark_skipped(
                task=task,
                error_text=journal.last_error or "OLAP-журнал пропущен: чек не найден или не подходит под фильтры.",
                result={"journal_status": journal.status},
            )
            stats.skipped += 1
            return False

        self._mark_failed(
            task=task,
            error_text=journal.last_error or f"OLAP-журнал завершён статусом {journal.status}.",
            result={"journal_status": journal.status},
        )
        stats.failed += 1
        return False

    def _load_order_keys(self, *, journal: OlapCheckSyncJournal) -> list[tuple[date, str, int, str]]:
        if journal.business_date is None or journal.order_number is None:
            return []

        rows = (
            OlapSalesRawLine.objects.filter(
                business_date=journal.business_date,
                order_number=int(journal.order_number),
            )
            .filter(department_id=journal.department_id or "")
            .values_list("business_date", "department_id", "order_number", "uniq_order_id")
            .distinct()
        )
        return [
            (
                business_day,
                str(department_id or ""),
                int(order_number),
                str(uniq_order_id or ""),
            )
            for business_day, department_id, order_number, uniq_order_id in rows
        ]

    @staticmethod
    def _load_order_facts(*, order_keys: Iterable[tuple[date, str, int, str]]) -> list[OrderFact]:
        key_filter = Q()
        keys = list(order_keys)
        for business_day, department_id, order_number, uniq_order_id in keys:
            key_filter |= Q(
                business_date=business_day,
                department_id=department_id,
                order_number=int(order_number),
                uniq_order_id=uniq_order_id,
            )
        if not keys:
            return []
        return list(OrderFact.objects.filter(key_filter).order_by("id"))

    @staticmethod
    def _serialize_order_keys(order_keys: Iterable[tuple[date, str, int, str]]) -> list[dict[str, object]]:
        return [
            {
                "business_date": business_day.isoformat(),
                "department_id": department_id,
                "order_number": int(order_number),
                "uniq_order_id": uniq_order_id,
            }
            for business_day, department_id, order_number, uniq_order_id in order_keys
        ]

    def _save_stage(
        self,
        *,
        task: OlapLivePipelineQueue,
        status: str,
        result: dict,
    ) -> None:
        task.status = status
        task.locked_at = None
        task.next_retry_at = None
        task.last_error = None
        task.last_step_result = result
        task.save(
            update_fields=[
                "status",
                "locked_at",
                "next_retry_at",
                "last_error",
                "last_step_result",
                "updated_at",
            ]
        )

    def _mark_retry(
        self,
        *,
        task: OlapLivePipelineQueue,
        status: str,
        error_text: str,
        result: dict,
        next_retry_at=None,
    ) -> None:
        if int(task.attempt_count or 0) >= self.max_attempts:
            self._mark_failed(task=task, error_text=error_text, result=result)
            return

        now = timezone.now()
        delay_seconds = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, int(task.attempt_count or 1) - 1)),
        )
        task.status = status
        task.locked_at = None
        task.next_retry_at = next_retry_at or (now + timedelta(seconds=delay_seconds))
        task.last_error = (error_text or "")[:2000]
        task.last_step_result = result
        task.save(
            update_fields=[
                "status",
                "locked_at",
                "next_retry_at",
                "last_error",
                "last_step_result",
                "updated_at",
            ]
        )

    @staticmethod
    def _mark_done(*, task: OlapLivePipelineQueue, result: dict) -> None:
        now = timezone.now()
        task.status = OlapLivePipelineQueue.Status.DONE
        task.locked_at = None
        task.next_retry_at = None
        task.last_error = None
        task.last_step_result = result
        task.processed_at = now
        task.save(
            update_fields=[
                "status",
                "locked_at",
                "next_retry_at",
                "last_error",
                "last_step_result",
                "processed_at",
                "updated_at",
            ]
        )

    @staticmethod
    def _mark_skipped(*, task: OlapLivePipelineQueue, error_text: str, result: dict) -> None:
        now = timezone.now()
        task.status = OlapLivePipelineQueue.Status.SKIPPED
        task.locked_at = None
        task.next_retry_at = None
        task.last_error = (error_text or "")[:2000]
        task.last_step_result = result
        task.processed_at = now
        task.save(
            update_fields=[
                "status",
                "locked_at",
                "next_retry_at",
                "last_error",
                "last_step_result",
                "processed_at",
                "updated_at",
            ]
        )

    @staticmethod
    def _mark_failed(*, task: OlapLivePipelineQueue, error_text: str, result: dict) -> None:
        task.status = OlapLivePipelineQueue.Status.FAILED
        task.locked_at = None
        task.next_retry_at = None
        task.last_error = (error_text or "")[:2000]
        task.last_step_result = result
        task.save(
            update_fields=[
                "status",
                "locked_at",
                "next_retry_at",
                "last_error",
                "last_step_result",
                "updated_at",
            ]
        )
