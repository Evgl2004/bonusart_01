"""Проверки глобального минутного счётчика входящих пакетов."""

from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from redis import RedisError

from guests.services.message_interaction_rate_limit import (
    RATE_LIMIT_KEY_PREFIX,
    RATE_LIMIT_TTL_SECONDS,
    REDIS_CONNECT_TIMEOUT_SECONDS,
    REDIS_SOCKET_TIMEOUT_SECONDS,
    MessageInteractionRateLimitUnavailable,
    _build_rate_limit_redis_client,
    check_message_interaction_rate_limit_redis,
    increment_message_interaction_rate_limit,
)


class MessageInteractionRateLimitTests(SimpleTestCase):
    """Проверяет атомарность, общее состояние и безопасные отказы Redis."""

    def test_increment_uses_atomic_script_and_expiring_namespaced_key(self):
        redis_client = Mock()
        redis_client.eval.return_value = 1

        with patch(
            "guests.services.message_interaction_rate_limit."
            "get_message_interaction_rate_limit_redis_client",
            return_value=redis_client,
        ):
            current = increment_message_interaction_rate_limit(minute_bucket=123456)

        self.assertEqual(current, 1)
        script, key_count, key, ttl = redis_client.eval.call_args.args
        self.assertIn("redis.call('INCR', KEYS[1])", script)
        self.assertIn("redis.call('EXPIRE', KEYS[1]", script)
        self.assertEqual(key_count, 1)
        self.assertEqual(key, f"{RATE_LIMIT_KEY_PREFIX}:123456")
        self.assertEqual(ttl, RATE_LIMIT_TTL_SECONDS)

    def test_independent_calls_receive_one_shared_counter_value(self):
        redis_client = Mock()
        redis_client.eval.side_effect = [1, 2]

        with patch(
            "guests.services.message_interaction_rate_limit."
            "get_message_interaction_rate_limit_redis_client",
            return_value=redis_client,
        ):
            first = increment_message_interaction_rate_limit(minute_bucket=77)
            second = increment_message_interaction_rate_limit(minute_bucket=77)

        self.assertEqual((first, second), (1, 2))
        self.assertEqual(redis_client.eval.call_count, 2)
        first_key = redis_client.eval.call_args_list[0].args[2]
        second_key = redis_client.eval.call_args_list[1].args[2]
        self.assertEqual(first_key, second_key)

    def test_redis_failure_is_wrapped_without_connection_details(self):
        redis_client = Mock()
        redis_client.eval.side_effect = RedisError("redis://user:secret@internal")

        with (
            patch(
                "guests.services.message_interaction_rate_limit."
                "get_message_interaction_rate_limit_redis_client",
                return_value=redis_client,
            ),
            self.assertRaises(MessageInteractionRateLimitUnavailable) as captured,
        ):
            increment_message_interaction_rate_limit(minute_bucket=88)

        self.assertNotIn("secret", str(captured.exception))
        self.assertNotIn("internal", str(captured.exception))

    def test_readiness_ping_requires_positive_response(self):
        redis_client = Mock()
        redis_client.ping.return_value = False

        with (
            patch(
                "guests.services.message_interaction_rate_limit."
                "get_message_interaction_rate_limit_redis_client",
                return_value=redis_client,
            ),
            self.assertRaises(MessageInteractionRateLimitUnavailable),
        ):
            check_message_interaction_rate_limit_redis()

    @override_settings(
        UNIVERSAL_QUEUE_REDIS_URL="",
        REDIS_QUEUE_URL="redis://fallback:6379/1",
    )
    def test_existing_webhook_redis_is_used_as_fallback(self):
        with patch(
            "guests.services.message_interaction_rate_limit._build_rate_limit_redis_client",
            return_value=Mock(),
        ) as build_client:
            from guests.services.message_interaction_rate_limit import (
                get_message_interaction_rate_limit_redis_client,
            )

            get_message_interaction_rate_limit_redis_client()

        build_client.assert_called_once_with("redis://fallback:6379/1")

    def test_client_uses_bounded_network_timeouts_without_hidden_retry(self):
        """Отказ Redis не должен надолго удерживать входящий HTTP-запрос."""

        _build_rate_limit_redis_client.cache_clear()
        self.addCleanup(_build_rate_limit_redis_client.cache_clear)
        with patch(
            "guests.services.message_interaction_rate_limit.redis_from_url",
            return_value=Mock(),
        ) as from_url:
            _build_rate_limit_redis_client("redis://redis:6379/1")

        from_url.assert_called_once_with(
            "redis://redis:6379/1",
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            retry_on_timeout=False,
        )
