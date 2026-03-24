"""
Тестовые настройки Django для локального прогона через SQLite.

Назначение:
1. Убрать зависимость локальных тестов от внешнего PostgreSQL-хоста `db`.
2. Дать стабильный запуск `pytest` в окружениях разработчика.
3. Сохранить основной профиль `loyalty_viewer.settings` без изменений для production.

Важно:
1. SQLite-профиль используется только для локальных/быстрых тестов.
2. Для финальной регрессии на SQL-специфичных кейсах рекомендуется отдельный прогон
   в docker-контуре с PostgreSQL.
"""

from .settings import *  # noqa: F401,F403


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_default.sqlite3",
    },
    "webhooks": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_webhooks.sqlite3",
    },
}


# Для локального прогона отключаем миграции приложения guests:
# схема создается напрямую из моделей, что делает запуск быстрее и стабильнее.
MIGRATION_MODULES = {
    "guests": None,
}

