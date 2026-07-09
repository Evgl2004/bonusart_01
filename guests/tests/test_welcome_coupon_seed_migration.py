from __future__ import annotations

import importlib

from django.apps import apps
from django.test import TestCase

from guests.models import (
    BotProfile,
    CouponAutomationConfig,
    MessageTemplate,
    NotificationScenario,
)
from guests.services.notification_registry import SCENARIO_CODE_WELCOME_COUPON


class WelcomeCouponSeedMigrationTests(TestCase):
    """
    Проверки воспроизводимого создания системного welcome-автосценария.
    """

    def _run_seed(self):
        migration = importlib.import_module(
            "guests.migrations.0059_seed_welcome_coupon_autoscenario"
        )
        migration.seed_welcome_coupon_autoscenario(apps, None)

    def test_seed_creates_draft_visible_in_coupon_autoscenario_list(self):
        bot = BotProfile.objects.create(
            code="tg_seed_welcome",
            name="Telegram seed welcome",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            token="test-token",
            is_active=True,
        )

        self._run_seed()

        scenario = NotificationScenario.objects.get(code=SCENARIO_CODE_WELCOME_COUPON)
        config = scenario.coupon_automation_config
        self.assertFalse(scenario.is_active)
        self.assertTrue(scenario.is_system)
        self.assertEqual(scenario.trigger_type, NotificationScenario.TriggerType.SCHEDULE)
        self.assertEqual(
            scenario.distribution_mode,
            NotificationScenario.DistributionMode.IMMEDIATE,
        )
        self.assertIn("{coupon_code}", scenario.template.message_text)
        self.assertEqual(list(scenario.bot_profiles.values_list("id", flat=True)), [bot.id])
        self.assertEqual(
            config.scenario_type,
            CouponAutomationConfig.ScenarioType.WELCOME_REGISTRATION_COUPON,
        )
        self.assertEqual(config.execution_mode, CouponAutomationConfig.ExecutionMode.REPORT_ONLY)
        self.assertEqual(config.cooldown_days, 3650)
        self.assertEqual(config.coupon_rules.count(), 0)

    def test_seed_is_idempotent(self):
        self._run_seed()
        self._run_seed()

        self.assertEqual(
            NotificationScenario.objects.filter(code=SCENARIO_CODE_WELCOME_COUPON).count(),
            1,
        )
        self.assertEqual(
            MessageTemplate.objects.filter(name="SYSTEM_WELCOME_COUPON_TEMPLATE").count(),
            1,
        )
        scenario = NotificationScenario.objects.get(code=SCENARIO_CODE_WELCOME_COUPON)
        self.assertEqual(CouponAutomationConfig.objects.filter(scenario=scenario).count(), 1)
