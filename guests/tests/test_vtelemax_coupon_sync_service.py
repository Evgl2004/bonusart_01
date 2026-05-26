from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    BotProfile,
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponAutomationConfig,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    GuestBotBinding,
    Mailing,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
)
from guests.services.vtelemax_coupon_sync import VtelemaxCouponSyncService


class VtelemaxCouponSyncServiceTests(TestCase):
    """
    Проверки delivery-контура очереди купонов SAGUR -> vtelemax.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self._digits_counter = 0
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

    def _create_autoscenario_assignment_with_event(
        self,
    ) -> tuple[CouponAutoscenarioAssignment, CouponVtelemaxSyncQueue]:
        scenario = NotificationScenario.objects.create(
            code="coupon_sync_auto_test",
            name="Autoscenario coupon sync test",
            description="",
            is_active=True,
            is_system=True,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            template=self.template,
            priority=NotificationScenario.Priority.BULK,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            settings={"inactive_days": 30},
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            coupon_series="AUTO_SYNC",
            venue_code="DEP_1",
            venue_name="Автосценарий",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            max_active_coupons_per_guest=1,
            cooldown_days=30,
        )
        run = CouponAutoscenarioRun.objects.create(
            scenario=scenario,
            config=config,
            status=CouponAutoscenarioRun.Status.SYNC_PENDING,
            execution_mode=config.execution_mode,
            scan_limit=10,
            max_recipients_per_run=10,
            scanned_guests=1,
            matched_guests=1,
            sendable_guests=1,
            eligible_guests=1,
            planned_assignments=1,
        )
        guest = Guest.objects.create(
            phone=f"+7998{self._random_digits(7)}",
            first_name="Auto",
            created_at=self.now,
            updated_at=self.now,
        )
        bot = BotProfile.objects.create(
            code=f"auto-test-bot-{self._random_digits(4)}",
            name="Autoscenario test bot",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            token="test-token",
            is_active=True,
        )
        GuestBotBinding.objects.create(
            guest=guest,
            bot=bot,
            external_chat_id=f"chat-{guest.id}",
            external_user_id=f"user-{guest.id}",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="AUTO_SYNC",
            code=f"AUTO-{self._random_digits(6)}",
            venue_code="DEP_1",
            venue_name="Автосценарий",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        assignment = CouponAutoscenarioAssignment.objects.create(
            run=run,
            scenario=scenario,
            config=config,
            guest=guest,
            coupon=coupon,
            phone_e164=guest.phone,
            coupon_series=coupon.series,
            coupon_code=coupon.code,
            venue_code=coupon.venue_code,
            venue_name=coupon.venue_name,
            promo_text="Автосценарий промо",
            assigned_at=self.now,
            lifetime_expires_at=self.now + timedelta(days=14),
            status=CouponAutoscenarioAssignment.Status.RESERVED,
            vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )
        event = CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
            autoscenario_assignment=assignment,
            payload_json={
                "source": "autoscenario",
                "autoscenario_run_id": int(run.id),
                "scenario_code": scenario.code,
                "assignment_id": int(assignment.id),
                "guest_id": int(guest.id),
                "coupon_series": coupon.series,
                "coupon_code": coupon.code,
                "status": assignment.status,
            },
            status=CouponVtelemaxSyncQueue.Status.PENDING,
            attempts=0,
            next_retry_at=self.now - timedelta(seconds=5),
        )
        return assignment, event

    def _random_digits(self, length: int) -> str:
        """
        Возвращает детерминированный набор цифр фиксированной длины для тестовых ключей.
        """
        self._digits_counter += 1
        return str(self._digits_counter).zfill(length)[-length:]

    @staticmethod
    def _mock_vtelemax_response(mocked_client_cls, *, results: list[dict], status_code: int = 200, text: str = ""):
        mocked_response = Mock()
        mocked_response.status_code = status_code
        mocked_response.text = text
        mocked_response.json.return_value = {"ok": status_code < 400, "results": results}
        mocked_client = Mock()
        mocked_client.post.return_value = mocked_response
        mocked_client_cls.return_value.__enter__.return_value = mocked_client
        return mocked_client

    @staticmethod
    def _acked_result(event: CouponVtelemaxSyncQueue) -> dict:
        return {"event_id": str(event.event_id), "status": "acked"}

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

    def test_queue_lock_queryset_locks_only_queue_table(self):
        """
        Lock очереди не должен распространяться на nullable-связь assignment.
        """
        queryset = (
            VtelemaxCouponSyncService._queue_events_for_update_queryset()
            .select_related("assignment")
            .filter(direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS)
        )

        self.assertEqual(queryset.query.select_for_update_of, ("self",))

    @patch("guests.services.vtelemax_coupon_sync.httpx.Client")
    def test_process_batch_sends_assignments_as_single_batch_and_marks_items_acked(self, mocked_client_cls):
        assignment, event = self._create_assignment_with_event()
        second_assignment, second_event = self._create_assignment_with_event()
        mocked_client = self._mock_vtelemax_response(
            mocked_client_cls,
            results=[self._acked_result(event), self._acked_result(second_event)],
        )

        stats = self._build_service().process_batch(limit=10, now=self.now)

        self.assertEqual(stats.processed, 2)
        self.assertEqual(stats.acked, 2)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.assignments_acked, 2)
        self.assertEqual(mocked_client.post.call_count, 1)
        request_body = json.loads(mocked_client.post.call_args.kwargs["content"].decode("utf-8"))
        self.assertIn("request_id", request_body)
        self.assertEqual(request_body["direction"], CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS)
        self.assertEqual(len(request_body["items"]), 2)
        self.assertEqual(
            {item["event_id"] for item in request_body["items"]},
            {str(event.event_id), str(second_event.event_id)},
        )
        self.assertNotIn("payload", request_body)

        event.refresh_from_db()
        assignment.refresh_from_db()
        second_event.refresh_from_db()
        second_assignment.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.ACKED)
        self.assertEqual(second_event.status, CouponVtelemaxSyncQueue.Status.ACKED)
        self.assertEqual(event.attempts, 1)
        self.assertEqual(second_event.attempts, 1)
        self.assertIsNotNone(event.sent_at)
        self.assertIsNotNone(event.ack_at)
        self.assertEqual(assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.OK)
        self.assertEqual(second_assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.OK)
        self.assertIsNotNone(assignment.vtelemax_synced_at)
        self.assertIsNone(assignment.vtelemax_sync_error)

    @patch("guests.services.vtelemax_coupon_sync.httpx.Client")
    def test_process_batch_marks_autoscenario_assignment_acked(self, mocked_client_cls):
        assignment, event = self._create_autoscenario_assignment_with_event()
        assignment.scenario.is_active = False
        assignment.scenario.save(update_fields=["is_active", "updated_at"])
        mocked_client = self._mock_vtelemax_response(
            mocked_client_cls,
            results=[self._acked_result(event)],
        )

        stats = self._build_service().process_batch(limit=10, now=self.now)

        self.assertEqual(stats.processed, 1)
        self.assertEqual(stats.acked, 1)
        self.assertEqual(stats.assignments_acked, 1)
        request_body = json.loads(mocked_client.post.call_args.kwargs["content"].decode("utf-8"))
        item = request_body["items"][0]
        self.assertEqual(item["source"], "autoscenario")
        self.assertEqual(item["assignment_id"], assignment.id)
        self.assertEqual(item["autoscenario_assignment_id"], assignment.id)

        event.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.ACKED)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
        )
        self.assertIsNotNone(assignment.vtelemax_synced_at)
        self.assertIsNone(assignment.vtelemax_sync_error)
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.SENT)
        self.assertIsNotNone(assignment.sent_at)
        self.assertFalse(assignment.scenario.is_active)
        self.assertEqual(DispatchTask.objects.filter(notification_scenario=assignment.scenario).count(), 1)
        task = DispatchTask.objects.get(notification_scenario=assignment.scenario)
        self.assertEqual(task.source_type, DispatchTask.SourceType.SYSTEM)
        self.assertEqual(task.guest_id, assignment.guest_id)
        self.assertEqual(task.provider_type, BotProfile.ProviderType.TELEGRAM)
        self.assertEqual(task.priority, DispatchTask.Priority.BULK)
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertEqual(task.payload["source"], "coupon_autoscenario")
        self.assertEqual(task.payload["autoscenario_assignment_id"], assignment.id)
        self.assertEqual(task.payload["coupon_code"], assignment.coupon_code)
        self.assertIn(f"{assignment.scenario.code}:coupon_autoscenario_assignment:{assignment.id}", task.idempotency_key)
        event_obj = NotificationEvent.objects.get(scenario=assignment.scenario)
        self.assertEqual(event_obj.status, NotificationEvent.Status.TASK_CREATED)
        assignment.run.refresh_from_db()
        self.assertEqual(assignment.run.status, CouponAutoscenarioRun.Status.COMPLETED)

    @patch("guests.services.vtelemax_coupon_sync.httpx.Client")
    def test_process_batch_handles_item_level_partial_ack(self, mocked_client_cls):
        first_assignment, first_event = self._create_assignment_with_event()
        second_assignment, second_event = self._create_assignment_with_event()
        self._mock_vtelemax_response(
            mocked_client_cls,
            results=[
                self._acked_result(first_event),
                {
                    "event_id": str(second_event.event_id),
                    "status": "rejected",
                    "code": "recipient_not_found",
                    "message": "Получатель не найден",
                },
            ],
        )

        stats = self._build_service().process_batch(limit=10, now=self.now)

        self.assertEqual(stats.processed, 2)
        self.assertEqual(stats.acked, 1)
        self.assertEqual(stats.failed, 1)

        first_event.refresh_from_db()
        first_assignment.refresh_from_db()
        second_event.refresh_from_db()
        second_assignment.refresh_from_db()
        self.assertEqual(first_event.status, CouponVtelemaxSyncQueue.Status.ACKED)
        self.assertEqual(first_assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.OK)
        self.assertEqual(second_event.status, CouponVtelemaxSyncQueue.Status.ERROR)
        self.assertIn("recipient_not_found", str(second_event.last_error))
        self.assertEqual(second_assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.ERROR)
        self.assertIn("Получатель не найден", str(second_assignment.vtelemax_sync_error))

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

        self._mock_vtelemax_response(
            mocked_client_cls,
            results=[self._acked_result(event)],
        )

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

        self._mock_vtelemax_response(
            mocked_client_cls,
            results=[self._acked_result(event)],
        )

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
        mocked_client = self._mock_vtelemax_response(
            mocked_client_cls,
            results=[self._acked_result(first_event)],
        )

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

        mocked_client.post.return_value.json.return_value = {
            "ok": True,
            "results": [self._acked_result(second_event)],
        }

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
        self._mock_vtelemax_response(
            mocked_client_cls,
            results=[self._acked_result(event)],
        )

        stats = self._build_service().process_batch(limit=10, now=self.now)
        self.assertEqual(stats.acked, 1)
        self.assertEqual(stats.status_updates_acked, 1)

        event.refresh_from_db()
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.ACKED)

        assignment.refresh_from_db()
        assignment.coupon.refresh_from_db()
        self.assertFalse(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)
