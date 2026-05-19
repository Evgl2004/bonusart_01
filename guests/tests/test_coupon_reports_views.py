from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from guests.models import (
    CouponCampaignAssignment,
    CouponPoolBatch,
    CouponRegistryEntry,
    Guest,
    Mailing,
    MessageTemplate,
    TerminalDepartmentMap,
)


class CouponReportsViewsTests(TestCase):
    """
    Проверки новых экранов этапа C:
    1. хаб «Отчёты»;
    2. реестр купонов;
    3. отчёт по купонной кампании.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Coupon reports template",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Coupon campaign A",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(hours=1),
            scheduled_time_end=self.now + timedelta(hours=6),
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=(self.now - timedelta(hours=1)).time(),
            send_window_end=(self.now + timedelta(hours=2)).time(),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.NORMAL,
            coupon_series="TEST",
            coupon_venue_code="DEP_1",
            coupon_venue_name="Тестовое заведение",
            coupon_promo_text="Скидка 20%",
        )

    def test_reports_hub_opens_and_shows_coupon_links(self):
        response = self.client.get(reverse("reports"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Купонные кампании")
        self.assertContains(response, "Реестр купонов")

    def test_coupon_registry_filters_by_series_and_campaign(self):
        guest = Guest.objects.create(
            phone="+79991112233",
            first_name="Иван",
            created_at=self.now,
            updated_at=self.now,
        )
        batch = CouponPoolBatch.objects.create(
            batch_code="TEST_20260514_001",
            series="TEST",
            prefix="TST-",
            random_length=12,
            count_requested=2,
            count_generated=2,
            generated_by="tester",
        )
        coupon_match = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-AAAA1111",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            batch=batch,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
            iiko_checked_at=self.now,
        )
        CouponRegistryEntry.objects.create(
            series="OTHER",
            code="OTH-BBBB2222",
            venue_code="DEP_2",
            venue_name="Другое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            batch=batch,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
            iiko_checked_at=self.now,
        )
        CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=guest,
            coupon=coupon_match,
            person_id=uuid4(),
            phone_e164=guest.phone,
            coupon_series=coupon_match.series,
            coupon_code=coupon_match.code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Скидка 20%",
            assigned_at=self.now,
            status=CouponCampaignAssignment.Status.USED,
            used_at=self.now,
            used_order_id=123456789,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )

        response = self.client.get(
            reverse("coupon_registry"),
            {"series": "TEST", "campaign_id": str(self.mailing.id)},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TST-AAAA1111")
        self.assertNotContains(response, "OTH-BBBB2222")
        self.assertContains(response, str(self.mailing.id))
        self.assertContains(response, guest.phone)
        self.assertContains(response, "Использован")
        self.assertContains(response, "123456789")
        self.assertContains(response, "Синхронизирован")
        self.assertContains(response, "Генерация купонов")
        self.assertNotContains(response, "Операции реестра")

    def test_coupon_generation_form_uses_venue_catalog(self):
        TerminalDepartmentMap.objects.create(
            terminal_group_id="terminal-gruzinka",
            department_id="DEP_GRUZINKA",
            department_name="Грузинка",
            is_active=True,
        )

        response = self.client.get(reverse("coupon_generation"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Грузинка (DEP_GRUZINKA)")
        self.assertContains(response, '<select name="venue_code"', html=False)
        self.assertNotContains(response, 'name="venue_name"', html=False)

    def test_coupon_generation_shows_selected_batch_actions(self):
        batch = CouponPoolBatch.objects.create(
            batch_code="TEST_BATCH_ACTIONS",
            series="TEST_ACTIONS",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            prefix="TST-",
            random_length=8,
            count_requested=2,
            count_generated=2,
            generated_by="tester",
            export_file_path="tools/test_actions.csv",
        )
        CouponRegistryEntry.objects.create(
            series=batch.series,
            code="TST-ACTION1",
            venue_code=batch.venue_code,
            venue_name=batch.venue_name,
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            batch=batch,
            pool_status=CouponRegistryEntry.PoolStatus.GENERATED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.NOT_CHECKED,
        )

        response = self.client.get(
            reverse("coupon_generation"),
            {"batch_code": batch.batch_code},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Текущая партия")
        self.assertContains(response, batch.batch_code)
        self.assertContains(response, "Скачать CSV")
        self.assertContains(response, "Проверить iikoCard")
        self.assertContains(response, "Открыть в реестре")

    def test_coupon_generation_shows_recent_batches_filtered_by_series(self):
        matched_batch = CouponPoolBatch.objects.create(
            batch_code="TEST_RECENT_MATCH_001",
            series="TEST_RECENT",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            prefix="TST-",
            random_length=8,
            count_requested=2,
            count_generated=2,
            generated_by="tester",
            export_file_path="tools/test_recent_match.csv",
        )
        CouponPoolBatch.objects.create(
            batch_code="TEST_RECENT_OTHER_001",
            series="OTHER_RECENT",
            venue_code="DEP_2",
            venue_name="Другое заведение",
            prefix="OTH-",
            random_length=8,
            count_requested=1,
            count_generated=1,
            generated_by="tester",
            export_file_path="tools/test_recent_other.csv",
        )

        response = self.client.get(
            reverse("coupon_generation"),
            {"series_hint": matched_batch.series},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Последние партии")
        self.assertContains(response, matched_batch.batch_code)
        self.assertNotContains(response, "TEST_RECENT_OTHER_001")
        self.assertContains(response, "Скачать CSV")

    def test_coupon_campaign_reports_builds_selected_campaign_report(self):
        snapshot_mock = Mock()
        snapshot_mock.to_dict.return_value = {
            "coupon_series": "TEST",
            "recipients_total": 120,
            "assignments_total": 120,
            "assignments_reserved": 10,
            "assignments_sent": 90,
            "assignments_used": 40,
            "assignments_used_after_campaign": 5,
            "assignments_error": 2,
            "coupons_sent_total": 130,
            "used_within_campaign": 35,
            "used_late_total": 5,
            "returned_guest_coupon": 12,
            "returned_window_days": 30,
            "revenue_net_used": "57000.00",
            "coupon_orders_avg_check": "1425.00",
            "unique_used_guests": 38,
            "usage_rate_percent": 30.77,
            "returned_guests_rate_percent": 34.29,
            "late_usage_rows": [],
        }

        with patch(
            "guests.views_reports.build_coupon_campaign_performance_snapshot",
            return_value=snapshot_mock,
        ) as build_mock:
            response = self.client.get(
                reverse("reports_coupon_campaigns"),
                {"campaign_id": str(self.mailing.id)},
                secure=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Кампания #")
        self.assertContains(response, "57000.00")
        self.assertContains(response, "30,77%")
        self.assertContains(response, "Карточка кампании")
        build_mock.assert_called_once_with(mailing=self.mailing)
