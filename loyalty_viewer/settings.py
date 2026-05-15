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
        "run_notification_scenarios": {
            "func": "guests.tasks.run_scheduled_notification_scenarios_task",
            "minutes": 30,
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

# Настройки monitor-процесса universal queue (F6).
try:
    UNIVERSAL_MONITOR_INTERVAL_SECONDS = float(os.getenv("UNIVERSAL_MONITOR_INTERVAL_SECONDS", "60"))
except ValueError:
    UNIVERSAL_MONITOR_INTERVAL_SECONDS = 60.0

try:
    UNIVERSAL_STALE_QUEUED_SECONDS = int(os.getenv("UNIVERSAL_STALE_QUEUED_SECONDS", "180"))
except ValueError:
    UNIVERSAL_STALE_QUEUED_SECONDS = 180

try:
    UNIVERSAL_STALE_IN_PROGRESS_SECONDS = int(os.getenv("UNIVERSAL_STALE_IN_PROGRESS_SECONDS", "600"))
except ValueError:
    UNIVERSAL_STALE_IN_PROGRESS_SECONDS = 600

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

SAGUR_BASE_URL = os.getenv("SAGUR_BASE_URL", "").strip()
SAGUR_USERNAME = os.getenv("SAGUR_USERNAME", "").strip()
SAGUR_PASSWORD = os.getenv("SAGUR_PASSWORD", "").strip()

IIKO_API_KEY = os.getenv("IIKO_API_KEY")
IIKO_API_BASE_URL = os.getenv("IIKO_API_BASE_URL")
IIKO_ORGANIZATION_ID = os.getenv("IIKO_ORGANIZATION_ID")
IIKO_OLAP_BASE_URL = os.getenv("IIKO_OLAP_BASE_URL", "").strip()
IIKO_OLAP_LOGIN = os.getenv("IIKO_OLAP_LOGIN", "").strip()
IIKO_OLAP_PASS_HASH = os.getenv("IIKO_OLAP_PASS_HASH", "").strip()

try:
    IIKO_OLAP_AUTH_TIMEOUT_SECONDS = float(os.getenv("IIKO_OLAP_AUTH_TIMEOUT_SECONDS", "10"))
except ValueError:
    IIKO_OLAP_AUTH_TIMEOUT_SECONDS = 10.0

try:
    IIKO_OLAP_REQUEST_TIMEOUT_SECONDS = float(os.getenv("IIKO_OLAP_REQUEST_TIMEOUT_SECONDS", "30"))
except ValueError:
    IIKO_OLAP_REQUEST_TIMEOUT_SECONDS = 30.0

try:
    IIKO_OLAP_KEY_TTL_SECONDS = int(os.getenv("IIKO_OLAP_KEY_TTL_SECONDS", "240"))
except ValueError:
    IIKO_OLAP_KEY_TTL_SECONDS = 240

try:
    IIKO_OLAP_MAX_RETRIES = int(os.getenv("IIKO_OLAP_MAX_RETRIES", "3"))
except ValueError:
    IIKO_OLAP_MAX_RETRIES = 3

try:
    IIKO_OLAP_RETRY_BASE_SECONDS = float(os.getenv("IIKO_OLAP_RETRY_BASE_SECONDS", "1"))
except ValueError:
    IIKO_OLAP_RETRY_BASE_SECONDS = 1.0

try:
    IIKO_OLAP_PORTION_SIZE = int(os.getenv("IIKO_OLAP_PORTION_SIZE", "200"))
except ValueError:
    IIKO_OLAP_PORTION_SIZE = 200


def _env_bool(name: str, default: bool = False) -> bool:
    """
    Безопасно читает bool-переменную окружения.

    Поддерживаемые значения True: 1, true, yes, on.
    """
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    """
    Безопасно читает целое число из env с опциональной нижней границей.
    """
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)

    if min_value is not None:
        value = max(int(min_value), value)
    return value


def _env_int_set(name: str, default_csv: str) -> set[int]:
    """
    Читает CSV-список целых чисел из env в `set[int]`.

    Если значение пустое или содержит ошибки, возвращается набор по умолчанию.
    """
    raw_value = str(os.getenv(name, default_csv) or "").strip()
    parsed_values: set[int] = set()

    for item in raw_value.split(","):
        token = item.strip()
        if not token:
            continue
        try:
            parsed_values.add(int(token))
        except ValueError:
            continue

    if parsed_values:
        return parsed_values

    fallback_values: set[int] = set()
    for item in default_csv.split(","):
        token = item.strip()
        if not token:
            continue
        try:
            fallback_values.add(int(token))
        except ValueError:
            continue
    return fallback_values or {1}


