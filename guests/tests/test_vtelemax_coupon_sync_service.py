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

    def _create_status_update_event(
        self,
        *,
        status_value: str,
        release_to_pool: bool,
        event_status: str = CouponVtelemaxSyncQueue.Status.PENDING,
    ) -> tuple[CouponCampaignAssignment, CouponVtelemaxSyncQueue]:
        assignment, _ = self._create_assignment_with_event()
        CouponVtelemaxSyncQueue.objects.filter(assignment=assignment).delete()
        assignment.status = status_value
        assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.PENDING
        assignment.vtelemax_synced_at = None
        assignment.save(
            update_fields=["status", "vtelemax_sync_status", "vtelemax_synced_at", "updated_at"]
        )
        event = CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            assignment=assignment,
            payload_json={
                "campaign_id": int(self.mailing.id),
                "assignment_id": int(assignment.id),
                "coupon_series": assignment.coupon_series,
                "coupon_code": assignment.coupon_code,
                "status": status_value,
                "meta": {
                    "release_to_pool": bool(release_to_pool),
                    "remove_from_guest": True,
                },
            },
            status=event_status,
            attempts=0,
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

    @patch("guests.services.vtelemax_coupon_sync.httpx.Client")
    def test_status_update_canceled_release_to_pool_happens_only_after_ack(self, mocked_client_cls):
        assignment, event = self._create_status_update_event(
            status_value=CouponCampaignAssignment.Status.CANCELED,
            release_to_pool=True,
        )
        assignment.coupon.refresh_from_db()
        self.assertFalse(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)

        mocked_response = Mock()
        mocked_response.status_code = 200
        mocked_response.text = ""
        mocked_response.json.return_value = {"ok": True}
        mocked_client = Mock()
        mocked_client.post.return_value = mocked_response
        mocked_client_cls.return_value.__enter__.return_value = mocked_client

        stats = self._build_service().process_batch(limit=10, now=self.now)
        self.assertEqual(stats.acked, 1)
        self.assertEqual(stats.status_updates_acked, 1)

        event.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.ACKED)

        assignment.refresh_from_db()
        self.assertEqual(assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.OK)

        assignment.coupon.refresh_from_db()
        self.assertTrue(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.VERIFIED_LOADED)
        self.assertIsNone(assignment.coupon.assigned_at)

    @patch("guests.services.vtelemax_coupon_sync.httpx.Client")
    def test_status_update_canceled_without_release_flag_does_not_release_coupon(self, mocked_client_cls):
        assignment, event = self._create_status_update_event(
            status_value=CouponCampaignAssignment.Status.CANCELED,
            release_to_pool=False,
        )
        assignment.coupon.refresh_from_db()
        self.assertFalse(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)

        mocked_response = Mock()
        mocked_response.status_code = 200
        mocked_response.text = ""
        mocked_response.json.return_value = {"ok": True}
        mocked_client = Mock()
        mocked_client.post.return_value = mocked_response
        mocked_client_cls.return_value.__enter__.return_value = mocked_client

        stats = self._build_service().process_batch(limit=10, now=self.now)
        self.assertEqual(stats.acked, 1)
        self.assertEqual(stats.status_updates_acked, 1)

        event.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.ACKED)

        assignment.refresh_from_db()
        assignment.coupon.refresh_from_db()
        self.assertFalse(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)

    @patch("guests.services.vtelemax_coupon_sync.httpx.Client")
    def test_status_update_canceled_release_is_idempotent_on_repeated_events(self, mocked_client_cls):
        assignment, first_event = self._create_status_update_event(
            status_value=CouponCampaignAssignment.Status.CANCELED,
            release_to_pool=True,
        )
        mocked_response = Mock()
        mocked_response.status_code = 200
        mocked_response.text = ""
        mocked_response.json.return_value = {"ok": True}
        mocked_client = Mock()
        mocked_client.post.return_value = mocked_response
        mocked_client_cls.return_value.__enter__.return_value = mocked_client

        service = self._build_service()
        first_stats = service.process_batch(limit=10, now=self.now)
        self.assertEqual(first_stats.acked, 1)

        assignment.refresh_from_db()
        assignment.coupon.refresh_from_db()
        self.assertTrue(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.VERIFIED_LOADED)
        self.assertIsNone(assignment.coupon.assigned_at)

        second_event = CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            assignment=assignment,
            payload_json={
                "campaign_id": int(self.mailing.id),
                "assignment_id": int(assignment.id),
                "coupon_series": assignment.coupon_series,
                "coupon_code": assignment.coupon_code,
                "status": CouponCampaignAssignment.Status.CANCELED,
                "meta": {
                    "release_to_pool": True,
                    "remove_from_guest": True,
                },
            },
            status=CouponVtelemaxSyncQueue.Status.PENDING,
            attempts=0,
            next_retry_at=self.now - timedelta(seconds=1),
        )

        second_stats = service.process_batch(limit=10, now=self.now)
        self.assertEqual(second_stats.acked, 1)
        self.assertEqual(second_stats.status_updates_acked, 1)

        first_event.refresh_from_db()
        second_event.refresh_from_db()
        self.assertEqual(first_event.status, CouponVtelemaxSyncQueue.Status.ACKED)
        self.assertEqual(second_event.status, CouponVtelemaxSyncQueue.Status.ACKED)

        assignment.refresh_from_db()
        assignment.coupon.refresh_from_db()
        self.assertTrue(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.VERIFIED_LOADED)
        self.assertIsNone(assignment.coupon.assigned_at)

    @patch("guests.services.vtelemax_coupon_sync.httpx.Client")
    def test_status_update_used_never_releases_coupon_even_with_release_flag(self, mocked_client_cls):
        assignment, event = self._create_status_update_event(
            status_value=CouponCampaignAssignment.Status.USED,
            release_to_pool=True,
        )
        mocked_response = Mock()
        mocked_response.status_code = 200
        mocked_response.text = ""
        mocked_response.json.return_value = {"ok": True}
        mocked_client = Mock()
        mocked_client.post.return_value = mocked_response
        mocked_client_cls.return_value.__enter__.return_value = mocked_client

        stats = self._build_service().process_batch(limit=10, now=self.now)
        self.assertEqual(stats.acked, 1)
        self.assertEqual(stats.status_updates_acked, 1)

        event.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.ACKED)

        assignment.refresh_from_db()
        assignment.coupon.refresh_from_db()
        self.assertFalse(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)
