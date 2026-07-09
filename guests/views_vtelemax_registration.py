from __future__ import annotations

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from guests.services.vtelemax_registration_callback import (
    VtelemaxRegistrationCallbackError,
    receive_vtelemax_registration_event,
)


@csrf_exempt
def vtelemax_registration_events(request):
    """
    Принимает событие регистрации гостя из vtelemax для welcome-автосценария.
    """

    if request.method != "POST":
        response = JsonResponse(
            {
                "ok": False,
                "status": "error",
                "code": "method_not_allowed",
                "message": "Метод не поддерживается. Используйте POST.",
            },
            status=405,
        )
        response["Allow"] = "POST"
        return response

    if not getattr(settings, "VTELEMAX_REGISTRATION_CALLBACK_ENABLED", False):
        return JsonResponse(
            {
                "ok": False,
                "status": "disabled",
                "code": "callback_disabled",
                "message": "Приём событий регистрации от vtelemax отключён настройкой.",
            },
            status=503,
        )

    if getattr(settings, "VTELEMAX_REGISTRATION_CALLBACK_REQUIRE_HTTPS", True) and not request.is_secure():
        return JsonResponse(
            {
                "ok": False,
                "status": "error",
                "code": "https_required",
                "message": "Событие регистрации vtelemax должно поступать по HTTPS.",
            },
            status=403,
        )

    try:
        result = receive_vtelemax_registration_event(
            method=request.method,
            path=request.path,
            headers=request.headers,
            body=request.body,
        )
    except VtelemaxRegistrationCallbackError as exc:
        return JsonResponse(
            {
                "ok": False,
                "status": "error",
                "code": exc.code,
                "message": exc.message,
            },
            status=exc.status_code,
        )

    return JsonResponse(
        {
            "ok": True,
            "status": "accepted",
            "duplicate": result.duplicate,
            "event_id": result.event.event_id,
            "welcome_event_id": result.event.id,
            "message": "Событие регистрации принято.",
        },
        status=202,
    )