def _env_text_set(name: str, default_csv: str = "") -> set[str]:
    """
    Читает CSV-список строк из env в `set[str]`.

    Пустые значения и дубликаты отбрасываются.
    """
    raw_value = str(os.getenv(name, default_csv) or "").strip()
    parsed_values: set[str] = set()

    for item in raw_value.split(","):
        token = item.strip()
        if not token:
            continue
        parsed_values.add(token)

    if parsed_values:
        return parsed_values

    fallback_values: set[str] = set()
    for item in str(default_csv or "").split(","):
        token = item.strip()
        if not token:
            continue
        fallback_values.add(token)
    return fallback_values


# Интеграция SAGUR <- vtelemax (read-only recipients API).
VTELEMAX_SYNC_ENABLED = _env_bool("VTELEMAX_SYNC_ENABLED", False)
VTELEMAX_SYNC_BASE_URL = str(os.getenv("VTELEMAX_SYNC_BASE_URL", "") or "").strip()
VTELEMAX_SYNC_HMAC_SECRET = str(os.getenv("VTELEMAX_SYNC_HMAC_SECRET", "") or "").strip()
VTELEMAX_SYNC_REQUIRE_HTTPS = _env_bool("VTELEMAX_SYNC_REQUIRE_HTTPS", True)
try:
    VTELEMAX_SYNC_HTTP_TIMEOUT_SECONDS = float(
        os.getenv("VTELEMAX_SYNC_HTTP_TIMEOUT_SECONDS", "20") or "20"
    )
except ValueError:
    VTELEMAX_SYNC_HTTP_TIMEOUT_SECONDS = 20.0
VTELEMAX_SYNC_DEFAULT_LIMIT = _env_int("VTELEMAX_SYNC_DEFAULT_LIMIT", 1000, min_value=1)
VTELEMAX_SYNC_MAX_LIMIT = _env_int("VTELEMAX_SYNC_MAX_LIMIT", 5000, min_value=1)
if VTELEMAX_SYNC_MAX_LIMIT < VTELEMAX_SYNC_DEFAULT_LIMIT:
    VTELEMAX_SYNC_MAX_LIMIT = VTELEMAX_SYNC_DEFAULT_LIMIT

VTELEMAX_SYNC_CREATE_MISSING_GUESTS = _env_bool("VTELEMAX_SYNC_CREATE_MISSING_GUESTS", False)

VTELEMAX_SYNC_BOT_CODE_TELEGRAM = str(
    os.getenv("VTELEMAX_SYNC_BOT_CODE_TELEGRAM", "") or ""
).strip()
VTELEMAX_SYNC_BOT_CODE_MAX = str(os.getenv("VTELEMAX_SYNC_BOT_CODE_MAX", "") or "").strip()
VTELEMAX_SYNC_BOT_CODE_VK = str(os.getenv("VTELEMAX_SYNC_BOT_CODE_VK", "") or "").strip()

VTELEMAX_SYNC_SCHEDULE_ENABLED = _env_bool("VTELEMAX_SYNC_SCHEDULE_ENABLED", False)
VTELEMAX_SYNC_SCHEDULE_MINUTES = _env_int("VTELEMAX_SYNC_SCHEDULE_MINUTES", 5, min_value=1)

# Очередь доставки купонных событий SAGUR -> vtelemax.
VTELEMAX_COUPON_SYNC_ENABLED = _env_bool("VTELEMAX_COUPON_SYNC_ENABLED", False)
VTELEMAX_COUPON_SYNC_BASE_URL = str(
    os.getenv("VTELEMAX_COUPON_SYNC_BASE_URL", "") or ""
).strip() or VTELEMAX_SYNC_BASE_URL
VTELEMAX_COUPON_SYNC_HMAC_SECRET = str(
    os.getenv("VTELEMAX_COUPON_SYNC_HMAC_SECRET", "") or ""
).strip() or VTELEMAX_SYNC_HMAC_SECRET
VTELEMAX_COUPON_SYNC_REQUIRE_HTTPS = _env_bool("VTELEMAX_COUPON_SYNC_REQUIRE_HTTPS", True)
VTELEMAX_COUPON_SYNC_ENDPOINT = str(
    os.getenv("VTELEMAX_COUPON_SYNC_ENDPOINT", "/internal/integration/v1/sagur/coupons/events")
    or "/internal/integration/v1/sagur/coupons/events"
).strip()
try:
    VTELEMAX_COUPON_SYNC_HTTP_TIMEOUT_SECONDS = float(
        os.getenv("VTELEMAX_COUPON_SYNC_HTTP_TIMEOUT_SECONDS", "20") or "20"
    )
except ValueError:
    VTELEMAX_COUPON_SYNC_HTTP_TIMEOUT_SECONDS = 20.0
