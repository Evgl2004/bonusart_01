import logging
import asyncio

from telegram import Bot
from telegram.error import TelegramError, RetryAfter, TimedOut
from telegram.request import HTTPXRequest

log = logging.getLogger(__name__)


class TelegramSender:
    def __init__(self, token: str):
        self.token = token  # ⚠️ не храним Bot между вызовами

    def send_message(self, chat_id: int, text: str):
        async def _send():
            try:
                log.info(f"[TG] Sending message to chat_id={chat_id}")

                # ✅ создаём новый request+bot внутри активного event loop
                request = HTTPXRequest(
                    connect_timeout=10.0,
                    read_timeout=20.0,
                    write_timeout=20.0,
                    pool_timeout=20.0,
                )
                bot = Bot(token=self.token, request=request)

                response = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                )

                log.info(
                    f"[TG] SUCCESS chat_id={chat_id} "
                    f"message_id={response.message_id} "
                    f"date={response.date}"
                )
                print(
                    f"[TG] SUCCESS chat_id={chat_id} "
                    f"message_id={response.message_id}"
                )

                return response

            except RetryAfter as e:
                log.warning(f"[TG] Rate limit. Retry after {e.retry_after}s")
                print(f"[TG] Rate limit. Retry after {e.retry_after}s")
                raise

            except TimedOut as e:
                log.error(f"[TG] Telegram API error: Timed out")
                print(f"[TG] Telegram API error: Timed out")
                raise

            except TelegramError as e:
                log.error(f"[TG] Telegram API error: {e}")
                print(f"[TG] Telegram API error: {e}")
                raise

        # ✅ каждый вызов поднимает loop, внутри которого создаётся Bot+HTTPXRequest
        return asyncio.run(_send())