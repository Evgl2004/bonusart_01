from __future__ import annotations

import io
import uuid

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from guests.models import (
    BotProfile,
    CouponAutoscenarioAssignment,
    CouponAutomationConfig,
    CouponAutomationRule,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    GuestBotBinding,
    GuestWelcomeRegistrationEvent,
    IikoCustomerCategorySyncEvent,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
    VtelemaxRecipientChannel,
)
from guests.services.coupon_autoscenarios import create_autoscenario_dispatch_after_vtelemax_ack
from guests.services.coupon_constants import COUPON_VENUE_GLOBAL_CODE
from guests.services.notification_registry import SCENARIO_CODE_WELCOME_COUPON
from guests.services.welcome_registration_events import WelcomeRegistrationEventProcessor


@override_settings(
    IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
    IIKO_ACTIVE_COUPON_CATEGORY_ID="active-coupon-category",
    IIKO_CUSTOMER_CATEGORY_GATE_REQUIRE_ACK=True,
    WELCOME_COUPON_SCENARIO_CODE=SCENARIO_CODE_WELCOME_COUPON,
    WELCOME_COUPON_PROCESSING_ENABLED=True,
    WELCOME_COUPON_PROCESSING_BATCH_SIZE=10,
    WELCOME_COUPON_PROCESSING_MAX_ATTEMPTS=3,
    WELCOME_COUPON_PROCESSING_RETRY_BASE_SECONDS=10,
    WELCOME_COUPON_PROCESSING_RETRY_MAX_SECONDS=60,
    VTELEMAX_SYNC_BOT_CODE_TELEGRAM="tg-welcome",
    VTELEMAX_SYNC_BOT_CODE_MAX="max-welcome",
    VTELEMAX_SYNC_BOT_CODE_VK="vk-welcome",
)
class WelcomeRegistrationEventProcessorTests(TestCase):
    """
    Проверки welcome-очереди регистрации гостей vtelemax.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.telegram_bot = BotProfile.objects.create(
            code="tg-welcome",
            name="Telegram welcome",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            token="test-token",
            is_active=True,
        )
        self.max_bot = BotProfile.objects.create(
            code="max-welcome",
            name="MAX welcome",
            provider_type=BotProfile.ProviderType.MAX,
            token="test-token",
            is_active=True,
        )
        self.vk_bot = BotProfile.objects.create(
            code="vk-welcome",
            name="VK welcome",
            provider_type=BotProfile.ProviderType.VK,
            token="test-token",
            is_active=True,
        )
        self.template = MessageTemplate.objects.create(
            name="Welcome coupon template",
            description="",
            message_text="Ваш приветственный купон: {coupon_code}",
            created_by="tests",
            is_active=True,
        )
        self.scenario = NotificationScenario.objects.create(
            code=SCENARIO_CODE_WELCOME_COUPON,
            name="Регистрация гостя + приветственный купон",
            description="",
            is_active=True,
            is_system=True,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            template=self.template,
            priority=NotificationScenario.Priority.HIGH,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            settings={"coupon_required": True, "registration_event_source": "vtelemax"},
        )
        self.scenario.bot_profiles.add(self.telegram_bot, self.max_bot, self.vk_bot)
        self.config = CouponAutomationConfig.objects.create(
            scenario=self.scenario,
            scenario_type=CouponAutomationConfig.ScenarioType.WELCOME_REGISTRATION_COUPON,
            execution_mode=CouponAutomationConfig.ExecutionMode.AUTOMATIC,
            coupon_validity_days=14,
            max_recipients_per_run=100,
            max_active_coupons_per_guest=1,
            cooldown_days=3650,
            settings={"registration_event_source": "vtelemax"},
        )
        CouponAutomationRule.objects.create(
            config=self.config,
            is_active=True,
            scope_type=CouponAutomationRule.ScopeType.GLOBAL,
            coupon_series="WELCOME_SERIES",
            venue_code=COUPON_VENUE_GLOBAL_CODE,
            venue_name="Вся сеть",
            coupon_validity_days=14,
            priority=10,
        )

    def _create_coupon(self, *, code: str = "WELCOME001") -> CouponRegistryEntry:
        return CouponRegistryEntry.objects.create(
            series="WELCOME_SERIES",
            code=code,
            venue_code=COUPON_VENUE_GLOBAL_CODE,
            venue_name="Вся сеть",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
        )

    def _create_event(self, **overrides) -> GuestWelcomeRegistrationEvent:
        person_id = overrides.pop("person_id", uuid.uuid4())
        platform = overrides.pop("platform", "telegram")
        phone = overrides.pop("phone_e164", "+79990001001")
        external_id = overrides.pop("external_id", f"{platform}-chat-1")
        event_id = overrides.pop("event_id", f"evt-{uuid.uuid4()}")
        customer_id = overrides.pop("iiko_customer_id", f"iiko-{event_id}")
        registered_at = overrides.pop("registered_at", self.now)
        profile = overrides.pop("profile", {"first_name": "Анна", "last_name": "Петрова"})
        payload = {
            "request_id": f"req-{event_id}",
            "event_id": event_id,
            "event_type": GuestWelcomeRegistrationEvent.EventType.GUEST_REGISTERED,
            "person_id": str(person_id),
            "platform": platform,
            "phone_e164": phone,
            "customerId": customer_id,
            "external_id": external_id,
            "rules_accepted": True,
            "notifications_allowed": True,
            "is_registered": True,
            "registered_at": registered_at.isoformat(),
            "state_updated_at": registered_at.isoformat(),
            "account_created_at": registered_at.isoformat(),
            "effective_updated_at": registered_at.isoformat(),
            "profile": profile,
        }
        payload.update(overrides.pop("payload_overrides", {}))
        rules_accepted = overrides.pop("rules_accepted", bool(payload["rules_accepted"]))
        notifications_allowed = overrides.pop(
            "notifications_allowed",
            bool(payload["notifications_allowed"]),
        )
        is_registered = overrides.pop("is_registered", bool(payload["is_registered"]))
        return GuestWelcomeRegistrationEvent.objects.create(
            event_id=event_id,
            request_id=payload["request_id"],
            event_type=GuestWelcomeRegistrationEvent.EventType.GUEST_REGISTERED,
            person_id=person_id,
            platform=platform,
            phone_e164=phone,
            iiko_customer_id=customer_id,
            external_id=external_id,
            rules_accepted=rules_accepted,
            notifications_allowed=notifications_allowed,
            is_registered=is_registered,
            registered_at=registered_at,
            state_updated_at=registered_at,
            account_created_at=registered_at,
            effective_updated_at=registered_at,
            next_retry_at=self.now,
            profile=profile,
            payload_json=payload,
            **overrides,
        )

    def _processor(self) -> WelcomeRegistrationEventProcessor:
        return WelcomeRegistrationEventProcessor.from_settings()

    def test_process_event_reserves_coupon_and_waits_for_external_gates(self):
        self._create_coupon()
        event = self._create_event(event_id="evt-welcome-success")

        stats = self._processor().process_batch(limit=10, now=self.now)

        self.assertEqual(stats.processed, 1)
        self.assertEqual(stats.channel_applied, 1)
        self.assertEqual(stats.coupon_reserved, 1)
        event.refresh_from_db()
        self.assertEqual(event.status, GuestWelcomeRegistrationEvent.Status.COUPON_RESERVED)
        self.assertIsNotNone(event.guest_id)
        self.assertIsNotNone(event.vtelemax_channel_id)
        self.assertIsNotNone(event.coupon_assignment_id)

        guest = event.guest
        self.assertEqual(guest.phone, "+79990001001")
        self.assertEqual(guest.iiko_id, "iiko-evt-welcome-success")
        channel = event.vtelemax_channel
        self.assertEqual(channel.guest_id, guest.id)
        self.assertEqual(channel.person_id, event.person_id)
        self.assertEqual(channel.platform, "telegram")
        self.assertTrue(channel.notifications_allowed)
        self.assertIsNotNone(channel.guest_binding_id)

        assignment = event.coupon_assignment
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.RESERVED)
        self.assertEqual(assignment.trigger_key, "welcome_registration")
        self.assertEqual(assignment.person_id, event.person_id)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )
        self.assertEqual(
            assignment.iiko_category_add_status,
            CouponAutoscenarioAssignment.IikoCategorySyncStatus.PENDING,
        )
        self.assertEqual(CouponVtelemaxSyncQueue.objects.filter(autoscenario_assignment=assignment).count(), 1)
        self.assertEqual(
            IikoCustomerCategorySyncEvent.objects.filter(autoscenario_assignment=assignment).count(),
            1,
        )
        self.assertEqual(NotificationEvent.objects.count(), 0)
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_second_registration_in_another_bot_does_not_issue_second_coupon(self):
        self._create_coupon()
        first_event = self._create_event(
            event_id="evt-welcome-first",
            phone_e164="+79990001002",
            platform="telegram",
            external_id="tg-chat-2",
        )
        second_event = self._create_event(
            event_id="evt-welcome-second",
            phone_e164="+79990001002",
            platform="max",
            external_id="max-chat-2",
            iiko_customer_id="iiko-evt-welcome-first",
        )
        processor = self._processor()

        processor.process_batch(limit=10, now=self.now)
        processor.process_batch(limit=10, now=self.now)

        first_event.refresh_from_db()
        second_event.refresh_from_db()
        self.assertEqual(first_event.status, GuestWelcomeRegistrationEvent.Status.COUPON_RESERVED)
        self.assertEqual(second_event.status, GuestWelcomeRegistrationEvent.Status.SKIPPED)
        self.assertEqual(second_event.skip_reason, "welcome_coupon_already_issued")
        self.assertEqual(CouponAutoscenarioAssignment.objects.count(), 1)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 1)

    def test_dispatch_after_ack_uses_registration_bot_even_when_old_primary_exists(self):
        self._create_coupon()
        guest = Guest.objects.create(
            phone="+79990001003",
            first_name="Иван",
            iiko_id="iiko-existing-guest",
            created_at=self.now,
            updated_at=self.now,
        )
        GuestBotBinding.objects.create(
            guest=guest,
            bot=self.vk_bot,
            external_chat_id="vk-old-chat",
            external_user_id="vk-old-chat",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        event = self._create_event(
            event_id="evt-welcome-route",
            phone_e164="+79990001003",
            platform="telegram",
            external_id="tg-new-chat",
            iiko_customer_id="iiko-existing-guest",
        )

        self._processor().process_batch(limit=10, now=self.now)
        event.refresh_from_db()
        assignment = event.coupon_assignment
        self.assertEqual(assignment.person_id, event.person_id)
        self.assertFalse(
            GuestBotBinding.objects.get(guest=guest, bot=self.telegram_bot).is_primary
        )

        self.assertEqual(create_autoscenario_dispatch_after_vtelemax_ack(assignment_id=assignment.id, now=self.now), 0)
        assignment.vtelemax_sync_status = CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK
        assignment.vtelemax_synced_at = self.now
        assignment.save(update_fields=["vtelemax_sync_status", "vtelemax_synced_at", "updated_at"])
        self.assertEqual(create_autoscenario_dispatch_after_vtelemax_ack(assignment_id=assignment.id, now=self.now), 0)
        assignment.iiko_category_add_status = CouponAutoscenarioAssignment.IikoCategorySyncStatus.OK
        assignment.iiko_category_add_synced_at = self.now
        assignment.save(update_fields=["iiko_category_add_status", "iiko_category_add_synced_at", "updated_at"])

        self.assertEqual(create_autoscenario_dispatch_after_vtelemax_ack(assignment_id=assignment.id, now=self.now), 1)

        task = DispatchTask.objects.get()
        self.assertEqual(task.bot_profile_id, self.telegram_bot.id)
        self.assertEqual(task.external_chat_id, "tg-new-chat")
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.SENT)

    def test_no_available_coupon_keeps_event_for_retry(self):
        event = self._create_event(event_id="evt-welcome-no-coupon")

        stats = self._processor().process_batch(limit=10, now=self.now)

        self.assertEqual(stats.failed, 1)
        event.refresh_from_db()
        self.assertEqual(event.status, GuestWelcomeRegistrationEvent.Status.ERROR)
        self.assertEqual(event.attempts, 1)
        self.assertGreater(event.next_retry_at, self.now)
        self.assertIn("Нет свободного проверенного купона", event.error_text)
        self.assertEqual(CouponAutoscenarioAssignment.objects.count(), 0)

    def test_not_sendable_event_is_skipped_without_guest_creation(self):
        event = self._create_event(
            event_id="evt-welcome-no-optin",
            phone_e164="+79990001004",
            payload_overrides={"notifications_allowed": False},
            notifications_allowed=False,
        )

        stats = self._processor().process_batch(limit=10, now=self.now)

        self.assertEqual(stats.skipped, 1)
        event.refresh_from_db()
        self.assertEqual(event.status, GuestWelcomeRegistrationEvent.Status.SKIPPED)
        self.assertEqual(event.skip_reason, "channel_not_sendable")
        self.assertEqual(Guest.objects.filter(phone="+79990001004").count(), 0)
        self.assertEqual(VtelemaxRecipientChannel.objects.count(), 0)

    def test_command_health_check_and_dry_run_do_not_process_events(self):
        self._create_coupon()
        self._create_event(event_id="evt-welcome-command")
        health_stdout = io.StringIO()
        dry_stdout = io.StringIO()

        call_command("process_welcome_registration_events", "--health-check", stdout=health_stdout)
        call_command("process_welcome_registration_events", "--dry-run", stdout=dry_stdout)

        self.assertIn("due_events=1", health_stdout.getvalue())
        self.assertIn("dry_run=True", dry_stdout.getvalue())
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.get().status, GuestWelcomeRegistrationEvent.Status.NEW)
