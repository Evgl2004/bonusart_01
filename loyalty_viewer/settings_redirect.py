"""Минимальные производственные настройки отдельной службы переходов."""

from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F401,F403


SECRET_KEY = str(os.getenv("SECRET_KEY", "") or "").strip()
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "Для службы переходов не задан отдельный SECRET_KEY."
    )

DEBUG = False

ALLOWED_HOSTS = [
    str(host).strip()
    for host in ALLOWED_HOSTS  # noqa: F405
    if str(host).strip()
]
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "Для службы переходов не задан ни один допустимый узел ALLOWED_HOSTS."
    )

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "guests",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "loyalty_viewer.redirect_urls"
WSGI_APPLICATION = "loyalty_viewer.redirect_wsgi.application"
TEMPLATES = []

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("PG_NAME"),
        "USER": os.getenv("PG_USER"),
        "PASSWORD": os.getenv("PG_PASSWORD"),
        "HOST": os.getenv("PG_HOST"),
        "PORT": os.getenv("PG_PORT"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

for variable_name in (
    "PG_NAME",
    "PG_USER",
    "PG_PASSWORD",
    "PG_HOST",
    "PG_PORT",
):
    if not str(os.getenv(variable_name, "") or "").strip():
        raise ImproperlyConfigured(
            f"Для службы переходов не задана переменная {variable_name}."
        )
