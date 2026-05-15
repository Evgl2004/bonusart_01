from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from guests.models import CouponCampaignAssignment, CouponRegistryEntry, Guest, Mailing, MessageTemplate


class CloseCouponCampaignsCommandTests(TestCase):
    """
    Проверки консольной команды close_coupon_campaigns.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Close coupons cmd template",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Close coupons cmd campaign",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(days=2),
            scheduled_time_end=self.now - timedelta(days=1),
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
            phone="+79990000901",
            first_name="Тест",
            created_at=self.now,
            updated_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-CMD-1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        self.assignment = CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=guest,
            coupon=coupon,
            coupon_series="TEST",
            coupon_code="TST-CMD-1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Скидка 20%",
            assigned_at=self.now,
            lifetime_expires_at=self.mailing.scheduled_time_end,
            status=CouponCampaignAssignment.Status.SENT,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )

    def test_command_closes_campaign_assignments(self):
        out = StringIO()
        call_command(
            "close_coupon_campaigns",
            campaign_id=self.mailing.id,
            stdout=out,
        )
        output = out.getvalue()
        self.assertIn("close_coupon_campaigns done", output)
        self.assertIn("assignments_expired=1", output)

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, CouponCampaignAssignment.Status.EXPIRED)
