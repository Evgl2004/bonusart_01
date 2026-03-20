"""
Bug-seeking тесты для provider_clients.

Покрываем нестабильные/ошибочные сценарии токенов и ответов API.
"""

from __future__ import annotations

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from unittest.mock import patch

from guests.services.universal_queue.provider_clients import (
    MaxAsyncSender,
    ProviderPermanentError,
    TelegramAsyncSender,
    VkAsyncSender,
    _resolve_bot_token,
)


class ProviderClientsBugSeekingTests(SimpleTestCase):
    """
    Негативные сценарии provider clients.
    """

    class _TaskStub:
        def __init__(self, bot_profile=None, payload=None, task_id=1):
            self.id = task_id
            self.bot_profile = bot_profile
            self.payload = payload

    class _BrokenBotProfile:
        def resolve_token(self) -> str:
            raise RuntimeError("broken token backend")

    class _OkBotProfile:
        def __init__(self, token: str):
            self._token = token

        def resolve_token(self) -> str:
            return self._token

    @staticmethod
    def _response(
        status_code: int,
        *,
        json_data=None,
        text: str = "",
        headers: dict | None = None,
    ) -> httpx.Response:
        request = httpx.Request("POST", "https://provider.example/send")
        if json_data is not None:
            return httpx.Response(status_code=status_code, json=json_data, headers=headers or {}, request=request)
        return httpx.Response(status_code=status_code, text=text, headers=headers or {}, request=request)

    def test_resolve_bot_token_uses_fallback_when_bot_profile_raises(self):
        """
        Исключение в bot_profile.resolve_token не должно ронять обработку:
        ожидаем fallback-токен из окружения.
        """
        with patch.dict("os.environ", {"UNIVERSAL_QUEUE_TELEGRAM_FALLBACK_TOKEN": "tg-fallback"}):
            task = self._TaskStub(bot_profile=self._BrokenBotProfile(), payload={})
            token = async_to_sync(_resolve_bot_token)(task, "UNIVERSAL_QUEUE_TELEGRAM_FALLBACK_TOKEN")
            self.assertEqual(token, "tg-fallback")

    def test_resolve_bot_token_uses_payload_token_ref_when_profile_raises(self):
        """
        Если bot_profile сломан, токен должен браться из payload.bot_token_ref.
        """
        with patch.dict("os.environ", {"BOT_TOKEN_REF_TG": "tg-from-ref"}):
            task = self._TaskStub(
                bot_profile=self._BrokenBotProfile(),
                payload={"bot_token_ref": "BOT_TOKEN_REF_TG"},
            )
            token = async_to_sync(_resolve_bot_token)(task, "UNIVERSAL_QUEUE_TELEGRAM_FALLBACK_TOKEN")
            self.assertEqual(token, "tg-from-ref")

    def test_telegram_sender_extract_retry_after_invalid_values_fallback_default(self):
        """
        Если retry_after в payload и заголовке невалидны, должен использоваться default.
        """
        response = self._response(
            429,
            json_data={"ok": False, "parameters": {"retry_after": "oops"}},
            headers={"Retry-After": "NaN-value"},
        )
        retry_after = TelegramAsyncSender._extract_retry_after(
            payload={"parameters": {"retry_after": "oops"}},
            response=response,
            default=7.0,
        )
        self.assertEqual(retry_after, 7.0)

    def test_max_sender_extract_retry_after_invalid_header_fallback_default(self):
        """
        Для MAX: невалидный Retry-After должен давать default.
        """
        response = self._response(
            429,
            json_data={"retry_after": "invalid"},
            headers={"Retry-After": "invalid"},
        )
        retry_after = MaxAsyncSender._extract_retry_after(
            payload={"retry_after": "invalid"},
            response=response,
            default=5.0,
        )
        self.assertEqual(retry_after, 5.0)

    def test_vk_safe_json_handles_non_json_response(self):
        """
        VK safe_json должен возвращать raw_text, если API отдало не-JSON тело.
        """
        response = self._response(200, text="<html>bad gateway</html>")
        parsed = VkAsyncSender._safe_json(response)
        self.assertIn("raw_text", parsed)
        self.assertIn("bad gateway", parsed["raw_text"])

    def test_sender_raises_permanent_when_token_missing_even_with_payload_not_dict(self):
        """
        Если токен не найден и payload поврежден (не dict), отправитель должен
        завершаться контролируемой постоянной ошибкой, а не TypeError.
        """
        sender = TelegramAsyncSender()
        sender.client = object()  # маркер, что startup как будто уже вызван
        task = self._TaskStub(bot_profile=self._OkBotProfile(""), payload=["not", "dict"])

        with self.assertRaises(ProviderPermanentError):
            async_to_sync(sender.send)(task, "777000", "text")
