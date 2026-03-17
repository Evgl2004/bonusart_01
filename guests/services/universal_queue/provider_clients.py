import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from guests.models import DispatchTask, MailingChannel

logger = logging.getLogger(__name__)


class ProviderSendError(Exception):
    """
    Базовый класс ошибок отправки в провайдера.
    """


class ProviderTemporaryError(ProviderSendError):
    """
    Временная ошибка провайдера (можно повторить позже).
    """


class ProviderPermanentError(ProviderSendError):
    """
    Невосстановимая ошибка провайдера (повтор бессмысленен).
    """


class ProviderBlockedError(ProviderPermanentError):
    """
    Получатель недоступен или запретил сообщения боту.
    """


class ProviderRateLimitError(ProviderTemporaryError):
    """
    Провайдер вернул ограничение частоты отправки.
    """

    def __init__(self, retry_after_seconds: float, message: str):
        super().__init__(message)
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))


@dataclass
class ProviderSendResult:
    """
    Результат успешной отправки сообщения в провайдера.
    """

    provider_message_id: str | None
    sent_at: datetime
    raw_response: Dict[str, Any]


class BaseAsyncProviderSender:
    """
    Базовый контракт async-отправителя в конкретного провайдера.
    """

    provider_type: str

    async def startup(self) -> None:
        """
        Инициализирует клиент/пул соединений провайдера.
        """

    async def shutdown(self) -> None:
        """
        Корректно закрывает ресурсы клиента провайдера.
        """

    async def send(self, task: DispatchTask, chat_id: str, text: str) -> ProviderSendResult:
        """
        Отправляет одно сообщение и возвращает метаданные доставки.
        """
        raise NotImplementedError


