"""Внутренняя HTTP-точка пакетного приёма взаимодействий vtelemax."""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from guests.services.message_interaction_inbound import (
    MessageInteractionCallbackError,
    receive_vtelemax_message_interaction_events,
)


logger = logging.getLogger(__name__)


def _error_response(*, code: str, message: str, status_code: int) -> JsonResponse:
    return JsonResponse(
        {
            "ok": False,
            "status": "error",
            "code": code,
            "message": message,
        },
        status=status_code,
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
def vtelemax_message_interaction_events(request):
    """Принимает один подписанный пакет событий нажатия из vtelemax."""

    if request.method != "POST":
        response = _error_response(
            code="method_not_allowed",
            message="Метод не поддерживается. Используйте POST.",
            status_code=405,
        )
        response["Allow"] = "POST"
        return response

    if not getattr(settings, "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_ENABLED", False):
        return _error_response(
            code="callback_disabled",
            message="Приём событий взаимодействия от vtelemax отключён настройкой.",
            status_code=503,
        )

    require_https = getattr(
        settings,
        "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_REQUIRE_HTTPS",
        True,
    )
    if require_https and not request.is_secure():
        return _error_response(
            code="https_required",
            message="Пакет событий взаимодействия должен поступать по HTTPS.",
            status_code=403,
        )

    started_at = time.monotonic()
    try:
        result = receive_vtelemax_message_interaction_events(
            method=request.method,
            path=request.path,
            headers=request.headers,
            body=request.body,
        )
    except MessageInteractionCallbackError as error:
        logger.warning(
            "Пакет взаимодействий vtelemax отклонён: код=%s длительность_мс=%s",
            error.code,
            round((time.monotonic() - started_at) * 1000, 2),
        )
        response = _error_response(
            code=error.code,
            message=error.message,
            status_code=error.status_code,
        )
        if error.retry_after_seconds is not None:
            response["Retry-After"] = str(error.retry_after_seconds)
        return response
    except Exception:
        logger.exception(
            "Непредвиденная ошибка пакетного приёма взаимодействий vtelemax; тело запроса не журналируется."
        )
        return _error_response(
            code="internal_error",
            message="Временная внутренняя ошибка обработки пакета.",
            status_code=500,
        )

    accepted_count = sum(item["status"] == "accepted" for item in result.results)
    logger.info(
        "Пакет взаимодействий vtelemax обработан: request_id=%s элементов=%s принято=%s "
        "отклонено=%s длительность_мс=%s",
        result.request_id,
        len(result.results),
        accepted_count,
        len(result.results) - accepted_count,
        round((time.monotonic() - started_at) * 1000, 2),
    )
    return JsonResponse(
        result.as_dict(),
        status=200,
        json_dumps_params={"ensure_ascii": False},
    )
