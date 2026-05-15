from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from guests.models import (
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    Guest,
    Mailing,
    MessageTemplate,
    VtelemaxSyncState,
)


READY_SETTINGS = {
    "VTELEMAX_COUPON_SYNC_ENABLED": True,
    "VTELEMAX_COUPON_SYNC_BASE_URL": "https://vtelemax.example",
    "VTELEMAX_COUPON_SYNC_HMAC_SECRET": "secret",
    "VTELEMAX_COUPON_SYNC_ENDPOINT": "/internal/integration/v1/sagur/coupons/events",
    "VTELEMAX_COUPON_SYNC_REQUIRE_HTTPS": True,
    "VTELEMAX_COUPON_SYNC_BATCH_SIZE": 100,
    "VTELEMAX_COUPON_SYNC_MAX_ATTEMPTS": 8,
    "VTELEMAX_COUPON_SYNC_SCHEDULE_ENABLED": True,
    "COUPON_CAMPAIGN_CLOSE_ENABLED": True,
    "COUPON_CAMPAIGN_CLOSE_SCHEDULE_ENABLED": True,
    "COUPON_REDEMPTION_SYNC_ENABLED": True,
    "VTELEMAX_COUPON_SYNC_GATE_REQUIRE_FRESH_STATE": True,
    "VTELEMAX_COUPON_SYNC_GATE_MAX_SYNC_AGE_MINUTES": 120,
}


class AuditCouponReleaseReadinessCommandTests(TestCase):
    """
    Проверки read-only аудита готовности купонного контура к релизу.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def _create_fresh_vtelemax_state(self) -> None:
        VtelemaxSyncState.objects.create(
            key="vtelemax_recipients",
            last_status=VtelemaxSyncState.Status.SUCCESS,
            last_success_at=self.now,
            last_finished_at=self.now,
        )

    def _create_coupon_assignment(
        self,
        *,
        status: str,
        coupon_status: str,
        coupon_active: bool,
    ) -> CouponCampaignAssignment:
        template = MessageTemplate.objects.create(
            name="Readiness template",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )
        mailing = Mailing.objects.create(
            name="Readiness campaign",
            template=template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(days=1),
            scheduled_time_end=self.now + timedelta(days=1),
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
        guest = Guest.objects.create(
            phone="+79998887766",
            first_name="Guest",
            created_at=self.now,
            updated_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-READY-001",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=coupon_active,
            pool_status=coupon_status,
            assigned_at=self.now,
        )
        return CouponCampaignAssignment.objects.create(
            campaign=mailing,
            guest=guest,
            coupon=coupon,
            coupon_series="TEST",
            coupon_code=coupon.code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Скидка 20%",
            assigned_at=self.now,
            lifetime_expires_at=mailing.scheduled_time_end,
            status=status,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )

    @override_settings(**READY_SETTINGS)
    def test_reports_ready_when_config_state_and_queues_are_clean(self):
        self._create_fresh_vtelemax_state()

        out = StringIO()
        call_command("audit_coupon_release_readiness", "--as-json", stdout=out)

        payload = json.loads(out.getvalue())
        self.assertEqual(payload["summary"]["overall_status"], "ready")
        self.assertEqual(payload["summary"]["checks_blocked"], 0)
        self.assertEqual(payload["summary"]["checks_warning"], 0)

    @override_settings(
        VTELEMAX_COUPON_SYNC_ENABLED=False,
        VTELEMAX_COUPON_SYNC_BASE_URL="",
        VTELEMAX_SYNC_BASE_URL="",
        VTELEMAX_COUPON_SYNC_HMAC_SECRET="",
        VTELEMAX_SYNC_HMAC_SECRET="",
        VTELEMAX_COUPON_SYNC_BATCH_SIZE=150,
        VTELEMAX_COUPON_SYNC_MAX_ATTEMPTS=3,
        VTELEMAX_COUPON_SYNC_SCHEDULE_ENABLED=False,
        COUPON_CAMPAIGN_CLOSE_ENABLED=False,
        COUPON_CAMPAIGN_CLOSE_SCHEDULE_ENABLED=False,
        COUPON_REDEMPTION_SYNC_ENABLED=False,
        VTELEMAX_COUPON_SYNC_GATE_REQUIRE_FRESH_STATE=True,
    )
    def test_reports_blocked_for_bad_config_stale_state_and_exhausted_queue(self):
        CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
            payload_json={},
            status=CouponVtelemaxSyncQueue.Status.ERROR,
            attempts=3,
            next_retry_at=self.now,
        )

        out = StringIO()
        call_command("audit_coupon_release_readiness", "--as-json", stdout=out)

        payload = json.loads(out.getvalue())
        checks = {item["code"]: item for item in payload["checks"]}
        self.assertEqual(payload["summary"]["overall_status"], "blocked")
        self.assertEqual(checks["coupon_sync_enabled"]["status"], "blocked")
        self.assertEqual(checks["coupon_sync_config"]["status"], "blocked")
        self.assertEqual(checks["coupon_sync_batch_size"]["status"], "warning")
        self.assertEqual(checks["recipient_sync_freshness"]["status"], "blocked")
        self.assertEqual(checks["coupon_queue_max_attempts"]["status"], "blocked")
        self.assertEqual(checks["coupon_campaign_close_enabled"]["status"], "blocked")

    @override_settings(**READY_SETTINGS)
    def test_reports_blocked_when_release_was_acked_but_coupon_not_released(self):
        self._create_fresh_vtelemax_state()
        assignment = self._create_coupon_assignment(
            status=CouponCampaignAssignment.Status.CANCELED,
            coupon_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
            coupon_active=False,
        )
        CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            assignment=assignment,
            payload_json={
                "status": CouponCampaignAssignment.Status.CANCELED,
                "meta": {"release_to_pool": True, "remove_from_guest": True},
            },
            status=CouponVtelemaxSyncQueue.Status.ACKED,
            attempts=1,
            next_retry_at=self.now,
            ack_at=self.now,
        )

        out = StringIO()
        call_command("audit_coupon_release_readiness", "--as-json", stdout=out)

        payload = json.loads(out.getvalue())
        checks = {item["code"]: item for item in payload["checks"]}
        self.assertEqual(payload["summary"]["overall_status"], "blocked")
        self.assertEqual(checks["coupon_release_ack_side_effects"]["status"], "blocked")
        self.assertEqual(
            checks["coupon_release_ack_side_effects"]["details"]["release_acked_not_released_total"],
            1,
        )
