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
def _build_csrf_trusted_origins() -> list[str]:
    """
    Формирует и нормализует список доверенных origins для CSRF.

    Поддерживаются оба варианта в .env:
    1. Полный origin со схемой (`https://example.com`);
    2. Только хост/домен (`example.com`) - в этом случае добавляется `https://`.
    """
    raw_value = os.getenv(
        "CSRF_TRUSTED_ORIGINS",
        "http://localhost,https://localhost,http://127.0.0.1,https://127.0.0.1",
    )
    origins: list[str] = []

    for item in raw_value.split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        if not (origin.startswith("http://") or origin.startswith("https://")):
            origin = f"https://{origin}"
        origins.append(origin)

    return origins


CSRF_TRUSTED_ORIGINS = _build_csrf_trusted_origins()

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

# Настройки новой универсальной очереди (v1).
# По умолчанию использует тот же Redis-инстанс, но отдельный namespace ключей.
UNIVERSAL_QUEUE_REDIS_URL = os.getenv('UNIVERSAL_QUEUE_REDIS_URL', REDIS_QUEUE_URL)
UNIVERSAL_QUEUE_NAMESPACE = os.getenv('UNIVERSAL_QUEUE_NAMESPACE', 'uq:v1')

try:
    UNIVERSAL_DISPATCH_BATCH_SIZE = int(os.getenv('UNIVERSAL_DISPATCH_BATCH_SIZE', '200'))
except ValueError:
    UNIVERSAL_DISPATCH_BATCH_SIZE = 200

try:
    UNIVERSAL_DISPATCH_SLEEP_SECONDS = float(os.getenv('UNIVERSAL_DISPATCH_SLEEP_SECONDS', '2'))
except ValueError:
    UNIVERSAL_DISPATCH_SLEEP_SECONDS = 2.0

# Включение producer-а webhook -> DispatchTask.
# По умолчанию выключено для безопасного поэтапного включения.
UNIVERSAL_QUEUE_ENABLE_WEBHOOK_ENQUEUE = os.getenv(
    "UNIVERSAL_QUEUE_ENABLE_WEBHOOK_ENQUEUE",
    "False",
).strip().lower() in ("1", "true", "yes", "on")

# Список notificationType через запятую.
# Если пусто, producer использует эвристику "есть текст в событии".
UNIVERSAL_QUEUE_WEBHOOK_NOTIFY_TYPES = os.getenv("UNIVERSAL_QUEUE_WEBHOOK_NOTIFY_TYPES", "")

# Приоритет задач, создаваемых из webhook producer-а.
UNIVERSAL_QUEUE_WEBHOOK_PRIORITY = os.getenv("UNIVERSAL_QUEUE_WEBHOOK_PRIORITY", "high")

# Режим маршрутизации: True -> только основной бот гостя.
UNIVERSAL_QUEUE_WEBHOOK_PRIMARY_ONLY = os.getenv(
    "UNIVERSAL_QUEUE_WEBHOOK_PRIMARY_ONLY",
    "True",
).strip().lower() in ("1", "true", "yes", "on")

# Fallback на legacy GuestChannelLink (Telegram), если новых привязок ещё нет.
UNIVERSAL_QUEUE_FALLBACK_OLD_TG_LINKS = os.getenv(
    "UNIVERSAL_QUEUE_FALLBACK_OLD_TG_LINKS",
    "True",
).strip().lower() in ("1", "true", "yes", "on")

# Включение F4: массовая рассылка ставит задачи в DispatchTask (через producer),
# а не отправляет сообщения напрямую из mailing_worker.
UNIVERSAL_QUEUE_ENABLE_MAILING_DISPATCH = os.getenv(
    "UNIVERSAL_QUEUE_ENABLE_MAILING_DISPATCH",
    "False",
).strip().lower() in ("1", "true", "yes", "on")

# Режим целевых каналов для массовой рассылки:
# 1) primary_only - только основной бот гостя (по умолчанию);
# 2) all_bots - все активные привязки гостя.
UNIVERSAL_QUEUE_MAILING_TARGET_MODE = os.getenv(
    "UNIVERSAL_QUEUE_MAILING_TARGET_MODE",
    "primary_only",
).strip().lower()

# Приоритет задач, создаваемых из MailingGuest.
UNIVERSAL_QUEUE_MAILING_PRIORITY = os.getenv(
    "UNIVERSAL_QUEUE_MAILING_PRIORITY",
    "bulk",
).strip().lower()

