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
            "COUPON_AUTOSCENARIO_CLOSE_ENABLED",
            "COUPON_AUTOSCENARIO_CLOSE_SCHEDULE_ENABLED",
            "COUPON_AUTOSCENARIO_CLOSE_SCHEDULE_CRON",
            "COUPON_AUTOSCENARIO_CLOSE_LIMIT",
        )

        for variable_name in expected_variables:
            with self.subTest(variable_name=variable_name):
                self.assertIn(f"{variable_name}=", env_sample)

        self.assertNotIn("COUPON_AUTOSCENARIO_SCHEDULE_MINUTES=", env_sample)
        self.assertNotIn("COUPON_AUTOSCENARIO_CLOSE_SCHEDULE_MINUTES=", env_sample)

    def test_iiko_customer_category_sync_variables_are_documented(self):
        root_dir = Path(__file__).resolve().parents[2]
        env_sample = (root_dir / ".env.sample").read_text(encoding="utf-8")

        expected_variables = (
            "IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED",
            "IIKO_ACTIVE_COUPON_CATEGORY_ID",
            "IIKO_ACTIVE_COUPON_CATEGORY_NAME",
            "IIKO_CUSTOMER_CATEGORY_GATE_REQUIRE_ACK",
            "IIKO_CUSTOMER_CATEGORY_SYNC_HTTP_TIMEOUT_SECONDS",
            "IIKO_CUSTOMER_CATEGORY_SYNC_MAX_ATTEMPTS",
            "IIKO_CUSTOMER_CATEGORY_SYNC_RETRY_BASE_SECONDS",
            "IIKO_CUSTOMER_CATEGORY_SYNC_RETRY_MAX_SECONDS",
            "IIKO_CUSTOMER_CATEGORY_SYNC_BATCH_SIZE",
            "IIKO_CUSTOMER_CATEGORY_SYNC_REQUEST_INTERVAL_SECONDS",
            "IIKO_CUSTOMER_CATEGORY_SYNC_SCHEDULE_ENABLED",
            "IIKO_CUSTOMER_CATEGORY_SYNC_SCHEDULE_MINUTES",
        )

        for variable_name in expected_variables:
            with self.subTest(variable_name=variable_name):
                self.assertIn(f"{variable_name}=", env_sample)

    def test_olap_live_pipeline_variables_are_documented(self):
        root_dir = Path(__file__).resolve().parents[2]
        env_sample = (root_dir / ".env.sample").read_text(encoding="utf-8")

        expected_variables = (
            "OLAP_LIVE_PIPELINE_ENABLED",
            "OLAP_LIVE_PIPELINE_SCHEDULE_ENABLED",
            "OLAP_LIVE_PIPELINE_SCHEDULE_MINUTES",
            "OLAP_LIVE_PIPELINE_BATCH_SIZE",
            "OLAP_LIVE_PIPELINE_ORDER_FACT_BATCH_SIZE",
            "OLAP_LIVE_PIPELINE_OLAP_PORTION_SIZE",
            "OLAP_LIVE_PIPELINE_MAX_ATTEMPTS",
            "OLAP_LIVE_PIPELINE_RETRY_BASE_SECONDS",
            "OLAP_LIVE_PIPELINE_RETRY_MAX_SECONDS",
            "OLAP_LIVE_PIPELINE_LOCK_TIMEOUT_SECONDS",
        )

        for variable_name in expected_variables:
            with self.subTest(variable_name=variable_name):
                self.assertIn(f"{variable_name}=", env_sample)
