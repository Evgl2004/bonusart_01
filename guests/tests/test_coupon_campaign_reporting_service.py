from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    CouponCampaignAssignment,
    CouponRegistryEntry,
    Guest,
    Mailing,
    MailingGuest,
    MessageTemplate,
    OlapCheckSyncJournal,
    OlapSalesRawLine,
    OrderFact,
)
from guests.services.coupon_campaign_reporting import (
    CouponCampaignPerformanceSnapshot,
    build_coupon_campaign_performance_snapshot,
)


class CouponCampaignReportingServiceTests(TestCase):
    """
    Проверки расчёта KPI по купонной кампании.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Coupon reporting template",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )

    def _create_mailing(self, *, coupon_series: str = "TEST") -> Mailing:
        mailing = Mailing.objects.create(
            name="Coupon report campaign",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(days=2),
            scheduled_time_end=self.now + timedelta(days=2),
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
        return mailing

    def _create_guest(self, suffix: str) -> Guest:
        return Guest.objects.create(
            phone=f"+7999555{suffix}",
            first_name=f"Guest-{suffix}",
            created_at=self.now,
            updated_at=self.now,
        )

    def _create_assignment(
        self,
        *,
        mailing: Mailing,
        guest: Guest,
        code: str,
        status: str,
        used_at=None,
    ) -> CouponCampaignAssignment:
        sent_statuses = {
            CouponCampaignAssignment.Status.SENT,
            CouponCampaignAssignment.Status.USED,
            CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN,
        }
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
            sent_at=self.now if status in sent_statuses else None,
            used_at=used_at,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )

    def _create_order_fact(
        self,
        *,
        guest: Guest,
        order_number: int,
        uniq_suffix: str,
        business_date,
        coupon_number: str,
        net_sum: str,
    ) -> OrderFact:
        return OrderFact.objects.create(
            guest=guest,
            business_date=business_date,
            department_id="DEP_1",
            department_name="Тестовое заведение",
            order_number=order_number,
            uniq_order_id=f"uniq-{uniq_suffix}",
            gross_sum=net_sum,
            net_sum=net_sum,
            discount_sum="0.00",
            bonus_sum="0.00",
            items_count=1,
            categories_count=1,
            coupon_used=True,
            coupon_series="TEST",
            coupon_number=coupon_number,
            first_seen_at=self.now,
        )

    def _create_olap_journal(self, *, guest: Guest, order_number: int, uniq_order_id: str):
        return OlapCheckSyncJournal.objects.create(
            idempotency_key=f"journal-{uniq_order_id}",
            status=OlapCheckSyncJournal.Status.LOADED,
            guest=guest,
            terminal_group_id="TERM_1",
            order_number=order_number,
            order_external_id=uniq_order_id,
            business_date=self.now.date(),
            department_id="DEP_1",
            department_code="DEP_1",
            loaded_at=self.now,
        )

    def _create_raw_line(
        self,
        *,
        journal: OlapCheckSyncJournal,
        guest: Guest,
        uniq_order_id: str,
        order_number: int,
        dish_code: str,
        dish_name: str,
        before: str,
        after: str,
        coupon_number: str,
    ) -> OlapSalesRawLine:
        return OlapSalesRawLine.objects.create(
            row_fingerprint=f"{uniq_order_id}-{dish_code}",
            sync_journal=journal,
            guest=guest,
            business_date=self.now.date(),
            department_id="DEP_1",
            department_code="DEP_1",
            department_name="Тестовое заведение",
            order_number=order_number,
            uniq_order_id=uniq_order_id,
            dish_code=dish_code,
            dish_name=dish_name,
            dish_amount="1",
            dish_sum_before_discount=before,
            dish_sum_after_discount=after,
            discount_sum=str(Decimal(before) - Decimal(after)),
            bonus_sum="0.00",
            coupon_series="TEST",
            coupon_number=coupon_number,
        )

    def test_builds_kpi_snapshot_with_usage_and_return_metrics(self):
        mailing = self._create_mailing(coupon_series="TEST")

        guest_used_in_campaign = self._create_guest("1001")
        guest_used_late = self._create_guest("1002")
        guest_sent = self._create_guest("1003")
        guest_reserved = self._create_guest("1004")

        for guest in [guest_used_in_campaign, guest_used_late, guest_sent, guest_reserved]:
            MailingGuest.objects.create(
                mailing=mailing,
                guest=guest,
                phone=guest.phone,
                email="",
                text_mailing_list="Купонная рассылка",
                scheduled_datetime=self.now,
                status=MailingGuest.Status.PLANNED,
                created_at=self.now,
            )

        assignment_used_in_campaign = self._create_assignment(
            mailing=mailing,
            guest=guest_used_in_campaign,
            code="TST-USED-IN",
            status=CouponCampaignAssignment.Status.USED,
            used_at=self.now,
        )
        assignment_used_late = self._create_assignment(
            mailing=mailing,
            guest=guest_used_late,
            code="TST-USED-LATE",
            status=CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN,
            used_at=mailing.scheduled_time_end + timedelta(days=1),
        )
        self._create_assignment(
            mailing=mailing,
            guest=guest_sent,
            code="TST-SENT",
            status=CouponCampaignAssignment.Status.SENT,
            used_at=None,
        )
        self._create_assignment(
            mailing=mailing,
            guest=guest_reserved,
            code="TST-RESERVED",
            status=CouponCampaignAssignment.Status.RESERVED,
            used_at=None,
        )

        self._create_order_fact(
            guest=guest_used_in_campaign,
            order_number=101,
            uniq_suffix="in-campaign",
            business_date=mailing.scheduled_time_begin.date(),
            coupon_number=assignment_used_in_campaign.coupon_code,
            net_sum="500.00",
        )
        self._create_order_fact(
            guest=guest_used_late,
            order_number=102,
            uniq_suffix="late",
            business_date=(mailing.scheduled_time_end + timedelta(days=1)).date(),
            coupon_number=assignment_used_late.coupon_code,
            net_sum="200.00",
        )

        snapshot = build_coupon_campaign_performance_snapshot(
            mailing=mailing,
            returned_window_days=30,
            late_rows_limit=20,
        )
        payload = snapshot.to_dict()

        self.assertEqual(payload["recipients_total"], 4)
        self.assertEqual(payload["assignments_total"], 4)
        self.assertEqual(payload["assignments_reserved"], 1)
        self.assertEqual(payload["assignments_sent"], 1)
        self.assertEqual(payload["assignments_used"], 2)
        self.assertEqual(payload["assignments_used_after_campaign"], 1)
        self.assertEqual(payload["coupons_sent_total"], 3)
        self.assertEqual(payload["used_within_campaign"], 1)
        self.assertEqual(payload["used_late_total"], 1)
        self.assertEqual(payload["returned_guest_coupon"], 1)
        self.assertEqual(payload["returned_window_days"], 30)
        self.assertEqual(payload["revenue_net_used"], "700.00")
        self.assertEqual(payload["coupon_orders_avg_check"], "350.00")
        self.assertEqual(payload["coupon_orders_total"], 2)
        self.assertEqual(payload["unique_used_guests"], 2)
        self.assertEqual(payload["usage_rate_percent"], 66.67)
        self.assertEqual(payload["returned_guests_rate_percent"], 100.0)
        self.assertEqual(len(payload["late_usage_rows"]), 1)
        self.assertEqual(payload["late_usage_rows"][0]["coupon_code"], "TST-USED-LATE")

    def test_uses_paid_olap_lines_for_revenue_and_product_breakdown(self):
        mailing = self._create_mailing(coupon_series="TEST")
        guest = self._create_guest("3001")
        MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Купонная рассылка",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )
        assignment = self._create_assignment(
            mailing=mailing,
            guest=guest,
            code="TST-GIFT",
            status=CouponCampaignAssignment.Status.USED,
            used_at=self.now,
        )
        self._create_order_fact(
            guest=guest,
            order_number=301,
            uniq_suffix="gift",
            business_date=self.now.date(),
            coupon_number=assignment.coupon_code,
            net_sum="540.00",
        )
        journal = self._create_olap_journal(
            guest=guest,
            order_number=301,
            uniq_order_id="uniq-gift",
        )
        self._create_raw_line(
            journal=journal,
            guest=guest,
            uniq_order_id="uniq-gift",
            order_number=301,
            dish_code="DRINK-1",
            dish_name="Фейхоа",
            before="350.00",
            after="350.00",
            coupon_number=assignment.coupon_code,
        )
        self._create_raw_line(
            journal=journal,
            guest=guest,
            uniq_order_id="uniq-gift",
            order_number=301,
            dish_code="COFFEE-AM",
            dish_name="Американо",
            before="190.00",
            after="0.00",
            coupon_number=assignment.coupon_code,
        )

        payload = build_coupon_campaign_performance_snapshot(mailing=mailing).to_dict()

        self.assertEqual(payload["revenue_net_used"], "350.00")
        self.assertEqual(payload["coupon_orders_avg_check"], "350.00")
        self.assertEqual(payload["coupon_orders_total"], 1)
        self.assertEqual(len(payload["daily_usage_rows"]), 5)
        today_row = next(
            row
            for row in payload["daily_usage_rows"]
            if row["business_date"] == self.now.date().isoformat()
        )
        self.assertEqual(payload["daily_usage_rows"][0]["orders_count"], 0)
        self.assertEqual(today_row["orders_count"], 1)
        self.assertEqual(today_row["used_coupons_count"], 1)
        self.assertEqual(today_row["revenue_net"], "350.00")
        self.assertEqual(today_row["avg_check"], "350.00")
        self.assertEqual(len(payload["product_rank_rows"]), 2)
        product_names = {row["dish_name"] for row in payload["product_rank_rows"]}
        self.assertEqual(product_names, {"Фейхоа", "Американо"})
        gift_row = next(row for row in payload["product_rank_rows"] if row["dish_name"] == "Американо")
        self.assertEqual(gift_row["gross_sum"], "190.00")
        self.assertEqual(gift_row["revenue_net"], "0.00")
        self.assertEqual(payload["order_detail_rows"][0]["revenue_net"], "350.00")
        self.assertEqual(len(payload["order_detail_rows"][0]["items"]), 2)

    def test_returns_empty_metrics_when_campaign_has_no_coupon_series(self):
        mailing = self._create_mailing(coupon_series="")
        guest = self._create_guest("2001")
        MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Обычная рассылка",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )

        snapshot = build_coupon_campaign_performance_snapshot(mailing=mailing)
        payload = snapshot.to_dict()

        self.assertEqual(payload["coupon_series"], "")
        self.assertEqual(payload["recipients_total"], 1)
        self.assertEqual(payload["assignments_total"], 0)
        self.assertEqual(payload["coupons_sent_total"], 0)
        self.assertEqual(payload["assignments_used"], 0)
        self.assertEqual(payload["assignments_used_after_campaign"], 0)
        self.assertEqual(payload["usage_rate_percent"], 0.0)
        self.assertEqual(payload["returned_guest_coupon"], 0)
        self.assertEqual(payload["returned_guests_rate_percent"], 0.0)
        self.assertEqual(payload["revenue_net_used"], str(Decimal("0")))
        self.assertEqual(payload["coupon_orders_total"], 0)

    def test_returned_guests_rate_falls_back_to_assignments_used_when_in_campaign_zero(self):
        snapshot = CouponCampaignPerformanceSnapshot(
            campaign_id=1,
            coupon_series="TEST",
            assignments_used=4,
            used_within_campaign=0,
            returned_guest_coupon=2,
        )
        self.assertEqual(snapshot.returned_guests_rate_percent, 50.0)
