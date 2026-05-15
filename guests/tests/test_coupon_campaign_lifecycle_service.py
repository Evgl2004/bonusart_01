from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    BotProfile,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    Mailing,
    MailingGuest,
    MessageTemplate,
)
from guests.services.coupon_campaign_lifecycle import CouponCampaignLifecycleService


class CouponCampaignLifecycleServiceTests(TestCase):
    """
    Проверки безопасной отмены и post-campaign закрытия купонных кампаний.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Lifecycle template",
            description="",
            message_text="Купон",
            created_by="test",
            is_active=True,
        )

    def _create_mailing(self, *, is_active: bool = True, end_shift_days: int = 7) -> Mailing:
        return Mailing.objects.create(
            name="Lifecycle campaign",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(days=1),
            scheduled_time_end=self.now + timedelta(days=end_shift_days),
            is_active=is_active,
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
        mailing: Mailing,
        guest: Guest,
        code: str,
        status: str,
    ) -> CouponCampaignAssignment:
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code=code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
            assigned_at=self.now,
        )
        return CouponCampaignAssignment.objects.create(
            campaign=mailing,
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
            lifetime_expires_at=mailing.scheduled_time_end,
            status=status,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )

    def test_cancel_campaign_releases_reserved_and_creates_status_update(self):
        mailing = self._create_mailing(is_active=True)
        guest = Guest.objects.create(
            phone="+79990000101",
            first_name="Иван",
            created_at=self.now,
            updated_at=self.now,
        )
        row = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.PENDING,
            mailing_guest=row,
            guest=guest,
        )
        assignment = self._create_assignment(
            mailing=mailing,
            guest=guest,
            code="TST-CANCEL-1",
            status=CouponCampaignAssignment.Status.RESERVED,
        )

        stats = CouponCampaignLifecycleService().cancel_campaign(
            mailing=mailing,
            reason="manual_cancel",
            now=self.now,
            dry_run=False,
        )

        payload = stats.to_dict()
        self.assertEqual(payload["campaigns_processed"], 1)
        self.assertEqual(payload["rows_canceled"], 1)
        self.assertEqual(payload["dispatch_tasks_canceled"], 1)
        self.assertEqual(payload["assignments_canceled"], 1)
        self.assertEqual(payload["assignments_release_pending"], 1)
        self.assertEqual(payload["assignments_released_to_pool"], 0)
        self.assertEqual(payload["queue_events_created"], 1)

        mailing.refresh_from_db()
        self.assertFalse(mailing.is_active)
        row.refresh_from_db()
        self.assertEqual(row.status, MailingGuest.Status.ERROR)
        self.assertEqual(row.delivery_status, "campaign_canceled")
        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertEqual(task.status, DispatchTask.Status.CANCELED)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.CANCELED)
        self.assertEqual(assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.PENDING)

        assignment.coupon.refresh_from_db()
        self.assertFalse(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)
        self.assertIsNotNone(assignment.coupon.assigned_at)

        event = CouponVtelemaxSyncQueue.objects.get(
            assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
        )
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.PENDING)
        self.assertEqual(event.payload_json.get("status"), CouponCampaignAssignment.Status.CANCELED)
        self.assertEqual(event.payload_json.get("meta", {}).get("cancel_reason"), "manual_cancel")
        self.assertTrue(bool(event.payload_json.get("meta", {}).get("release_to_pool")))
        self.assertTrue(bool(event.payload_json.get("meta", {}).get("remove_from_guest")))

    def test_close_finished_campaigns_marks_sent_expired_and_reserved_canceled(self):
        mailing = self._create_mailing(is_active=True, end_shift_days=-1)
        guest_sent = Guest.objects.create(
            phone="+79990000201",
            first_name="Сергей",
            created_at=self.now,
            updated_at=self.now,
        )
        guest_reserved = Guest.objects.create(
            phone="+79990000202",
            first_name="Олег",
            created_at=self.now,
            updated_at=self.now,
        )
        sent_assignment = self._create_assignment(
            mailing=mailing,
            guest=guest_sent,
            code="TST-CLOSE-SENT",
            status=CouponCampaignAssignment.Status.SENT,
        )
        reserved_assignment = self._create_assignment(
            mailing=mailing,
            guest=guest_reserved,
            code="TST-CLOSE-RES",
            status=CouponCampaignAssignment.Status.RESERVED,
        )

        stats = CouponCampaignLifecycleService().close_finished_campaigns(
            close_before=self.now,
            campaign_ids=[mailing.id],
            limit=10,
            dry_run=False,
        )
        payload = stats.to_dict()
        self.assertEqual(payload["campaigns_scanned"], 1)
        self.assertEqual(payload["campaigns_processed"], 1)
        self.assertEqual(payload["campaigns_deactivated"], 1)
        self.assertEqual(payload["assignments_scanned"], 2)
        self.assertEqual(payload["assignments_expired"], 1)
        self.assertEqual(payload["assignments_canceled"], 1)
        self.assertEqual(payload["queue_events_created"], 2)

        mailing.refresh_from_db()
        self.assertFalse(mailing.is_active)

        sent_assignment.refresh_from_db()
        self.assertEqual(sent_assignment.status, CouponCampaignAssignment.Status.EXPIRED)
        sent_assignment.coupon.refresh_from_db()
        self.assertFalse(sent_assignment.coupon.is_active)
        self.assertEqual(sent_assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.EXPIRED)

        reserved_assignment.refresh_from_db()
        self.assertEqual(reserved_assignment.status, CouponCampaignAssignment.Status.CANCELED)
        reserved_assignment.coupon.refresh_from_db()
        self.assertFalse(reserved_assignment.coupon.is_active)
        self.assertEqual(reserved_assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.CANCELED)

        self.assertEqual(
            CouponVtelemaxSyncQueue.objects.filter(
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
                assignment__campaign=mailing,
            ).count(),
            2,
        )
