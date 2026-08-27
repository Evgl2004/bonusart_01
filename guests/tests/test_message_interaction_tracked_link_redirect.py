"""Положительные и отрицательные проверки публичной службы переходов."""

from __future__ import annotations

import logging
from unittest.mock import patch

from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase, override_settings

from guests.models import (
    BotProfile,
    DispatchTask,
    InteractionButtonSet,
    InteractionLinkLabelCode,
    MessageInteraction,
    MessageInteractionLinkTransition,
    MessageInteractionTrackedLink,
)
from guests.services.message_interaction_links import (
    MessageInteractionConfigurationError,
    validate_tracked_link_target_url,
)
from guests.logging_filters import TrackedLinkTokenRedactingFilter


PUBLIC_TOKEN = "C" * 32
TARGET_URL = "https://rest.market/order?source=sagur#menu"


class TrackedLinkAddressSecurityTests(SimpleTestCase):
    """Проверяет закрытую границу конечного перенаправления."""

    @override_settings(
        MESSAGE_TRACKED_LINK_ALLOWED_HOSTS={
            "rest.market",
            "127.0.0.1",
        }
    )
    def test_ip_subdomain_credentials_and_custom_port_are_rejected(self):
        invalid_urls = (
            "https://127.0.0.1/",
            "https://sub.rest.market/",
            "https://localhost/",
            "https://service.localhost/",
            "https://service.local/",
            "https://service.internal/",
            "https://user:password@rest.market/",
            "https://rest.market:8443/",
        )

        for target_url in invalid_urls:
            with self.subTest(target_url=target_url):
                with self.assertRaises(MessageInteractionConfigurationError):
                    validate_tracked_link_target_url(target_url)

    @override_settings(MESSAGE_TRACKED_LINK_ALLOWED_HOSTS="rest.market")
    def test_exact_allowed_domain_keeps_path_query_and_fragment(self):
        self.assertEqual(
            validate_tracked_link_target_url(TARGET_URL),
            TARGET_URL,
        )


