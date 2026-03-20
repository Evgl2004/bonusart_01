"""
Тесты сервиса guests.services.webhook_worker.

Покрывают аварийные ветки, ретраи и корректность метрик воркера.
"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from guests.services.webhook_worker import (
    FatalMessageError,
    RetryableError,
    WebhookMessage,
    WebhookWorker,
    redis_ConnectionError,
)


class WebhookMessageTests(SimpleTestCase):
    """
    Тесты преобразования webhook_id в целое число.
    """

    def test_parse_webhook_id_supported_types(self):
        self.assertEqual(WebhookMessage._parse_webhook_id(10), 10)
        self.assertEqual(WebhookMessage._parse_webhook_id(" 42 "), 42)
        self.assertEqual(WebhookMessage._parse_webhook_id(3.9), 3)

    def test_parse_webhook_id_unsupported_or_invalid_types(self):
        self.assertIsNone(WebhookMessage._parse_webhook_id(None))
        self.assertIsNone(WebhookMessage._parse_webhook_id("not-int"))
        self.assertIsNone(WebhookMessage._parse_webhook_id({"id": 1}))


class WebhookWorkerParseMessageTests(SimpleTestCase):
    """
    Тесты парсинга сообщения из Redis.
    """

    def test_parse_message_success(self):
        message_bytes = json.dumps(
            {
                "id": "123",
                "category": "balance",
                "parsed_body": {"guest_id": "abc"},
                "retry_count": 1,
            }
        ).encode("utf-8")

        parsed = WebhookWorker._parse_message(message_bytes)
        self.assertEqual(parsed.id, "123")
        self.assertEqual(parsed.category, "balance")
        self.assertEqual(parsed.parsed_body, {"guest_id": "abc"})
        self.assertEqual(parsed.retry_count, 1)

    def test_parse_message_cp1251_fallback_preserves_cyrillic(self):
        payload = {
            "id": "124",
            "category": "balance",
            "parsed_body": {"message": "Баланс обновлён"},
            "retry_count": 0,
        }
        message_bytes = json.dumps(payload, ensure_ascii=False).encode("cp1251")

        parsed = WebhookWorker._parse_message(message_bytes)

        self.assertEqual(parsed.id, "124")
        self.assertEqual(parsed.parsed_body["message"], "Баланс обновлён")

    def test_parse_message_invalid_utf8_raises_fatal(self):
        with self.assertRaises(FatalMessageError):
            WebhookWorker._parse_message(b"\xff\xfe\xfd")

    def test_parse_message_invalid_json_raises_fatal(self):
        with self.assertRaises(FatalMessageError):
            WebhookWorker._parse_message(b"{not-json")

    def test_parse_message_missing_required_field_raises_fatal(self):
        payload = {"id": 1, "parsed_body": {}}
        with self.assertRaises(FatalMessageError):
            WebhookWorker._parse_message(json.dumps(payload).encode("utf-8"))

    def test_parse_message_parsed_body_must_be_dict(self):
        payload = {"id": 1, "category": "x", "parsed_body": ["not", "dict"]}
        with self.assertRaises(FatalMessageError):
            WebhookWorker._parse_message(json.dumps(payload).encode("utf-8"))


class WebhookWorkerServiceTests(SimpleTestCase):
    """
    Тесты бизнес-веток воркера: retry, DLQ, метрики, health-check.
    """

    @staticmethod
    def _build_worker() -> tuple[WebhookWorker, Mock]:
        """
        Создает воркер с подмененным Redis-клиентом.
        """
        fake_redis = Mock()
        fake_redis.ping.return_value = True
        fake_redis.llen.return_value = 0
        with (
            patch("guests.services.webhook_worker.redis_from_url", return_value=fake_redis),
            patch.object(WebhookWorker, "_setup_signal_handlers"),
        ):
            worker = WebhookWorker()
        return worker, fake_redis

    @staticmethod
    def _valid_message_bytes(**overrides) -> bytes:
        payload = {
            "id": 101,
            "category": "balance",
            "parsed_body": {"guest_id": "g-1"},
            "retry_count": 0,
        }
        payload.update(overrides)
        return json.dumps(payload).encode("utf-8")

    def test_process_with_retry_parse_error_updates_failed_and_dlq_metrics(self):
        worker, fake_redis = self._build_worker()

        worker._process_with_retry(b"\xff\xfe")

        self.assertEqual(worker.metrics["messages_failed"], 1)
        self.assertEqual(worker.metrics["messages_dlq"], 1)
        fake_redis.rpush.assert_called_once()

    def test_process_with_retry_max_retries_sends_to_dlq(self):
        worker, fake_redis = self._build_worker()
        worker.max_retries = 3

        worker._process_with_retry(self._valid_message_bytes(retry_count=3))

        self.assertEqual(worker.metrics["messages_failed"], 1)
        self.assertEqual(worker.metrics["messages_dlq"], 1)
        fake_redis.rpush.assert_called_once()

    def test_process_with_retry_retryable_error_increments_failed_and_requeues(self):
        worker, _ = self._build_worker()
        with (
            patch.object(worker, "_process_single_message", side_effect=RetryableError("temporary")),
            patch.object(worker, "_retry_message") as mocked_retry,
        ):
            worker._process_with_retry(self._valid_message_bytes())

        self.assertEqual(worker.metrics["messages_failed"], 1)
        self.assertEqual(worker.metrics["messages_dlq"], 0)
        mocked_retry.assert_called_once()

    def test_process_with_retry_fatal_error_increments_failed_and_sends_dlq(self):
        worker, _ = self._build_worker()
        with patch.object(worker, "_process_single_message", side_effect=FatalMessageError("bad message")):
            worker._process_with_retry(self._valid_message_bytes())

        self.assertEqual(worker.metrics["messages_failed"], 1)
        self.assertEqual(worker.metrics["messages_dlq"], 1)

    def test_process_with_retry_unexpected_error_increments_failed_and_retries(self):
        worker, _ = self._build_worker()
        with (
            patch.object(worker, "_process_single_message", side_effect=RuntimeError("boom")),
            patch.object(worker, "_retry_message") as mocked_retry,
        ):
            worker._process_with_retry(self._valid_message_bytes())

        self.assertEqual(worker.metrics["messages_failed"], 1)
        self.assertEqual(worker.metrics["messages_dlq"], 0)
        mocked_retry.assert_called_once()

    def test_send_to_dlq_does_not_increment_metric_when_redis_push_failed(self):
        worker, fake_redis = self._build_worker()
        fake_redis.rpush.side_effect = RuntimeError("redis down")

        worker._send_to_dlq(b"{}", reason="x")

        self.assertEqual(worker.metrics["messages_dlq"], 0)

    def test_check_dependencies_success_sets_flag(self):
        worker, _ = self._build_worker()
        with patch("guests.services.webhook_worker._get_sagur_access_token_cached", return_value="token"):
            is_ok = worker._check_dependencies()

        self.assertTrue(is_ok)
        self.assertTrue(worker.dependencies_checked)

    def test_check_dependencies_returns_false_when_redis_unavailable(self):
        worker, fake_redis = self._build_worker()
        fake_redis.ping.return_value = False

        self.assertFalse(worker._check_dependencies())
        self.assertFalse(worker.dependencies_checked)

    def test_process_single_message_retryable_when_status_update_failed(self):
        worker, _ = self._build_worker()
        message = WebhookMessage(id=5, category="balance", parsed_body={})

        with (
            patch("guests.services.webhook_worker.handle_api_webhook", return_value=(True, "ok")),
            patch("guests.services.webhook_worker._get_sagur_access_token_cached", return_value="token"),
            patch("guests.services.webhook_worker._update_webhook_business_status", side_effect=RuntimeError("timeout")),
        ):
            with self.assertRaises(RetryableError):
                worker._process_single_message(message)

    @override_settings(BALANCE_WEBHOOK_NOTIFY_ENABLED=False)
    def test_process_single_message_passes_balance_notify_flag_from_settings(self):
        """
        Воркер должен прокидывать флаг BALANCE_WEBHOOK_NOTIFY_ENABLED в handle_api_webhook.
        """
        worker, _ = self._build_worker()
        message = WebhookMessage(id=55, category="balance", parsed_body={})

        with (
            patch("guests.services.webhook_worker.handle_api_webhook", return_value=(True, "ok")) as mocked_handle,
            patch("guests.services.webhook_worker._get_sagur_access_token_cached", return_value="token"),
            patch("guests.services.webhook_worker._update_webhook_business_status"),
        ):
            worker._process_single_message(message)

        call_kwargs = mocked_handle.call_args.kwargs
        self.assertIn("send_balance_notification", call_kwargs)
        self.assertFalse(call_kwargs["send_balance_notification"])

    def test_process_single_message_nonretryable_error_becomes_fatal(self):
        worker, _ = self._build_worker()
        message = WebhookMessage(id=5, category="balance", parsed_body={})

        with patch(
            "guests.services.webhook_worker.handle_api_webhook",
            side_effect=RuntimeError("invalid payload format"),
        ):
            with self.assertRaises(FatalMessageError):
                worker._process_single_message(message)

    def test_process_single_message_timeout_error_becomes_retryable(self):
        worker, _ = self._build_worker()
        message = WebhookMessage(id=5, category="balance", parsed_body={})

        with patch(
            "guests.services.webhook_worker.handle_api_webhook",
            side_effect=RuntimeError("timeout while calling upstream"),
        ):
            with self.assertRaises(RetryableError):
                worker._process_single_message(message)

    def test_health_check_returns_unhealthy_when_ping_raises_connection_error(self):
        worker, fake_redis = self._build_worker()
        fake_redis.llen.side_effect = [4, 1]
        fake_redis.ping.side_effect = redis_ConnectionError("down")

        result = worker.health_check()

        self.assertEqual(result["status"], "unhealthy")
        self.assertFalse(result["redis_connected"])
        self.assertEqual(result["queue_length"], 4)
        self.assertEqual(result["dlq_length"], 1)

    def test_health_check_returns_error_when_llen_fails(self):
        worker, fake_redis = self._build_worker()
        fake_redis.llen.side_effect = RuntimeError("boom")

        result = worker.health_check()

        self.assertEqual(result["status"], "error")
        self.assertIn("boom", result["error"])

    def test_sleep_with_stop_returns_immediately_when_should_stop_set(self):
        """
        При активном stop-флаге helper-пауза не должна вызывать time.sleep.
        """
        worker, _ = self._build_worker()
        worker.should_stop = True

        with patch("guests.services.webhook_worker.time.sleep") as mocked_sleep:
            worker._sleep_with_stop(60.0)

        mocked_sleep.assert_not_called()
