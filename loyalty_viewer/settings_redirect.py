"""Минимальные производственные настройки отдельной службы переходов."""

from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F401,F403


SECRET_KEY = str(os.getenv("TRACKED_LINK_SECRET_KEY", "") or "").strip()
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "Для службы переходов не задан отдельный TRACKED_LINK_SECRET_KEY."
    )

DEBUG = False
ALLOWED_HOSTS = [
    host.strip()
    for host in str(
        os.getenv(
            "TRACKED_LINK_DJANGO_ALLOWED_HOSTS",
            "localhost,127.0.0.1",
        )
        or ""
    ).split(",")
    if host.strip()
]

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
        "NAME": os.getenv("TRACKED_LINK_PG_NAME"),
        "USER": os.getenv("TRACKED_LINK_PG_USER"),
        "PASSWORD": os.getenv("TRACKED_LINK_PG_PASSWORD"),
        "HOST": os.getenv("TRACKED_LINK_PG_HOST", "db"),
        "PORT": os.getenv("TRACKED_LINK_PG_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

for variable_name in (
    "TRACKED_LINK_PG_NAME",
    "TRACKED_LINK_PG_USER",
    "TRACKED_LINK_PG_PASSWORD",
):
    if not str(os.getenv(variable_name, "") or "").strip():
        raise ImproperlyConfigured(
            f"Для службы переходов не задана переменная {variable_name}."
        )
