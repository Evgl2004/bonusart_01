from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor

import requests
from django.test import SimpleTestCase, override_settings

from guests.services.iiko_cloud_auth import (
    IikoCloudAuthConfig,
    IikoCloudAuthError,
    IikoCloudTokenProvider,
    build_iiko_cloud_token_provider_from_settings,
)


def _jwt(*, expires_at: int) -> str:
    """Создаёт неподписанный тестовый JWT; криптографическая проверка здесь не нужна."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    payload_raw = json.dumps({"exp": expires_at}, separators=(",", ":")).encode("utf-8")
    payload = base64.urlsafe_b64encode(payload_raw).decode("ascii").rstrip("=")
    return f"{header}.{payload}.signature"


class _FakeResponse:
    def __init__(self, *, status_code: int, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = dict(headers or {})

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts: list[dict[str, object]] = []
        self.closed = False

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class IikoCloudTokenProviderTests(SimpleTestCase):
    """Проверки временного двухрежимного поставщика токена iiko Cloud API."""

    @staticmethod
    def _v2_config(**overrides) -> IikoCloudAuthConfig:
        values = {
            "mode": "v2",
            "app_id": "app-secret-value",
            "client_secret": "client-secret-value",
            "api_key": "api-secret-value",
            "v2_auth_url": "https://iiko.example/api/v2/access_token",
            "max_retries": 0,
            "token_refresh_margin_seconds": 60,
        }
        values.update(overrides)
        return IikoCloudAuthConfig(**values)

    def test_v2_uses_exact_url_and_payload_and_caches_by_exp(self):
        now = 1_800_000_000.0
        token = _jwt(expires_at=int(now + 3600))
        session = _FakeSession(
            [_FakeResponse(status_code=200, body={"token": token, "correlationId": "corr-1"})]
        )
        provider = IikoCloudTokenProvider(
            config=self._v2_config(),
            session=session,
            clock=lambda: now,
            sleeper=lambda _seconds: None,
        )

        self.assertEqual(provider.get_token(), token)
        self.assertEqual(provider.get_token(), token)

        self.assertEqual(len(session.posts), 1)
        self.assertEqual(session.posts[0]["url"], "https://iiko.example/api/v2/access_token")
        self.assertEqual(
            session.posts[0]["json"],
            {
                "appId": "app-secret-value",
                "clientSecret": "client-secret-value",
                "apiKey": "api-secret-value",
            },
        )
        self.assertEqual(session.posts[0]["timeout"], (5.0, 15.0))
        self.assertEqual(provider.last_correlation_id, "corr-1")

    def test_legacy_uses_only_api_login_and_legacy_url(self):
        session = _FakeSession(
            [_FakeResponse(status_code=200, body={"token": "legacy-token", "correlationId": "corr-old"})]
        )
        config = IikoCloudAuthConfig(
            mode="legacy",
            legacy_api_login="legacy-secret",
            app_id="unused-app",
            client_secret="unused-client-secret",
            api_key="unused-api-key",
            legacy_auth_url="https://iiko.example/api/1/access_token",
            max_retries=0,
        )
        provider = IikoCloudTokenProvider(config=config, session=session, clock=lambda: 1000.0)

        self.assertEqual(provider.get_token(), "legacy-token")
        self.assertEqual(session.posts[0]["url"], "https://iiko.example/api/1/access_token")
        self.assertEqual(session.posts[0]["json"], {"apiLogin": "legacy-secret"})

    def test_invalid_or_incomplete_mode_fails_without_network_request(self):
        for config in (
            IikoCloudAuthConfig(mode=""),
            IikoCloudAuthConfig(mode="unknown"),
            IikoCloudAuthConfig(mode="legacy"),
            IikoCloudAuthConfig(mode="v2", app_id="app", client_secret="secret"),
        ):
            with self.subTest(mode=config.mode):
                with self.assertRaises(IikoCloudAuthError):
                    IikoCloudTokenProvider(config=config, session=_FakeSession([]))

    def test_v2_error_never_calls_legacy_url(self):
        session = _FakeSession(
            [_FakeResponse(status_code=401, body={"correlationId": "corr-denied"})]
        )
        provider = IikoCloudTokenProvider(config=self._v2_config(), session=session)

        with self.assertRaises(IikoCloudAuthError) as error_context:
            provider.get_token()

        self.assertEqual(error_context.exception.status_code, 401)
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(session.posts[0]["url"], "https://iiko.example/api/v2/access_token")

    def test_retry_after_is_honored_for_429(self):
        now = 1_800_000_000.0
        delays: list[float] = []
        session = _FakeSession(
            [
                _FakeResponse(
                    status_code=429,
                    body={"correlationId": "corr-rate"},
                    headers={"Retry-After": "2"},
                ),
                _FakeResponse(
                    status_code=200,
                    body={"token": _jwt(expires_at=int(now + 3600)), "correlationId": "corr-ok"},
                ),
            ]
        )
        provider = IikoCloudTokenProvider(
            config=self._v2_config(max_retries=1, retry_max_seconds=5),
            session=session,
            clock=lambda: now,
            sleeper=delays.append,
        )

        provider.get_token()

        self.assertEqual(delays, [2.0])
        self.assertEqual(len(session.posts), 2)

    def test_network_retries_are_limited_and_error_is_safe(self):
        secret = "do-not-log-this-secret"
        session = _FakeSession(
            [requests.Timeout(secret), requests.ConnectionError(secret)]
        )
        provider = IikoCloudTokenProvider(
            config=self._v2_config(client_secret=secret, max_retries=1),
            session=session,
            sleeper=lambda _seconds: None,
        )

        with self.assertRaises(IikoCloudAuthError) as error_context:
            provider.get_token()

        self.assertEqual(len(session.posts), 2)
        self.assertTrue(error_context.exception.retryable)
        self.assertNotIn(secret, str(error_context.exception))
        self.assertNotIn(secret, repr(provider.config))

    def test_invalid_json_missing_token_and_invalid_jwt_are_rejected(self):
        now = 1_800_000_000.0
        responses = (
            _FakeResponse(status_code=200, body=ValueError("bad json")),
            _FakeResponse(status_code=200, body={"correlationId": "corr-missing"}),
            _FakeResponse(status_code=200, body={"token": "not-a-jwt"}),
            _FakeResponse(status_code=200, body={"token": "header.%.signature"}),
            _FakeResponse(status_code=200, body={"token": _jwt(expires_at=int(now - 1))}),
        )
        for response in responses:
            with self.subTest(body=response._body):
                provider = IikoCloudTokenProvider(
                    config=self._v2_config(),
                    session=_FakeSession([response]),
                    clock=lambda: now,
                )
                with self.assertRaises(IikoCloudAuthError):
                    provider.get_token()

    def test_concurrent_get_token_performs_single_request(self):
        now = 1_800_000_000.0
        token = _jwt(expires_at=int(now + 3600))
        session = _FakeSession([_FakeResponse(status_code=200, body={"token": token})])
        provider = IikoCloudTokenProvider(
            config=self._v2_config(),
            session=session,
            clock=lambda: now,
        )

        with ThreadPoolExecutor(max_workers=8) as executor:
            tokens = list(executor.map(lambda _index: provider.get_token(), range(16)))

        self.assertEqual(tokens, [token] * 16)
        self.assertEqual(len(session.posts), 1)

    def test_invalidate_does_not_remove_token_refreshed_by_another_request(self):
        now = 1_800_000_000.0
        first_token = _jwt(expires_at=int(now + 3600))
        second_token = _jwt(expires_at=int(now + 7200))
        session = _FakeSession(
            [
                _FakeResponse(status_code=200, body={"token": first_token}),
                _FakeResponse(status_code=200, body={"token": second_token}),
            ]
        )
        provider = IikoCloudTokenProvider(
            config=self._v2_config(),
            session=session,
            clock=lambda: now,
        )

        self.assertEqual(provider.get_token(), first_token)
        self.assertEqual(provider.get_token(force_refresh=True), second_token)
        provider.invalidate_token(expected_token=first_token)

        self.assertEqual(provider.get_token(), second_token)
        self.assertEqual(len(session.posts), 2)

    @override_settings(
        IIKO_AUTH_MODE="v2",
        IIKO_APP_ID="app-from-settings",
        IIKO_CLIENT_SECRET="secret-from-settings",
        IIKO_API_KEY="key-from-settings",
        IIKO_AUTH_URL="https://iiko.example/api/v2/access_token",
        IIKO_AUTH_MAX_RETRIES=0,
    )
    def test_builder_reads_only_selected_settings_at_call_time(self):
        now = 1_800_000_000.0
        session = _FakeSession(
            [
                _FakeResponse(
                    status_code=200,
                    body={"token": _jwt(expires_at=int(now + 3600))},
                )
            ]
        )

        provider = build_iiko_cloud_token_provider_from_settings(
            session=session,
            clock=lambda: now,
        )
        provider.get_token()

        self.assertEqual(provider.mode, "v2")
        self.assertEqual(session.posts[0]["json"]["appId"], "app-from-settings")