def _float_setting(name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def _get_payload(task: DispatchTask) -> Dict[str, Any]:
    return task.payload if isinstance(task.payload, dict) else {}


def _resolve_legacy_channel_token_sync(task: DispatchTask) -> str:
    """
    Возвращает token из legacy MailingChannel по `legacy_channel_id` в payload.
    """
    payload = _get_payload(task)
    channel_id = payload.get("legacy_channel_id")
    if not channel_id:
        return ""

    channel = MailingChannel.objects.filter(id=channel_id, is_active=True).only("token").first()
    if channel and channel.token:
        return str(channel.token).strip()
    return ""


async def _resolve_bot_token(task: DispatchTask, fallback_env_name: str) -> str:
    """
    Унифицированное разрешение токена бота:
    1. BotProfile.resolve_token();
    2. token/token_ref в payload задачи;
    3. token legacy канала MailingChannel (если `legacy_channel_id` передан в payload);
    4. fallback из env-переменной.
    """
    if task.bot_profile:
        token = task.bot_profile.resolve_token()
        if token:
            return token

    payload = _get_payload(task)
    payload_token = str(payload.get("bot_token") or "").strip()
    if payload_token:
        return payload_token

    payload_token_ref = str(payload.get("bot_token_ref") or "").strip()
    if payload_token_ref:
        token_from_ref = os.getenv(payload_token_ref, "").strip()
        if token_from_ref:
            return token_from_ref

    legacy_token = await sync_to_async(_resolve_legacy_channel_token_sync, thread_sensitive=True)(task)
    if legacy_token:
        return legacy_token

    return os.getenv(fallback_env_name, "").strip()


class TelegramAsyncSender(BaseAsyncProviderSender):
    """
    Async-отправитель Telegram Bot API через общий пул HTTP-соединений.
    """

    provider_type = "telegram"

    def __init__(self):
        self.base_url = str(getattr(settings, "TELEGRAM_API_BASE_URL", "https://api.telegram.org")).rstrip("/")
        self.timeout = _float_setting("UNIVERSAL_PROVIDER_HTTP_TIMEOUT", 20.0)
        self.client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    async def shutdown(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def send(self, task: DispatchTask, chat_id: str, text: str) -> ProviderSendResult:
        if self.client is None:
            raise RuntimeError("TelegramAsyncSender не инициализирован. Вызовите startup().")

        token = await _resolve_bot_token(task, fallback_env_name="UNIVERSAL_QUEUE_TELEGRAM_FALLBACK_TOKEN")
        if not token:
            raise ProviderPermanentError("Не найден token для Telegram-бота.")

        payload = _get_payload(task)
        request_body: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if payload.get("parse_mode"):
            request_body["parse_mode"] = payload["parse_mode"]
        if payload.get("disable_web_page_preview") is not None:
            request_body["disable_web_page_preview"] = bool(payload["disable_web_page_preview"])

        url = f"{self.base_url}/bot{token}/sendMessage"
        response = await self.client.post(url, json=request_body)
        response_data = self._safe_json(response)

        if response.status_code == 429:
            retry_after = self._extract_retry_after(response_data, response, default=3.0)
            raise ProviderRateLimitError(retry_after_seconds=retry_after, message="Telegram rate limit.")

        if response.status_code in (401, 404):
            raise ProviderPermanentError(f"Telegram auth error: status={response.status_code}")

        if response.status_code == 403:
            raise ProviderBlockedError("Telegram сообщает, что пользователь недоступен/заблокировал бота.")

        if response.status_code >= 500:
            raise ProviderTemporaryError(f"Telegram server error: status={response.status_code}")

        if response.status_code >= 400:
            raise ProviderPermanentError(
                f"Telegram rejected request: status={response.status_code} body={response.text[:500]}"
            )

        if not response_data.get("ok", False):
            description = str(response_data.get("description") or "Telegram API returned ok=False")
            error_code = int(response_data.get("error_code") or 0)
            if error_code == 429:
                retry_after = self._extract_retry_after(response_data, response, default=3.0)
                raise ProviderRateLimitError(retry_after_seconds=retry_after, message=description)
            if error_code in (403,):
                raise ProviderBlockedError(description)
            if error_code >= 500:
                raise ProviderTemporaryError(description)
            raise ProviderPermanentError(description)

        result = response_data.get("result") or {}
        message_id = result.get("message_id")
        sent_at = timezone.now()
        return ProviderSendResult(
            provider_message_id=str(message_id) if message_id is not None else None,
            sent_at=sent_at,
            raw_response=response_data,
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> Dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"raw": data}
        except Exception:
            return {"raw_text": response.text[:1000]}

    @staticmethod
    def _extract_retry_after(
        payload: Dict[str, Any],
        response: httpx.Response,
        default: float,
    ) -> float:
        params = payload.get("parameters") or {}
        from_payload = params.get("retry_after") or payload.get("retry_after")
        if from_payload is not None:
            try:
                return float(from_payload)
            except (TypeError, ValueError):
                pass

        header_value = response.headers.get("Retry-After")
        if header_value:
            try:
                return float(header_value)
            except (TypeError, ValueError):
                pass
        return default


class MaxAsyncSender(BaseAsyncProviderSender):
    """
    Async-отправитель MAX Bot API.
    """

    provider_type = "max"

    def __init__(self):
        self.base_url = str(getattr(settings, "MAX_API_BASE_URL", "https://platform-api.max.ru")).rstrip("/")
        self.timeout = _float_setting("UNIVERSAL_PROVIDER_HTTP_TIMEOUT", 20.0)
        self.client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    async def shutdown(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def send(self, task: DispatchTask, chat_id: str, text: str) -> ProviderSendResult:
        if self.client is None:
            raise RuntimeError("MaxAsyncSender не инициализирован. Вызовите startup().")

        token = await _resolve_bot_token(task, fallback_env_name="UNIVERSAL_QUEUE_MAX_FALLBACK_TOKEN")
        if not token:
            raise ProviderPermanentError("Не найден token для MAX-бота.")

        payload = _get_payload(task)
        query_field = "chat_id"
        if payload.get("max_user_id"):
            query_field = "user_id"
            chat_or_user_id = str(payload["max_user_id"]).strip()
        else:
            chat_or_user_id = chat_id

        request_body = {"text": text}
        request_url = f"{self.base_url}/messages"
        auth_prefix = str(getattr(settings, "MAX_API_AUTH_PREFIX", "")).strip()
        # Формируем стандартный формат заголовка: "<prefix> <token>".
        # Пример: "Bearer <token>".
        authorization = f"{auth_prefix} {token}" if auth_prefix else token

        response = await self.client.post(
            request_url,
            params={query_field: chat_or_user_id},
            headers={"Authorization": authorization},
            json=request_body,
        )
        response_data = self._safe_json(response)

        if response.status_code == 429:
            retry_after = self._extract_retry_after(response_data, response, default=3.0)
            raise ProviderRateLimitError(retry_after_seconds=retry_after, message="MAX rate limit.")

        if response.status_code in (401, 403):
            raise ProviderPermanentError(f"MAX auth error: status={response.status_code}")

        if response.status_code == 404:
            raise ProviderBlockedError("MAX chat/user не найден или недоступен.")

        if response.status_code >= 500:
            raise ProviderTemporaryError(f"MAX server error: status={response.status_code}")

        if response.status_code >= 400:
            raise ProviderPermanentError(
                f"MAX rejected request: status={response.status_code} body={response.text[:500]}"
            )

        message_id = response_data.get("message_id") or response_data.get("id")
        return ProviderSendResult(
            provider_message_id=str(message_id) if message_id is not None else None,
            sent_at=timezone.now(),
            raw_response=response_data,
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> Dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"raw": data}
        except Exception:
            return {"raw_text": response.text[:1000]}

    @staticmethod
    def _extract_retry_after(
        payload: Dict[str, Any],
        response: httpx.Response,
        default: float,
    ) -> float:
        retry_after = payload.get("retry_after")
        if retry_after is not None:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                pass

        header_value = response.headers.get("Retry-After")
        if header_value:
            try:
                return float(header_value)
            except (TypeError, ValueError):
                pass
        return default


class VkAsyncSender(BaseAsyncProviderSender):
    """
    Async-отправитель VK API (метод `messages.send`).
    """

    provider_type = "vk"

    def __init__(self):
        self.base_url = str(getattr(settings, "VK_API_BASE_URL", "https://api.vk.com/method")).rstrip("/")
        self.api_version = str(getattr(settings, "VK_API_VERSION", "5.199")).strip()
        self.timeout = _float_setting("UNIVERSAL_PROVIDER_HTTP_TIMEOUT", 20.0)
        self.client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    async def shutdown(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def send(self, task: DispatchTask, chat_id: str, text: str) -> ProviderSendResult:
        if self.client is None:
            raise RuntimeError("VkAsyncSender не инициализирован. Вызовите startup().")

        token = await _resolve_bot_token(task, fallback_env_name="UNIVERSAL_QUEUE_VK_FALLBACK_TOKEN")
        if not token:
            raise ProviderPermanentError("Не найден token для VK-бота.")

        request_url = f"{self.base_url}/messages.send"
        request_params = {
            "access_token": token,
            "v": self.api_version,
            "peer_id": chat_id,
            "random_id": random.randint(1, 2_147_483_647),
            "message": text,
        }
        response = await self.client.post(request_url, data=request_params)
        response_data = self._safe_json(response)

        if response.status_code >= 500:
            raise ProviderTemporaryError(f"VK server error: status={response.status_code}")

        if response.status_code >= 400:
            raise ProviderPermanentError(f"VK rejected request: status={response.status_code} body={response.text[:500]}")

        if "error" in response_data:
            error = response_data.get("error") or {}
            error_code = int(error.get("error_code") or 0)
            error_message = str(error.get("error_msg") or "VK API error")

            if error_code in (6, 9, 10, 29):
                raise ProviderRateLimitError(retry_after_seconds=1.0, message=error_message)
            if error_code in (901, 902, 912):
                raise ProviderBlockedError(error_message)
            if error_code >= 1000:
                raise ProviderTemporaryError(error_message)
            raise ProviderPermanentError(error_message)

        response_payload = response_data.get("response")
        message_id = None
        if isinstance(response_payload, dict):
            message_id = response_payload.get("message_id")
        elif response_payload is not None:
            message_id = response_payload

        return ProviderSendResult(
            provider_message_id=str(message_id) if message_id is not None else None,
            sent_at=timezone.now(),
            raw_response=response_data,
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> Dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"raw": data}
        except (ValueError, json.JSONDecodeError):
            return {"raw_text": response.text[:1000]}


def build_provider_sender(provider_type: str) -> BaseAsyncProviderSender:
    """
    Фабрика отправителей по типу провайдера.
    """
    provider = str(provider_type).strip().lower()
    if provider == "telegram":
        return TelegramAsyncSender()
    if provider == "max":
        return MaxAsyncSender()
    if provider == "vk":
        return VkAsyncSender()
    raise ValueError(f"Неподдерживаемый provider_type={provider_type}")
