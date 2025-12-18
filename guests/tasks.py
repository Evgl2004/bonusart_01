# loyalty_viewer/guests/tasks.py

from .services.webhooks import process_recent_webhooks


def fetch_pending_webhooks():
    """
    Задача для Django Q: обрабатывает вебхуки из внешней БД за последние 10 минут.
    """
    return process_recent_webhooks(period_minutes=10)
