from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    CouponAutomationConfig,
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    Guest,
    Mailing,
    MessageTemplate,
    NotificationScenario,
    OrderFact,
)
from guests.services.coupon_redemption_sync import CouponRedemptionSyncService


class CouponRedemptionSyncServiceTests(TestCase):
    """
    Проверки синхронизации применения купонов из OLAP в реестр купонов.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Coupon redemption template",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Coupon redemption campaign",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(hours=1),
            scheduled_time_end=self.now + timedelta(days=7),
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=(self.now - timedelta(hours=1)).time(),
            send_window_end=(self.now + timedelta(hours=1)).time(),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.NORMAL,
            coupon_series="TEST",
            coupon_venue_code="DEP_1",
            coupon_venue_name="Тестовое заведение",
            coupon_promo_text="Скидка 20%",
        )

    def _create_guest(self, suffix: str) -> Guest:
        return Guest.objects.create(
            phone=f"+79990000{suffix}",
            first_name=f"Guest-{suffix}",
            created_at=self.now,
            updated_at=self.now,
        )

    def _create_order_fact(
        self,
        *,
        guest: Guest | None,
        order_number: int,
        coupon_series: str,
        coupon_number: str,
        uniq_suffix: str,
        business_date=None,
        first_seen_at=None,
    ) -> OrderFact:
        return OrderFact.objects.create(
            guest=guest,
            business_date=business_date or self.now.date(),
            department_id="DEP_1",
            department_name="Тестовое заведение",
            order_number=order_number,
            uniq_order_id=f"uniq-{uniq_suffix}",
            gross_sum="1000.00",
            net_sum="900.00",
            discount_sum="100.00",
            bonus_sum="0.00",
            items_count=2,
            categories_count=1,
            coupon_used=True,
            coupon_series=coupon_series,
            coupon_number=coupon_number,
            first_seen_at=first_seen_at or self.now,
        )

    def _create_assignment(self, *, guest: Guest, code: str) -> CouponCampaignAssignment:
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code=code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        return CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=guest,
            coupon=coupon,
            person_id=None,
            phone_e164=guest.phone,
            coupon_series="TEST",
            coupon_code=code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Скидка 20%",
            assigned_at=self.now,
            lifetime_expires_at=self.now + timedelta(days=7),
            status=CouponCampaignAssignment.Status.SENT,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )

    def _create_autoscenario_assignment(
        self,
        *,
        guest: Guest,
        code: str,
        status: str = CouponAutoscenarioAssignment.Status.SENT,
        expires_at=None,
    ) -> CouponAutoscenarioAssignment:
        scenario = NotificationScenario.objects.create(
            code=f"auto_redemption_{code.lower().replace('-', '_')}",
            name=f"Auto redemption {code}",
            description="",
            is_active=True,
            is_system=True,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            template=self.template,
            priority=NotificationScenario.Priority.BULK,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            settings={"inactive_days": 30},
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            coupon_series="AUTO",
            venue_code="DEP_1",
            venue_name="Автосценарий",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            max_active_coupons_per_guest=1,
            cooldown_days=30,
        )
        run = CouponAutoscenarioRun.objects.create(
            scenario=scenario,
            config=config,
            status=CouponAutoscenarioRun.Status.COMPLETED,
            execution_mode=config.execution_mode,
            scan_limit=10,
            max_recipients_per_run=10,
            scanned_guests=1,
            matched_guests=1,
            sendable_guests=1,
            eligible_guests=1,
            planned_assignments=1,
            created_assignments=1,
            queue_events_created=1,
            coupon_shortage=0,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="AUTO",
            code=code,
            venue_code="DEP_1",
            venue_name="Автосценарий",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        return CouponAutoscenarioAssignment.objects.create(
            run=run,
            scenario=scenario,
            config=config,
            guest=guest,
            coupon=coupon,
            person_id=None,
            phone_e164=guest.phone,
            coupon_series="AUTO",
            coupon_code=code,
            venue_code="DEP_1",
            venue_name="Автосценарий",
            promo_text="Автосценарий: тестовый купон",
            assigned_at=self.now,
            sent_at=self.now,
            lifetime_expires_at=expires_at or self.now + timedelta(days=14),
            status=status,
            vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )

    def test_marks_assignment_and_registry_as_used_and_creates_queue_event(self):
        guest = self._create_guest("1111")
        assignment = self._create_assignment(guest=guest, code="TST-USED-1")
        self._create_order_fact(
            guest=guest,
            order_number=101,
            coupon_series="TEST",
            coupon_number="TST-USED-1",
            uniq_suffix="used-1",
        )

        stats = CouponRedemptionSyncService().sync_from_order_facts()

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.USED)
        self.assertEqual(assignment.used_order_id, 101)
        self.assertIsNotNone(assignment.used_at)
        self.assertEqual(assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.PENDING)

        assignment.coupon.refresh_from_db()
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.USED)
        self.assertFalse(assignment.coupon.is_active)

        event = CouponVtelemaxSyncQueue.objects.get(
            assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
        )
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.PENDING)
        self.assertEqual(event.payload_json.get("status"), CouponCampaignAssignment.Status.USED)
        self.assertEqual(event.payload_json.get("used_order_id"), 101)

        self.assertEqual(stats.assignments_matched, 1)
        self.assertEqual(stats.assignments_marked_used, 1)
        self.assertEqual(stats.assignments_marked_used_after_campaign, 0)
        self.assertEqual(stats.queue_events_created, 1)
        self.assertEqual(stats.registry_marked_used, 1)

    def test_marks_autoscenario_assignment_as_used_and_creates_queue_event(self):
        guest = self._create_guest("6111")
        assignment = self._create_autoscenario_assignment(guest=guest, code="AUTO-USED-1")
        self._create_order_fact(
            guest=guest,
            order_number=601,
            coupon_series="AUTO",
            coupon_number="AUTO-USED-1",
            uniq_suffix="auto-used-1",
        )

        stats = CouponRedemptionSyncService().sync_from_order_facts()

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.USED)
        self.assertEqual(assignment.used_order_id, 601)
        self.assertEqual(assignment.used_business_date, self.now.date())
        self.assertIsNotNone(assignment.used_at)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )

        assignment.coupon.refresh_from_db()
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.USED)
        self.assertFalse(assignment.coupon.is_active)

        event = CouponVtelemaxSyncQueue.objects.get(
            autoscenario_assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
        )
        self.assertIsNone(event.assignment_id)
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.PENDING)
        self.assertEqual(event.payload_json.get("source"), "autoscenario")
        self.assertEqual(event.payload_json.get("assignment_id"), assignment.id)
        self.assertEqual(event.payload_json.get("autoscenario_assignment_id"), assignment.id)
        self.assertEqual(event.payload_json.get("scenario_code"), assignment.scenario.code)
        self.assertEqual(event.payload_json.get("status"), CouponAutoscenarioAssignment.Status.USED)
        self.assertEqual(event.payload_json.get("used_order_id"), 601)
        self.assertEqual(event.payload_json.get("used_business_date"), self.now.date().isoformat())

        self.assertEqual(stats.assignments_matched, 1)
        self.assertEqual(stats.campaign_assignments_matched, 0)
        self.assertEqual(stats.autoscenario_assignments_matched, 1)
        self.assertEqual(stats.assignments_marked_used, 1)
        self.assertEqual(stats.queue_events_created, 1)
        self.assertEqual(stats.registry_marked_used, 1)

    def test_marks_expired_assignment_as_used_after_campaign(self):
        self.mailing.scheduled_time_begin = self.now - timedelta(days=2)
        self.mailing.scheduled_time_end = self.now - timedelta(days=1)
        self.mailing.save(update_fields=["scheduled_time_begin", "scheduled_time_end", "updated_at"])
        guest = self._create_guest("1212")
        assignment = self._create_assignment(guest=guest, code="TST-USED-AFTER-1")
        assignment.status = CouponCampaignAssignment.Status.EXPIRED
        assignment.save(update_fields=["status", "updated_at"])
        assignment.coupon.pool_status = CouponRegistryEntry.PoolStatus.EXPIRED
        assignment.coupon.is_active = False
        assignment.coupon.save(update_fields=["pool_status", "is_active", "updated_at"])
        self._create_order_fact(
            guest=guest,
            order_number=121,
            coupon_series="TEST",
            coupon_number="TST-USED-AFTER-1",
            uniq_suffix="used-after-1",
        )

        stats = CouponRedemptionSyncService().sync_from_order_facts()

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN)
        self.assertEqual(assignment.used_order_id, 121)
        assignment.coupon.refresh_from_db()
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.USED_AFTER_CAMPAIGN)
        self.assertFalse(assignment.coupon.is_active)

        event = CouponVtelemaxSyncQueue.objects.get(
            assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
        )
        self.assertEqual(event.payload_json.get("status"), CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN)
        self.assertEqual(event.payload_json.get("meta", {}).get("used_after_campaign"), True)
        self.assertEqual(event.payload_json.get("meta", {}).get("release_to_pool"), False)

        self.assertEqual(stats.assignments_marked_used, 1)
        self.assertEqual(stats.assignments_marked_used_after_campaign, 1)

    def test_marks_autoscenario_expired_assignment_as_used_after_campaign(self):
        guest = self._create_guest("6121")
        assignment = self._create_autoscenario_assignment(
            guest=guest,
            code="AUTO-USED-AFTER-1",
            status=CouponAutoscenarioAssignment.Status.EXPIRED,
            expires_at=self.now - timedelta(days=1),
        )
        assignment.coupon.pool_status = CouponRegistryEntry.PoolStatus.EXPIRED
        assignment.coupon.is_active = False
        assignment.coupon.save(update_fields=["pool_status", "is_active", "updated_at"])
        self._create_order_fact(
            guest=guest,
            order_number=621,
            coupon_series="AUTO",
            coupon_number="AUTO-USED-AFTER-1",
            uniq_suffix="auto-used-after-1",
        )

        stats = CouponRedemptionSyncService().sync_from_order_facts()

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN)
        self.assertEqual(assignment.used_order_id, 621)
        self.assertEqual(assignment.used_business_date, self.now.date())
        assignment.coupon.refresh_from_db()
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.USED_AFTER_CAMPAIGN)
        self.assertFalse(assignment.coupon.is_active)

        event = CouponVtelemaxSyncQueue.objects.get(
            autoscenario_assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
        )
        self.assertEqual(event.payload_json.get("status"), CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN)
        self.assertEqual(event.payload_json.get("meta", {}).get("used_after_campaign"), True)
        self.assertEqual(event.payload_json.get("meta", {}).get("release_to_pool"), False)

        self.assertEqual(stats.autoscenario_assignments_matched, 1)
        self.assertEqual(stats.assignments_marked_used, 1)
        self.assertEqual(stats.assignments_marked_used_after_campaign, 1)

    def test_marks_same_business_date_usage_after_campaign_end_time_as_late(self):
        self.mailing.scheduled_time_end = self.now - timedelta(minutes=5)
        self.mailing.save(update_fields=["scheduled_time_end", "updated_at"])
        guest = self._create_guest("1313")
        assignment = self._create_assignment(guest=guest, code="TST-USED-AFTER-2")
        self._create_order_fact(
            guest=guest,
            order_number=131,
            coupon_series="TEST",
            coupon_number="TST-USED-AFTER-2",
            uniq_suffix="used-after-2",
            business_date=self.now.date(),
            first_seen_at=self.now,
        )

        stats = CouponRedemptionSyncService().sync_from_order_facts()

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN)
        self.assertEqual(stats.assignments_marked_used_after_campaign, 1)

    def test_is_idempotent_for_already_used_assignment(self):
        guest = self._create_guest("2222")
        assignment = self._create_assignment(guest=guest, code="TST-USED-2")
        assignment.status = CouponCampaignAssignment.Status.USED
        assignment.used_order_id = 202
        assignment.used_at = self.now
        assignment.save(update_fields=["status", "used_order_id", "used_at", "updated_at"])

        existing_event = CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            assignment=assignment,
            payload_json={"status": "old"},
            status=CouponVtelemaxSyncQueue.Status.ERROR,
            attempts=2,
            next_retry_at=self.now,
            last_error="old",
        )

        self._create_order_fact(
            guest=guest,
            order_number=202,
            coupon_series="TEST",
            coupon_number="TST-USED-2",
            uniq_suffix="used-2",
        )

        stats = CouponRedemptionSyncService().sync_from_order_facts()

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.USED)
        self.assertEqual(assignment.used_order_id, 202)

        self.assertEqual(
            CouponVtelemaxSyncQueue.objects.filter(
                assignment=assignment,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            ).count(),
            1,
        )
        existing_event.refresh_from_db()
        self.assertEqual(existing_event.status, CouponVtelemaxSyncQueue.Status.PENDING)
        self.assertIsNone(existing_event.last_error)
        self.assertEqual(existing_event.payload_json.get("status"), CouponCampaignAssignment.Status.USED)

        self.assertEqual(stats.assignments_already_used, 1)
        self.assertEqual(stats.queue_events_created, 0)
        self.assertEqual(stats.queue_events_updated, 1)

    def test_repeat_sync_does_not_reopen_acked_used_event(self):
        guest = self._create_guest("2323")
        assignment = self._create_assignment(guest=guest, code="TST-USED-ACKED-1")
        self._create_order_fact(
            guest=guest,
            order_number=232,
            coupon_series="TEST",
            coupon_number="TST-USED-ACKED-1",
            uniq_suffix="used-acked-1",
        )

        service = CouponRedemptionSyncService()
        service.sync_from_order_facts()

        assignment.refresh_from_db()
        event = CouponVtelemaxSyncQueue.objects.get(
            assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
        )
        acked_at = self.now + timedelta(minutes=1)
        event.status = CouponVtelemaxSyncQueue.Status.ACKED
        event.attempts = 1
        event.sent_at = acked_at
        event.ack_at = acked_at
        event.last_error = None
        event.save(update_fields=["status", "attempts", "sent_at", "ack_at", "last_error", "updated_at"])
        assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.OK
        assignment.vtelemax_synced_at = acked_at
        assignment.save(update_fields=["vtelemax_sync_status", "vtelemax_synced_at", "updated_at"])

        stats = service.sync_from_order_facts()

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.USED)
        self.assertEqual(assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.OK)
        self.assertEqual(
            CouponVtelemaxSyncQueue.objects.filter(
                assignment=assignment,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            ).count(),
            1,
        )
        event.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.ACKED)
        self.assertEqual(event.ack_at, acked_at)
        self.assertIsNone(event.last_error)

        self.assertEqual(stats.assignments_already_used, 1)
        self.assertEqual(stats.queue_events_created, 0)
        self.assertEqual(stats.queue_events_updated, 0)

    def test_autoscenario_repeat_sync_does_not_reopen_acked_used_event(self):
        guest = self._create_guest("6323")
        assignment = self._create_autoscenario_assignment(guest=guest, code="AUTO-USED-ACKED-1")
        self._create_order_fact(
            guest=guest,
            order_number=632,
            coupon_series="AUTO",
            coupon_number="AUTO-USED-ACKED-1",
            uniq_suffix="auto-used-acked-1",
        )

        service = CouponRedemptionSyncService()
        service.sync_from_order_facts()

        assignment.refresh_from_db()
        event = CouponVtelemaxSyncQueue.objects.get(
            autoscenario_assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
        )
        acked_at = self.now + timedelta(minutes=1)
        event.status = CouponVtelemaxSyncQueue.Status.ACKED
        event.attempts = 1
        event.sent_at = acked_at
        event.ack_at = acked_at
        event.last_error = None
        event.save(update_fields=["status", "attempts", "sent_at", "ack_at", "last_error", "updated_at"])
        assignment.vtelemax_sync_status = CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK
        assignment.vtelemax_synced_at = acked_at
        assignment.save(update_fields=["vtelemax_sync_status", "vtelemax_synced_at", "updated_at"])

        stats = service.sync_from_order_facts()

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.USED)
        self.assertEqual(assignment.used_business_date, self.now.date())
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
        )
        self.assertEqual(
            CouponVtelemaxSyncQueue.objects.filter(
                autoscenario_assignment=assignment,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            ).count(),
            1,
        )
        event.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.ACKED)
        self.assertEqual(event.ack_at, acked_at)
        self.assertIsNone(event.last_error)

        self.assertEqual(stats.assignments_already_used, 1)
        self.assertEqual(stats.queue_events_created, 0)
        self.assertEqual(stats.queue_events_updated, 0)

    def test_used_after_acked_expired_creates_new_status_event(self):
        self.mailing.scheduled_time_begin = self.now - timedelta(days=3)
        self.mailing.scheduled_time_end = self.now - timedelta(days=2)
        self.mailing.save(update_fields=["scheduled_time_begin", "scheduled_time_end", "updated_at"])
        guest = self._create_guest("2424")
        assignment = self._create_assignment(guest=guest, code="TST-EXPIRED-ACKED-1")
        assignment.status = CouponCampaignAssignment.Status.EXPIRED
        assignment.save(update_fields=["status", "updated_at"])
        assignment.coupon.pool_status = CouponRegistryEntry.PoolStatus.EXPIRED
        assignment.coupon.is_active = False
        assignment.coupon.save(update_fields=["pool_status", "is_active", "updated_at"])

        expired_event = CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            assignment=assignment,
            payload_json={
                "campaign_id": assignment.campaign_id,
                "assignment_id": assignment.id,
                "coupon_series": assignment.coupon_series,
                "coupon_code": assignment.coupon_code,
                "status": CouponCampaignAssignment.Status.EXPIRED,
                "meta": {
                    "post_campaign_close": True,
                    "remove_from_guest": True,
                    "release_to_pool": False,
                },
            },
            status=CouponVtelemaxSyncQueue.Status.ACKED,
            attempts=1,
            sent_at=self.now - timedelta(days=1),
            ack_at=self.now - timedelta(days=1),
            next_retry_at=self.now - timedelta(days=1),
        )
        self._create_order_fact(
            guest=guest,
            order_number=242,
            coupon_series="TEST",
            coupon_number="TST-EXPIRED-ACKED-1",
            uniq_suffix="expired-acked-1",
        )

        stats = CouponRedemptionSyncService().sync_from_order_facts()

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN)
        self.assertEqual(assignment.used_order_id, 242)
        assignment.coupon.refresh_from_db()
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.USED_AFTER_CAMPAIGN)

        events = list(
            CouponVtelemaxSyncQueue.objects.filter(
                assignment=assignment,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            ).order_by("id")
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].id, expired_event.id)
        self.assertEqual(events[0].status, CouponVtelemaxSyncQueue.Status.ACKED)
        self.assertEqual(events[0].payload_json.get("status"), CouponCampaignAssignment.Status.EXPIRED)
        self.assertEqual(events[1].status, CouponVtelemaxSyncQueue.Status.PENDING)
        self.assertEqual(events[1].payload_json.get("status"), CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN)
        self.assertEqual(events[1].payload_json.get("used_order_id"), 242)
        self.assertEqual(events[1].payload_json.get("meta", {}).get("used_after_campaign"), True)

        self.assertEqual(stats.assignments_marked_used, 1)
        self.assertEqual(stats.assignments_marked_used_after_campaign, 1)
        self.assertEqual(stats.queue_events_created, 1)
        self.assertEqual(stats.queue_events_updated, 0)

    def test_counts_missing_assignment_and_guest_mismatch(self):
        guest_expected = self._create_guest("3333")
        guest_actual = self._create_guest("4444")
        self._create_assignment(guest=guest_expected, code="TST-MISMATCH-1")

        self._create_order_fact(
            guest=guest_actual,
            order_number=303,
            coupon_series="TEST",
            coupon_number="TST-MISMATCH-1",
            uniq_suffix="mismatch-1",
        )
        self._create_order_fact(
            guest=guest_actual,
            order_number=404,
            coupon_series="TEST",
            coupon_number="TST-NO-ASSIGNMENT",
            uniq_suffix="no-assignment",
        )

        stats = CouponRedemptionSyncService().sync_from_order_facts()

        self.assertEqual(stats.assignments_matched, 1)
        self.assertEqual(stats.assignments_guest_mismatch, 1)
        self.assertEqual(stats.assignments_missing, 1)

    def test_autoscenario_reassign_ignores_old_canceled_assignment(self):
        old_guest = self._create_guest("6501")
        new_guest = self._create_guest("6502")
        old_assignment = self._create_autoscenario_assignment(
            guest=old_guest,
            code="AUTO-REASSIGN-1",
            status=CouponAutoscenarioAssignment.Status.CANCELED,
        )
        old_assignment.coupon.pool_status = CouponRegistryEntry.PoolStatus.VERIFIED_LOADED
        old_assignment.coupon.is_active = True
        old_assignment.coupon.assigned_at = None
        old_assignment.coupon.save(update_fields=["pool_status", "is_active", "assigned_at", "updated_at"])

        new_run = CouponAutoscenarioRun.objects.create(
            scenario=old_assignment.scenario,
            config=old_assignment.config,
            status=CouponAutoscenarioRun.Status.COMPLETED,
            execution_mode=old_assignment.config.execution_mode,
            scan_limit=10,
            max_recipients_per_run=10,
            scanned_guests=1,
            matched_guests=1,
            sendable_guests=1,
            eligible_guests=1,
            planned_assignments=1,
            created_assignments=1,
            queue_events_created=1,
            coupon_shortage=0,
        )
        new_assignment = CouponAutoscenarioAssignment.objects.create(
            run=new_run,
            scenario=old_assignment.scenario,
            config=old_assignment.config,
            guest=new_guest,
            coupon=old_assignment.coupon,
            phone_e164=new_guest.phone,
            coupon_series="AUTO",
            coupon_code="AUTO-REASSIGN-1",
            venue_code="DEP_1",
            venue_name="Автосценарий",
            promo_text="Автосценарий: повторная выдача",
            assigned_at=self.now + timedelta(minutes=1),
            sent_at=self.now + timedelta(minutes=1),
            lifetime_expires_at=self.now + timedelta(days=14),
            status=CouponAutoscenarioAssignment.Status.SENT,
            vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now + timedelta(minutes=1),
        )
        old_assignment.coupon.pool_status = CouponRegistryEntry.PoolStatus.ASSIGNED
        old_assignment.coupon.is_active = False
        old_assignment.coupon.assigned_at = self.now + timedelta(minutes=1)
        old_assignment.coupon.save(update_fields=["pool_status", "is_active", "assigned_at", "updated_at"])
        self._create_order_fact(
            guest=new_guest,
            order_number=650,
            coupon_series="AUTO",
            coupon_number="AUTO-REASSIGN-1",
            uniq_suffix="auto-reassign-1",
        )

        stats = CouponRedemptionSyncService().sync_from_order_facts()

        old_assignment.refresh_from_db()
        new_assignment.refresh_from_db()
        self.assertEqual(old_assignment.status, CouponAutoscenarioAssignment.Status.CANCELED)
        self.assertIsNone(old_assignment.used_order_id)
        self.assertEqual(new_assignment.status, CouponAutoscenarioAssignment.Status.USED)
        self.assertEqual(new_assignment.used_order_id, 650)
        self.assertEqual(new_assignment.used_business_date, self.now.date())
        self.assertEqual(
            CouponVtelemaxSyncQueue.objects.filter(
                autoscenario_assignment=new_assignment,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            ).count(),
            1,
        )
        self.assertEqual(stats.assignments_matched, 1)
        self.assertEqual(stats.autoscenario_assignments_matched, 1)
        self.assertEqual(stats.assignments_guest_mismatch, 0)

    def test_dry_run_does_not_write_changes(self):
        guest = self._create_guest("5555")
        assignment = self._create_assignment(guest=guest, code="TST-DRY-1")
        self._create_order_fact(
            guest=guest,
            order_number=505,
            coupon_series="TEST",
            coupon_number="TST-DRY-1",
            uniq_suffix="dry-1",
        )

        stats = CouponRedemptionSyncService().sync_from_order_facts(dry_run=True)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.SENT)
        self.assertIsNone(assignment.used_order_id)
        self.assertEqual(
            CouponVtelemaxSyncQueue.objects.filter(
                assignment=assignment,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            ).count(),
            0,
        )
        self.assertEqual(stats.assignments_marked_used, 1)