@override_settings(
    ROOT_URLCONF="loyalty_viewer.redirect_urls",
    SECURE_SSL_REDIRECT=False,
)
class TrackedLinkRedirectTests(TestCase):
    """Проверяет минимальный публичный HTTP-контракт без внешней сети."""

    def setUp(self):
        task = DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            priority=DispatchTask.Priority.NORMAL,
            status=DispatchTask.Status.DONE,
        )
        interaction = MessageInteraction.objects.create(
            dispatch_task=task,
            button_set=InteractionButtonSet.RATING_MENU_LINK,
        )
        self.tracked_link = MessageInteractionTrackedLink.objects.create(
            interaction=interaction,
            public_token=PUBLIC_TOKEN,
            label_code=InteractionLinkLabelCode.DELIVERY,
            target_url=TARGET_URL,
        )
        self.path = f"/r/v1/{PUBLIC_TOKEN}"

    @staticmethod
    def _assert_hardened(response) -> None:
        assert response["Cache-Control"] == "no-store, private"
        assert response["Pragma"] == "no-cache"
        assert response["Referrer-Policy"] == "no-referrer"
        assert response["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    def test_every_valid_get_creates_transition_before_redirect(self):
        with self.assertNumQueries(2):
            first_response = self.client.get(self.path)
        second_response = self.client.get(self.path)

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(first_response["Location"], TARGET_URL)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(MessageInteractionLinkTransition.objects.count(), 2)
        self._assert_hardened(first_response)

    def test_head_returns_204_without_lookup_or_transition(self):
        with self.assertNumQueries(0):
            response = self.client.head(self.path)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(MessageInteractionLinkTransition.objects.count(), 0)
        self._assert_hardened(response)

    def test_invalid_token_format_returns_410_without_database_lookup(self):
        with self.assertNumQueries(0):
            response = self.client.get("/r/v1/short")

        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "Ссылка недоступна.", status_code=410)
        self._assert_hardened(response)

    def test_unknown_and_disabled_links_are_indistinguishable(self):
        unknown_response = self.client.get(f"/r/v1/{'D' * 32}")
        self.tracked_link.disabled_at = self.tracked_link.created_at
        self.tracked_link.save(update_fields=["disabled_at"])
        disabled_response = self.client.get(self.path)

        self.assertEqual(unknown_response.status_code, 410)
        self.assertEqual(disabled_response.status_code, 410)
        self.assertEqual(unknown_response.content, disabled_response.content)
        self.assertEqual(MessageInteractionLinkTransition.objects.count(), 0)

    def test_invalid_stored_target_returns_410_without_transition(self):
        MessageInteractionTrackedLink.objects.filter(pk=self.tracked_link.pk).update(
            target_url="https://user:password@example.org/"
        )

        response = self.client.get(self.path)

        self.assertEqual(response.status_code, 410)
        self.assertEqual(MessageInteractionLinkTransition.objects.count(), 0)

    @override_settings(MESSAGE_TRACKED_LINK_ALLOWED_HOSTS=set())
    def test_saved_snapshot_does_not_depend_on_current_allowed_hosts(self):
        response = self.client.get(self.path)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], TARGET_URL)
        self.assertEqual(MessageInteractionLinkTransition.objects.count(), 1)

    def test_database_read_error_returns_503_without_redirect(self):
        with patch.object(
            MessageInteractionTrackedLink.objects,
            "only",
            side_effect=DatabaseError("forced read error"),
        ):
            response = self.client.get(self.path)

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Location", response)
        self.assertEqual(MessageInteractionLinkTransition.objects.count(), 0)
        self._assert_hardened(response)

    def test_database_insert_error_returns_503_without_redirect(self):
        with patch.object(
            MessageInteractionLinkTransition.objects,
            "create",
            side_effect=DatabaseError("forced insert error"),
        ):
            response = self.client.get(self.path)

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Location", response)
        self.assertEqual(MessageInteractionLinkTransition.objects.count(), 0)

    def test_unexpected_error_returns_500_without_logging_full_token(self):
        records = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _CaptureHandler()
        handler.addFilter(TrackedLinkTokenRedactingFilter())
        request_logger = logging.getLogger("django.request")
        request_logger.addHandler(handler)
        previous_level = request_logger.level
        request_logger.setLevel(logging.ERROR)
        self.client.raise_request_exception = False
        try:
            with patch.object(
                MessageInteractionTrackedLink.objects,
                "only",
                side_effect=RuntimeError("forced unexpected redirect error"),
            ):
                response = self.client.get(self.path)
        finally:
            request_logger.removeHandler(handler)
            request_logger.setLevel(previous_level)

        rendered_records = [logging.Formatter().format(record) for record in records]
        rendered_log = "\n".join(rendered_records)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(any(record.exc_info for record in records))
        self.assertNotIn(PUBLIC_TOKEN, rendered_log)
        self.assertIn(f"link_marker={PUBLIC_TOKEN[:10]}", rendered_log)
        self.assertIn("RuntimeError", rendered_log)
        self.assertIn("forced unexpected redirect error", rendered_log)

    def test_unsupported_method_returns_hardened_405(self):
        response = self.client.post(self.path, data={"ignored": True})

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET, HEAD")
        self.assertEqual(MessageInteractionLinkTransition.objects.count(), 0)
        self._assert_hardened(response)

    def test_health_checks_database_connection(self):
        with self.assertNumQueries(1):
            response = self.client.get("/internal/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        self._assert_hardened(response)

    def test_health_returns_503_when_database_is_unavailable(self):
        with patch(
            "guests.views_tracked_links.connection.cursor",
            side_effect=DatabaseError("forced health error"),
        ):
            response = self.client.get("/internal/health")

        self.assertEqual(response.status_code, 503)
        self._assert_hardened(response)