VTELEMAX_COUPON_SYNC_MAX_ATTEMPTS = _env_int("VTELEMAX_COUPON_SYNC_MAX_ATTEMPTS", 8, min_value=1)
VTELEMAX_COUPON_SYNC_RETRY_BASE_SECONDS = _env_int(
    "VTELEMAX_COUPON_SYNC_RETRY_BASE_SECONDS",
    30,
    min_value=1,
)
VTELEMAX_COUPON_SYNC_RETRY_MAX_SECONDS = _env_int(
    "VTELEMAX_COUPON_SYNC_RETRY_MAX_SECONDS",
    3600,
    min_value=1,
)
if VTELEMAX_COUPON_SYNC_RETRY_MAX_SECONDS < VTELEMAX_COUPON_SYNC_RETRY_BASE_SECONDS:
    VTELEMAX_COUPON_SYNC_RETRY_MAX_SECONDS = VTELEMAX_COUPON_SYNC_RETRY_BASE_SECONDS

VTELEMAX_COUPON_SYNC_BATCH_SIZE = _env_int("VTELEMAX_COUPON_SYNC_BATCH_SIZE", 100, min_value=1)
try:
    VTELEMAX_COUPON_SYNC_LOOP_SLEEP_SECONDS = float(
        os.getenv("VTELEMAX_COUPON_SYNC_LOOP_SLEEP_SECONDS", "5") or "5"
    )
except ValueError:
    VTELEMAX_COUPON_SYNC_LOOP_SLEEP_SECONDS = 5.0
if VTELEMAX_COUPON_SYNC_LOOP_SLEEP_SECONDS < 0.1:
    VTELEMAX_COUPON_SYNC_LOOP_SLEEP_SECONDS = 0.1
VTELEMAX_COUPON_SYNC_SCHEDULE_ENABLED = _env_bool(
    "VTELEMAX_COUPON_SYNC_SCHEDULE_ENABLED",
    False,
)
VTELEMAX_COUPON_SYNC_SCHEDULE_MINUTES = _env_int(
    "VTELEMAX_COUPON_SYNC_SCHEDULE_MINUTES",
    1,
    min_value=1,
)

# Pre-send gate для купонных кампаний.
VTELEMAX_COUPON_SYNC_GATE_REQUIRE_FRESH_STATE = _env_bool(
    "VTELEMAX_COUPON_SYNC_GATE_REQUIRE_FRESH_STATE",
    True,
)
VTELEMAX_COUPON_SYNC_GATE_MAX_SYNC_AGE_MINUTES = _env_int(
    "VTELEMAX_COUPON_SYNC_GATE_MAX_SYNC_AGE_MINUTES",
    120,
    min_value=1,
)

# Автосинхронизация статусов купонов после обновления `order_fact`.
# Если включено, плановая задача `run_order_fact_scheduled_task` сразу после пересчёта
# запускает `sync_coupon_redemptions` на том же date-range.
COUPON_REDEMPTION_SYNC_ENABLED = _env_bool("COUPON_REDEMPTION_SYNC_ENABLED", True)
COUPON_REDEMPTION_SYNC_LIMIT = _env_int("COUPON_REDEMPTION_SYNC_LIMIT", 0, min_value=0)


# Управление отправкой balance-уведомлений в ботов из webhook-контура.
# Позволяет включать/выключать создание DispatchTask без изменений кода.
# Автосинхронизация `settings.Q_CLUSTER["schedule"]` -> `django_q_schedule`.
# Срабатывает на старте `manage.py qcluster` (см. guests.apps.GuestsConfig.ready).
DJANGO_Q_SCHEDULE_AUTOSYNC_ON_QCLUSTER_START = _env_bool(
    "DJANGO_Q_SCHEDULE_AUTOSYNC_ON_QCLUSTER_START",
    True,
)
DJANGO_Q_SCHEDULE_AUTOSYNC_PRUNE_STALE = _env_bool(
    "DJANGO_Q_SCHEDULE_AUTOSYNC_PRUNE_STALE",
    True,
)
# Дополнительные managed-имена расписаний (CSV), которые нужно удалять при stale-prune.
DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES = _env_text_set(
    "DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES",
    "",
)
# Базовый набор managed-имен расписаний проекта.
DJANGO_Q_SCHEDULE_MANAGED_NAMES = (
    "sync_webhooks_recent",
    "run_notification_scenarios",
    "run_vtelemax_recipients_delta",
    "run_vtelemax_coupon_sync_queue",
    "run_olap_sync_windowed",
    "run_olap_rebuild_nightly",
    "run_order_fact_tail",
    "run_daily_fact_tail",
    "run_daily_order_fact_tail",
    "run_order_focus_fact_tail",
    "run_window_metrics_hourly",
    "run_window_category_metrics_hourly",
    "run_olap_control_pull_daily",
)

BALANCE_WEBHOOK_NOTIFY_ENABLED = _env_bool(
    "BALANCE_WEBHOOK_NOTIFY_ENABLED",
    True,
)


