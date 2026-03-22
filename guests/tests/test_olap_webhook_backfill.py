"""
Тесты сервиса исторического прогона webhook -> olap_check_sync_journal.
"""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from guests.models import Guest, OlapCheckSyncJournal, Restaurant, TerminalDepartmentMap
from guests.services.olap_webhook_backfill import (
    OlapWebhookBackfillOptions,
    OlapWebhookBackfillService,
)


class OlapWebhookBackfillServiceTests(TestCase):
    """
    Проверки backfill-сервиса: dry-run, идемпотентность и backpressure.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest = Guest.objects.create(
            first_name="Тест",
            phone="+79995554433",
            created_at=now,
            updated_at=now,
        )
        self.restaurant = Restaurant.objects.create(
            iiko_id="rest-backfill-001",
            name="Ресторан backfill",
        )
        TerminalDepartmentMap.objects.create(
            organization_id="org-backfill-001",
            terminal_group_id=self.restaurant.iiko_id,
            restoraunt_group_id=self.restaurant.iiko_id,
            department_id="dep-backfill-001",
            department_code="01",
            department_name="Restaurant backfill",
            is_active=True,
        )

    @staticmethod
    def _build_options(
        *,
        dry_run: bool,
        pause_queue_gt: int = 5000,
        resume_queue_lt: int = 2000,
        allowed_notification_types: set[int] | None = None,
    ) -> OlapWebhookBackfillOptions:
        return OlapWebhookBackfillOptions(
            dry_run=dry_run,
            date_from="2025-12-01T00:00:00Z",
            date_to=None,
            page_size=100,
            max_pages_per_cycle=5,
            sleep_between_pages_seconds=0.0,
            pause_queue_gt=pause_queue_gt,
            resume_queue_lt=resume_queue_lt,
            statuses=["complete"],
            business_statuses=[],
            category_external_ids=[],
            allowed_notification_types=allowed_notification_types or {1},
        )

    def _build_webhook_nt1(self, *, webhook_id: str, event_id: str, order_number: int) -> dict:
        return {
            "id": webhook_id,
            "parsed_body": {
                "id": event_id,
                "notificationType": 1,
                "phone": self.guest.phone,
                "terminalGroupId": self.restaurant.iiko_id,
                "organizationId": "org-backfill-001",
                "transactionId": f"tx-{order_number}",
                "orderId": f"order-{order_number}",
                "orderNumber": order_number,
                "changedOn": "2026-03-18T11:56:03+05:00",
            },
        }

    def test_run_cycle_dry_run_counts_without_db_write(self):
        """
        В dry-run режиме сервис считает кандидатов, но не пишет в OLAP-журнал.
        """
        service = OlapWebhookBackfillService(
            base_url="https://example.com",
            username="svc",
            password="pwd",
        )
        options = self._build_options(dry_run=True)
        pages = [[self._build_webhook_nt1(webhook_id="wh-dry-1", event_id="evt-dry-1", order_number=53110)]]

        stats = service.run_cycle(options=options, pages_override=pages)
        service.close()

        self.assertEqual(stats.pages_fetched, 1)
        self.assertEqual(stats.webhooks_seen, 1)
        self.assertEqual(stats.would_enqueue, 1)
        self.assertEqual(stats.created_rows, 0)
        self.assertEqual(OlapCheckSyncJournal.objects.count(), 0)

    def test_run_cycle_create_and_idempotent_duplicate(self):
        """
        Повторный прогон с тем же webhook не должен создавать дубль в OLAP-журнале.
        """
        service = OlapWebhookBackfillService(
            base_url="https://example.com",
            username="svc",
            password="pwd",
        )
        options = self._build_options(dry_run=False)
        page = [self._build_webhook_nt1(webhook_id="wh-idem-1", event_id="evt-idem-1", order_number=698698)]

        first_stats = service.run_cycle(options=options, pages_override=[page])
        second_stats = service.run_cycle(options=options, pages_override=[page])
        service.close()

        self.assertEqual(first_stats.created_rows, 1)
        self.assertEqual(second_stats.duplicate_rows, 1)
        self.assertEqual(OlapCheckSyncJournal.objects.count(), 1)

        row = OlapCheckSyncJournal.objects.get()
        self.assertEqual(row.order_number, 698698)
        self.assertEqual(row.source_webhook_id, "wh-idem-1")

    def test_run_cycle_skips_unknown_terminal_group(self):
        """
        Если terminalGroupId не входит в активный справочник сопоставления,
        задача не должна создаваться.
        """
        service = OlapWebhookBackfillService(
            base_url="https://example.com",
            username="svc",
            password="pwd",
        )
        options = self._build_options(dry_run=False)
        pages = [[
            {
                "id": "wh-unknown-terminal-1",
                "parsed_body": {
                    "id": "evt-unknown-terminal-1",
                    "notificationType": 1,
                    "phone": self.guest.phone,
                    "terminalGroupId": "terminal-unknown-1",
                    "organizationId": "org-backfill-001",
                    "transactionId": "tx-unknown-1",
                    "orderId": "order-unknown-1",
                    "orderNumber": 900001,
                    "changedOn": "2026-03-18T11:56:03+05:00",
                },
            }
        ]]

        stats = service.run_cycle(options=options, pages_override=pages)
        service.close()

        self.assertEqual(stats.created_rows, 0)
        self.assertEqual(stats.other_skipped_rows, 1)
        self.assertEqual(OlapCheckSyncJournal.objects.count(), 0)

    def test_run_cycle_backpressure_pauses_and_then_resumes(self):
        """
        При превышении порога очереди сервис ставит intake на паузу и возобновляет после снижения глубины.
        """
        OlapCheckSyncJournal.objects.create(
            idempotency_key="preload-1",
            status=OlapCheckSyncJournal.Status.NEW,
            order_number=111001,
            business_date=timezone.now().date(),
        )
        OlapCheckSyncJournal.objects.create(
            idempotency_key="preload-2",
            status=OlapCheckSyncJournal.Status.NEW,
            order_number=111002,
            business_date=timezone.now().date(),
        )

        service = OlapWebhookBackfillService(
            base_url="https://example.com",
            username="svc",
            password="pwd",
        )
        options = self._build_options(
            dry_run=False,
            pause_queue_gt=1,
            resume_queue_lt=1,
        )
        pages = [[self._build_webhook_nt1(webhook_id="wh-resume-1", event_id="evt-resume-1", order_number=700001)]]

        paused_stats = service.run_cycle(options=options, pages_override=pages)
        self.assertTrue(paused_stats.paused_by_backpressure)
        self.assertEqual(paused_stats.pages_fetched, 0)

        OlapCheckSyncJournal.objects.filter(status=OlapCheckSyncJournal.Status.NEW).update(
            status=OlapCheckSyncJournal.Status.LOADED
        )

        resumed_stats = service.run_cycle(options=options, pages_override=pages)
        service.close()

        self.assertFalse(resumed_stats.paused_by_backpressure)
        self.assertEqual(resumed_stats.created_rows, 1)
