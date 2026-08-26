"""Точка запуска отдельной публичной службы переходов."""

import os

from django.core.wsgi import get_wsgi_application


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "loyalty_viewer.settings_redirect",
)

application = get_wsgi_application()
