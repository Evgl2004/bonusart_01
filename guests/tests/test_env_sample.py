from pathlib import Path

from django.test import SimpleTestCase


class EnvSampleDocumentationTests(SimpleTestCase):
    def test_iiko_cloud_auth_variables_are_documented(self):
        root_dir = Path(__file__).resolve().parents[2]
        env_sample = (root_dir / ".env.sample").read_text(encoding="utf-8")

        expected_variables = (
            "IIKO_AUTH_MODE",
            "IIKO_LEGACY_API_LOGIN",
            "IIKO_LEGACY_AUTH_URL",
            "IIKO_APP_ID",
            "IIKO_CLIENT_SECRET",
            "IIKO_API_KEY",
            "IIKO_AUTH_URL",
            "IIKO_API_BASE_URL",
            "IIKO_ORGANIZATION_ID",
            "IIKO_AUTH_CONNECT_TIMEOUT_SECONDS",
            "IIKO_AUTH_READ_TIMEOUT_SECONDS",
            "IIKO_AUTH_MAX_RETRIES",
            "IIKO_AUTH_RETRY_BASE_SECONDS",
            "IIKO_AUTH_RETRY_MAX_SECONDS",
            "IIKO_AUTH_TOKEN_REFRESH_MARGIN_SECONDS",
            "IIKO_API_MAX_RETRIES",
            "IIKO_API_RETRY_BASE_SECONDS",
            "IIKO_API_RETRY_MAX_SECONDS",
        )

        for variable_name in expected_variables:
            with self.subTest(variable_name=variable_name):
                self.assertIn(f"{variable_name}=", env_sample)

        self.assertIn("IIKO_API_BASE_URL=https://api-ru.iiko.services/api/1", env_sample)

    def test_message_interaction_variables_are_documented(self):
        root_dir = Path(__file__).resolve().parents[2]
        env_sample = (root_dir / ".env.sample").read_text(encoding="utf-8")

        expected_variables = (
            "MESSAGE_INTERACTIONS_ENABLED",
            "MESSAGE_INTERACTIONS_ALLOWED_PROVIDERS",
            "MESSAGE_TRACKED_LINKS_ENABLED",
            "MESSAGE_TRACKED_LINK_PUBLIC_BASE_URL",
            "MESSAGE_TRACKED_LINK_ALLOWED_HOSTS",
            "TRACKED_LINK_SECRET_KEY",
            "TRACKED_LINK_DJANGO_ALLOWED_HOSTS",
            "TRACKED_LINK_PG_NAME",
            "TRACKED_LINK_PG_USER",
            "TRACKED_LINK_PG_PASSWORD",
            "TRACKED_LINK_PG_HOST",
            "TRACKED_LINK_PG_PORT",
            "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_ENABLED",
            "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_HMAC_SECRET",
            "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_REQUIRE_HTTPS",
            "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_TIMESTAMP_TOLERANCE_SECONDS",
            "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_MAX_BODY_BYTES",
            "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_RATE_LIMIT_PER_MINUTE",
        )

        for variable_name in expected_variables:
            with self.subTest(variable_name=variable_name):
                self.assertIn(f"{variable_name}=", env_sample)

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
            "COUPON_AUTOSCENARIO_DELIVERY_GUARD_ENABLED",
            "COUPON_AUTOSCENARIO_DELIVERY_GUARD_SCHEDULE_ENABLED",
            "COUPON_AUTOSCENARIO_DELIVERY_GUARD_SCHEDULE_CRON",
            "COUPON_AUTOSCENARIO_DELIVERY_GUARD_BATCH_SIZE",
        )

        for variable_name in expected_variables:
            with self.subTest(variable_name=variable_name):
                self.assertIn(f"{variable_name}=", env_sample)

        self.assertNotIn("COUPON_AUTOSCENARIO_SCHEDULE_MINUTES=", env_sample)
        self.assertNotIn("COUPON_AUTOSCENARIO_CLOSE_SCHEDULE_MINUTES=", env_sample)
        self.assertNotIn("COUPON_AUTOSCENARIO_DELIVERY_GUARD_SCHEDULE_MINUTES=", env_sample)

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

    def test_welcome_coupon_vtelemax_variables_are_documented(self):
        root_dir = Path(__file__).resolve().parents[2]
        env_sample = (root_dir / ".env.sample").read_text(encoding="utf-8")

        expected_variables = (
            "VTELEMAX_REGISTRATION_CALLBACK_ENABLED",
            "VTELEMAX_REGISTRATION_CALLBACK_HMAC_SECRET",
            "VTELEMAX_REGISTRATION_CALLBACK_REQUIRE_HTTPS",
            "VTELEMAX_REGISTRATION_CALLBACK_TIMESTAMP_TOLERANCE_SECONDS",
            "VTELEMAX_REGISTRATION_CALLBACK_MAX_BODY_BYTES",
            "WELCOME_COUPON_ACCEPT_REGISTRATIONS_FROM",
            "WELCOME_COUPON_PROCESSING_ENABLED",
            "WELCOME_COUPON_PROCESSING_SCHEDULE_ENABLED",
            "WELCOME_COUPON_PROCESSING_SCHEDULE_MINUTES",
            "WELCOME_COUPON_SCENARIO_CODE",
            "WELCOME_COUPON_PROCESSING_BATCH_SIZE",
            "WELCOME_COUPON_PROCESSING_MAX_ATTEMPTS",
            "WELCOME_COUPON_PROCESSING_RETRY_BASE_SECONDS",
            "WELCOME_COUPON_PROCESSING_RETRY_MAX_SECONDS",
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
