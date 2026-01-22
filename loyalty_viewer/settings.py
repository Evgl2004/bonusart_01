from pathlib import Path
import os
from dotenv import load_dotenv
import re

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "devkey")
DEBUG = os.getenv("DEBUG", "False") == "True"

# Создаем папку для логов, если она не существует
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# Проверяет, что запрос пришел с разрешенного домена/IP.
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,0.0.0.0').split(',')
ALLOWED_HOSTS = [host.strip() for host in ALLOWED_HOSTS]

# Настройки безопасности для работы через HTTPS
SECURE_SSL_REDIRECT = True  # Перенаправлять все HTTP-запросы на HTTPS
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')  # Указывает Django, что запрос пришел по HTTPS

SESSION_COOKIE_SECURE = True  # Отправлять сессионные куки только по HTTPS
CSRF_COOKIE_SECURE = True  # Отправлять CSRF-куки только по HTTPS
USE_X_FORWARDED_HOST = False    # если нужен в сложных сценариях с несколькими прокси или особой маршрутизацией.

# Включает XSS фильтр в браузерах. Добавляет заголовок "X-XSS-Protection: 1; mode=block"
SECURE_BROWSER_XSS_FILTER = True
# Запрещает браузерам "угадывать" MIME-типы. Добавляет заголовок "X-Content-Type-Options: nosniff"
SECURE_CONTENT_TYPE_NOSNIFF = True
# Запрещает встраивание сайта в iframe. Добавляет заголовок "X-Frame-Options: DENY".
X_FRAME_OPTIONS = 'DENY'

# Дополнительные усиления безопасности (HSTS)
SECURE_HSTS_SECONDS = 31536000  # 1 год: предписывает браузеру использовать только HTTPS
SECURE_HSTS_INCLUDE_SUBDOMAINS = True  # Распространяет правило HSTS на все поддомены
SECURE_HSTS_PRELOAD = True  # Позволяет включить домен в предзагрузку HSTS в браузерах

# Блокировка User-Agent сканеров
# Django CommonMiddleware проверяет User-Agent каждого запроса.
# Для проверки используется метод .search() у объектов регулярных выражений.
DISALLOWED_USER_AGENTS = [
    # Целевые парсеры и скрипты
    re.compile(r'^cypex\.ai'),
    re.compile(r'^libredtail-http'),
    # Автоматизированные HTTP-клиенты (могут быть легитимными, будьте осторожны)
    # Временно отключает блокировку легитимных методов
    # re.compile(r'^python-requests'),
    # re.compile(r'^Go-http-client'),
    # re.compile(r'^curl'),
    # Общие шаблоны (используйте, если хотите агрессивную блокировку)
    re.compile(r'scanner', re.IGNORECASE),
    re.compile(r'bot', re.IGNORECASE),
    re.compile(r'crawler', re.IGNORECASE),
    re.compile(r'spider', re.IGNORECASE),
]

# Без CORS браузер блокирует JavaScript запросы между разными доменами
CORS_ALLOW_ALL_ORIGINS = False

# Защита от подделки межсайтовых запросов.
CSRF_TRUSTED_ORIGINS = os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',')
CSRF_TRUSTED_ORIGINS = [host.strip() for host in CSRF_TRUSTED_ORIGINS]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    "django_q",
    'guests',  # наше приложение
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'loyalty_viewer.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'loyalty_viewer.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('PG_NAME', 'programm_loyalty'),
        'USER': os.getenv('PG_USER', 'postgres'),
        'PASSWORD': os.getenv('PG_PASSWORD', '1234'),
        'HOST': os.getenv('PG_HOST', 'db'),
        'PORT': os.getenv('PG_PORT', '5432'),
    },
    'webhooks': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('WH_DB_NAME'),
        'USER': os.getenv('WH_DB_USER'),
        'PASSWORD': os.getenv('WH_DB_PASSWORD'),
        'HOST': os.getenv('WH_DB_HOST'),
        'PORT': os.getenv('WH_DB_PORT', '5432'),
    }

}
Q_CLUSTER = {
    "name": "loyalty_cluster",
    "workers": 1,             # сколько воркеров выполнять задачи
    "recycle": 500,
    "timeout": 59,
    "retry": 180,
    'catch_up': False,
    "compress": True,
    "save_limit": 250,
    "queue_limit": 500,
    "label": "Django Q",
    "django_redis": "default",
    "orm": "default",         # хранить задачи в БД (simple mode)
    "log_level": "INFO",  # Уровень логирования INFO
    "schedule": {
        # Резерв: раз в 10 минут проверяем API на наличие упущенных Уведомлений
        "sync_webhooks_recent": {
            "func": "guests.tasks.fetch_pending_webhooks",
            "minutes": 10,
        },
        # # Ночная глобальная проверка в 22:00 по UTC
        # "nightly_health_check": {
        #     "func": "guests.tasks.nightly_health_check",
        #     "schedule_type": "C",  # Cron-тип расписания
        #     "cron": "0 22 * * *",   # Каждый день в 22:00
        # },
    },
}

# Эти переменные используются для подключения к очереди,
# куда внешний сервис Уведомлений складываем сообщения.
REDIS_QUEUE_URL = os.getenv('REDIS_QUEUE_URL', 'redis://localhost:6379/1')

# Имя очереди с сообщениями из сервиса Уведомлений Webhook
REDIS_QUEUE_NAME = os.getenv('REDIS_QUEUE_NAME', 'webhook_queue')

# Имя специальной очереди для хранения сообщений, которые не удалось обработать после нескольких попыток.
REDIS_DLQ_NAME = os.getenv('REDIS_DLQ_NAME', 'webhook_queue_dlq')

# Количество повторных попыток для обработки одного сообщения из очереди.
try:
    MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))
except ValueError:
    # Если значение не может быть преобразовано в число
    MAX_RETRIES = 3  # значение по умолчанию

# Значение с периодичностью которого будет выполняться логирование Обработка сообщений из очереди Redis.
try:
    ACTIVITY_LOG_INTERVAL = int(os.getenv('ACTIVITY_LOG_INTERVAL', '300'))
except ValueError:
    # Если значение не может быть преобразовано в число
    ACTIVITY_LOG_INTERVAL = 300  # значение по умолчанию

# Значение таймаута для BLPOP (секунды ожидания нового сообщения).
try:
    BLPOP_TIMEOUT = int(os.getenv('BLPOP_TIMEOUT', '2'))
except ValueError:
    # Если значение не может быть преобразовано в число
    BLPOP_TIMEOUT = 2  # значение по умолчанию

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,

    "formatters": {
        "simple": {
            "format": "{levelname} [{asctime}] {message}",
            "style": "{",
        },
        "verbose": {
            "format": "{levelname} [{asctime}] {module} {funcName} {message}",
            "style": "{",
        },
    },

    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },

    "loggers": {
        # Логгер для приложения guests
        "guests": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        # Логгер для Django Q
        "django_q": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },

    "root": {
        "handlers": ["console"],
        "level": "INFO",   # Выводить INFO, WARNING, ERROR
    },
}
LANGUAGE_CODE = 'ru-ru'
# Все даты в БД и логи будут храниться в UTC.
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/loyalty/'
MEDIA_URL = '/media/loyalty/'

STATIC_ROOT = BASE_DIR / 'static'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

#WEBHOOK_SERVICE_URL = "http://webhook-service:8000"

IIKO_API_KEY = os.getenv("IIKO_API_KEY")
IIKO_API_BASE_URL = os.getenv("IIKO_API_BASE_URL")
IIKO_ORGANIZATION_ID = os.getenv("IIKO_ORGANIZATION_ID")