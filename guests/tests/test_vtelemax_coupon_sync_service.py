from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch

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
from guests.services.vtelemax_coupon_sync import VtelemaxCouponSyncService


class VtelemaxCouponSyncServiceTests(TestCase):
    """
    Проверки delivery-контура очереди купонов SAGUR -> vtelemax.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Coupon sync template",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Coupon sync campaign",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(hours=1),
            scheduled_time_end=self.now + timedelta(hours=3),
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
            coupon_promo_text="Тестовый промо-текст",
        )

    def _create_assignment_with_event(
        self,
        *,
        event_status: str = CouponVtelemaxSyncQueue.Status.PENDING,
        attempts: int = 0,
    ) -> tuple[CouponCampaignAssignment, CouponVtelemaxSyncQueue]:
        guest = Guest.objects.create(
            phone=f"+7999{self._random_digits(7)}",
            first_name="Guest",
            created_at=self.now,
            updated_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code=f"TST-{self._random_digits(6)}",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        assignment = CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=guest,
            coupon=coupon,
            person_id=None,
            phone_e164=guest.phone,
            coupon_series=coupon.series,
            coupon_code=coupon.code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Тестовый промо-текст",
            assigned_at=self.now,
            lifetime_expires_at=self.now + timedelta(days=7),
            status=CouponCampaignAssignment.Status.RESERVED,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.PENDING,
            vtelemax_synced_at=None,
            vtelemax_sync_error=None,
        )
        event = CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
            assignment=assignment,
            payload_json={
                "campaign_id": int(self.mailing.id),
                "guest_id": int(guest.id),
                "coupon_series": coupon.series,
                "coupon_code": coupon.code,
                "status": assignment.status,
            },
            status=event_status,
            attempts=attempts,
            next_retry_at=self.now - timedelta(seconds=5),
        )
        return assignment, event

    @staticmethod
    def _random_digits(length: int) -> str:
        """
        Возвращает детерминированный набор цифр фиксированной длины для тестовых ключей.
        """
        return ("1234567890" * ((length // 10) + 1))[:length]

    def _build_service(self) -> VtelemaxCouponSyncService:
        return VtelemaxCouponSyncService(
            base_url="https://vtelemax.example",
            endpoint_path="/internal/integration/v1/sagur/coupons/events",
            hmac_secret="secret-key",
            timeout_seconds=5.0,
            require_https=True,
            max_attempts=8,
            retry_base_seconds=30,
            retry_max_seconds=300,
        )

    @patch("guests.services.vtelemax_coupon_sync.httpx.Client")
    def test_process_batch_marks_event_acked_and_assignment_ok(self, mocked_client_cls):
        assignment, event = self._create_assignment_with_event()
        mocked_response = Mock()
        mocked_response.status_code = 200
        mocked_response.text = ""
        mocked_response.json.return_value = {"ok": True}
        mocked_client = Mock()
        mocked_client.post.return_value = mocked_response
        mocked_client_cls.return_value.__enter__.return_value = mocked_client

        stats = self._build_service().process_batch(limit=10, now=self.now)

        self.assertEqual(stats.processed, 1)
        self.assertEqual(stats.acked, 1)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.assignments_acked, 1)

        event.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.ACKED)
        self.assertEqual(event.attempts, 1)
        self.assertIsNotNone(event.sent_at)
        self.assertIsNotNone(event.ack_at)
        self.assertEqual(assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.OK)
        self.assertIsNotNone(assignment.vtelemax_synced_at)
        self.assertIsNone(assignment.vtelemax_sync_error)

    @patch("guests.services.vtelemax_coupon_sync.httpx.Client")
    def test_process_batch_marks_event_error_and_assignment_error(self, mocked_client_cls):
        assignment, event = self._create_assignment_with_event()
        mocked_response = Mock()
        mocked_response.status_code = 500
        mocked_response.text = "Internal error"
        mocked_response.json.return_value = {"message": "integration failed"}
        mocked_client = Mock()
        mocked_client.post.return_value = mocked_response
        mocked_client_cls.return_value.__enter__.return_value = mocked_client

        stats = self._build_service().process_batch(limit=10, now=self.now)

        self.assertEqual(stats.processed, 1)
        self.assertEqual(stats.acked, 0)
        self.assertEqual(stats.failed, 1)

        event.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.ERROR)
        self.assertEqual(event.attempts, 1)
        self.assertIn("status=500", str(event.last_error))
        self.assertGreater(event.next_retry_at, self.now)
        self.assertEqual(assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.ERROR)
        self.assertIn("status=500", str(assignment.vtelemax_sync_error))
        self.assertIsNone(assignment.vtelemax_synced_at)

    def test_process_batch_counts_max_attempts_as_skipped(self):
        _, event = self._create_assignment_with_event(attempts=8)

        stats = self._build_service().process_batch(limit=10, now=self.now)

        self.assertEqual(stats.skipped_max_attempts, 1)
        self.assertEqual(stats.processed, 0)
        self.assertEqual(stats.acked, 0)
        self.assertEqual(stats.failed, 0)

        event.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.PENDING)
