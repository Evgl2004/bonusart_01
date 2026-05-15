from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import requests

logger = logging.getLogger(__name__)


class IikoCouponApiError(Exception):
    """Ошибка работы с API купонов iiko."""


class IikoCouponClient:
    """
    Клиент API купонов iiko.

    Поддерживаемые endpoints:
    1. `/api/1/access_token`;
    2. `/api/1/loyalty/iiko/coupons/series`;
    3. `/api/1/loyalty/iiko/coupons/by_series`;
    4. `/api/1/loyalty/iiko/coupons/info`.
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
        self.timeout_seconds = float(timeout_seconds)

        self._session = requests.Session()
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    @classmethod
    def _normalize_base_url(cls, base_url: str) -> str:
        """
        Приводит URL iiko к версии API v1.

        В настройках удобнее хранить официальный корневой адрес iiko, но
        endpoints купонов живут под `/api/1`. Если оператор уже указал URL с
        `/api/1`, повторно суффикс не добавляем.
        """
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

        url = f"{self.base_url}/access_token"
        payload = {"apiLogin": self.api_key}
        headers = {"Content-Type": "application/json"}
        try:
            response = self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise IikoCouponApiError(f"Сетевая ошибка запроса токена iiko: {exc}") from exc

        if response.status_code != 200:
            raise IikoCouponApiError(
                f"Ошибка токена iiko: status={response.status_code}, body={response.text[:300]}"
            )

        body = response.json()
        token = str(body.get("token") or "").strip()
        if not token:
            raise IikoCouponApiError("В ответе iiko отсутствует токен.")

        self._token = token
        # Страховка: обновляем немного раньше официального TTL.
        self._token_expires_at = datetime.utcnow() + timedelta(minutes=14)
        return token

    def _build_headers(self) -> dict[str, str]:
        token = self._get_token()
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _post(self, *, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._build_headers()
        try:
            response = self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise IikoCouponApiError(f"Сетевая ошибка iiko API `{path}`: {exc}") from exc

        if response.status_code != 200:
            raise IikoCouponApiError(
                f"iiko API `{path}` вернул status={response.status_code}, body={response.text[:500]}"
            )
        return response.json()

    def get_coupon_series_with_non_activated(self) -> list[dict[str, Any]]:
        payload = {"organizationId": self.organization_id}
        body = self._post(path="/loyalty/iiko/coupons/series", payload=payload)
        rows = body.get("seriesWithNotActivatedCoupons") or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []

    def get_non_activated_coupons(
        self,
        *,
        series: str,
        page_size: int = 500,
        page: int = 0,
    ) -> list[dict[str, Any]]:
        payload = {
            "series": str(series or "").strip(),
            "pageSize": int(page_size),
            "page": int(page),
            "organizationId": self.organization_id,
        }
        body = self._post(path="/loyalty/iiko/coupons/by_series", payload=payload)
        rows = body.get("notActivatedCoupon") or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []

    def get_coupon_info(
        self,
        *,
        number: str,
        series: str | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "number": str(number or "").strip(),
            "organizationId": self.organization_id,
        }
        if series:
            payload["series"] = str(series).strip()
        body = self._post(path="/loyalty/iiko/coupons/info", payload=payload)
        rows = body.get("couponInfo") or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []

    def fetch_all_non_activated_numbers(
        self,
        *,
        series: str,
        page_size: int = 500,
        max_pages: int = 200,
    ) -> set[str]:
        """
        Возвращает множество номеров купонов серии через постраничный обход.
        """
        result: set[str] = set()
        safe_page_size = max(1, int(page_size))
        safe_max_pages = max(1, int(max_pages))

        page = 0
        while page < safe_max_pages:
            rows = self.get_non_activated_coupons(series=series, page_size=safe_page_size, page=page)
            if not rows:
                break
            for row in rows:
                number = str(row.get("number") or "").strip()
                if number:
                    result.add(number)
            if len(rows) < safe_page_size:
                break
            page += 1
        return result