# Fallback на legacy GuestChannelLink (Telegram) при отсутствии новых привязок.
UNIVERSAL_QUEUE_MAILING_FALLBACK_OLD_TG_LINKS = os.getenv(
    "UNIVERSAL_QUEUE_MAILING_FALLBACK_OLD_TG_LINKS",
    "True",
).strip().lower() in ("1", "true", "yes", "on")

# Настройки async provider-worker (F5).
try:
    UNIVERSAL_PROVIDER_BLOCK_TIMEOUT_SECONDS = int(os.getenv("UNIVERSAL_PROVIDER_BLOCK_TIMEOUT_SECONDS", "2"))
except ValueError:
    UNIVERSAL_PROVIDER_BLOCK_TIMEOUT_SECONDS = 2

try:
    UNIVERSAL_PROVIDER_IDLE_SLEEP_SECONDS = float(os.getenv("UNIVERSAL_PROVIDER_IDLE_SLEEP_SECONDS", "0.2"))
except ValueError:
    UNIVERSAL_PROVIDER_IDLE_SLEEP_SECONDS = 0.2

try:
    UNIVERSAL_PROVIDER_RETRY_BASE_SECONDS = float(os.getenv("UNIVERSAL_PROVIDER_RETRY_BASE_SECONDS", "3"))
except ValueError:
    UNIVERSAL_PROVIDER_RETRY_BASE_SECONDS = 3.0

try:
    UNIVERSAL_PROVIDER_RETRY_MAX_SECONDS = float(os.getenv("UNIVERSAL_PROVIDER_RETRY_MAX_SECONDS", "300"))
except ValueError:
    UNIVERSAL_PROVIDER_RETRY_MAX_SECONDS = 300.0

try:
    UNIVERSAL_FAIR_HIGH = int(os.getenv("UNIVERSAL_FAIR_HIGH", "10"))
except ValueError:
    UNIVERSAL_FAIR_HIGH = 10

try:
    UNIVERSAL_FAIR_NORMAL = int(os.getenv("UNIVERSAL_FAIR_NORMAL", "3"))
except ValueError:
    UNIVERSAL_FAIR_NORMAL = 3

try:
    UNIVERSAL_FAIR_BULK = int(os.getenv("UNIVERSAL_FAIR_BULK", "1"))
except ValueError:
    UNIVERSAL_FAIR_BULK = 1

# Централизованные лимиты отправки (сообщений в секунду) для Redis rate limiter.
try:
    UNIVERSAL_RATE_LIMIT_TELEGRAM_PER_SECOND = float(os.getenv("UNIVERSAL_RATE_LIMIT_TELEGRAM_PER_SECOND", "28"))
except ValueError:
    UNIVERSAL_RATE_LIMIT_TELEGRAM_PER_SECOND = 28.0

try:
    UNIVERSAL_RATE_LIMIT_MAX_PER_SECOND = float(os.getenv("UNIVERSAL_RATE_LIMIT_MAX_PER_SECOND", "20"))
except ValueError:
    UNIVERSAL_RATE_LIMIT_MAX_PER_SECOND = 20.0

try:
    UNIVERSAL_RATE_LIMIT_VK_PER_SECOND = float(os.getenv("UNIVERSAL_RATE_LIMIT_VK_PER_SECOND", "20"))
except ValueError:
    UNIVERSAL_RATE_LIMIT_VK_PER_SECOND = 20.0

try:
    UNIVERSAL_PROVIDER_HTTP_TIMEOUT = float(os.getenv("UNIVERSAL_PROVIDER_HTTP_TIMEOUT", "20"))
except ValueError:
    UNIVERSAL_PROVIDER_HTTP_TIMEOUT = 20.0

# Базовые URL и параметры API провайдеров.
TELEGRAM_API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org").strip()
MAX_API_BASE_URL = os.getenv("MAX_API_BASE_URL", "https://platform-api.max.ru").strip()
MAX_API_AUTH_PREFIX = os.getenv("MAX_API_AUTH_PREFIX", "").strip()
VK_API_BASE_URL = os.getenv("VK_API_BASE_URL", "https://api.vk.com/method").strip()
VK_API_VERSION = os.getenv("VK_API_VERSION", "5.199").strip()

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

USE_TZ = True
TIME_ZONE = "Asia/Yekaterinburg"
#USE_I18N = True

STATIC_URL = '/static/loyalty/'
MEDIA_URL = '/media/loyalty/'

STATIC_ROOT = BASE_DIR / 'static'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

#WEBHOOK_SERVICE_URL = "http://webhook-service:8000"

IIKO_API_KEY = os.getenv("IIKO_API_KEY")
IIKO_API_BASE_URL = os.getenv("IIKO_API_BASE_URL")
IIKO_ORGANIZATION_ID = os.getenv("IIKO_ORGANIZATION_ID")
