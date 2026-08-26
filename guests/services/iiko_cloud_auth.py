from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

IIKO_AUTH_MODE_LEGACY = "legacy"
IIKO_AUTH_MODE_V2 = "v2"
IIKO_AUTH_MODES = frozenset({IIKO_AUTH_MODE_LEGACY, IIKO_AUTH_MODE_V2})

DEFAULT_IIKO_LEGACY_AUTH_URL = "https://api-ru.iiko.services/api/1/access_token"
DEFAULT_IIKO_V2_AUTH_URL = "https://api-ru.iiko.services/api/v2/access_token"
LEGACY_TOKEN_CACHE_SECONDS = 14 * 60


class IikoCloudAuthError(Exception):
    """Безопасная ошибка получения токена iiko Cloud API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        correlation_id: str = "",
        error_code: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(str(message or "Ошибка авторизации iiko Cloud API."))
        self.status_code = status_code
        self.correlation_id = str(correlation_id or "").strip()
        self.error_code = str(error_code or "").strip()
        self.retryable = bool(retryable)


@dataclass(frozen=True, slots=True)
class IikoCloudAuthConfig:
    """Настройки ровно одного активного способа авторизации iiko Cloud API."""

    mode: str
    legacy_api_login: str = field(default="", repr=False)
    app_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    legacy_auth_url: str = DEFAULT_IIKO_LEGACY_AUTH_URL
    v2_auth_url: str = DEFAULT_IIKO_V2_AUTH_URL
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 15.0
    max_retries: int = 2
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 5.0
    token_refresh_margin_seconds: float = 60.0

    def __post_init__(self) -> None:
        for attribute_name in (
            "mode",
            "legacy_api_login",
            "app_id",
            "client_secret",
            "api_key",
            "legacy_auth_url",
            "v2_auth_url",
        ):
            raw_value = getattr(self, attribute_name)
            normalized = str(raw_value or "").strip()
            if attribute_name == "mode":
                normalized = normalized.lower()
            object.__setattr__(self, attribute_name, normalized)

        object.__setattr__(self, "connect_timeout_seconds", max(0.1, float(self.connect_timeout_seconds)))
        object.__setattr__(self, "read_timeout_seconds", max(0.1, float(self.read_timeout_seconds)))
        object.__setattr__(self, "max_retries", max(0, int(self.max_retries)))
        object.__setattr__(self, "retry_base_seconds", max(0.0, float(self.retry_base_seconds)))
        object.__setattr__(self, "retry_max_seconds", max(0.0, float(self.retry_max_seconds)))
        object.__setattr__(
            self,
            "token_refresh_margin_seconds",
            max(0.0, float(self.token_refresh_margin_seconds)),
        )

    @property
    def auth_url(self) -> str:
        """Возвращает адрес авторизации только выбранного режима."""
        if self.mode == IIKO_AUTH_MODE_LEGACY:
            return self.legacy_auth_url
        return self.v2_auth_url

    def request_payload(self) -> dict[str, str]:
        """Формирует тело авторизации без смешивания реквизитов двух режимов."""
        if self.mode == IIKO_AUTH_MODE_LEGACY:
            return {"apiLogin": self.legacy_api_login}
        return {
            "appId": self.app_id,
            "clientSecret": self.client_secret,
            "apiKey": self.api_key,
        }

    def validate(self) -> None:
        """Проверяет активный набор без раскрытия значений секретов."""
        if self.mode not in IIKO_AUTH_MODES:
            raise IikoCloudAuthError(
                "Не задан корректный IIKO_AUTH_MODE: допустимы legacy и v2."
            )

        if self.mode == IIKO_AUTH_MODE_LEGACY:
            missing = []
            if not self.legacy_api_login:
                missing.append("IIKO_LEGACY_API_LOGIN")
            if not self.legacy_auth_url:
                missing.append("IIKO_LEGACY_AUTH_URL")
        else:
            missing = []
            if not self.app_id:
                missing.append("IIKO_APP_ID")
            if not self.client_secret:
                missing.append("IIKO_CLIENT_SECRET")
            if not self.api_key:
                missing.append("IIKO_API_KEY")
            if not self.v2_auth_url:
                missing.append("IIKO_AUTH_URL")

        if missing:
            raise IikoCloudAuthError(
                "Неполная конфигурация авторизации iiko Cloud API для режима "
                f"{self.mode}: не заданы {', '.join(missing)}."
            )


class IikoCloudTokenProvider:
    """
    Потокобезопасно получает и кэширует токен iiko Cloud API.

    Поставщик выполняет запрос только в явно выбранном режиме. Ошибка версии 2
    никогда не запускает скрытый запрос через старую точку авторизации.
    """

    def __init__(
        self,
        *,
        config: IikoCloudAuthConfig,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        config.validate()
        self.config = config
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._clock = clock
        self._sleeper = sleeper
        self._refresh_lock = threading.Lock()
        self._token: str | None = None
        self._token_valid_until = 0.0
        self._last_correlation_id = ""

    @property
    def mode(self) -> str:
        """Возвращает активный режим без обращения к сети."""
        return self.config.mode

    @property
    def last_correlation_id(self) -> str:
        """Возвращает последний безопасный идентификатор диагностики iiko."""
        return self._last_correlation_id

    def close(self) -> None:
        """Закрывает собственную HTTP-сессию поставщика."""
        if self._owns_session:
            self._session.close()

    def get_token(self, *, force_refresh: bool = False) -> str:
        """Возвращает кэшированный токен либо синхронно обновляет его один раз."""
        if not force_refresh and self._is_cached_token_valid():
            return str(self._token)

        with self._refresh_lock:
            if not force_refresh and self._is_cached_token_valid():
                return str(self._token)
            return self._request_and_cache_token()

    def invalidate_token(self, *, expected_token: str | None = None) -> None:
        """Сбрасывает кэш, не удаляя токен, уже обновлённый другим потоком."""
        with self._refresh_lock:
            if expected_token is not None and self._token != expected_token:
                return
            self._token = None
            self._token_valid_until = 0.0

    def _is_cached_token_valid(self) -> bool:
        return bool(self._token and self._clock() < self._token_valid_until)

    def _request_and_cache_token(self) -> str:
        response = self._request_token_response()
        try:
            body = response.json()
        except ValueError as exc:
            raise IikoCloudAuthError(
                "iiko Cloud API вернул некорректный JSON при авторизации.",
                status_code=int(response.status_code),
            ) from exc

        if not isinstance(body, dict):
            raise IikoCloudAuthError(
                "iiko Cloud API вернул неожиданный формат ответа авторизации.",
                status_code=int(response.status_code),
            )

        correlation_id = str(body.get("correlationId") or "").strip()
        self._last_correlation_id = correlation_id
        token = str(body.get("token") or "").strip()
        if not token:
            raise IikoCloudAuthError(
                "В ответе авторизации iiko Cloud API отсутствует token.",
                status_code=int(response.status_code),
                correlation_id=correlation_id,
            )

        now = self._clock()
        if self.mode == IIKO_AUTH_MODE_V2:
            expires_at = self._extract_jwt_exp(token)
            if expires_at <= now:
                raise IikoCloudAuthError(
                    "iiko Cloud API вернул уже истёкший JWT.",
                    status_code=int(response.status_code),
                    correlation_id=correlation_id,
                )
            valid_until = max(now, expires_at - self.config.token_refresh_margin_seconds)
        else:
            valid_until = now + LEGACY_TOKEN_CACHE_SECONDS

        self._token = token
        self._token_valid_until = valid_until
        return token

    def _request_token_response(self):
        total_attempts = self.config.max_retries + 1
        for attempt_index in range(total_attempts):
            try:
                response = self._session.post(
                    self.config.auth_url,
                    json=self.config.request_payload(),
                    headers={"Content-Type": "application/json"},
                    timeout=(
                        self.config.connect_timeout_seconds,
                        self.config.read_timeout_seconds,
                    ),
                )
            except requests.RequestException as exc:
                if attempt_index + 1 < total_attempts:
                    logger.warning(
                        "Повтор авторизации iiko Cloud API после сетевой ошибки: "
                        "режим=%s попытка=%s/%s",
                        self.mode,
                        attempt_index + 2,
                        total_attempts,
                    )
                    self._sleeper(self._retry_delay(response=None, attempt_index=attempt_index))
                    continue
                raise IikoCloudAuthError(
                    "Сетевая ошибка авторизации iiko Cloud API.",
                    retryable=True,
                ) from exc

            if int(response.status_code) == 200:
                return response

            correlation_id, error_code = self._extract_error_details(response)
            self._last_correlation_id = correlation_id
            retryable = self._is_retryable_status(int(response.status_code))
            if retryable and attempt_index + 1 < total_attempts:
                logger.warning(
                    "Повтор авторизации iiko Cloud API после временного ответа: "
                    "режим=%s статус=%s попытка=%s/%s correlation_id=%s",
                    self.mode,
                    int(response.status_code),
                    attempt_index + 2,
                    total_attempts,
                    correlation_id or "-",
                )
                self._sleeper(self._retry_delay(response=response, attempt_index=attempt_index))
                continue

            detail_parts = [
                f"режим={self.mode}",
                f"статус={int(response.status_code)}",
            ]
            if error_code:
                detail_parts.append(f"код={error_code}")
            if correlation_id:
                detail_parts.append(f"correlationId={correlation_id}")
            raise IikoCloudAuthError(
                "Ошибка авторизации iiko Cloud API: " + ", ".join(detail_parts) + ".",
                status_code=int(response.status_code),
                correlation_id=correlation_id,
                error_code=error_code,
                retryable=retryable,
            )

        raise AssertionError("Недостижимая ветка запроса авторизации iiko Cloud API.")

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
            delay = self.config.retry_base_seconds * (2 ** max(0, int(attempt_index)))
        return min(delay, self.config.retry_max_seconds)

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {408, 429} or 500 <= status_code <= 599

    @staticmethod
    def _extract_error_details(response) -> tuple[str, str]:
        try:
            body = response.json()
        except ValueError:
            return "", ""
        if not isinstance(body, dict):
            return "", ""
        correlation_id = str(body.get("correlationId") or "").strip()
        error_code = str(body.get("errorCode") or body.get("code") or "").strip()
        return correlation_id, error_code

    @staticmethod
    def _extract_jwt_exp(token: str) -> float:
        parts = str(token or "").split(".")
        if len(parts) != 3:
            raise IikoCloudAuthError("iiko Cloud API вернул токен без структуры JWT.")

        payload_segment = parts[1]
        padding = "=" * (-len(payload_segment) % 4)
        try:
            payload_raw = base64.urlsafe_b64decode((payload_segment + padding).encode("ascii"))
            payload = json.loads(payload_raw.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise IikoCloudAuthError("Не удалось разобрать JWT, полученный от iiko Cloud API.") from exc

        if not isinstance(payload, dict) or isinstance(payload.get("exp"), bool):
            raise IikoCloudAuthError("В JWT iiko Cloud API отсутствует корректное поле exp.")
        try:
            return float(payload["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IikoCloudAuthError("В JWT iiko Cloud API отсутствует корректное поле exp.") from exc


def build_iiko_cloud_token_provider_from_settings(
    *,
    session: requests.Session | None = None,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
) -> IikoCloudTokenProvider:
    """
    Собирает поставщик из Django settings только в момент обращения к iiko.

    Функция намеренно не вызывается из `settings.py`, поэтому неполная настройка
    iiko не останавливает запуск остальных частей приложения.
    """
    config = IikoCloudAuthConfig(
        mode=getattr(settings, "IIKO_AUTH_MODE", ""),
        legacy_api_login=getattr(settings, "IIKO_LEGACY_API_LOGIN", ""),
        app_id=getattr(settings, "IIKO_APP_ID", ""),
        client_secret=getattr(settings, "IIKO_CLIENT_SECRET", ""),
        api_key=getattr(settings, "IIKO_API_KEY", ""),
        legacy_auth_url=getattr(
            settings,
            "IIKO_LEGACY_AUTH_URL",
            DEFAULT_IIKO_LEGACY_AUTH_URL,
        ),
        v2_auth_url=getattr(settings, "IIKO_AUTH_URL", DEFAULT_IIKO_V2_AUTH_URL),
        connect_timeout_seconds=getattr(settings, "IIKO_AUTH_CONNECT_TIMEOUT_SECONDS", 5.0),
        read_timeout_seconds=getattr(settings, "IIKO_AUTH_READ_TIMEOUT_SECONDS", 15.0),
        max_retries=getattr(settings, "IIKO_AUTH_MAX_RETRIES", 2),
        retry_base_seconds=getattr(settings, "IIKO_AUTH_RETRY_BASE_SECONDS", 0.5),
        retry_max_seconds=getattr(settings, "IIKO_AUTH_RETRY_MAX_SECONDS", 5.0),
        token_refresh_margin_seconds=getattr(
            settings,
            "IIKO_AUTH_TOKEN_REFRESH_MARGIN_SECONDS",
            60.0,
        ),
    )
    return IikoCloudTokenProvider(
        config=config,
        session=session,
        clock=clock,
        sleeper=sleeper,
    )
