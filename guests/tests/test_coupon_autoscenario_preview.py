from __future__ import annotations

from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from guests.models import (
    CouponAutomationConfig,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
    Restaurant,
    VisitHistory,
    VtelemaxRecipientChannel,
)
from guests.services.coupon_autoscenarios import (
    CouponAutoscenarioPreviewError,
    preview_coupon_autoscenario_audience,
)
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON


class CouponAutoscenarioPreviewTests(TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Autoscenario preview template",
            description="",
            message_text="Ваш купон: {coupon_code}",
            created_by="tests",
            is_active=True,
        )
        self.scenario = self._prepare_scenario()
        self.config = CouponAutomationConfig.objects.create(
            scenario=self.scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="AUTO_INACTIVE_30",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            max_active_coupons_per_guest=1,
            cooldown_days=30,
        )
        self.restaurant = Restaurant.objects.create(iiko_id="DEP_1", name="Тестовое заведение")

    def _prepare_scenario(self) -> NotificationScenario:
        scenario, _ = NotificationScenario.objects.get_or_create(
            code=SCENARIO_CODE_INACTIVE_30D_COUPON,
            defaults={
                "name": "Не был 30 дней + купон",
                "description": "",
                "is_active": True,
                "is_system": True,
                "trigger_type": NotificationScenario.TriggerType.SCHEDULE,
                "template": self.template,
                "priority": NotificationScenario.Priority.BULK,
                "target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "distribution_mode": NotificationScenario.DistributionMode.IMMEDIATE,
                "timezone": "Asia/Yekaterinburg",
                "settings": {"inactive_days": 30},
            },
        )
        scenario.is_active = True
        scenario.trigger_type = NotificationScenario.TriggerType.SCHEDULE
        scenario.template = self.template
        scenario.settings = {"inactive_days": 30}
        scenario.timezone = "Asia/Yekaterinburg"
        scenario.save(
            update_fields=[
                "is_active",
                "trigger_type",
                "template",
                "settings",
                "timezone",
                "updated_at",
            ]
        )
        return scenario

    def _guest(self, *, phone: str, first_name: str) -> Guest:
        return Guest.objects.create(
            phone=phone,
            first_name=first_name,
            last_name="Тестовый",
            created_at=self.now,
            updated_at=self.now,
        )

    def _visit(self, *, guest: Guest, days_ago: int) -> None:
        VisitHistory.objects.create(
            guest=guest,
            restaurant=self.restaurant,
            visit_date=self.now - timedelta(days=days_ago),
            visit_count=1,
        )

    def _sendable_channel(self, *, guest: Guest, platform: str = VtelemaxRecipientChannel.Platform.TELEGRAM) -> None:
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid4(),
            platform=platform,
            phone_e164=guest.phone,
            external_id=f"external-{guest.id}",
            rules_accepted=True,
            notifications_allowed=True,
            is_registered=True,
            guest=guest,
        )

    def _available_coupon(self, *, code: str) -> CouponRegistryEntry:
        return CouponRegistryEntry.objects.create(
            series=self.config.coupon_series,
            code=code,
            venue_code=self.config.venue_code,
            venue_name=self.config.venue_name,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )

    def test_preview_counts_audience_channels_and_coupons_without_side_effects(self):
        old_sendable = self._guest(phone="+79990000001", first_name="Можно")
        old_blocked = self._guest(phone="+79990000002", first_name="БезКанала")
        recent = self._guest(phone="+79990000003", first_name="Свежий")
        self._visit(guest=old_sendable, days_ago=45)
        self._visit(guest=old_blocked, days_ago=60)
        self._visit(guest=recent, days_ago=5)
        self._sendable_channel(guest=old_sendable)
        self._available_coupon(code="AUTO-1")

        before_counts = self._side_effect_counts()

        preview = preview_coupon_autoscenario_audience(
            scenario_code=self.scenario.code,
            now=self.now,
        )

        self.assertEqual(preview.scanned_guests, 2)
        self.assertEqual(preview.matched_guests, 2)
        self.assertEqual(preview.sendable_guests, 1)
        self.assertEqual(preview.blocked_without_channel, 1)
        self.assertEqual(preview.available_coupons, 1)
        self.assertEqual(preview.coupon_shortage, 0)
        self.assertEqual([row.guest_id for row in preview.sample_rows], [old_sendable.id, old_blocked.id])
        self.assertEqual(preview.sample_rows[0].sendable_channels, ("telegram",))
        self.assertEqual(self._side_effect_counts(), before_counts)

    def test_preview_reports_coupon_shortage(self):
        old_sendable = self._guest(phone="+79990000011", first_name="Можно")
        self._visit(guest=old_sendable, days_ago=45)
        self._sendable_channel(guest=old_sendable)

        preview = preview_coupon_autoscenario_audience(
            scenario_code=self.scenario.code,
            now=self.now,
        )

        self.assertEqual(preview.available_coupons, 0)
        self.assertEqual(preview.coupon_shortage, 1)
        self.assertTrue(any("не хватает 1" in warning for warning in preview.warnings))

    def test_preview_command_prints_summary(self):
        old_sendable = self._guest(phone="+79990000021", first_name="Команда")
        self._visit(guest=old_sendable, days_ago=45)
        self._sendable_channel(guest=old_sendable)
        self._available_coupon(code="AUTO-CMD")
        stdout = StringIO()

        call_command(
            "preview_coupon_autoscenario",
            "--scenario-code",
            self.scenario.code,
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn("scenario_code=inactive_30d_coupon", output)
        self.assertIn("matched_guests=1", output)
        self.assertIn("sendable_guests=1", output)
        self.assertIn("available_coupons=1", output)

    def test_preview_requires_coupon_config(self):
        self.config.delete()

        with self.assertRaises(CouponAutoscenarioPreviewError):
            preview_coupon_autoscenario_audience(scenario_code=self.scenario.code, now=self.now)

    @staticmethod
    def _side_effect_counts() -> dict[str, int]:
        return {
            "notification_events": NotificationEvent.objects.count(),
            "dispatch_tasks": DispatchTask.objects.count(),
            "coupon_assignments": CouponCampaignAssignment.objects.count(),
            "coupon_queue": CouponVtelemaxSyncQueue.objects.count(),
        }
