import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def enqueue_high_priority_webhook_tasks(webhook: Dict[str, Any]) -> int:
    """
    Совместимый адаптер для legacy-вызова webhook producer-а.

    Сейчас используется явный бизнес-метод:
    `guests.services.webhooks.enqueue_balance_notification_from_webhook`.
    Этот адаптер оставлен временно, чтобы внешние импорты не ломались.
    """
    from guests.services.webhooks import enqueue_balance_notification_from_webhook

    logger.warning(
        "enqueue_high_priority_webhook_tasks устарел: используйте "
        "enqueue_balance_notification_from_webhook."
    )
    return enqueue_balance_notification_from_webhook(webhook)
