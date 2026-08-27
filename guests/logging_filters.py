"""Фильтры журналирования, исключающие раскрытие служебных секретов."""

from __future__ import annotations

import logging
import re


_TELEGRAM_TOKEN_IN_URL = re.compile(
    r"(?P<prefix>https?://api\.telegram\.org/bot)[^/\s\"']+",
    flags=re.IGNORECASE,
)
_TRACKED_LINK_TOKEN_IN_PATH = re.compile(
    r"(?P<prefix>/r/v1/)(?P<marker>[A-Za-z0-9_-]{10})[A-Za-z0-9_-]{22}"
    r"(?![A-Za-z0-9_-])"
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


def redact_tracked_link_tokens(value: object) -> str:
    """Заменяет полный публичный токен диагностическим маркером."""

    return _TRACKED_LINK_TOKEN_IN_PATH.sub(
        lambda match: (
            f"{match.group('prefix')}[СКРЫТО] "
            f"link_marker={match.group('marker')}"
        ),
        str(value),
    )


class TrackedLinkTokenRedactingFilter(logging.Filter):
    """Скрывает полный токен ссылки, сохраняя первые десять символов."""

    def filter(self, record: logging.LogRecord) -> bool:
        rendered_message = record.getMessage()
        redacted_message = redact_tracked_link_tokens(rendered_message)
        if redacted_message != rendered_message:
            record.msg = redacted_message
            record.args = ()
        return True
