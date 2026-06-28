"""
Тесты оперативного OLAP-конвейера после входящих уведомлений iikoCard.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from guests.models import Guest, OlapCheckSyncJournal, OlapLivePipelineQueue, OlapSalesRawLine, OrderFact
from guests.services.iiko_olap_client import OlapPortionLoadStats
from guests.services.olap_live_pipeline import (
    OlapLivePipelineService,
    ensure_live_pipeline_task_for_journal,
)


class _FakeOlapClient:
    def __init__(self, *, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    def fetch_sales_in_portions(self, **kwargs):
        self.calls.append(kwargs)
        stats = OlapPortionLoadStats(
            requested_portions=1,
            successful_portions=1,
            failed_portions=0,
            total_data_rows=len(self.rows),
            total_summary_rows=0,
            failed_order_number_portions=[],
        )
        return list(self.rows), [], stats


class _CouponSyncStats:
    def to_dict(self):
        return {"assignments_marked_used": 1, "queue_events_created": 1}


class OlapLivePipelineServiceTests(TestCase):
    """
    Проверяет короткий путь обработки: OLAP-журнал -> сырые строки -> OrderFact -> купон.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.guest = Guest.objects.create(
            phone="+79990001234",
            first_name="Гость",
            created_at=self.now,
            updated_at=self.now,
        )

    def _create_journal(
        self,
        *,
        key: str = "live-pipeline-journal-1",
        status: str = OlapCheckSyncJournal.Status.NEW,
        next_try_at=None,
    ) -> OlapCheckSyncJournal:
        return OlapCheckSyncJournal.objects.create(
            idempotency_key=key,
            status=status,
            guest=self.guest,
            source_webhook_id="wh-live-1",
            order_number=113,
            order_external_id="order-113",
            event_at=self.now,
            business_date=date(2026, 6, 26),
            department_id="dep-1",
            department_code="01",
            terminal_group_id="terminal-1",
            next_try_at=next_try_at,
        )

    @override_settings(OLAP_LIVE_PIPELINE_ENABLED=False)
    def test_ensure_live_pipeline_task_respects_feature_flag(self):
        journal = self._create_journal()

        task, created = ensure_live_pipeline_task_for_journal(journal=journal)

        self.assertIsNone(task)
        self.assertFalse(created)
        self.assertEqual(OlapLivePipelineQueue.objects.count(), 0)

    @override_settings(OLAP_LIVE_PIPELINE_ENABLED=True)
    def test_ensure_live_pipeline_task_is_idempotent(self):
        journal = self._create_journal()

        first_task, first_created = ensure_live_pipeline_task_for_journal(journal=journal)
        second_task, second_created = ensure_live_pipeline_task_for_journal(journal=journal)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_task.id, second_task.id)
        self.assertEqual(OlapLivePipelineQueue.objects.count(), 1)
        self.assertEqual(first_task.status, OlapLivePipelineQueue.Status.NEW)

    @override_settings(
        OLAP_LIVE_PIPELINE_ENABLED=True,
        COUPON_REDEMPTION_SYNC_ENABLED=True,
    )
    @patch("guests.services.olap_live_pipeline.CouponRedemptionSyncService")
    def test_process_batch_loads_olap_builds_exact_order_fact_and_syncs_coupon(self, mocked_coupon_sync_cls):
        journal = self._create_journal()
        task, _created = ensure_live_pipeline_task_for_journal(journal=journal)
        fake_client = _FakeOlapClient(
            rows=[
                {
                    "OpenDate.Typed": "2026-06-26",
                    "OrderNum": 113,
                    "Department.Id": "dep-1",
                    "Department": "Сами Сусами",
                    "UniqOrderId.Id": "order-113",
                    "ItemSaleEvent.Id": "event-1",
                    "DishCode": "dish-set",
                    "DishName": "Сет",
                    "DishCategory.Id": "cat-set",
                    "DishSumInt": 790,
                    "DishDiscountSumInt": 0,
                    "CouponInfo.Series": "SAMI_SUSAMI_INACTIVE_30D",
                    "CouponInfo.Number": "B5C13E",
                    "DeletedWithWriteoff": "NOT_DELETED",
                },
                {
                    "OpenDate.Typed": "2026-06-26",
                    "OrderNum": 113,
                    "Department.Id": "dep-1",
                    "Department": "Сами Сусами",
                    "UniqOrderId.Id": "order-113",
                    "ItemSaleEvent.Id": "event-2",
                    "DishCode": "dish-lemon",
                    "DishName": "Лимон",
                    "DishCategory.Id": "cat-extra",
                    "DishSumInt": 30,
                    "DishDiscountSumInt": 30,
                    "DeletedWithWriteoff": "NOT_DELETED",
                },
            ]
        )
        mocked_coupon_sync = mocked_coupon_sync_cls.return_value
        mocked_coupon_sync.sync_from_order_facts.return_value = _CouponSyncStats()

        service = OlapLivePipelineService(
            client=fake_client,
            batch_size=10,
            order_fact_batch_size=100,
            retry_base_seconds=1,
            olap_portion_size=10,
        )
        stats = service.process_batch()

        journal.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(journal.status, OlapCheckSyncJournal.Status.LOADED)
        self.assertEqual(task.status, OlapLivePipelineQueue.Status.DONE)
        self.assertEqual(stats.claimed, 1)
        self.assertEqual(stats.done, 1)
        self.assertEqual(OlapSalesRawLine.objects.filter(sync_journal=journal).count(), 2)

        fact = OrderFact.objects.get()
        self.assertEqual(fact.guest_id, self.guest.id)
        self.assertEqual(fact.business_date, date(2026, 6, 26))
        self.assertEqual(fact.department_id, "dep-1")
        self.assertEqual(fact.order_number, 113)
        self.assertEqual(fact.uniq_order_id, "order-113")
        self.assertEqual(fact.items_count, 2)
        self.assertTrue(fact.coupon_used)
        self.assertEqual(fact.coupon_series, "SAMI_SUSAMI_INACTIVE_30D")
        self.assertEqual(fact.coupon_number, "B5C13E")
        mocked_coupon_sync.sync_from_order_facts.assert_called_once_with(
            order_fact_ids=[fact.id],
            dry_run=False,
        )

    @override_settings(OLAP_LIVE_PIPELINE_ENABLED=True)
    def test_process_batch_waits_for_journal_retry_time(self):
        next_try_at = self.now + timedelta(minutes=20)
        journal = self._create_journal(
            key="live-pipeline-waiting-1",
            status=OlapCheckSyncJournal.Status.RETRY,
            next_try_at=next_try_at,
        )
        task, _created = ensure_live_pipeline_task_for_journal(journal=journal)
        task.next_retry_at = self.now
        task.save(update_fields=["next_retry_at", "updated_at"])
        fake_client = _FakeOlapClient(rows=[])

        stats = OlapLivePipelineService(
            client=fake_client,
            batch_size=10,
            retry_base_seconds=1,
        ).process_batch()

        task.refresh_from_db()
        self.assertEqual(stats.waiting_olap, 1)
        self.assertEqual(task.status, OlapLivePipelineQueue.Status.WAITING_OLAP)
        self.assertEqual(task.next_retry_at, next_try_at)
        self.assertEqual(fake_client.calls, [])

    @override_settings(OLAP_LIVE_PIPELINE_ENABLED=True)
    def test_process_batch_marks_skipped_when_olap_journal_skipped(self):
        journal = self._create_journal(
            key="live-pipeline-skipped-1",
            status=OlapCheckSyncJournal.Status.SKIPPED,
        )
        journal.last_error = "Чек 113: в OLAP нет строк по строгому фильтру."
        journal.save(update_fields=["last_error", "updated_at"])
        task, _created = ensure_live_pipeline_task_for_journal(journal=journal)
        fake_client = _FakeOlapClient(rows=[])

        stats = OlapLivePipelineService(
            client=fake_client,
            batch_size=10,
            retry_base_seconds=1,
        ).process_batch()

        task.refresh_from_db()
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(task.status, OlapLivePipelineQueue.Status.SKIPPED)
        self.assertEqual(
            task.last_step_result,
            {"journal_status": OlapCheckSyncJournal.Status.SKIPPED},
        )
        self.assertIn("строгому фильтру", task.last_error or "")
        self.assertEqual(fake_client.calls, [])
