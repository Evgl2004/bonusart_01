"""Проверки защиты секретов в журналах приложения."""

import logging
import sys

from django.conf import settings
from django.test import SimpleTestCase

from guests.logging_filters import (
    TelegramBotTokenRedactingFilter,
    TrackedLinkTokenRedactingFilter,
    redact_telegram_bot_tokens,
    redact_tracked_link_tokens,
)


class TelegramLoggingSecurityTests(SimpleTestCase):
    """Не допускает возврат полного токена Telegram в журналы."""

    def test_redacts_token_from_formatted_httpx_message(self):
        secret = "123456789:AA_TEST_SECRET"
        record = logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='HTTP Request: POST %s "HTTP/1.1 200 OK"',
            args=(f"https://api.telegram.org/bot{secret}/sendMessage",),
            exc_info=None,
        )

        allowed = TelegramBotTokenRedactingFilter().filter(record)
        rendered = record.getMessage()

        self.assertTrue(allowed)
        self.assertNotIn(secret, rendered)
        self.assertIn("/bot[СКРЫТО]/sendMessage", rendered)

    def test_redaction_does_not_change_non_telegram_message(self):
        message = "HTTP Request: POST https://api.vk.com/method/messages.send"

        self.assertEqual(redact_telegram_bot_tokens(message), message)

    def test_http_client_loggers_do_not_emit_information_messages(self):
        for logger_name in ("httpx", "httpcore"):
            with self.subTest(logger_name=logger_name):
                logger_settings = settings.LOGGING["loggers"][logger_name]
                self.assertEqual(logger_settings["level"], "WARNING")
                self.assertFalse(logger_settings["propagate"])

    def test_console_handler_uses_secret_redaction_filter(self):
        handler_settings = settings.LOGGING["handlers"]["console"]
        filter_settings = settings.LOGGING["filters"]["redact_telegram_bot_tokens"]

        self.assertIn("redact_telegram_bot_tokens", handler_settings["filters"])
        self.assertEqual(
            filter_settings["()"],
            "guests.logging_filters.TelegramBotTokenRedactingFilter",
        )


class TrackedLinkLoggingSecurityTests(SimpleTestCase):
    """Не допускает полный публичный токен в журнал службы переходов."""

    def test_redacts_exact_token_and_keeps_ten_character_marker(self):
        token = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        message = f"Internal Server Error: /r/v1/{token}"

        redacted = redact_tracked_link_tokens(message)

        self.assertNotIn(token, redacted)
        self.assertIn("link_marker=AbCdEfGhIj", redacted)
        self.assertIn("/r/v1/[СКРЫТО]", redacted)

    def test_does_not_change_short_or_long_token_like_segments(self):
        values = (
            "/r/v1/short",
            "/r/v1/AbCdEfGhIjKlMnOpQrStUvWxYz0123456",
            "/reports/r/v1/not-a-public-token",
        )

        for value in values:
            with self.subTest(value=value):
                self.assertEqual(redact_tracked_link_tokens(value), value)

    def test_filter_preserves_exception_information(self):
        token = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        try:
            raise RuntimeError("forced unexpected error")
        except RuntimeError:
            exception_info = sys.exc_info()

        record = logging.LogRecord(
            name="django.request",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Internal Server Error: %s",
            args=(f"/r/v1/{token}",),
            exc_info=exception_info,
        )

        allowed = TrackedLinkTokenRedactingFilter().filter(record)
        rendered = logging.Formatter().format(record)

        self.assertTrue(allowed)
        self.assertNotIn(token, rendered)
        self.assertIn("link_marker=AbCdEfGhIj", rendered)
        self.assertIn("RuntimeError", rendered)
        self.assertIn("forced unexpected error", rendered)
