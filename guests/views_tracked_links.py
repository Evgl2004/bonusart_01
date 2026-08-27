"""Минимальные HTTP-представления публичной службы переходов."""

from __future__ import annotations

import logging

from django.db import DatabaseError, connection
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
)
from django.views.decorators.http import require_http_methods

from guests.models import (
    MessageInteractionLinkTransition,
    MessageInteractionTrackedLink,
)
from guests.services.message_interaction_links import (
    PUBLIC_TOKEN_PATTERN,
    MessageInteractionConfigurationError,
    validate_tracked_link_snapshot_url,
)


logger = logging.getLogger(__name__)

_UNAVAILABLE_BODY = "Ссылка недоступна."
_DATABASE_UNAVAILABLE_BODY = "Сервис временно недоступен."


def _harden_response(response: HttpResponse) -> HttpResponse:
    """Добавляет заголовки, запрещающие кэширование и индексирование."""

    response["Cache-Control"] = "no-store, private"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def _unavailable_response() -> HttpResponse:
    """Не раскрывает, отсутствует ссылка или была отключена."""

    return _harden_response(
        HttpResponse(
            _UNAVAILABLE_BODY,
            status=410,
            content_type="text/plain; charset=utf-8",
        )
    )


def _database_unavailable_response() -> HttpResponse:
    """Не выполняет переход, если обещанный факт нельзя сохранить."""

    return _harden_response(
        HttpResponse(
            _DATABASE_UNAVAILABLE_BODY,
            status=503,
            content_type="text/plain; charset=utf-8",
        )
    )


def tracked_link_redirect(request: HttpRequest, public_token: str) -> HttpResponse:
    """Сохраняет каждый допустимый GET и перенаправляет на снимок адреса."""

    if request.method not in {"GET", "HEAD"}:
        return _harden_response(HttpResponseNotAllowed(["GET", "HEAD"]))
    if request.method == "HEAD":
        return _harden_response(HttpResponse(status=204))
    if PUBLIC_TOKEN_PATTERN.fullmatch(str(public_token or "")) is None:
        return _unavailable_response()

    try:
        tracked_link = (
            MessageInteractionTrackedLink.objects.only(
                "interaction_id",
                "target_url",
                "disabled_at",
            )
            .filter(
                public_token=public_token,
                disabled_at__isnull=True,
            )
            .first()
        )
    except DatabaseError as error:
        logger.error(
            "Не удалось прочитать отслеживаемую ссылку: тип=%s",
            type(error).__name__,
        )
        return _database_unavailable_response()

    if tracked_link is None:
        return _unavailable_response()
    try:
        target_url = validate_tracked_link_snapshot_url(tracked_link.target_url)
    except MessageInteractionConfigurationError as error:
        logger.error(
            "Снимок отслеживаемой ссылки отклонён защитной проверкой: "
            "interaction_id=%s тип=%s",
            tracked_link.interaction_id,
            type(error).__name__,
        )
        return _unavailable_response()

    try:
        MessageInteractionLinkTransition.objects.create(
            tracked_link_id=tracked_link.interaction_id,
        )
    except DatabaseError as error:
        logger.error(
            "Не удалось сохранить переход по отслеживаемой ссылке: "
            "interaction_id=%s тип=%s",
            tracked_link.interaction_id,
            type(error).__name__,
        )
        return _database_unavailable_response()

    return _harden_response(HttpResponseRedirect(target_url))


@require_http_methods(["GET"])
def tracked_link_health(request: HttpRequest) -> HttpResponse:
    """Проверяет доступность процесса и его минимального соединения с базой."""

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError as error:
        logger.error(
            "Проверка состояния службы переходов не прошла: тип=%s",
            type(error).__name__,
        )
        return _database_unavailable_response()
    return _harden_response(
        HttpResponse("ok", content_type="text/plain; charset=utf-8")
    )
