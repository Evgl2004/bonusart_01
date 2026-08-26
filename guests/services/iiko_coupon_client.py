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

class IikoCouponApiError(Exception):
    """Безопасная ошибка работы с API купонов iiko."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        path: str = "",
        correlation_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = str(path or "").strip()
        self.correlation_id = str(correlation_id or "").strip()


class IikoCouponClient:
    """Клиент read-only методов купонов iiko Cloud API версии 1."""

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
        self.timeout_seconds = max(0.1, float(timeout_seconds or 15.0))
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
            raise IikoCouponApiError("Не задан IIKO_ORGANIZATION_ID для API купонов iiko.")
        try:
            return self._transport.post_json(
                path=path,
                payload=payload,
                retry_transient=True,
            )
        except IikoCloudAuthError as exc:
            raise IikoCouponApiError(f"Ошибка авторизации API купонов iiko: {exc}") from exc
        except IikoCloudTransportError as exc:
            raise IikoCouponApiError(
                str(exc),
                status_code=exc.status_code,
                path=exc.path,
                correlation_id=exc.correlation_id,
            ) from exc

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
