from __future__ import annotations

import io
import json
from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from guests.models import (
    CouponCampaignAssignment,
    CouponRegistryEntry,
    Guest,
    Mailing,
    MailingGuest,
    MessageTemplate,
    OrderFact,
)


class ReportCouponCampaignPerformanceCommandTests(TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Coupon report cmd template",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Coupon report command campaign",
            template=self.template,
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

        self.guest = Guest.objects.create(
            phone="+79995550101",
            first_name="Guest-1",
            created_at=self.now,
            updated_at=self.now,
        )
        MailingGuest.objects.create(
            mailing=self.mailing,
            guest=self.guest,
            phone=self.guest.phone,
            email="",
            text_mailing_list="Купонная рассылка",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-CMD-001",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=self.guest,
            coupon=coupon,
            phone_e164=self.guest.phone,
            coupon_series="TEST",
            coupon_code="TST-CMD-001",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Скидка 20%",
            assigned_at=self.now,
            lifetime_expires_at=self.mailing.scheduled_time_end,
            status=CouponCampaignAssignment.Status.USED,
            sent_at=self.now,
            used_at=self.now,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )
        OrderFact.objects.create(
            guest=self.guest,
            business_date=self.now.date(),
            department_id="DEP_1",
            department_name="Тестовое заведение",
            order_number=5001,
            uniq_order_id="uniq-cmd-5001",
            gross_sum="1000.00",
            net_sum="800.00",
            discount_sum="200.00",
            bonus_sum="0.00",
            items_count=2,
            categories_count=1,
            coupon_used=True,
            coupon_series="TEST",
            coupon_number="TST-CMD-001",
            first_seen_at=self.now,
        )

    def test_raises_for_unknown_campaign(self):
        with self.assertRaises(CommandError):
            call_command(
                "report_coupon_campaign_performance",
                "--campaign-id",
                "999999",
                stdout=io.StringIO(),
            )

    def test_outputs_json_payload(self):
        out = io.StringIO()
        call_command(
            "report_coupon_campaign_performance",
            "--campaign-id",
            str(self.mailing.id),
            "--as-json",
            stdout=out,
        )
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["campaign_id"], self.mailing.id)
        self.assertEqual(payload["coupon_series"], "TEST")
        self.assertEqual(payload["assignments_used"], 1)
        self.assertEqual(payload["coupons_sent_total"], 1)
        self.assertEqual(payload["revenue_net_used"], "800.00")
        self.assertIn("coupon_orders_avg_check", payload)
        self.assertIn("returned_guests_rate_percent", payload)

    def test_outputs_human_readable_report(self):
        out = io.StringIO()
        call_command(
            "report_coupon_campaign_performance",
            "--campaign-id",
            str(self.mailing.id),
            stdout=out,
        )
        text = out.getvalue()
        self.assertIn("=== Отчёт по купонной кампании ===", text)
        self.assertIn("campaign_id=", text)
        self.assertIn("coupon_usage_rate=", text)
        self.assertIn("coupon_orders_avg_check=", text)
