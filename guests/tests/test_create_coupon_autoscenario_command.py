from __future__ import annotations

import io
from datetime import time

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from guests.models import (
    BotProfile,
    CouponAutomationConfig,
    CouponAutomationRule,
    MessageTemplate,
    NotificationScenario,
)


class CreateCouponAutoscenarioCommandTests(TestCase):
    """
    Проверки сервисной команды создания купонного автосценария.
    """

    def setUp(self):
        self.bot = BotProfile.objects.create(
            code="tg_cmd_main",
            name="Telegram command",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            token="test-token",
            is_active=True,
        )

    def test_dry_run_validates_without_creating_records(self):
        """
        Без --confirm команда должна только проверить входные данные.
        """
        stdout = io.StringIO()

        call_command(
            "create_coupon_autoscenario",
            "--code",
            "cmd_dry_coupon",
            "--name",
            "Командный сухой прогон",
            "--scenario-type",
            CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
            "--inactive-days",
            "35",
            "--template-name",
            "Командный шаблон",
            "--template-text",
            "Ваш купон: {coupon_code}",
            "--bot-profile-code",
            self.bot.code,
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn("Режим: сухой прогон", output)
        self.assertIn("scenario_code=cmd_dry_coupon", output)
        self.assertFalse(NotificationScenario.objects.filter(code="cmd_dry_coupon").exists())
        self.assertFalse(MessageTemplate.objects.filter(name="Командный шаблон").exists())

    def test_confirm_creates_draft_with_existing_template(self):
        """
        С --confirm команда должна создать выключенный сценарий и черновую купонную настройку.
        """
        template = MessageTemplate.objects.create(
            name="Существующий командный шаблон",
            message_text="Ваш купон: {coupon_code}",
            created_by="test",
            is_active=True,
        )
        stdout = io.StringIO()

        call_command(
            "create_coupon_autoscenario",
            "--code",
            "cmd_existing_template",
            "--name",
            "Командный сценарий с шаблоном",
            "--scenario-type",
            CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
            "--inactive-days",
            "40",
            "--template-id",
            str(template.id),
            "--bot-profile-id",
            str(self.bot.id),
            "--confirm",
            stdout=stdout,
        )

        scenario = NotificationScenario.objects.get(code="cmd_existing_template")
        config = scenario.coupon_automation_config
        self.assertFalse(scenario.is_active)
        self.assertFalse(scenario.is_system)
        self.assertEqual(scenario.trigger_type, NotificationScenario.TriggerType.SCHEDULE)
        self.assertEqual(scenario.template_id, template.id)
        self.assertEqual(scenario.settings["inactive_days"], 40)
        self.assertEqual(list(scenario.bot_profiles.values_list("id", flat=True)), [self.bot.id])
        self.assertEqual(config.execution_mode, CouponAutomationConfig.ExecutionMode.REPORT_ONLY)
        self.assertIn("config_id=", stdout.getvalue())

    def test_confirm_copies_source_config_and_rules(self):
        """
        Команда должна уметь создать отдельный черновик на основе существующего автосценария.
        """
        source_template = MessageTemplate.objects.create(
            name="Источник команды",
            message_text="Купон источника: {coupon_code}",
            created_by="test",
            is_active=True,
        )
        source_scenario = NotificationScenario.objects.create(
            code="cmd_source_coupon",
            name="Источник команды",
            template=source_template,
            is_active=True,
            is_system=False,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.HIGH,
            target_mode=NotificationScenario.TargetMode.ALL_BOTS,
            distribution_mode=NotificationScenario.DistributionMode.UNIFORM,
            send_window_begin=time(9, 0),
            send_window_end=time(18, 0),
            timezone="Asia/Yekaterinburg",
            settings={"coupon_required": True, "inactive_days": 45},
        )
        source_scenario.bot_profiles.add(self.bot)
        source_config = CouponAutomationConfig.objects.create(
            scenario=source_scenario,
            scenario_type=CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
            execution_mode=CouponAutomationConfig.ExecutionMode.AUTOMATIC,
            venue_selection_mode=CouponAutomationConfig.VenueSelectionMode.ALL_VISITED,
            coupon_series="SRC_FALLBACK",
            coupon_validity_days=21,
            max_recipients_per_run=25,
            cooldown_days=45,
            settings={"pilot_phones": ["+79120000000"]},
        )
        CouponAutomationRule.objects.create(
            config=source_config,
            is_active=True,
            scope_type=CouponAutomationRule.ScopeType.VENUE,
            venue_code="DEP_1",
            venue_name="Сами Сусами",
            coupon_series="SRC_DEP",
            coupon_validity_days=14,
            priority=10,
        )
        stdout = io.StringIO()

        call_command(
            "create_coupon_autoscenario",
            "--code",
            "cmd_copied_coupon",
            "--name",
            "Командная копия",
            "--source-config-id",
            str(source_config.id),
            "--confirm",
            stdout=stdout,
        )

        copied_scenario = NotificationScenario.objects.get(code="cmd_copied_coupon")
        copied_config = copied_scenario.coupon_automation_config
        copied_rule = copied_config.coupon_rules.get()
        self.assertFalse(copied_scenario.is_active)
        self.assertEqual(copied_scenario.priority, source_scenario.priority)
        self.assertEqual(copied_scenario.target_mode, source_scenario.target_mode)
        self.assertEqual(copied_scenario.distribution_mode, source_scenario.distribution_mode)
        self.assertEqual(copied_scenario.send_window_begin, time(9, 0))
        self.assertEqual(copied_scenario.settings["inactive_days"], 45)
        self.assertEqual(copied_config.execution_mode, CouponAutomationConfig.ExecutionMode.REPORT_ONLY)
        self.assertEqual(copied_config.venue_selection_mode, source_config.venue_selection_mode)
        self.assertEqual(copied_config.coupon_series, "SRC_FALLBACK")
        self.assertEqual(copied_config.settings["pilot_phones"], ["+79120000000"])
        self.assertEqual(copied_rule.coupon_series, "SRC_DEP")
        self.assertEqual(copied_rule.venue_code, "DEP_1")
        self.assertIn("source_rules_count=1", stdout.getvalue())

    def test_rejects_template_without_coupon_code(self):
        """
        Команда не должна создавать автосценарий с шаблоном без кода купона.
        """
        template = MessageTemplate.objects.create(
            name="Шаблон без купона",
            message_text="Привет без купона",
            created_by="test",
            is_active=True,
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command(
                "create_coupon_autoscenario",
                "--code",
                "cmd_invalid_template",
                "--name",
                "Неверный шаблон",
                "--scenario-type",
                CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
                "--inactive-days",
                "30",
                "--template-id",
                str(template.id),
                "--bot-profile-id",
                str(self.bot.id),
                "--confirm",
                stdout=stdout,
            )

        self.assertIn("В шаблоне купонного автосценария должен быть параметр", stdout.getvalue())
        self.assertFalse(NotificationScenario.objects.filter(code="cmd_invalid_template").exists())
