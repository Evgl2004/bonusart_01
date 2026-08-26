# guests/services/iiko_client.py

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests
from django.conf import settings

from guests.services.iiko_cloud_auth import (
    IikoCloudAuthError,
    IikoCloudTokenProvider,
    build_iiko_cloud_token_provider_from_settings,
)
from guests.services.iiko_cloud_transport import (
    IikoCloudJsonTransport,
    IikoCloudTransportError,
    normalize_iiko_cloud_api_base_url,
)

logger = logging.getLogger(__name__)


class IikoClient:
    """
    Клиент поиска гостя в iiko Cloud API.

    Авторизация создаётся лениво при первом вызове, поэтому неполная настройка
    iiko не останавливает запуск остальных частей Django-приложения.
    """

    def __init__(
        self,
        *,
        base_url: str,
        organization_id: str,
        timeout: float = 10.0,
        token_provider: IikoCloudTokenProvider | None = None,
        close_token_provider: bool | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.base_url = normalize_iiko_cloud_api_base_url(base_url)
        self.organization_id = str(organization_id or "").strip()
        self.timeout = max(0.1, float(timeout or 10.0))
        self._session = requests.Session()
        self._token_provider = token_provider
        self._close_token_provider = (
            token_provider is None if close_token_provider is None else bool(close_token_provider)
        )
        self._max_retries = max_retries
        self._transport: IikoCloudJsonTransport | None = None

    def close(self) -> None:
        """Закрывает HTTP-сессии клиента и принадлежащего ему поставщика токена."""
        self._session.close()
        if self._close_token_provider and self._token_provider is not None:
            self._token_provider.close()

    def _get_token_provider(self) -> IikoCloudTokenProvider:
        if self._token_provider is None:
            self._token_provider = build_iiko_cloud_token_provider_from_settings()
        return self._token_provider

    def _get_transport(self) -> IikoCloudJsonTransport:
        if self._transport is None:
            self._transport = IikoCloudJsonTransport(
                base_url=self.base_url,
                token_provider=self._get_token_provider(),
                session=self._session,
                connect_timeout_seconds=min(5.0, self.timeout),
                read_timeout_seconds=self.timeout,
                max_retries=(
                    int(getattr(settings, "IIKO_API_MAX_RETRIES", 2))
                    if self._max_retries is None
                    else int(self._max_retries)
                ),
                retry_base_seconds=float(getattr(settings, "IIKO_API_RETRY_BASE_SECONDS", 0.5)),
                retry_max_seconds=float(getattr(settings, "IIKO_API_RETRY_MAX_SECONDS", 5.0)),
            )
        return self._transport

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Приводит телефон к формату `+7XXXXXXXXXX`."""
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if digits.startswith("7"):
            return "+" + digits
        if digits.startswith("8"):
            return "+7" + digits[1:]
        if len(digits) == 10:
            return "+7" + digits
        return "+" + digits

    def get_customer_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        """Возвращает исходный ответ iiko для гостя по телефону либо `None`."""
        formatted_phone = self._normalize_phone(phone)
        return self._get_customer(payload={
            "phone": formatted_phone,
            "type": "phone",
            "organizationId": self.organization_id,
        })

    def get_customer_by_id(self, customer_id: str) -> Optional[Dict[str, Any]]:
        """Возвращает исходный ответ iiko для гостя по идентификатору либо `None`."""
        return self._get_customer(payload={
            "id": str(customer_id or "").strip(),
            "type": "id",
            "organizationId": self.organization_id,
        })

    def _get_customer(self, *, payload: dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.organization_id:
            logger.error("Не задан IIKO_ORGANIZATION_ID для поиска гостя в iiko Cloud API.")
            return None
        try:
            return self._get_transport().post_json(
                path="/loyalty/iiko/customer/info",
                payload=payload,
                retry_transient=True,
            )
        except IikoCloudAuthError as exc:
            logger.error("Ошибка авторизации при поиске гостя в iiko Cloud API: %s", exc)
            return None
        except IikoCloudTransportError as exc:
            if exc.status_code in {400, 404}:
                logger.info(
                    "Гость не найден в iiko Cloud API: статус=%s correlation_id=%s",
                    exc.status_code,
                    exc.correlation_id or "-",
                )
            else:
                logger.error("Ошибка поиска гостя в iiko Cloud API: %s", exc)
            return None


# Один общий экземпляр клиента, чтобы переиспользовать сессию и токен
iiko_client = IikoClient(
    base_url=getattr(settings, "IIKO_API_BASE_URL", ""),
    organization_id=getattr(settings, "IIKO_ORGANIZATION_ID", ""),
)
