from __future__ import annotations

import requests
from django.test import SimpleTestCase

from guests.services.iiko_cloud_transport import (
    IikoCloudJsonTransport,
    IikoCloudTransportError,
    normalize_iiko_cloud_api_base_url,
)


class _FakeTokenProvider:
    """Управляемый поставщик токенов для проверки повторной авторизации."""

    def __init__(self, tokens: list[str] | None = None):
        self.tokens = list(tokens or ["token-1"])
        self.token_index = 0
        self.get_calls = 0
        self.invalidated: list[str | None] = []

    def get_token(self):
        self.get_calls += 1
        return self.tokens[self.token_index]

    def invalidate_token(self, *, expected_token=None):
        self.invalidated.append(expected_token)
        if self.token_index + 1 < len(self.tokens):
            self.token_index += 1


class _FakeResponse:
    def __init__(self, *, status_code: int, body=None, text: str | None = None, headers=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else ("" if body is None else "json")
        self.headers = dict(headers or {})

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts: list[dict[str, object]] = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class IikoCloudJsonTransportTests(SimpleTestCase):
    """Регрессионные проверки общего транспорта рабочих методов iiko."""

    @staticmethod
    def _transport(*, responses, provider=None, **overrides):
        session = _FakeSession(responses)
        values = {
            "base_url": "https://iiko.example",
            "token_provider": provider or _FakeTokenProvider(),
            "session": session,
            "max_retries": 0,
            "sleeper": lambda _seconds: None,
        }
        values.update(overrides)
        return IikoCloudJsonTransport(**values), session

    def test_normalizes_root_and_existing_api_v1(self):
        self.assertEqual(
            normalize_iiko_cloud_api_base_url("https://iiko.example/"),
            "https://iiko.example/api/1",
        )
        self.assertEqual(
            normalize_iiko_cloud_api_base_url("https://iiko.example/api/1/"),
            "https://iiko.example/api/1",
        )

    def test_success_uses_api_v1_bearer_and_exact_payload(self):
        transport, session = self._transport(
            responses=[_FakeResponse(status_code=200, body={"ok": True})]
        )

        result = transport.post_json(
            path="/loyalty/iiko/customer/info",
            payload={"organizationId": "org-1"},
            retry_transient=True,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            session.posts[0]["url"],
            "https://iiko.example/api/1/loyalty/iiko/customer/info",
        )
        self.assertEqual(session.posts[0]["json"], {"organizationId": "org-1"})
        self.assertEqual(session.posts[0]["headers"]["Authorization"], "Bearer token-1")

    def test_401_refreshes_token_and_replays_exactly_once(self):
        provider = _FakeTokenProvider(["old-token", "new-token"])
        transport, session = self._transport(
            provider=provider,
            responses=[
                _FakeResponse(status_code=401, body={"correlationId": "corr-old"}),
                _FakeResponse(status_code=200, body={"ok": True}),
            ],
        )

        result = transport.post_json(path="/read", payload={}, retry_transient=True)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(provider.invalidated, ["old-token"])
        self.assertEqual(len(session.posts), 2)
        self.assertEqual(session.posts[0]["headers"]["Authorization"], "Bearer old-token")
        self.assertEqual(session.posts[1]["headers"]["Authorization"], "Bearer new-token")

    def test_second_401_is_returned_without_third_request(self):
        provider = _FakeTokenProvider(["old-token", "new-token"])
        transport, session = self._transport(
            provider=provider,
            responses=[
                _FakeResponse(status_code=401, body={}),
                _FakeResponse(status_code=401, body={"correlationId": "corr-new"}),
            ],
        )

        with self.assertRaises(IikoCloudTransportError) as error_context:
            transport.post_json(path="/read", payload={}, retry_transient=True)

        self.assertEqual(error_context.exception.status_code, 401)
        self.assertEqual(len(session.posts), 2)

    def test_read_only_network_error_is_retried_with_limit(self):
        transport, session = self._transport(
            responses=[
                requests.Timeout("private-body"),
                _FakeResponse(status_code=200, body={"ok": True}),
            ],
            max_retries=1,
        )

        result = transport.post_json(path="/read", payload={}, retry_transient=True)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(session.posts), 2)

    def test_mutating_network_error_is_not_retried(self):
        transport, session = self._transport(
            responses=[
                requests.Timeout("private-body"),
                _FakeResponse(status_code=200, body={"ok": True}),
            ],
            max_retries=2,
        )

        with self.assertRaises(IikoCloudTransportError) as error_context:
            transport.post_json(path="/write", payload={}, retry_transient=False)

        self.assertTrue(error_context.exception.retryable)
        self.assertEqual(len(session.posts), 1)
        self.assertNotIn("private-body", str(error_context.exception))

    def test_retry_after_is_used_for_read_only_429(self):
        delays: list[float] = []
        transport, session = self._transport(
            responses=[
                _FakeResponse(status_code=429, body={}, headers={"Retry-After": "3"}),
                _FakeResponse(status_code=200, body={"ok": True}),
            ],
            max_retries=1,
            retry_max_seconds=5,
            sleeper=delays.append,
        )

        transport.post_json(path="/read", payload={}, retry_transient=True)

        self.assertEqual(delays, [3.0])
        self.assertEqual(len(session.posts), 2)

    def test_structured_error_keeps_body_but_does_not_print_it(self):
        private_body = "private-response-body"
        transport, _session = self._transport(
            responses=[
                _FakeResponse(
                    status_code=400,
                    body={
                        "errorCode": "BAD_REQUEST",
                        "correlationId": "corr-1",
                        "message": private_body,
                    },
                )
            ]
        )

        with self.assertRaises(IikoCloudTransportError) as error_context:
            transport.post_json(path="/read", payload={}, retry_transient=True)

        error = error_context.exception
        self.assertEqual(error.body["message"], private_body)
        self.assertEqual(error.error_code, "BAD_REQUEST")
        self.assertEqual(error.correlation_id, "corr-1")
        self.assertNotIn(private_body, str(error))

    def test_empty_success_and_invalid_json_have_defined_behavior(self):
        empty_transport, _session = self._transport(
            responses=[_FakeResponse(status_code=200, body=None, text="")]
        )
        self.assertEqual(
            empty_transport.post_json(
                path="/write",
                payload={},
                retry_transient=False,
                allow_empty=True,
            ),
            {},
        )

        invalid_transport, _session = self._transport(
            responses=[_FakeResponse(status_code=200, body=ValueError("bad json"))]
        )
        with self.assertRaises(IikoCloudTransportError):
            invalid_transport.post_json(path="/read", payload={}, retry_transient=True)