# Live-мост webhook -> olap_check_sync_journal.
# По умолчанию выключен, чтобы безопасно выкатывать функционал по флагу.
OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE = _env_bool(
    "OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE",
    False,
)
# Разрешённые notificationType для live-моста (CSV, например: "1,9").
OLAP_BRIDGE_ALLOWED_NOTIFICATION_TYPES = _env_int_set(
    "OLAP_BRIDGE_ALLOWED_NOTIFICATION_TYPES",
    "1",
)

# Исторический прогон webhook -> olap_check_sync_journal.
OLAP_BACKFILL_ENABLE = _env_bool("OLAP_BACKFILL_ENABLE", False)
OLAP_BACKFILL_DRY_RUN = _env_bool("OLAP_BACKFILL_DRY_RUN", True)
OLAP_BACKFILL_DATE_FROM = str(
    os.getenv("OLAP_BACKFILL_DATE_FROM", "2025-12-01T00:00:00Z") or ""
).strip()
OLAP_BACKFILL_DATE_TO = str(os.getenv("OLAP_BACKFILL_DATE_TO", "") or "").strip() or None

try:
    OLAP_BACKFILL_PAGE_SIZE = int(os.getenv("OLAP_BACKFILL_PAGE_SIZE", "100"))
except ValueError:
    OLAP_BACKFILL_PAGE_SIZE = 100

try:
    OLAP_BACKFILL_MAX_PAGES_PER_CYCLE = int(
        os.getenv("OLAP_BACKFILL_MAX_PAGES_PER_CYCLE", "5")
    )
except ValueError:
    OLAP_BACKFILL_MAX_PAGES_PER_CYCLE = 5

try:
    OLAP_BACKFILL_SLEEP_BETWEEN_PAGES_SECONDS = float(
        os.getenv("OLAP_BACKFILL_SLEEP_BETWEEN_PAGES_SECONDS", "1")
    )
except ValueError:
    OLAP_BACKFILL_SLEEP_BETWEEN_PAGES_SECONDS = 1.0

try:
    OLAP_BACKFILL_SLEEP_BETWEEN_CYCLES_SECONDS = float(
        os.getenv("OLAP_BACKFILL_SLEEP_BETWEEN_CYCLES_SECONDS", "20")
    )
except ValueError:
    OLAP_BACKFILL_SLEEP_BETWEEN_CYCLES_SECONDS = 20.0

try:
    OLAP_BACKFILL_PAUSE_QUEUE_GT = int(os.getenv("OLAP_BACKFILL_PAUSE_QUEUE_GT", "5000"))
except ValueError:
    OLAP_BACKFILL_PAUSE_QUEUE_GT = 5000

try:
    OLAP_BACKFILL_RESUME_QUEUE_LT = int(os.getenv("OLAP_BACKFILL_RESUME_QUEUE_LT", "2000"))
except ValueError:
    OLAP_BACKFILL_RESUME_QUEUE_LT = 2000

try:
    OLAP_BACKFILL_AUTH_TIMEOUT_SECONDS = float(
        os.getenv("OLAP_BACKFILL_AUTH_TIMEOUT_SECONDS", "10")
    )
except ValueError:
    OLAP_BACKFILL_AUTH_TIMEOUT_SECONDS = 10.0

try:
    OLAP_BACKFILL_REQUEST_TIMEOUT_SECONDS = float(
        os.getenv("OLAP_BACKFILL_REQUEST_TIMEOUT_SECONDS", "20")
    )
except ValueError:
    OLAP_BACKFILL_REQUEST_TIMEOUT_SECONDS = 20.0

# Плановый OLAP sync (one-shot) через Django Q.
OLAP_SYNC_SCHEDULE_ENABLED = _env_bool("OLAP_SYNC_SCHEDULE_ENABLED", False)
OLAP_SYNC_SCHEDULE_MINUTES = _env_int(
    "OLAP_SYNC_SCHEDULE_MINUTES",
    30,
    min_value=1,
)
OLAP_SYNC_SCHEDULE_CRON = str(
    os.getenv("OLAP_SYNC_SCHEDULE_CRON", "5 11-23,0 * * *") or "5 11-23,0 * * *"
).strip()
OLAP_SYNC_WINDOW_START_LOCAL = str(
    os.getenv("OLAP_SYNC_WINDOW_START_LOCAL", "12:00") or "12:00"
).strip()
OLAP_SYNC_WINDOW_END_LOCAL = str(
    os.getenv("OLAP_SYNC_WINDOW_END_LOCAL", "01:00") or "01:00"
).strip()
OLAP_SYNC_SCHEDULE_CLAIM_LIMIT = _env_int(
    "OLAP_SYNC_SCHEDULE_CLAIM_LIMIT",
    100,
    min_value=1,
)
OLAP_SYNC_SCHEDULE_PORTION_SIZE = _env_int(
    "OLAP_SYNC_SCHEDULE_PORTION_SIZE",
    50,
    min_value=1,
)
OLAP_SYNC_SCHEDULE_MAX_ATTEMPTS = _env_int(
    "OLAP_SYNC_SCHEDULE_MAX_ATTEMPTS",
    5,
    min_value=1,
)
OLAP_SYNC_SCHEDULE_RETRY_BASE_SECONDS = _env_int(
    "OLAP_SYNC_SCHEDULE_RETRY_BASE_SECONDS",
    120,
    min_value=1,
)
OLAP_SYNC_SCHEDULE_LOCK_TIMEOUT_SECONDS = _env_int(
    "OLAP_SYNC_SCHEDULE_LOCK_TIMEOUT_SECONDS",
    900,
    min_value=60,
)

