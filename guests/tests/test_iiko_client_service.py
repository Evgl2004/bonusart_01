"""Регрессионные тесты клиента поиска гостя в iiko Cloud API."""

from __future__ import annotations

from unittest.mock import patch

import requests
from django.test import SimpleTestCase

from guests.services.iiko_client import IikoClient
from guests.services.iiko_cloud_auth import IikoCloudAuthError


class _FakeTokenProvider:
    def __init__(self, token: str = "token-1"):
        self.token = token
        self.invalidated: list[str | None] = []

    def get_token(self):
        return self.token

    def invalidate_token(self, *, expected_token=None):
        self.invalidated.append(expected_token)

    def close(self):
        return None


class _FakeResponse:
    def __init__(self, *, status_code=200, body=None, text="json"):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text
        self.headers = {}

    def json(self):
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

    def close(self):
        return None


class IikoClientServiceTests(SimpleTestCase):
    """Проверки публичного поведения `IikoClient` после замены авторизации."""

    @staticmethod
    def _client(*, responses, base_url="https://iiko.example", organization_id="org-1"):
        provider = _FakeTokenProvider()
        session = _FakeSession(responses)
        client = IikoClient(
            base_url=base_url,
            organization_id=organization_id,
            timeout=5,
            token_provider=provider,
            max_retries=0,
        )
        client._session = session
        return client, provider, session

    def test_normalize_phone_variants(self):
        self.assertEqual(IikoClient._normalize_phone("+7 (999) 123-45-67"), "+79991234567")
        self.assertEqual(IikoClient._normalize_phone("8 (999) 123-45-67"), "+79991234567")
        self.assertEqual(IikoClient._normalize_phone("9991234567"), "+79991234567")

    def test_phone_lookup_uses_api_v1_and_existing_response_contract(self):
        client, _provider, session = self._client(
            responses=[_FakeResponse(body={"customer": {"id": "guest-1"}})]
        )

        result = client.get_customer_by_phone("8 (999) 123-45-67")

        self.assertEqual(result, {"customer": {"id": "guest-1"}})
        self.assertEqual(
            session.posts[0]["url"],
            "https://iiko.example/api/1/loyalty/iiko/customer/info",
        )
        self.assertEqual(
            session.posts[0]["json"],
            {
                "phone": "+79991234567",
                "type": "phone",
                "organizationId": "org-1",
            },
        )

    def test_id_lookup_keeps_existing_payload(self):
        client, _provider, session = self._client(
            responses=[_FakeResponse(body={"customer": {"id": "guest-1"}})]
        )

        result = client.get_customer_by_id("guest-1")

        self.assertEqual(result, {"customer": {"id": "guest-1"}})
        self.assertEqual(
            session.posts[0]["json"],
            {"id": "guest-1", "type": "id", "organizationId": "org-1"},
        )

    def test_not_found_and_other_http_errors_return_none_without_body_in_log(self):
        for status_code in (400, 404, 500):
            with self.subTest(status_code=status_code):
                private_body = "private-response-body"
                client, _provider, _session = self._client(
                    responses=[
                        _FakeResponse(
                            status_code=status_code,
                            body={"message": private_body, "correlationId": "corr-1"},
                        )
                    ]
                )
                with self.assertLogs("guests.services.iiko_client", level="INFO") as logs:
                    result = client.get_customer_by_phone("+79990000000")

                self.assertIsNone(result)
                self.assertNotIn(private_body, "\n".join(logs.output))
                self.assertNotIn("+79990000000", "\n".join(logs.output))

    def test_network_error_returns_none(self):
        client, _provider, session = self._client(
            responses=[requests.Timeout("private-network-detail")]
        )

        with self.assertLogs("guests.services.iiko_client", level="ERROR") as logs:
            result = client.get_customer_by_phone("+79990000000")

        self.assertIsNone(result)
        self.assertEqual(len(session.posts), 1)
        self.assertNotIn("private-network-detail", "\n".join(logs.output))

    def test_missing_configuration_is_local_to_iiko_operation(self):
        client = IikoClient(
            base_url="https://iiko.example/api/1",
            organization_id="org-1",
            max_retries=0,
        )

        with patch(
            "guests.services.iiko_client.build_iiko_cloud_token_provider_from_settings",
            side_effect=IikoCloudAuthError("Не задан корректный IIKO_AUTH_MODE."),
        ):
            with self.assertLogs("guests.services.iiko_client", level="ERROR"):
                result = client.get_customer_by_phone("+79990000000")

        self.assertIsNone(result)

    def test_missing_organization_returns_none_without_network(self):
        client, _provider, session = self._client(
            responses=[],
            organization_id="",
        )

        with self.assertLogs("guests.services.iiko_client", level="ERROR"):
            result = client.get_customer_by_phone("+79990000000")

        self.assertIsNone(result)
        self.assertEqual(session.posts, [])
