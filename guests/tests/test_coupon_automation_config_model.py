from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from guests.models import CouponAutomationConfig, MessageTemplate, NotificationScenario
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON


class CouponAutomationConfigModelTests(TestCase):
    """
    Проверки слоя настроек купонных автосценариев.

    Эти тесты не запускают рассылки и не создают купонные назначения: модель
    только фиксирует утверждённые параметры будущего автосценария.
    """

    def setUp(self):
        super().setUp()
        self.template = MessageTemplate.objects.create(
            name="Coupon automation template",
            description="",
            message_text="Ваш купон: {coupon_code}",
            created_by="test",
            is_active=True,
        )

    def _scenario(self, *, code: str = SCENARIO_CODE_INACTIVE_30D_COUPON) -> NotificationScenario:
        return NotificationScenario.objects.create(
            code=code,
            name="Остывшие гости 30 дней + купон",
            description="",
            is_active=False,
            is_system=True,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            template=self.template,
            priority=NotificationScenario.Priority.BULK,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            settings={},
        )

    def test_report_only_allows_empty_coupon_series(self):
        config = CouponAutomationConfig(
            scenario=self._scenario(),
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
        )

        config.full_clean()

    def test_pilot_requires_coupon_series(self):
        config = CouponAutomationConfig(
            scenario=self._scenario(),
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
        )

        with self.assertRaises(ValidationError) as raised:
            config.full_clean()

        self.assertIn("coupon_series", raised.exception.message_dict)

    def test_automatic_requires_coupon_series(self):
        config = CouponAutomationConfig(
            scenario=self._scenario(),
            execution_mode=CouponAutomationConfig.ExecutionMode.AUTOMATIC,
            coupon_series="",
        )

        with self.assertRaises(ValidationError) as raised:
            config.full_clean()

        self.assertIn("coupon_series", raised.exception.message_dict)

    def test_valid_coupon_config_normalizes_strings(self):
        config = CouponAutomationConfig(
            scenario=self._scenario(),
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            coupon_series="  E2E_AUTOSCENARIO  ",
            venue_code="  __global__  ",
            venue_name="  По сети  ",
            coupon_validity_days=14,
            min_order_amount=Decimal("200.00"),
            max_recipients_per_run=50,
            max_active_coupons_per_guest=1,
            cooldown_days=30,
        )

        config.full_clean()

        self.assertEqual(config.coupon_series, "E2E_AUTOSCENARIO")
        self.assertEqual(config.venue_code, "__global__")
        self.assertEqual(config.venue_name, "По сети")

    def test_numeric_validation(self):
        config = CouponAutomationConfig(
            scenario=self._scenario(),
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            coupon_series="E2E_AUTOSCENARIO",
            coupon_validity_days=0,
            min_order_amount=Decimal("-1.00"),
            max_recipients_per_run=0,
            max_active_coupons_per_guest=0,
            cooldown_days=-1,
        )

        with self.assertRaises(ValidationError) as raised:
            config.full_clean()

        errors = raised.exception.message_dict
        self.assertIn("coupon_validity_days", errors)
        self.assertIn("min_order_amount", errors)
        self.assertIn("max_recipients_per_run", errors)
        self.assertIn("max_active_coupons_per_guest", errors)
        self.assertIn("cooldown_days", errors)
