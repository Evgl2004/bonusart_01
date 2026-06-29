from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from guests.models import (
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponAutomationConfig,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    IikoCustomerCategorySyncEvent,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
)
from guests.services.coupon_autoscenarios import (
    COUPON_AUTOSCENARIO_STATUS_REASON_DELIVERY_FAILED,
    cancel_coupon_autoscenario_assignments_after_delivery_failure,
    close_expired_coupon_autoscenario_assignments,
)


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

    def _create_delivery_event(
        self,
        *,
        assignment: CouponAutoscenarioAssignment,
    ) -> NotificationEvent:
        source_ref = f"coupon_autoscenario_assignment:{assignment.id}"
        return NotificationEvent.objects.create(
            scenario=assignment.scenario,
            guest=assignment.guest,
            source_type=NotificationEvent.SourceType.SCHEDULE,
            source_ref=source_ref,
            dedupe_key=f"delivery-event-{assignment.id}",
            status=NotificationEvent.Status.TASK_CREATED,
            event_at=self.now,
            planned_send_at=self.now,
            payload={"assignment_id": int(assignment.id)},
        )

    def _create_delivery_task(
        self,
        *,
        event: NotificationEvent,
        assignment: CouponAutoscenarioAssignment,
        provider: str = "telegram",
        status: str = DispatchTask.Status.FAILED,
        error: str = "blocked: Telegram сообщает, что пользователь недоступен/заблокировал бота.",
    ) -> DispatchTask:
        return DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=provider,
            priority=DispatchTask.Priority.BULK,
            status=status,
            guest=assignment.guest,
            notification_scenario=assignment.scenario,
            notification_event=event,
            message_text="Coupon text",
            idempotency_key=(
                f"system:{assignment.scenario.code}:coupon_autoscenario_assignment:"
                f"{assignment.id}:guest:{assignment.guest_id}:provider:{provider}"
            ),
            available_at=self.now,
            scheduled_at=self.now,
            started_at=self.now,
            finished_at=self.now if status in {DispatchTask.Status.DONE, DispatchTask.Status.FAILED} else None,
            attempt=1,
            max_attempts=5,
            last_error=error if status == DispatchTask.Status.FAILED else None,
        )

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="cat-active-coupon",
        IIKO_ACTIVE_COUPON_CATEGORY_NAME="Активный купон SAGUR",
        IIKO_ORGANIZATION_ID="org-test",
    )
    def test_delivery_guard_cancels_assignment_when_all_channels_final_failed(self):
        assignment = self._create_assignment(code="FAIL01")
        assignment.guest.iiko_id = "iiko-guest-1"
        assignment.guest.save(update_fields=["iiko_id"])
        event = self._create_delivery_event(assignment=assignment)
        self._create_delivery_task(event=event, assignment=assignment, provider="telegram")

        stats = cancel_coupon_autoscenario_assignments_after_delivery_failure(
            limit=10,
            dry_run=False,
            now=self.now,
        ).as_dict()

        self.assertEqual(stats["assignments_scanned"], 1)
        self.assertEqual(stats["assignments_canceled"], 1)
        self.assertEqual(stats["queue_events_created"], 1)
        self.assertEqual(stats["iiko_remove_events_created"], 1)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.CANCELED)
        self.assertEqual(assignment.status_reason, COUPON_AUTOSCENARIO_STATUS_REASON_DELIVERY_FAILED)
        self.assertIn("не доставлено", assignment.status_details)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )

        queue_event = CouponVtelemaxSyncQueue.objects.get(autoscenario_assignment=assignment)
        self.assertEqual(queue_event.direction, CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE)
        self.assertEqual(queue_event.status, CouponVtelemaxSyncQueue.Status.PENDING)
        self.assertEqual(queue_event.payload_json["status"], CouponAutoscenarioAssignment.Status.CANCELED)
        self.assertEqual(
            queue_event.payload_json["meta"]["cancel_reason"],
            COUPON_AUTOSCENARIO_STATUS_REASON_DELIVERY_FAILED,
        )
        self.assertTrue(queue_event.payload_json["meta"]["remove_from_guest"])
        self.assertTrue(queue_event.payload_json["meta"]["release_to_pool"])

        assignment.coupon.refresh_from_db()
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)
        self.assertFalse(assignment.coupon.is_active)

        iiko_event = IikoCustomerCategorySyncEvent.objects.get(autoscenario_assignment=assignment)
        self.assertEqual(iiko_event.action, IikoCustomerCategorySyncEvent.Action.REMOVE)
        self.assertEqual(iiko_event.status, IikoCustomerCategorySyncEvent.Status.PENDING)
        self.assertEqual(iiko_event.iiko_customer_id, "iiko-guest-1")

    def test_delivery_guard_keeps_coupon_when_any_channel_delivered(self):
        assignment = self._create_assignment(code="DONE01")
        event = self._create_delivery_event(assignment=assignment)
        self._create_delivery_task(event=event, assignment=assignment, provider="telegram")
        self._create_delivery_task(
            event=event,
            assignment=assignment,
            provider="vk",
            status=DispatchTask.Status.DONE,
            error="",
        )

        stats = cancel_coupon_autoscenario_assignments_after_delivery_failure(
            limit=10,
            dry_run=False,
            now=self.now,
        ).as_dict()

        self.assertEqual(stats["assignments_scanned"], 1)
        self.assertEqual(stats["assignments_delivered"], 1)
        self.assertEqual(stats["assignments_canceled"], 0)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 0)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.SENT)

    def test_delivery_guard_waits_when_any_task_still_pending(self):
        assignment = self._create_assignment(code="WAIT01")
        event = self._create_delivery_event(assignment=assignment)
        self._create_delivery_task(event=event, assignment=assignment, provider="telegram")
        self._create_delivery_task(
            event=event,
            assignment=assignment,
            provider="max",
            status=DispatchTask.Status.PENDING,
            error="",
        )

        stats = cancel_coupon_autoscenario_assignments_after_delivery_failure(
            limit=10,
            dry_run=False,
            now=self.now,
        ).as_dict()

        self.assertEqual(stats["assignments_scanned"], 1)
        self.assertEqual(stats["assignments_waiting"], 1)
        self.assertEqual(stats["assignments_canceled"], 0)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 0)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.SENT)

    def test_delivery_guard_dry_run_does_not_write(self):
        assignment = self._create_assignment(code="DRY001")
        event = self._create_delivery_event(assignment=assignment)
        self._create_delivery_task(event=event, assignment=assignment)

        stats = cancel_coupon_autoscenario_assignments_after_delivery_failure(
            limit=10,
            dry_run=True,
            now=self.now,
        ).as_dict()

        self.assertTrue(stats["dry_run"])
        self.assertEqual(stats["assignments_scanned"], 1)
        self.assertEqual(stats["assignments_canceled"], 1)
        self.assertEqual(stats["queue_events_created"], 1)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 0)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.SENT)

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="cat-active-coupon",
    )
    def test_delivery_guard_is_idempotent_after_cancellation(self):
        assignment = self._create_assignment(code="IDEMP1")
        event = self._create_delivery_event(assignment=assignment)
        self._create_delivery_task(event=event, assignment=assignment)

        first_stats = cancel_coupon_autoscenario_assignments_after_delivery_failure(
            limit=10,
            dry_run=False,
            now=self.now,
        ).as_dict()
        second_stats = cancel_coupon_autoscenario_assignments_after_delivery_failure(
            limit=10,
            dry_run=False,
            now=self.now,
        ).as_dict()

        self.assertEqual(first_stats["assignments_canceled"], 1)
        self.assertEqual(second_stats["assignments_scanned"], 0)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 1)

    def test_delivery_guard_skips_old_canceled_failures_and_processes_live_candidate(self):
        old_assignment = self._create_assignment(
            code="OLDFAIL",
            status=CouponAutoscenarioAssignment.Status.CANCELED,
        )
        old_event = self._create_delivery_event(assignment=old_assignment)
        self._create_delivery_task(event=old_event, assignment=old_assignment)

        live_assignment = self._create_assignment(code="LIVE01")
        live_event = self._create_delivery_event(assignment=live_assignment)
        self._create_delivery_task(event=live_event, assignment=live_assignment)

        stats = cancel_coupon_autoscenario_assignments_after_delivery_failure(
            limit=1,
            dry_run=False,
            now=self.now,
        ).as_dict()

        self.assertEqual(stats["assignments_scanned"], 1)
        self.assertEqual(stats["assignments_canceled"], 1)

        old_assignment.refresh_from_db()
        self.assertEqual(old_assignment.status, CouponAutoscenarioAssignment.Status.CANCELED)
        live_assignment.refresh_from_db()
        self.assertEqual(live_assignment.status, CouponAutoscenarioAssignment.Status.CANCELED)
        self.assertEqual(live_assignment.status_reason, COUPON_AUTOSCENARIO_STATUS_REASON_DELIVERY_FAILED)

    def test_management_command_delivery_guard_dry_run_reports_stats(self):
        assignment = self._create_assignment(code="CMDFAIL")
        event = self._create_delivery_event(assignment=assignment)
        self._create_delivery_task(event=event, assignment=assignment)
        stdout = StringIO()

        call_command(
            "run_coupon_autoscenario_delivery_guard",
            limit=10,
            dry_run=True,
            force_run=True,
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn("Контроль недоставки купонных автосценариев", output)
        self.assertIn("assignments_canceled=1", output)
        self.assertIn("queue_events_created=1", output)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 0)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.SENT)

    def test_management_command_delivery_guard_health_check(self):
        stdout = StringIO()

        call_command(
            "run_coupon_autoscenario_delivery_guard",
            health_check=True,
            verbose=True,
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn("status=healthy", output)
        self.assertIn("контроль недоставки купонных автосценариев", output)
