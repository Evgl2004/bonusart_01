# loyalty_viewer/guests/tasks.py
import logging

from .services.webhooks import process_recent_webhooks

logger = logging.getLogger(__name__)


def fetch_pending_webhooks():
    """
    Задача для Django Q: обрабатывает вебхуки из внешней БД за последние 10 минут.
    """

    logger.info("🔄 Запуск периодической проверки Уведомлений")

    try:
        processed = process_recent_webhooks(period_minutes=10)
        if processed > 0:
            logger.info(f"✅ Периодическая проверка: обработано {processed} Уведомлений")
        else:
            logger.info("✅ Уведомлений старше 10 минут не обнаружено")
        return processed
    except Exception as err:
        logger.error(f"❌ Ошибка периодической проверки: {err}")
        return 0