# Плановый пересчет витрин OLAP (one-shot) через Django Q.
OLAP_REBUILD_SCHEDULE_ENABLED = _env_bool("OLAP_REBUILD_SCHEDULE_ENABLED", False)
OLAP_REBUILD_SCHEDULE_CRON = str(
    os.getenv("OLAP_REBUILD_SCHEDULE_CRON", "30 2 * * *") or "30 2 * * *"
).strip()
OLAP_REBUILD_SCHEDULE_CONTINUE_ON_STEP_ERROR = _env_bool(
    "OLAP_REBUILD_SCHEDULE_CONTINUE_ON_STEP_ERROR",
    True,
)
OLAP_REBUILD_SCHEDULE_BATCH_SIZE = _env_int(
    "OLAP_REBUILD_SCHEDULE_BATCH_SIZE",
    2000,
    min_value=100,
)
OLAP_REBUILD_SCHEDULE_WINDOW_DAYS = str(
    os.getenv("OLAP_REBUILD_SCHEDULE_WINDOW_DAYS", "7,14,30,60,180") or "7,14,30,60,180"
).strip()
OLAP_REBUILD_SCHEDULE_USE_TODAY_AS_OF_DATE = _env_bool(
    "OLAP_REBUILD_SCHEDULE_USE_TODAY_AS_OF_DATE",
    True,
)
OLAP_REBUILD_SCHEDULE_DEPARTMENT_ID = str(
    os.getenv("OLAP_REBUILD_SCHEDULE_DEPARTMENT_ID", "") or ""
).strip()

# Плановый инкрементальный пересчёт order_fact (tail window).
OLAP_ORDER_FACT_SCHEDULE_ENABLED = _env_bool("OLAP_ORDER_FACT_SCHEDULE_ENABLED", False)
OLAP_ORDER_FACT_SCHEDULE_MINUTES = _env_int(
    "OLAP_ORDER_FACT_SCHEDULE_MINUTES",
    30,
    min_value=1,
)
OLAP_ORDER_FACT_SCHEDULE_CRON = str(
    os.getenv("OLAP_ORDER_FACT_SCHEDULE_CRON", "15 11-23,0 * * *") or "15 11-23,0 * * *"
).strip()
OLAP_ORDER_FACT_SCHEDULE_TAIL_DAYS = _env_int(
    "OLAP_ORDER_FACT_SCHEDULE_TAIL_DAYS",
    3,
    min_value=1,
)
OLAP_ORDER_FACT_SCHEDULE_END_LAG_DAYS = _env_int(
    "OLAP_ORDER_FACT_SCHEDULE_END_LAG_DAYS",
    0,
    min_value=0,
)
OLAP_ORDER_FACT_SCHEDULE_BATCH_SIZE = _env_int(
    "OLAP_ORDER_FACT_SCHEDULE_BATCH_SIZE",
    2000,
    min_value=100,
)

# Плановый инкрементальный пересчёт daily_category_fact (tail window).
OLAP_DAILY_FACT_SCHEDULE_ENABLED = _env_bool("OLAP_DAILY_FACT_SCHEDULE_ENABLED", False)
OLAP_DAILY_FACT_SCHEDULE_MINUTES = _env_int(
    "OLAP_DAILY_FACT_SCHEDULE_MINUTES",
    60,
    min_value=1,
)
OLAP_DAILY_FACT_SCHEDULE_TAIL_DAYS = _env_int(
    "OLAP_DAILY_FACT_SCHEDULE_TAIL_DAYS",
    3,
    min_value=1,
)
OLAP_DAILY_FACT_SCHEDULE_END_LAG_DAYS = _env_int(
    "OLAP_DAILY_FACT_SCHEDULE_END_LAG_DAYS",
    0,
    min_value=0,
)
OLAP_DAILY_FACT_SCHEDULE_BATCH_SIZE = _env_int(
    "OLAP_DAILY_FACT_SCHEDULE_BATCH_SIZE",
    2000,
    min_value=100,
)

