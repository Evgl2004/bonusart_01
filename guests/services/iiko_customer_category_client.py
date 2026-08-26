from __future__ import annotations

from typing import Any

import requests
from django.conf import settings

from guests.services.iiko_cloud_auth import IikoCloudAuthError, IikoCloudTokenProvider
from guests.services.iiko_cloud_transport import (
    IikoCloudJsonTransport,
    IikoCloudTransportError,
    normalize_iiko_cloud_api_base_url,
)


class IikoCustomerCategoryApiError(Exception):
    """Ошибка работы с API категорий гостей iikoCard."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        path: str = "",
        body: Any | None = None,
        error_code: str = "",
        correlation_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = str(path or "").strip()
        self.body = body
        self.error_code = str(error_code or "").strip()
        self.correlation_id = str(correlation_id or "").strip()


class IikoCustomerCategoryClient:
    """Клиент методов категорий гостей iiko Cloud API версии 1."""

    _MUTATING_PATHS = frozenset(
        {
            "/loyalty/iiko/customer_category/add",
            "/loyalty/iiko/customer_category/remove",
        }
    )

    def __init__(
        self,
        *,
        base_url: str,
        organization_id: str,
        token_provider: IikoCloudTokenProvider,
        timeout_seconds: float = 15.0,
        close_token_provider: bool = False,
        max_retries: int | None = None,
    ) -> None:
        self.base_url = normalize_iiko_cloud_api_base_url(base_url)
        self.organization_id = str(organization_id or "").strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds or 15.0))
        self._token_provider = token_provider
        self._close_token_provider = bool(close_token_provider)
        self._session = requests.Session()
        self._transport = IikoCloudJsonTransport(
            base_url=self.base_url,
            token_provider=token_provider,
            session=self._session,
            connect_timeout_seconds=min(5.0, self.timeout_seconds),
            read_timeout_seconds=self.timeout_seconds,
            max_retries=(
                int(getattr(settings, "IIKO_API_MAX_RETRIES", 2))
                if max_retries is None
                else int(max_retries)
            ),
            retry_base_seconds=float(getattr(settings, "IIKO_API_RETRY_BASE_SECONDS", 0.5)),
            retry_max_seconds=float(getattr(settings, "IIKO_API_RETRY_MAX_SECONDS", 5.0)),
        )

    def close(self) -> None:
        """Закрывает клиент и при необходимости принадлежащий ему поставщик токена."""
        self._session.close()
        if self._close_token_provider:
            self._token_provider.close()

    def _post(self, *, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.organization_id:
            raise IikoCustomerCategoryApiError("Не задан IIKO_ORGANIZATION_ID для iikoCard.")

        try:
            return self._transport.post_json(
                path=path,
                payload=payload,
                retry_transient=path not in self._MUTATING_PATHS,
                allow_empty=True,
            )
        except IikoCloudAuthError as exc:
            raise IikoCustomerCategoryApiError(
                f"Ошибка авторизации iikoCard: {exc}",
                status_code=exc.status_code,
                correlation_id=exc.correlation_id,
            ) from exc
        except IikoCloudTransportError as exc:
            raise IikoCustomerCategoryApiError(
                str(exc),
                status_code=exc.status_code,
                path=exc.path,
                body=exc.body,
                error_code=exc.error_code,
                correlation_id=exc.correlation_id,
            ) from exc

    @staticmethod
    def normalize_phone_ru(raw_value: str | None) -> str:
        """
        Нормализует телефон для поиска гостя в iikoCard.
        """
        digits = "".join(ch for ch in str(raw_value or "") if ch.isdigit())
        if not digits:
            return ""
        if len(digits) == 10:
            return f"+7{digits}"
        if len(digits) == 11 and digits.startswith("8"):
            return f"+7{digits[1:]}"
        if len(digits) == 11 and digits.startswith("7"):
            return f"+{digits}"
        return f"+{digits}"

    def get_customer_by_phone(self, *, phone: str) -> dict[str, Any] | None:
        formatted_phone = self.normalize_phone_ru(phone)
        if not formatted_phone:
            return None
        payload = {
            "phone": formatted_phone,
            "type": "phone",
            "organizationId": self.organization_id,
        }
        return self._post(path="/loyalty/iiko/customer/info", payload=payload)

    def get_customer_by_id(self, *, customer_id: str) -> dict[str, Any] | None:
        safe_customer_id = str(customer_id or "").strip()
        if not safe_customer_id:
            return None
        payload = {
            "id": safe_customer_id,
            "type": "id",
            "organizationId": self.organization_id,
        }
        return self._post(path="/loyalty/iiko/customer/info", payload=payload)

    def get_customer_categories(self) -> list[dict[str, Any]]:
        body = self._post(
            path="/loyalty/iiko/customer_category",
            payload={"organizationId": self.organization_id},
        )
        rows = body.get("guestCategories") or body.get("customerCategories") or body.get("categories") or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []

    def add_customer_category(self, *, customer_id: str, category_id: str) -> dict[str, Any]:
        return self._post(
            path="/loyalty/iiko/customer_category/add",
            payload={
                "organizationId": self.organization_id,
                "customerId": str(customer_id or "").strip(),
                "categoryId": str(category_id or "").strip(),
            },
        )

    def remove_customer_category(self, *, customer_id: str, category_id: str) -> dict[str, Any]:
        return self._post(
            path="/loyalty/iiko/customer_category/remove",
            payload={
                "organizationId": self.organization_id,
                "customerId": str(customer_id or "").strip(),
                "categoryId": str(category_id or "").strip(),
            },
        )
