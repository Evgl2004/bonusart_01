"""Фильтры журналирования, исключающие раскрытие служебных секретов."""

from __future__ import annotations

import logging
import re


_TELEGRAM_TOKEN_IN_URL = re.compile(
    r"(?P<prefix>https?://api\.telegram\.org/bot)[^/\s\"']+",
    flags=re.IGNORECASE,
)


def redact_telegram_bot_tokens(value: object) -> str:
    """Скрывает токен Telegram Bot API внутри полного адреса запроса."""

    return _TELEGRAM_TOKEN_IN_URL.sub(
        lambda match: f"{match.group('prefix')}[СКРЫТО]",
        str(value),
    )


class TelegramBotTokenRedactingFilter(logging.Filter):
    """Не позволяет записи журнала вывести токен Telegram из URL."""

    def filter(self, record: logging.LogRecord) -> bool:
        rendered_message = record.getMessage()
        redacted_message = redact_telegram_bot_tokens(rendered_message)
        if redacted_message != rendered_message:
            # Сообщение уже отформатировано, поэтому прежние аргументы должны
            # быть удалены: иначе обработчик попытается применить их повторно.
            record.msg = redacted_message
            record.args = ()
        return True
