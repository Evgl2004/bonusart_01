# guests/services/iiko_client.py

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class IikoClient:
    """
    клиент для iiko API:
    - получить токен
    - найти гостя по телефону или customerId
    Никаких текстов для бота, только словари с данными.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        organization_id: str,
        timeout: int = 10,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.organization_id = organization_id
        self.timeout = timeout

        self._session = requests.Session()
        self._token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None

    # ------- служебное --------

    def _is_token_valid(self) -> bool:
        return bool(
            self._token and self._token_expires_at
            and datetime.now() < self._token_expires_at
        )

    def _get_token(self) -> Optional[str]:
        """Берём существующий токен или запрашиваем новый."""
        if self._is_token_valid():
            return self._token

        url = f"{self.base_url}/access_token"
        payload = {"apiLogin": self.api_key}
        headers = {"Content-Type": "application/json"}

        try:
            resp = self._session.post(
                url, json=payload, headers=headers, timeout=self.timeout
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Ошибка запроса токена iiko: %s", exc)
            return None

        data = resp.json()
        token = data.get("token")
        if not token:
            logger.error("В ответе iiko нет поля token: %r", data)
            return None

        self._token = token
        # по документации токен живёт 15 минут — оставим небольшой запас
        self._token_expires_at = datetime.now() + timedelta(minutes=14)
        return token

    def _auth_headers(self) -> Optional[Dict[str, str]]:
        token = self._get_token()
        if not token:
            return None
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Превращаем номер в формат +7XXXXXXXXXX (как у тебя в старом коде)."""
        digits = "".join(ch for ch in phone if ch.isdigit())
        if digits.startswith("7"):
            return "+" + digits
        if digits.startswith("8"):
            return "+7" + digits[1:]
        if len(digits) == 10:
            return "+7" + digits
        return "+" + digits

    # ------- публичные методы --------

    def get_customer_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        """
        Возвращает СЫРОЙ dict клиента из iiko по телефону
        или None, если не найден / ошибка.
        """
        headers = self._auth_headers()
        if not headers:
            return None

        formatted_phone = self._normalize_phone(phone)

        payload = {
            "phone": formatted_phone,
            "type": "phone",
            "organizationId": self.organization_id,
        }

        url = f"{self.base_url}/loyalty/iiko/customer/info"
        try:
            resp = self._session.post(
                url, json=payload, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            logger.error("Ошибка сети при запросе клиента по телефону: %s", exc)
            return None

        if resp.status_code == 200:
            data = resp.json()
            # В твоём старом коде ты сразу передавала data в _extract_customer_info.
            # Здесь мы возвращаем как есть, а разбор делаем уже в Django.
            return data
        elif resp.status_code in (400, 404):
            logger.info("Гость с телефоном %s в iiko не найден: %s", formatted_phone, resp.text)
            return None
        else:
            logger.error("Неожиданный код iiko %s: %s", resp.status_code, resp.text)
            return None

    def get_customer_by_id(self, customer_id: str) -> Optional[Dict[str, Any]]:
        """
        То же самое, но по customerId (iiko_id).
        """
        headers = self._auth_headers()
        if not headers:
            return None

        payload = {
            "id": customer_id,
            "type": "id",
            "organizationId": self.organization_id,
        }

        url = f"{self.base_url}/loyalty/iiko/customer/info"
        try:
            resp = self._session.post(
                url, json=payload, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            logger.error("Ошибка сети при запросе клиента по id: %s", exc)
            return None

        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code in (400, 404):
            logger.info("Гость с id %s в iiko не найден: %s", customer_id, resp.text)
            return None
        else:
            logger.error("Неожиданный код iiko %s: %s", resp.status_code, resp.text)
            return None


# Один общий экземпляр клиента, чтобы переиспользовать сессию и токен
iiko_client = IikoClient(
    api_key=settings.IIKO_API_KEY,
    base_url=settings.IIKO_API_BASE_URL,
    organization_id=settings.IIKO_ORGANIZATION_ID,
)
