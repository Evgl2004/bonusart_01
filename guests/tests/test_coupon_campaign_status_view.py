from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from guests.models import Mailing, MessageTemplate


class CouponCampaignStatusViewTests(TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Coupon status view template",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )

    def _create_mailing(self, *, coupon_series: str) -> Mailing:
        return Mailing.objects.create(
            name="Coupon status campaign",
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
            coupon_series=coupon_series,
            coupon_venue_code="DEP_1",
            coupon_venue_name="Тестовое заведение",
            coupon_promo_text="Скидка 20%",
        )

    def test_status_view_adds_coupon_report_context(self):
        mailing = self._create_mailing(coupon_series="TEST")
        snapshot_mock = Mock()
        snapshot_mock.to_dict.return_value = {
            "coupon_series": "TEST",
            "coupons_sent_total": 10,
            "assignments_used": 4,
            "usage_rate_percent": 40.0,
            "returned_guest_coupon": 2,
            "returned_guests_rate_percent": 50.0,
            "revenue_net_used": "3500.00",
            "coupon_orders_avg_check": "875.00",
            "used_late_total": 1,
        }

        with patch(
            "guests.views_mailings_v2.build_coupon_campaign_performance_snapshot",
            return_value=snapshot_mock,
        ) as build_mock:
            response = self.client.get(
                reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}),
                secure=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["coupon_campaign_report"]["coupon_series"], "TEST")
        self.assertEqual(response.context["coupon_campaign_report_error"], "")
        self.assertContains(response, "Купонный отчёт (оперативный)")
        build_mock.assert_called_once_with(mailing=mailing)

    def test_status_view_shows_warning_when_report_build_fails(self):
        mailing = self._create_mailing(coupon_series="TEST")
        with patch(
            "guests.views_mailings_v2.build_coupon_campaign_performance_snapshot",
            side_effect=RuntimeError("report failed"),
        ):
            response = self.client.get(
                reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}),
                secure=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["coupon_campaign_report"])
        self.assertTrue(response.context["coupon_campaign_report_error"])
        self.assertContains(response, "Не удалось построить купонный отчёт")

    def test_status_view_skips_report_for_non_coupon_campaign(self):
        mailing = self._create_mailing(coupon_series="")
        with patch("guests.views_mailings_v2.build_coupon_campaign_performance_snapshot") as build_mock:
            response = self.client.get(
                reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}),
                secure=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["coupon_campaign_report"])
        self.assertEqual(response.context["coupon_campaign_report_error"], "")
        build_mock.assert_not_called()