# Плановый инкрементальный пересчёт daily_order_fact (tail window).
OLAP_DAILY_ORDER_FACT_SCHEDULE_ENABLED = _env_bool("OLAP_DAILY_ORDER_FACT_SCHEDULE_ENABLED", False)
OLAP_DAILY_ORDER_FACT_SCHEDULE_MINUTES = _env_int(
    "OLAP_DAILY_ORDER_FACT_SCHEDULE_MINUTES",
    60,
    min_value=1,
)
OLAP_DAILY_ORDER_FACT_SCHEDULE_CRON = str(
    os.getenv("OLAP_DAILY_ORDER_FACT_SCHEDULE_CRON", "25 11-23,0 * * *") or "25 11-23,0 * * *"
).strip()
OLAP_DAILY_ORDER_FACT_SCHEDULE_TAIL_DAYS = _env_int(
    "OLAP_DAILY_ORDER_FACT_SCHEDULE_TAIL_DAYS",
    3,
    min_value=1,
)
OLAP_DAILY_ORDER_FACT_SCHEDULE_END_LAG_DAYS = _env_int(
    "OLAP_DAILY_ORDER_FACT_SCHEDULE_END_LAG_DAYS",
    0,
    min_value=0,
)
OLAP_DAILY_ORDER_FACT_SCHEDULE_BATCH_SIZE = _env_int(
    "OLAP_DAILY_ORDER_FACT_SCHEDULE_BATCH_SIZE",
    2000,
    min_value=100,
)
OLAP_DAILY_ORDER_FACT_SCHEDULE_DEPARTMENT_ID = str(
    os.getenv("OLAP_DAILY_ORDER_FACT_SCHEDULE_DEPARTMENT_ID", "") or ""
).strip()

# Плановый инкрементальный пересчёт order_focus_fact (tail window).
OLAP_ORDER_FOCUS_FACT_SCHEDULE_ENABLED = _env_bool("OLAP_ORDER_FOCUS_FACT_SCHEDULE_ENABLED", False)
OLAP_ORDER_FOCUS_FACT_SCHEDULE_MINUTES = _env_int(
    "OLAP_ORDER_FOCUS_FACT_SCHEDULE_MINUTES",
    60,
    min_value=1,
)
OLAP_ORDER_FOCUS_FACT_SCHEDULE_TAIL_DAYS = _env_int(
    "OLAP_ORDER_FOCUS_FACT_SCHEDULE_TAIL_DAYS",
    3,
    min_value=1,
)
OLAP_ORDER_FOCUS_FACT_SCHEDULE_END_LAG_DAYS = _env_int(
    "OLAP_ORDER_FOCUS_FACT_SCHEDULE_END_LAG_DAYS",
    0,
    min_value=0,
)
OLAP_ORDER_FOCUS_FACT_SCHEDULE_BATCH_SIZE = _env_int(
    "OLAP_ORDER_FOCUS_FACT_SCHEDULE_BATCH_SIZE",
    2000,
    min_value=100,
)
OLAP_ORDER_FOCUS_FACT_SCHEDULE_DEPARTMENT_ID = str(
    os.getenv("OLAP_ORDER_FOCUS_FACT_SCHEDULE_DEPARTMENT_ID", "") or ""
).strip()

# Плановый пересчёт оконных метрик.
OLAP_WINDOW_METRICS_SCHEDULE_ENABLED = _env_bool(
    "OLAP_WINDOW_METRICS_SCHEDULE_ENABLED",
    False,
)
OLAP_WINDOW_METRICS_SCHEDULE_MINUTES = _env_int(
    "OLAP_WINDOW_METRICS_SCHEDULE_MINUTES",
    60,
    min_value=1,
)
OLAP_WINDOW_METRICS_SCHEDULE_CRON = str(
    os.getenv("OLAP_WINDOW_METRICS_SCHEDULE_CRON", "35 11-23,0 * * *") or "35 11-23,0 * * *"
).strip()
OLAP_WINDOW_METRICS_SCHEDULE_AS_OF_LAG_DAYS = _env_int(
    "OLAP_WINDOW_METRICS_SCHEDULE_AS_OF_LAG_DAYS",
    0,
    min_value=0,
)
OLAP_WINDOW_METRICS_SCHEDULE_BATCH_SIZE = _env_int(
    "OLAP_WINDOW_METRICS_SCHEDULE_BATCH_SIZE",
    2000,
    min_value=100,
)
OLAP_WINDOW_METRICS_SCHEDULE_WINDOW_DAYS = str(
    os.getenv("OLAP_WINDOW_METRICS_SCHEDULE_WINDOW_DAYS", "7,14,30,60,180")
    or "7,14,30,60,180"
).strip()
OLAP_WINDOW_METRICS_SCHEDULE_DEPARTMENT_ID = str(
    os.getenv("OLAP_WINDOW_METRICS_SCHEDULE_DEPARTMENT_ID", "") or ""
).strip()

