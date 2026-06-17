from __future__ import annotations

from datetime import date, datetime
from typing import Any

from django.utils import timezone

from guests.models import Guest, GuestProfileCompletionEvent, NotificationEvent
from guests.services.notification_registry import SCENARIO_CODE_FILL_BIRTHDAY_REQUEST


def record_birthdate_filled_event(
    *,
    guest: Guest | None,
    birthdate: date | None,
    source: str = GuestProfileCompletionEvent.Source.VTELEMAX,
    source_ref: str = "",
    payload: dict[str, Any] | None = None,
    detected_at: datetime | None = None,
) -> tuple[GuestProfileCompletionEvent | None, bool]:
    """
    Фиксирует первый факт появления даты рождения у гостя.

    Повторные синхронизации не создают дубль: уникальность держится на паре
    `(guest, event_type)`. Купонная награда должна опираться именно на это
    событие, а не на сам факт наличия даты рождения в старой базе.
    """
    if guest is None or birthdate is None:
        return None, False

    safe_detected_at = detected_at or timezone.now()
    request_event = (
        NotificationEvent.objects.filter(
            guest=guest,
            scenario__code=SCENARIO_CODE_FILL_BIRTHDAY_REQUEST,
            status=NotificationEvent.Status.TASK_CREATED,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    event, created = GuestProfileCompletionEvent.objects.get_or_create(
        guest=guest,
        event_type=GuestProfileCompletionEvent.EventType.BIRTHDATE_FILLED,
        defaults={
            "source": source,
            "source_ref": str(source_ref or "").strip() or None,
            "detected_at": safe_detected_at,
            "profile_value": {"birthdate": birthdate.isoformat()},
            "request_notification_event": request_event,
            "status": GuestProfileCompletionEvent.Status.NEW,
            "payload": payload or {},
        },
    )
    return event, created
