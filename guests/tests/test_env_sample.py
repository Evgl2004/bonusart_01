from pathlib import Path

from django.test import SimpleTestCase


class EnvSampleDocumentationTests(SimpleTestCase):
    def test_coupon_autoscenario_schedule_variables_are_documented(self):
        root_dir = Path(__file__).resolve().parents[2]
        env_sample = (root_dir / ".env.sample").read_text(encoding="utf-8")

        expected_variables = (
            "COUPON_AUTOSCENARIO_SCHEDULE_ENABLED",
            "COUPON_AUTOSCENARIO_SCHEDULE_CRON",
            "COUPON_AUTOSCENARIO_SCHEDULE_CODES",
            "COUPON_AUTOSCENARIO_SCHEDULE_LIMIT",
            "COUPON_AUTOSCENARIO_SCHEDULE_SCAN_LIMIT",
        )

        for variable_name in expected_variables:
            with self.subTest(variable_name=variable_name):
                self.assertIn(f"{variable_name}=", env_sample)

        self.assertNotIn("COUPON_AUTOSCENARIO_SCHEDULE_MINUTES=", env_sample)