# Флаг режима category-window метрик в guests/workbench.
WORKBENCH_CATEGORY_WINDOW_METRICS_V2 = _env_bool(
    "WORKBENCH_CATEGORY_WINDOW_METRICS_V2",
    False,
)

# Плановый пересчёт category-window метрик.
OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_ENABLED = _env_bool(
    "OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_ENABLED",
    False,
)
OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_MINUTES = _env_int(
    "OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_MINUTES",
    60,
    min_value=1,
)
OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_AS_OF_LAG_DAYS = _env_int(
    "OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_AS_OF_LAG_DAYS",
    0,
    min_value=0,
)
OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_BATCH_SIZE = _env_int(
    "OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_BATCH_SIZE",
    2000,
    min_value=100,
)
OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_WINDOW_DAYS = str(
    os.getenv("OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_WINDOW_DAYS", "7,14,30,60,180")
    or "7,14,30,60,180"
).strip()
OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_DEPARTMENT_ID = str(
    os.getenv("OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_DEPARTMENT_ID", "") or ""
).strip()

# Плановый контрольный pull из OLAP (по Department.Id и диапазону business_date).
OLAP_CONTROL_PULL_SCHEDULE_ENABLED = _env_bool(
    "OLAP_CONTROL_PULL_SCHEDULE_ENABLED",
    False,
)
OLAP_CONTROL_PULL_SCHEDULE_CRON = str(
    os.getenv("OLAP_CONTROL_PULL_SCHEDULE_CRON", "0 6 * * *") or "0 6 * * *"
).strip()
OLAP_CONTROL_PULL_SCHEDULE_TAIL_DAYS = _env_int(
    "OLAP_CONTROL_PULL_SCHEDULE_TAIL_DAYS",
    2,
    min_value=1,
)
OLAP_CONTROL_PULL_SCHEDULE_DRY_RUN = _env_bool(
    "OLAP_CONTROL_PULL_SCHEDULE_DRY_RUN",
    False,
)
OLAP_CONTROL_PULL_SCHEDULE_DEPARTMENT_IDS = str(
    os.getenv("OLAP_CONTROL_PULL_SCHEDULE_DEPARTMENT_IDS", "") or ""
).strip()
# CSV-список телефонов, которые нужно игнорировать в control pull (например, номера агрегаторов доставки).
OLAP_CONTROL_PULL_PHONE_DENYLIST = _env_text_set(
    "OLAP_CONTROL_PULL_PHONE_DENYLIST",
    "",
)

# Единый режим расписания OLAP-витрин: почасовые волны cron (последовательные шаги).
# По умолчанию включён, чтобы избежать дрейфа интервалов 30/31/57/60 минут.
OLAP_SCHEDULE_USE_HOURLY_CRON_WAVES = _env_bool(
    "OLAP_SCHEDULE_USE_HOURLY_CRON_WAVES",
    True,
)


