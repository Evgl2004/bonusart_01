"""
Тесты сервиса iiko_client.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import requests
from django.conf import settings
from django.test import SimpleTestCase

# Модуль iiko_client создаёт глобальный singleton при импорте,
# поэтому задаём безопасные значения заранее для тестового окружения.
if not getattr(settings, "IIKO_API_KEY", None):
    settings.IIKO_API_KEY = "test-api-key"
if not getattr(settings, "IIKO_API_BASE_URL", None):
    settings.IIKO_API_BASE_URL = "https://iiko.example/api"
if not getattr(settings, "IIKO_ORGANIZATION_ID", None):
    settings.IIKO_ORGANIZATION_ID = "test-org-id"

from guests.services.iiko_client import IikoClient


class IikoClientServiceTests(SimpleTestCase):
    """
    Покрытие ключевых веток IikoClient.
    """

    @staticmethod
    def _response(*, status_code=200, json_data=None, text="ok", raise_exc=None):
        response = Mock()
        response.status_code = status_code
        response.text = text
        response.json.return_value = json_data if json_data is not None else {}
        if raise_exc is None:
            response.raise_for_status.return_value = None
        else:
            response.raise_for_status.side_effect = raise_exc
        return response

    def _client(self) -> IikoClient:
        return IikoClient(
            api_key="api-key",
            base_url="https://iiko.example/api/",
            organization_id="org-1",
            timeout=5,
        )

    def test_normalize_phone_variants(self):
        """
        _normalize_phone должен корректно приводить телефон к единому формату.
        """
        client = self._client()
        self.assertEqual(client._normalize_phone("+7 (999) 123-45-67"), "+79991234567")
        self.assertEqual(client._normalize_phone("8 (999) 123-45-67"), "+79991234567")
        self.assertEqual(client._normalize_phone("9991234567"), "+79991234567")

    def test_get_token_uses_cached_token_when_valid(self):
        """
        При валидном кэше _get_token должен вернуть токен без сетевого запроса.
        """
        client = self._client()
        client._token = "cached-token"
        fixed_now = datetime(2026, 3, 18, 12, 0, 0)
        client._token_expires_at = fixed_now + timedelta(minutes=1)

        with patch("guests.services.iiko_client.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = fixed_now
            token = client._get_token()

        self.assertEqual(token, "cached-token")

    def test_get_token_returns_none_on_request_error_or_missing_token(self):
        """
        _get_token должен возвращать None при сетевой ошибке и при ответе без token.
        """
        client = self._client()
        client._session.post = Mock(side_effect=requests.RequestException("network down"))
        self.assertIsNone(client._get_token())

        client = self._client()
        client._session.post = Mock(return_value=self._response(json_data={"access": "wrong-field"}))
        self.assertIsNone(client._get_token())

    def test_get_token_fetches_and_caches_new_token(self):
        """
        Успешный _get_token должен сохранить token и срок его жизни.
        """
        client = self._client()
        client._session.post = Mock(return_value=self._response(json_data={"token": "fresh-token"}))

        token = client._get_token()

        self.assertEqual(token, "fresh-token")
        self.assertEqual(client._token, "fresh-token")
        self.assertIsNotNone(client._token_expires_at)

    def test_auth_headers_returns_none_when_token_unavailable(self):
        """
        _auth_headers должен вернуть None, если получить токен не удалось.
        """
        client = self._client()
        with patch.object(client, "_get_token", return_value=None):
            headers = client._auth_headers()
        self.assertIsNone(headers)

    def test_get_customer_by_phone_branches(self):
        """
        get_customer_by_phone: success/not-found/error/request-exception.
        """
        client = self._client()

        with patch.object(client, "_auth_headers", return_value=None):
            self.assertIsNone(client.get_customer_by_phone("+79990000000"))

        with patch.object(client, "_auth_headers", return_value={"Authorization": "Bearer t"}):
            client._session.post = Mock(return_value=self._response(status_code=200, json_data={"customer": {"id": "1"}}))
            data = client.get_customer_by_phone("+79990000000")
            self.assertEqual(data, {"customer": {"id": "1"}})

        with patch.object(client, "_auth_headers", return_value={"Authorization": "Bearer t"}):
            client._session.post = Mock(return_value=self._response(status_code=404, text="not found"))
            self.assertIsNone(client.get_customer_by_phone("+79990000000"))

        with patch.object(client, "_auth_headers", return_value={"Authorization": "Bearer t"}):
            client._session.post = Mock(return_value=self._response(status_code=500, text="server error"))
            self.assertIsNone(client.get_customer_by_phone("+79990000000"))

        with patch.object(client, "_auth_headers", return_value={"Authorization": "Bearer t"}):
            client._session.post = Mock(side_effect=requests.RequestException("timeout"))
            self.assertIsNone(client.get_customer_by_phone("+79990000000"))

    def test_get_customer_by_id_branches(self):
        """
        get_customer_by_id: success/not-found/error/request-exception.
        """
        client = self._client()

        with patch.object(client, "_auth_headers", return_value=None):
            self.assertIsNone(client.get_customer_by_id("guest-id"))

        with patch.object(client, "_auth_headers", return_value={"Authorization": "Bearer t"}):
            client._session.post = Mock(return_value=self._response(status_code=200, json_data={"customer": {"id": "guest-id"}}))
            data = client.get_customer_by_id("guest-id")
            self.assertEqual(data, {"customer": {"id": "guest-id"}})

        with patch.object(client, "_auth_headers", return_value={"Authorization": "Bearer t"}):
            client._session.post = Mock(return_value=self._response(status_code=400, text="bad request"))
            self.assertIsNone(client.get_customer_by_id("guest-id"))

        with patch.object(client, "_auth_headers", return_value={"Authorization": "Bearer t"}):
            client._session.post = Mock(return_value=self._response(status_code=503, text="server error"))
            self.assertIsNone(client.get_customer_by_id("guest-id"))

        with patch.object(client, "_auth_headers", return_value={"Authorization": "Bearer t"}):
            client._session.post = Mock(side_effect=requests.RequestException("network"))
            self.assertIsNone(client.get_customer_by_id("guest-id"))
