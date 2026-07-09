from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Any, Mapping

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from guests.models import GuestWelcomeRegistrationEvent

VTELEMAX_REGISTRATION_CALLBACK_PATH = "/internal/integration/v1/vtelemax/registration-events"

_SUPPORTED_PLATFORMS = {"telegram", "max", "vk"}
_PHONE_E164_RE = re.compile(r"^\+\d{10,15}$")


class VtelemaxRegistrationCallbackError(Exception):
    """
    Ошибка приёма события регистрации из vtelemax.

    `status_code` используется HTTP-слоем, `code` уходит во внешний JSON-ответ
    для диагностики на стороне vtelemax.
    """

    def __init__(self, message: str, *, status_code: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VtelemaxRegistrationCallbackResult:
    event: GuestWelcomeRegistrationEvent
    duplicate: bool


def build_vtelemax_registration_signature(
    *,
    secret: str,
    method: str,
    path: str,
    timestamp: str,
    body: bytes,
) -> str:
    """
    Формирует HMAC-SHA256 подпись входящего события регистрации vtelemax.

    Каноническая строка совпадает с ранее согласованным контрактом:
    `METHOD\nPATH\nTIMESTAMP\nSHA256(BODY)`.
    """

    body_hash = hashlib.sha256(body).hexdigest()
    canonical_payload = "\n".join([method.upper(), path, timestamp, body_hash])
    return hmac.new(
        str(secret or "").encode("utf-8"),
        canonical_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def receive_vtelemax_registration_event(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
) -> VtelemaxRegistrationCallbackResult:
    """
    Проверяет входящий запрос vtelemax и фиксирует событие регистрации в журнале.

    Функция не выдаёт купон и не запускает тяжёлую обработку: её задача быстро
    и идемпотентно принять событие, чтобы последующий обработчик мог работать
    через очередь и ретраи.
    """

    _validate_transport(headers=headers, body=body)
    _validate_signature(method=method, path=path, headers=headers, body=body)
    payload = _parse_json_body(body)
    normalized = _validate_payload(payload=payload, headers=headers)

    payload_sha256 = hashlib.sha256(body).hexdigest()
    now = timezone.now()
    status = GuestWelcomeRegistrationEvent.Status.NEW
    skip_reason = None
    processed_at = None
    accept_from = _get_accept_registrations_from()
    if accept_from is not None and normalized["registered_at"] < accept_from:
        status = GuestWelcomeRegistrationEvent.Status.SKIPPED
        skip_reason = "registration_before_accept_from"
        processed_at = now

    defaults = {
        "request_id": normalized["request_id"],
        "event_type": normalized["event_type"],
        "person_id": normalized["person_id"],
        "platform": normalized["platform"],
        "phone_e164": normalized["phone_e164"],
        "iiko_customer_id": normalized["iiko_customer_id"],
        "external_id": normalized["external_id"],
        "rules_accepted": normalized["rules_accepted"],
        "notifications_allowed": normalized["notifications_allowed"],
        "is_registered": normalized["is_registered"],
        "registered_at": normalized["registered_at"],
        "state_updated_at": normalized["state_updated_at"],
        "account_created_at": normalized["account_created_at"],
        "effective_updated_at": normalized["effective_updated_at"],
        "status": status,
        "skip_reason": skip_reason,
        "next_retry_at": now,
        "profile": normalized["profile"],
        "payload_json": payload,
        "payload_sha256": payload_sha256,
        "received_at": now,
        "processed_at": processed_at,
    }

    event, created = _get_or_create_event(
        event_id=normalized["event_id"],
        defaults=defaults,
    )
    if not created and event.payload_sha256 and event.payload_sha256 != payload_sha256:
        raise VtelemaxRegistrationCallbackError(
            "Событие с таким event_id уже принято с другим содержимым.",
            status_code=409,
            code="event_id_payload_conflict",
        )

    return VtelemaxRegistrationCallbackResult(event=event, duplicate=not created)


def _get_or_create_event(
    *,
    event_id: str,
    defaults: dict[str, Any],
) -> tuple[GuestWelcomeRegistrationEvent, bool]:
    try:
        with transaction.atomic():
            return GuestWelcomeRegistrationEvent.objects.get_or_create(
                event_id=event_id,
                defaults=defaults,
            )
    except IntegrityError:
        return GuestWelcomeRegistrationEvent.objects.get(event_id=event_id), False


def _validate_transport(*, headers: Mapping[str, str], body: bytes) -> None:
    content_type = _get_header(headers, "Content-Type").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise VtelemaxRegistrationCallbackError(
            "Ожидается Content-Type: application/json.",
            status_code=415,
            code="unsupported_content_type",
        )

    max_body_bytes = int(getattr(settings, "VTELEMAX_REGISTRATION_CALLBACK_MAX_BODY_BYTES", 65536) or 65536)
    if len(body) > max_body_bytes:
        raise VtelemaxRegistrationCallbackError(
            "Тело события регистрации превышает допустимый размер.",
            status_code=413,
            code="body_too_large",
        )


def _validate_signature(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
) -> None:
    timestamp = _get_header(headers, "X-Vtelemax-Timestamp")
    signature = _get_header(headers, "X-Vtelemax-Signature")
    if not timestamp or not signature:
        raise VtelemaxRegistrationCallbackError(
            "Не переданы обязательные заголовки подписи vtelemax.",
            status_code=401,
            code="signature_headers_missing",
        )

    secret = str(getattr(settings, "VTELEMAX_REGISTRATION_CALLBACK_HMAC_SECRET", "") or "").strip()
    if not secret:
        raise VtelemaxRegistrationCallbackError(
            "Приём событий регистрации vtelemax не настроен: не задан HMAC-секрет.",
            status_code=503,
            code="callback_secret_missing",
        )

    try:
        timestamp_value = int(timestamp)
    except (TypeError, ValueError):
        raise VtelemaxRegistrationCallbackError(
            "Заголовок X-Vtelemax-Timestamp должен быть unix timestamp.",
            status_code=401,
            code="timestamp_invalid",
        )

    tolerance_seconds = int(
        getattr(settings, "VTELEMAX_REGISTRATION_CALLBACK_TIMESTAMP_TOLERANCE_SECONDS", 300)
        or 300
    )
    now_value = int(timezone.now().timestamp())
    if abs(now_value - timestamp_value) > tolerance_seconds:
        raise VtelemaxRegistrationCallbackError(
            "Временная метка события регистрации вне допустимого окна.",
            status_code=401,
            code="timestamp_out_of_window",
        )

    expected_signature = build_vtelemax_registration_signature(
        secret=secret,
        method=method,
        path=path,
        timestamp=timestamp,
        body=body,
    )
    if not hmac.compare_digest(expected_signature, signature):
        raise VtelemaxRegistrationCallbackError(
            "Подпись события регистрации vtelemax не совпала.",
            status_code=401,
            code="signature_invalid",
        )


def _parse_json_body(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VtelemaxRegistrationCallbackError(
            "Тело события регистрации должно быть корректным JSON.",
            status_code=400,
            code="json_invalid",
        )

    if not isinstance(payload, dict):
        raise VtelemaxRegistrationCallbackError(
            "Тело события регистрации должно быть JSON-объектом.",
            status_code=400,
            code="json_object_required",
        )
    return payload


def _validate_payload(*, payload: dict[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
    request_id = _required_text(payload, "request_id", max_length=128)
    header_request_id = _get_header(headers, "X-Vtelemax-Request-Id")
    if not header_request_id:
        raise VtelemaxRegistrationCallbackError(
            "Не передан заголовок X-Vtelemax-Request-Id.",
            status_code=400,
            code="request_id_header_missing",
        )
    if header_request_id != request_id:
        raise VtelemaxRegistrationCallbackError(
            "request_id в заголовке и теле события регистрации не совпадает.",
            status_code=400,
            code="request_id_mismatch",
        )

    event_id = _required_text(payload, "event_id", max_length=128)
    event_type = _required_text(payload, "event_type", max_length=64)
    if event_type != GuestWelcomeRegistrationEvent.EventType.GUEST_REGISTERED:
        raise VtelemaxRegistrationCallbackError(
            "Поддерживается только event_type=guest_registered.",
            status_code=400,
            code="event_type_unsupported",
        )

    person_id = _required_uuid(payload, "person_id")
    platform = _required_text(payload, "platform", max_length=32).lower()
    if platform not in _SUPPORTED_PLATFORMS:
        raise VtelemaxRegistrationCallbackError(
            "Платформа события регистрации не поддерживается.",
            status_code=400,
            code="platform_unsupported",
        )

    phone_e164 = _required_text(payload, "phone_e164", max_length=32)
    if not _PHONE_E164_RE.match(phone_e164):
        raise VtelemaxRegistrationCallbackError(
            "phone_e164 должен быть передан в формате E.164.",
            status_code=400,
            code="phone_e164_invalid",
        )

    iiko_customer_id = _required_text(payload, "customerId", max_length=64)
    external_id = _required_text(payload, "external_id", max_length=128)
    rules_accepted = _required_bool(payload, "rules_accepted")
    notifications_allowed = _required_bool(payload, "notifications_allowed")
    is_registered = _required_bool(payload, "is_registered")
    if not is_registered:
        raise VtelemaxRegistrationCallbackError(
            "Событие регистрации должно приходить только для is_registered=true.",
            status_code=400,
            code="registration_flag_invalid",
        )

    profile = payload.get("profile")
    if not isinstance(profile, dict):
        raise VtelemaxRegistrationCallbackError(
            "profile должен быть JSON-объектом.",
            status_code=400,
            code="profile_invalid",
        )

    return {
        "request_id": request_id,
        "event_id": event_id,
        "event_type": event_type,
        "person_id": person_id,
        "platform": platform,
        "phone_e164": phone_e164,
        "iiko_customer_id": iiko_customer_id,
        "external_id": external_id,
        "rules_accepted": rules_accepted,
        "notifications_allowed": notifications_allowed,
        "is_registered": is_registered,
        "registered_at": _required_datetime(payload, "registered_at"),
        "state_updated_at": _required_datetime(payload, "state_updated_at"),
        "account_created_at": _required_datetime(payload, "account_created_at"),
        "effective_updated_at": _required_datetime(payload, "effective_updated_at"),
        "profile": profile,
    }


def _required_text(payload: dict[str, Any], key: str, *, max_length: int) -> str:
    value = payload.get(key)
    if value is None:
        raise VtelemaxRegistrationCallbackError(
            f"Не передано обязательное поле {key}.",
            status_code=400,
            code=f"{key}_missing",
        )
    text = str(value).strip()
    if not text:
        raise VtelemaxRegistrationCallbackError(
            f"Поле {key} не должно быть пустым.",
            status_code=400,
            code=f"{key}_empty",
        )
    if len(text) > max_length:
        raise VtelemaxRegistrationCallbackError(
            f"Поле {key} превышает допустимую длину.",
            status_code=400,
            code=f"{key}_too_long",
        )
    return text


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise VtelemaxRegistrationCallbackError(
            f"Поле {key} должно быть boolean.",
            status_code=400,
            code=f"{key}_invalid",
        )
    return value


def _required_uuid(payload: dict[str, Any], key: str) -> uuid.UUID:
    text = _required_text(payload, key, max_length=64)
    try:
        return uuid.UUID(text)
    except (TypeError, ValueError):
        raise VtelemaxRegistrationCallbackError(
            f"Поле {key} должно быть UUID.",
            status_code=400,
            code=f"{key}_invalid",
        )


def _required_datetime(payload: dict[str, Any], key: str) -> datetime:
    text = _required_text(payload, key, max_length=64)
    parsed = _parse_rfc3339_datetime(text)
    if parsed is None:
        raise VtelemaxRegistrationCallbackError(
            f"Поле {key} должно быть датой-временем RFC3339.",
            status_code=400,
            code=f"{key}_invalid",
        )
    return parsed


def _get_accept_registrations_from() -> datetime | None:
    raw_value = str(getattr(settings, "WELCOME_COUPON_ACCEPT_REGISTRATIONS_FROM", "") or "").strip()
    if not raw_value:
        return None
    parsed = _parse_rfc3339_datetime(raw_value)
    if parsed is None:
        raise VtelemaxRegistrationCallbackError(
            "Некорректная настройка WELCOME_COUPON_ACCEPT_REGISTRATIONS_FROM.",
            status_code=503,
            code="accept_registrations_from_invalid",
        )
    return parsed


def _parse_rfc3339_datetime(raw_value: str) -> datetime | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text = f"{text}T00:00:00+00:00"
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def _get_header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    if value is None:
        value = headers.get(name.upper())
    return str(value or "").strip()