def _register_olap_schedule_tasks() -> None:
    """
    Регистрирует OLAP-задачи в Django Q по env-флагам.

    Это позволяет включать/выключать OLAP-контур без правки кода:
    1. sync-задача раз в N минут (legacy) или по cron-волнам;
    2. rebuild-задача по cron-расписанию;
    3. order_fact tail-задача по минутному расписанию (legacy) или по cron-волнам;
    4. daily_fact tail-задача по минутному расписанию;
    5. daily_order_fact tail-задача по минутному расписанию (legacy) или по cron-волнам;
    6. order_focus_fact tail-задача по минутному расписанию;
    7. window_metrics-задача по минутному расписанию (legacy) или по cron-волнам;
    8. control_pull-задача по cron (контрольная постановка пропущенных задач в journal).
    9. delta-синк каналов из vtelemax.
    """
    schedule_map = Q_CLUSTER.setdefault("schedule", {})

    if VTELEMAX_SYNC_SCHEDULE_ENABLED:
        schedule_map["run_vtelemax_recipients_delta"] = {
            "func": "guests.tasks.run_vtelemax_recipients_delta_task",
            "minutes": VTELEMAX_SYNC_SCHEDULE_MINUTES,
        }
    else:
        schedule_map.pop("run_vtelemax_recipients_delta", None)

    if VTELEMAX_COUPON_SYNC_ENABLED and VTELEMAX_COUPON_SYNC_SCHEDULE_ENABLED:
        schedule_map["run_vtelemax_coupon_sync_queue"] = {
            "func": "guests.tasks.run_vtelemax_coupon_sync_queue_task",
            "minutes": VTELEMAX_COUPON_SYNC_SCHEDULE_MINUTES,
        }
    else:
        schedule_map.pop("run_vtelemax_coupon_sync_queue", None)

    if OLAP_SYNC_SCHEDULE_ENABLED:
        if OLAP_SCHEDULE_USE_HOURLY_CRON_WAVES:
            schedule_map["run_olap_sync_windowed"] = {
                "func": "guests.tasks.run_olap_sync_scheduled_task",
                "schedule_type": "C",
                "cron": OLAP_SYNC_SCHEDULE_CRON,
            }
        else:
            schedule_map["run_olap_sync_windowed"] = {
                "func": "guests.tasks.run_olap_sync_scheduled_task",
                "minutes": OLAP_SYNC_SCHEDULE_MINUTES,
            }
    else:
        schedule_map.pop("run_olap_sync_windowed", None)

    if OLAP_REBUILD_SCHEDULE_ENABLED:
        schedule_map["run_olap_rebuild_nightly"] = {
            "func": "guests.tasks.run_olap_rebuild_scheduled_task",
            "schedule_type": "C",
            "cron": OLAP_REBUILD_SCHEDULE_CRON,
        }
    else:
        schedule_map.pop("run_olap_rebuild_nightly", None)

    if OLAP_ORDER_FACT_SCHEDULE_ENABLED:
        if OLAP_SCHEDULE_USE_HOURLY_CRON_WAVES:
            schedule_map["run_order_fact_tail"] = {
                "func": "guests.tasks.run_order_fact_scheduled_task",
                "schedule_type": "C",
                "cron": OLAP_ORDER_FACT_SCHEDULE_CRON,
            }
        else:
            schedule_map["run_order_fact_tail"] = {
                "func": "guests.tasks.run_order_fact_scheduled_task",
                "minutes": OLAP_ORDER_FACT_SCHEDULE_MINUTES,
            }
    else:
        schedule_map.pop("run_order_fact_tail", None)

    if OLAP_DAILY_FACT_SCHEDULE_ENABLED:
        schedule_map["run_daily_fact_tail"] = {
            "func": "guests.tasks.run_daily_fact_scheduled_task",
            "minutes": OLAP_DAILY_FACT_SCHEDULE_MINUTES,
        }
    else:
        schedule_map.pop("run_daily_fact_tail", None)

    if OLAP_DAILY_ORDER_FACT_SCHEDULE_ENABLED:
        if OLAP_SCHEDULE_USE_HOURLY_CRON_WAVES:
            schedule_map["run_daily_order_fact_tail"] = {
                "func": "guests.tasks.run_daily_order_fact_scheduled_task",
                "schedule_type": "C",
                "cron": OLAP_DAILY_ORDER_FACT_SCHEDULE_CRON,
            }
        else:
            schedule_map["run_daily_order_fact_tail"] = {
                "func": "guests.tasks.run_daily_order_fact_scheduled_task",
                "minutes": OLAP_DAILY_ORDER_FACT_SCHEDULE_MINUTES,
            }
    else:
        schedule_map.pop("run_daily_order_fact_tail", None)

    if OLAP_ORDER_FOCUS_FACT_SCHEDULE_ENABLED:
        schedule_map["run_order_focus_fact_tail"] = {
            "func": "guests.tasks.run_order_focus_fact_scheduled_task",
            "minutes": OLAP_ORDER_FOCUS_FACT_SCHEDULE_MINUTES,
        }
    else:
        schedule_map.pop("run_order_focus_fact_tail", None)

    if OLAP_WINDOW_METRICS_SCHEDULE_ENABLED:
        if OLAP_SCHEDULE_USE_HOURLY_CRON_WAVES:
            schedule_map["run_window_metrics_hourly"] = {
                "func": "guests.tasks.run_window_metrics_scheduled_task",
                "schedule_type": "C",
                "cron": OLAP_WINDOW_METRICS_SCHEDULE_CRON,
            }
        else:
            schedule_map["run_window_metrics_hourly"] = {
                "func": "guests.tasks.run_window_metrics_scheduled_task",
                "minutes": OLAP_WINDOW_METRICS_SCHEDULE_MINUTES,
            }
    else:
        schedule_map.pop("run_window_metrics_hourly", None)

    if OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_ENABLED:
        schedule_map["run_window_category_metrics_hourly"] = {
            "func": "guests.tasks.run_window_category_metrics_scheduled_task",
            "minutes": OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_MINUTES,
        }
    else:
        schedule_map.pop("run_window_category_metrics_hourly", None)

    if OLAP_CONTROL_PULL_SCHEDULE_ENABLED:
        schedule_map["run_olap_control_pull_daily"] = {
            "func": "guests.tasks.run_olap_control_pull_scheduled_task",
            "schedule_type": "C",
            "cron": OLAP_CONTROL_PULL_SCHEDULE_CRON,
        }
    else:
        schedule_map.pop("run_olap_control_pull_daily", None)


_register_olap_schedule_tasks()
