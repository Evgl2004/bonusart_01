from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "devkey")
DEBUG = os.getenv("DEBUG", "False") == "True"
ALLOWED_HOSTS = []

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
        'PASSWORD': os.getenv('PG_PASSWORD', ''),
        'HOST': os.getenv('PG_HOST', 'localhost'),
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
    #"schedule": {
    #    "sync_webhooks_recent": {
    #        "func": "guests.tasks.sync_webhooks_recent",
    #        "minutes": 10,  # раз в 10 минут запускать обработку
    #    },
    #},
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,

    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },

    "root": {
        "handlers": ["console"],
        "level": "INFO",   # Выводить INFO, WARNING, ERROR
    },
}
LANGUAGE_CODE = 'ru-ru'
TIME_ZONE = 'Europe/Moscow'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

#WEBHOOK_SERVICE_URL = "http://webhook-service:8000"

IIKO_API_KEY = os.getenv("IIKO_API_KEY")
IIKO_API_BASE_URL = os.getenv("IIKO_API_BASE_URL")
IIKO_ORGANIZATION_ID = os.getenv("IIKO_ORGANIZATION_ID")