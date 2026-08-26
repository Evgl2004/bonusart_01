from __future__ import annotations

import logging
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests

from guests.services.iiko_cloud_auth import IikoCloudTokenProvider

logger = logging.getLogger(__name__)

IIKO_CLOUD_API_V1_PREFIX = "/api/1"


class IikoCloudTransportError(Exception):
    """Структурированная безопасная ошибка рабочего метода iiko Cloud API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        path: str = "",
        body: dict[str, Any] | None = None,
        error_code: str = "",
        correlation_id: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(str(message or "Ошибка рабочего метода iiko Cloud API."))
        self.status_code = status_code
        self.path = str(path or "").strip()
        self.body = body
        self.error_code = str(error_code or "").strip()
        self.correlation_id = str(correlation_id or "").strip()
        self.retryable = bool(retryable)


def normalize_iiko_cloud_api_base_url(base_url: str | None) -> str:
    """Приводит корневой адрес iiko Cloud API к рабочему префиксу `/api/1`."""
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        return ""
    if normalized.endswith(IIKO_CLOUD_API_V1_PREFIX):
        return normalized
    return f"{normalized}{IIKO_CLOUD_API_V1_PREFIX}"


class IikoCloudJsonTransport:
    """
    Выполняет авторизованные JSON-запросы к рабочим методам iiko Cloud API.

    После HTTP 401 токен обновляется и запрос повторяется ровно один раз.
    Временные сетевые и HTTP-ошибки повторяются только для методов, которые
    вызывающий код явно пометил безопасными для немедленного повтора.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token_provider: IikoCloudTokenProvider,
        session: requests.Session | None = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 15.0,
        max_retries: int = 2,
        retry_base_seconds: float = 0.5,
        retry_max_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = normalize_iiko_cloud_api_base_url(base_url)
        self.token_provider = token_provider
        self._session = session or requests.Session()
        self._owns_session = session is None
        self.connect_timeout_seconds = max(0.1, float(connect_timeout_seconds))
        self.read_timeout_seconds = max(0.1, float(read_timeout_seconds))
        self.max_retries = max(0, int(max_retries))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.retry_max_seconds = max(0.0, float(retry_max_seconds))
        self._clock = clock
        self._sleeper = sleeper

    def close(self) -> None:
        """Закрывает собственную HTTP-сессию транспорта."""
        if self._owns_session:
            self._session.close()

    def post_json(
        self,
        *,
        path: str,
        payload: dict[str, Any],
        retry_transient: bool,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        """Выполняет POST и возвращает словарь без раскрытия тела ошибки в тексте."""
        safe_path = "/" + str(path or "").strip().lstrip("/")
        if not self.base_url:
            raise IikoCloudTransportError(
                "Не задан IIKO_API_BASE_URL для iiko Cloud API.",
                path=safe_path,
            )

        transient_attempt = 0
        authorization_replayed = False
        while True:
            token = self.token_provider.get_token()
            try:
                response = self._session.post(
                    f"{self.base_url}{safe_path}",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                    timeout=(self.connect_timeout_seconds, self.read_timeout_seconds),
                )
            except requests.RequestException as exc:
                if retry_transient and transient_attempt < self.max_retries:
                    transient_attempt += 1
                    logger.warning(
                        "Повтор рабочего запроса iiko Cloud API после сетевой ошибки: "
                        "путь=%s попытка=%s/%s",
                        safe_path,
                        transient_attempt + 1,
                        self.max_retries + 1,
                    )
                    self._sleeper(self._retry_delay(response=None, attempt_index=transient_attempt - 1))
                    continue
                raise IikoCloudTransportError(
                    "Сетевая ошибка рабочего метода iiko Cloud API.",
                    path=safe_path,
                    retryable=True,
                ) from exc

            status_code = int(response.status_code)
            if status_code == 401 and not authorization_replayed:
                self.token_provider.invalidate_token(expected_token=token)
                authorization_replayed = True
                continue

            retryable = self._is_retryable_status(status_code)
            if retry_transient and retryable and transient_attempt < self.max_retries:
                transient_attempt += 1
                correlation_id = self._extract_response_details(response)[2]
                logger.warning(
                    "Повтор рабочего запроса iiko Cloud API после временного ответа: "
                    "путь=%s статус=%s попытка=%s/%s correlation_id=%s",
                    safe_path,
                    status_code,
                    transient_attempt + 1,
                    self.max_retries + 1,
                    correlation_id or "-",
                )
                self._sleeper(self._retry_delay(response=response, attempt_index=transient_attempt - 1))
                continue

            if status_code != 200:
                body, error_code, correlation_id = self._extract_response_details(response)
                details = [f"путь={safe_path}", f"статус={status_code}"]
                if error_code:
                    details.append(f"код={error_code}")
                if correlation_id:
                    details.append(f"correlationId={correlation_id}")
                raise IikoCloudTransportError(
                    "Ошибка рабочего метода iiko Cloud API: " + ", ".join(details) + ".",
                    status_code=status_code,
                    path=safe_path,
                    body=body,
                    error_code=error_code,
                    correlation_id=correlation_id,
                    retryable=retryable,
                )

            response_text = str(getattr(response, "text", "") or "").strip()
            if not response_text and allow_empty:
                return {}
            try:
                body = response.json()
            except ValueError as exc:
                raise IikoCloudTransportError(
                    "iiko Cloud API вернул некорректный JSON рабочего метода.",
                    status_code=status_code,
                    path=safe_path,
                ) from exc
            if isinstance(body, dict):
                return body
            return {"result": body}

    def _retry_delay(self, *, response, attempt_index: int) -> float:
        retry_after = ""
        if response is not None:
            retry_after = str(getattr(response, "headers", {}).get("Retry-After") or "").strip()

        delay: float | None = None
        if retry_after:
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = max(0.0, retry_at.timestamp() - self._clock())
                except (TypeError, ValueError, OverflowError):
                    delay = None

        if delay is None:
            delay = self.retry_base_seconds * (2 ** max(0, int(attempt_index)))
        return min(delay, self.retry_max_seconds)

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {408, 429} or 500 <= status_code <= 599

    @staticmethod
    def _extract_response_details(response) -> tuple[dict[str, Any] | None, str, str]:
        try:
            body = response.json()
        except ValueError:
            return None, "", ""
        if not isinstance(body, dict):
            return None, "", ""
        error_code = str(body.get("errorCode") or body.get("code") or "").strip()
        correlation_id = str(body.get("correlationId") or "").strip()
        return body, error_code, correlation_id
