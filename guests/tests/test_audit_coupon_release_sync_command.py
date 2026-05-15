from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from guests.models import (
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    Guest,
    Mailing,
    MessageTemplate,
)


class AuditCouponReleaseSyncCommandTests(TestCase):
    """
    Проверки консольного аудита состояния освобождения купонов.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        template = MessageTemplate.objects.create(
            name="Audit release template",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Audit release campaign",
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

    def _create_assignment(
        self,
        *,
        code: str,
        status: str,
        is_active: bool,
        pool_status: str,
        assigned_at_shift_minutes: int = 0,
    ) -> CouponCampaignAssignment:
        guest = Guest.objects.create(
            phone=f"+7999000{code[-4:]}",
            first_name="Guest",
            created_at=self.now,
            updated_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code=code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=is_active,
            pool_status=pool_status,
            assigned_at=self.now + timedelta(minutes=assigned_at_shift_minutes),
        )
        return CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=guest,
            coupon=coupon,
            coupon_series="TEST",
            coupon_code=code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Скидка 20%",
            assigned_at=self.now + timedelta(minutes=assigned_at_shift_minutes),
            lifetime_expires_at=self.mailing.scheduled_time_end,
            status=status,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.PENDING,
        )

    def test_command_reports_release_waiting_ack_and_reserved_stale(self):
        waiting_assignment = self._create_assignment(
            code="TST-WAIT-1",
            status=CouponCampaignAssignment.Status.CANCELED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            assignment=waiting_assignment,
            payload_json={
                "status": CouponCampaignAssignment.Status.CANCELED,
                "meta": {"release_to_pool": True, "remove_from_guest": True},
            },
            status=CouponVtelemaxSyncQueue.Status.PENDING,
            next_retry_at=self.now,
        )

        released_assignment = self._create_assignment(
            code="TST-REL-1",
            status=CouponCampaignAssignment.Status.CANCELED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
        )
        released_assignment.coupon.assigned_at = None
        released_assignment.coupon.save(update_fields=["assigned_at", "updated_at"])
        CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            assignment=released_assignment,
            payload_json={
                "status": CouponCampaignAssignment.Status.CANCELED,
                "meta": {"release_to_pool": True, "remove_from_guest": True},
            },
            status=CouponVtelemaxSyncQueue.Status.ACKED,
            next_retry_at=self.now,
        )

        self._create_assignment(
            code="TST-RES-1",
            status=CouponCampaignAssignment.Status.RESERVED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
            assigned_at_shift_minutes=-180,
        )

        out = StringIO()
        call_command(
            "audit_coupon_release_sync",
            older_than_minutes=60,
            stdout=out,
        )
        output = out.getvalue()
        self.assertIn("canceled_release_requested_total: 2", output)
        self.assertIn("release_waiting_ack: 1", output)
        self.assertIn("release_done: 1", output)
        self.assertIn("reserved_stale_total: 1", output)
