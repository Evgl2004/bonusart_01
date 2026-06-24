from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from guests.models import (
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponAutomationConfig,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    Guest,
    MessageTemplate,
    NotificationScenario,
)
from guests.services.coupon_autoscenarios import close_expired_coupon_autoscenario_assignments


class CouponAutoscenarioLifecycleServiceTests(TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Autoscenario lifecycle template",
            description="",
            message_text="Coupon {coupon_code}",
            created_by="tests",
            is_active=True,
        )
        self.scenario = NotificationScenario.objects.create(
            code="custom_coupon_expire_test",
            name="Custom coupon expire test",
            description="",
            is_active=True,
            is_system=False,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            template=self.template,
            priority=NotificationScenario.Priority.BULK,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            settings={"inactive_days": 30},
        )
        self.config = CouponAutomationConfig.objects.create(
            scenario=self.scenario,
            scenario_type=CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
            execution_mode=CouponAutomationConfig.ExecutionMode.AUTOMATIC,
            coupon_series="AUTO_EXPIRE_TEST",
            venue_code="DEP_1",
            venue_name="Test venue",
            coupon_validity_days=10,
            max_recipients_per_run=100,
            max_active_coupons_per_guest=1,
            cooldown_days=30,
        )
        self.run = CouponAutoscenarioRun.objects.create(
            scenario=self.scenario,
            config=self.config,
            status=CouponAutoscenarioRun.Status.COMPLETED,
            execution_mode=self.config.execution_mode,
            scan_limit=100,
            max_recipients_per_run=100,
            scanned_guests=1,
            matched_guests=1,
            sendable_guests=1,
            eligible_guests=1,
            planned_assignments=1,
            created_assignments=1,
            queue_events_created=1,
        )

    def _create_assignment(
        self,
        *,
        code: str,
        status: str = CouponAutoscenarioAssignment.Status.SENT,
        vtelemax_sync_status: str = CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
        expires_delta: timedelta = -timedelta(minutes=1),
    ) -> CouponAutoscenarioAssignment:
        guest = Guest.objects.create(
            phone=f"+7999000{int(CouponRegistryEntry.objects.count()) + 1000:04d}",
            first_name="Test",
            created_at=self.now,
            updated_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="AUTO_EXPIRE_TEST",
            code=code,
            venue_code="DEP_1",
            venue_name="Test venue",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
            assigned_at=self.now - timedelta(days=11),
        )
        return CouponAutoscenarioAssignment.objects.create(
            run=self.run,
            scenario=self.scenario,
            config=self.config,
            guest=guest,
            coupon=coupon,
            person_id=None,
            phone_e164=guest.phone,
            coupon_series="AUTO_EXPIRE_TEST",
            coupon_code=code,
            venue_code="DEP_1",
            venue_name="Test venue",
            promo_text="Promo text",
            assigned_at=self.now - timedelta(days=11),
            sent_at=self.now - timedelta(days=11),
            lifetime_expires_at=self.now + expires_delta,
            status=status,
            vtelemax_sync_status=vtelemax_sync_status,
            vtelemax_synced_at=self.now - timedelta(days=11),
        )

    def test_close_expired_sent_assignment_marks_expired_and_queues_vtelemax_update(self):
        assignment = self._create_assignment(code="EXP001")

        stats = close_expired_coupon_autoscenario_assignments(
            close_before=self.now,
            limit=10,
            dry_run=False,
        ).as_dict()

        self.assertEqual(stats["assignments_scanned"], 1)
        self.assertEqual(stats["assignments_expired"], 1)
        self.assertEqual(stats["registry_marked_expired"], 1)
        self.assertEqual(stats["queue_events_created"], 1)
        self.assertEqual(stats["queue_events_updated"], 0)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.EXPIRED)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )
        self.assertIsNone(assignment.vtelemax_synced_at)
        self.assertEqual(assignment.vtelemax_sync_error, None)

        assignment.coupon.refresh_from_db()
        self.assertFalse(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.EXPIRED)

        event = CouponVtelemaxSyncQueue.objects.get(
            autoscenario_assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
        )
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.PENDING)
        self.assertEqual(event.payload_json.get("source"), "autoscenario")
        self.assertEqual(
            event.payload_json.get("status"),
            CouponAutoscenarioAssignment.Status.EXPIRED,
        )
        self.assertEqual(event.payload_json.get("coupon_code"), "EXP001")
        self.assertTrue(event.payload_json.get("meta", {}).get("remove_from_guest"))
        self.assertFalse(event.payload_json.get("meta", {}).get("release_to_pool"))
        self.assertTrue(event.payload_json.get("meta", {}).get("post_autoscenario_expire"))

    def test_close_expired_assignments_is_idempotent_after_first_pass(self):
        self._create_assignment(code="EXP002")

        first_stats = close_expired_coupon_autoscenario_assignments(
            close_before=self.now,
            limit=10,
            dry_run=False,
        ).as_dict()
        second_stats = close_expired_coupon_autoscenario_assignments(
            close_before=self.now,
            limit=10,
            dry_run=False,
        ).as_dict()

        self.assertEqual(first_stats["queue_events_created"], 1)
        self.assertEqual(second_stats["assignments_scanned"], 0)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 1)

    def test_close_expired_reserved_assignment_after_vtelemax_ack(self):
        assignment = self._create_assignment(
            code="EXPRES",
            status=CouponAutoscenarioAssignment.Status.RESERVED,
            vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
        )

        stats = close_expired_coupon_autoscenario_assignments(
            close_before=self.now,
            limit=10,
            dry_run=False,
        ).as_dict()

        self.assertEqual(stats["assignments_scanned"], 1)
        self.assertEqual(stats["assignments_expired"], 1)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.EXPIRED)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )
        event = CouponVtelemaxSyncQueue.objects.get(autoscenario_assignment=assignment)
        self.assertEqual(event.payload_json.get("status"), CouponAutoscenarioAssignment.Status.EXPIRED)
        self.assertTrue(event.payload_json.get("meta", {}).get("remove_from_guest"))
        self.assertFalse(event.payload_json.get("meta", {}).get("release_to_pool"))

    def test_close_expired_assignments_dry_run_does_not_write(self):
        assignment = self._create_assignment(code="EXP003")

        stats = close_expired_coupon_autoscenario_assignments(
            close_before=self.now,
            limit=10,
            dry_run=True,
        ).as_dict()

        self.assertEqual(stats["assignments_scanned"], 1)
        self.assertEqual(stats["assignments_expired"], 1)
        self.assertEqual(stats["queue_events_created"], 1)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 0)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.SENT)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
        )
        assignment.coupon.refresh_from_db()
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)

    def test_close_ignores_future_used_canceled_error_and_unsynced_assignments(self):
        self._create_assignment(code="FUTURE", expires_delta=timedelta(days=1))
        self._create_assignment(code="USED", status=CouponAutoscenarioAssignment.Status.USED)
        self._create_assignment(code="CANCEL", status=CouponAutoscenarioAssignment.Status.CANCELED)
        self._create_assignment(code="ERROR", status=CouponAutoscenarioAssignment.Status.ERROR)
        self._create_assignment(
            code="UNSYNC",
            vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )

        stats = close_expired_coupon_autoscenario_assignments(
            close_before=self.now,
            limit=10,
            dry_run=False,
        ).as_dict()

        self.assertEqual(stats["assignments_scanned"], 0)
        self.assertEqual(stats["assignments_expired"], 0)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 0)
        self.assertEqual(
            CouponAutoscenarioAssignment.objects.filter(
                status=CouponAutoscenarioAssignment.Status.EXPIRED
            ).count(),
            0,
        )

    def test_management_command_closes_expired_autoscenario_assignments(self):
        self._create_assignment(code="CMD001")
        stdout = StringIO()

        call_command("close_coupon_autoscenarios", limit=10, stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("close_coupon_autoscenarios done", output)
        self.assertIn("assignments_scanned=1", output)
        self.assertIn("assignments_expired=1", output)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 1)
