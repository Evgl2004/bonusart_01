from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import requests


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
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = str(path or "").strip()
        self.body = body
        self.error_code = str(error_code or "").strip()


class IikoCustomerCategoryClient:
    """
    Клиент iikoCloud API для управления категориями гостей iikoCard.

    Поддерживаемые endpoints:
    1. `/api/1/access_token`;
    2. `/api/1/loyalty/iiko/customer/info`;
    3. `/api/1/loyalty/iiko/customer_category`;
    4. `/api/1/loyalty/iiko/customer_category/add`;
    5. `/api/1/loyalty/iiko/customer_category/remove`.
    """

    API_V1_PREFIX = "/api/1"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        organization_id: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = self._normalize_base_url(base_url)
        self.organization_id = str(organization_id or "").strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds or 15.0))

        self._session = requests.Session()
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    @classmethod
    def _normalize_base_url(cls, base_url: str) -> str:
        normalized = str(base_url or "").strip().rstrip("/")
        if not normalized:
            return ""
        if normalized.endswith(cls.API_V1_PREFIX):
            return normalized
        return f"{normalized}{cls.API_V1_PREFIX}"

    def close(self) -> None:
        self._session.close()

    def _is_token_valid(self) -> bool:
        return bool(
            self._token
            and self._token_expires_at
            and datetime.utcnow() < self._token_expires_at
        )

    def _get_token(self) -> str:
        if self._is_token_valid():
            return str(self._token)
        if not self.api_key:
            raise IikoCustomerCategoryApiError("Не задан IIKO_API_KEY для iikoCard.")
        if not self.base_url:
            raise IikoCustomerCategoryApiError("Не задан IIKO_API_BASE_URL для iikoCard.")

        url = f"{self.base_url}/access_token"
        try:
            response = self._session.post(
                url,
                json={"apiLogin": self.api_key},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise IikoCustomerCategoryApiError(f"Сетевая ошибка запроса токена iikoCard: {exc}") from exc

        if response.status_code != 200:
            raise IikoCustomerCategoryApiError(
                f"Ошибка токена iikoCard: status={response.status_code}, body={response.text[:300]}"
            )

        body = response.json()
        token = str(body.get("token") or "").strip()
        if not token:
            raise IikoCustomerCategoryApiError("В ответе iikoCard отсутствует token.")

        self._token = token
        self._token_expires_at = datetime.utcnow() + timedelta(minutes=14)
        return token

    def _build_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._get_token()}",
        }

    def _post(self, *, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.organization_id:
            raise IikoCustomerCategoryApiError("Не задан IIKO_ORGANIZATION_ID для iikoCard.")

        url = f"{self.base_url}{path}"
        try:
            response = self._session.post(
                url,
                json=payload,
                headers=self._build_headers(),
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise IikoCustomerCategoryApiError(f"Сетевая ошибка iikoCard API `{path}`: {exc}") from exc

        if response.status_code != 200:
            error_body = None
            error_code = ""
            if str(response.text or "").strip():
                try:
                    error_body = response.json()
                except ValueError:
                    error_body = None
            if isinstance(error_body, dict):
                error_code = str(
                    error_body.get("errorCode") or error_body.get("code") or ""
                ).strip()
            raise IikoCustomerCategoryApiError(
                f"iikoCard API `{path}` вернул status={response.status_code}, body={response.text[:500]}",
                status_code=response.status_code,
                path=path,
                body=error_body,
                error_code=error_code,
            )
        if not str(response.text or "").strip():
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise IikoCustomerCategoryApiError(f"iikoCard API `{path}` вернул не JSON-ответ.") from exc
        return body if isinstance(body, dict) else {"result": body}

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
