"""Общие инварианты публичных отслеживаемых ссылок."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


PUBLIC_TOKEN_BYTES = 24
PUBLIC_TOKEN_LENGTH = 32
PUBLIC_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")
PUBLIC_REDIRECT_PATH_PREFIX = "/r/v1/"

_HTTPS_URL_VALIDATOR = URLValidator(schemes=["https"])


class MessageInteractionConfigurationError(ValueError):
    """Ошибка согласованной конфигурации интерактивного сообщения."""


def normalize_allowed_destination_hosts() -> frozenset[str]:
    """Возвращает закрытый перечень доменов конечного перенаправления."""

    raw_hosts = getattr(settings, "MESSAGE_TRACKED_LINK_ALLOWED_HOSTS", set())
    if isinstance(raw_hosts, str):
        raw_hosts = raw_hosts.split(",")
    return frozenset(
        str(host or "").strip().lower().rstrip(".")
        for host in raw_hosts
        if str(host or "").strip()
    )


def validate_https_url(*, value: str, purpose: str) -> str:
    """Строго проверяет HTTPS-адрес без учётных данных, IP и особого порта."""

    normalized = str(value or "").strip()
    try:
        _HTTPS_URL_VALIDATOR(normalized)
        parsed = urlsplit(normalized)
        port = parsed.port
    except (ValidationError, ValueError) as error:
        raise MessageInteractionConfigurationError(
            f"{purpose} должен быть корректным HTTPS-адресом."
        ) from error

    hostname = str(parsed.hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise MessageInteractionConfigurationError(
            f"{purpose} должен содержать доменное имя, а не IP-адрес."
        )

    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise MessageInteractionConfigurationError(
            f"{purpose} должен использовать HTTPS без учётных данных и нестандартного порта."
        )
    return normalized


def validate_tracked_link_target_url(value: str) -> str:
    """Проверяет конечный адрес по точному эксплуатационному перечню."""

    target_url = validate_https_url(
        value=value,
        purpose="Конечный адрес отслеживаемой ссылки",
    )
    target_host = str(urlsplit(target_url).hostname or "").lower().rstrip(".")
    if target_host not in normalize_allowed_destination_hosts():
        raise MessageInteractionConfigurationError(
            "Домен конечного адреса отсутствует в разрешённом перечне."
        )
    return target_url


def build_public_redirect_url(public_token: str) -> str:
    """Формирует точный внешний адрес перехода по публичному токену."""

    if PUBLIC_TOKEN_PATTERN.fullmatch(str(public_token or "")) is None:
        raise MessageInteractionConfigurationError(
            "Публичный токен отслеживаемой ссылки имеет неверный формат."
        )
    raw_base_url = str(
        getattr(settings, "MESSAGE_TRACKED_LINK_PUBLIC_BASE_URL", "") or ""
    ).strip()
    base_url = validate_https_url(
        value=raw_base_url,
        purpose="Публичный адрес службы переходов",
    )
    parsed = urlsplit(base_url)
    if parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/r/v1":
        raise MessageInteractionConfigurationError(
            "Публичный адрес службы переходов должен оканчиваться точным путём /r/v1/."
        )
    return f"{parsed.scheme}://{parsed.netloc}{PUBLIC_REDIRECT_PATH_PREFIX}{public_token}"
