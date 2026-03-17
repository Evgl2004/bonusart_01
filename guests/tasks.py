"""
Фоновые задачи проекта для запуска через Django Q.
"""

import logging

from .services.notification_scenarios import run_scheduled_inactive_scenarios
from .services.webhooks import process_recent_webhooks

logger = logging.getLogger(__name__)


def fetch_pending_webhooks() -> int:
    """
    Периодическая задача: обработка pending webhook из внешнего сервиса.
    """
    logger.info("Запуск периодической обработки веб-хуков")

    try:
        processed = int(process_recent_webhooks(period_minutes=10))
        if processed > 0:
            logger.info("Периодическая обработка веб-хуков завершена: обработано=%s", processed)
        else:
            logger.info("Периодическая обработка веб-хуков: новых событий нет")
        return processed
    except Exception as err:
        logger.exception("Ошибка периодической обработки веб-хуков: %s", err)
        return 0


def run_scheduled_notification_scenarios_task() -> int:
    """
    Периодическая задача: запуск авто-сценариев неактивности гостей.

    Возвращает суммарное количество созданных DispatchTask.
    """
    logger.info("Запуск периодического скана авто-сценариев уведомлений")
    try:
        scenario_stats = run_scheduled_inactive_scenarios()
        total_created_tasks = sum(int(stat.created_tasks) for stat in scenario_stats.values())
        logger.info(
            "Авто-сценарии завершены: сценариев=%s, создано задач=%s",
            len(scenario_stats),
            total_created_tasks,
        )
        return total_created_tasks
    except Exception as err:
        logger.exception("Ошибка выполнения авто-сценариев уведомлений: %s", err)
        return 0
