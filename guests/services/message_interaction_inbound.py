"""Защищённый пакетный приём событий взаимодействия из vtelemax."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Mapping

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from guests.models import InteractionButtonSet, MessageInteraction, MessageInteractionEvent


MESSAGE_INTERACTION_CALLBACK_PATH = (
    "/internal/integration/v1/vtelemax/message-interactions/events"
)
MESSAGE_INTERACTION_SCHEMA_VERSION = 1
MAX_BATCH_ITEMS = 100
MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
MAX_OCCURRED_AT_FUTURE_SECONDS = 300

_LOWER_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE_FIELDS = {"request_id", "schema_version", "sent_at", "items"}
_REQUIRED_ITEM_FIELDS = {"event_id", "interaction_id", "action", "occurred_at"}
_OPTIONAL_ITEM_FIELDS = {"provider_message_id"}
_ACTIONS_BY_BUTTON_SET = {
    InteractionButtonSet.RATING_MENU: {"l", "d", "m"},
    InteractionButtonSet.RATING_COUPONS: {"l", "d", "c"},
}


class MessageInteractionCallbackError(Exception):
    """Ошибка оболочки, защиты или транспорта пакетного запроса."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class _ItemRejected(Exception):
    """Ожидаемый поэлементный отказ корректной оболочки пакета."""

    def __init__(self, result: str, message: str, *, event_id: str | None = None):
        super().__init__(message)
        self.result = result
        self.message = message
        self.event_id = event_id


@dataclass(frozen=True, slots=True)
class _NormalizedEvent:
    event_id: uuid.UUID
    interaction_id: int
    action: str
    occurred_at: datetime
    provider_message_id: str | None


@dataclass(frozen=True, slots=True)
class MessageInteractionBatchResult:
    """Поэлементный результат обработки одного корректного пакета."""

    request_id: str
    received_at: datetime
    results: tuple[dict[str, Any], ...]

    @property
    def status(self) -> str:
        accepted_count = sum(item["status"] == "accepted" for item in self.results)
        if accepted_count == len(self.results):
            return "accepted"
        if accepted_count == 0:
            return "rejected"
        return "partial"

    def as_dict(self) -> dict[str, Any]:
        """Возвращает согласованный JSON-ответ версии 1."""

        return {
            "ok": True,
            "schema_version": MESSAGE_INTERACTION_SCHEMA_VERSION,
            "request_id": self.request_id,
            "status": self.status,
            "received_at": _format_rfc3339(self.received_at),
            "results": list(self.results),
        }


def build_vtelemax_message_interaction_signature(
    *,
    secret: str,
    method: str,
    path: str,
    timestamp: str,
    body: bytes,
) -> str:
    """Формирует HMAC-SHA256 по действующей канонической строке проекта."""

    body_hash = hashlib.sha256(body).hexdigest()
    canonical_payload = "\n".join([method.upper(), path, timestamp, body_hash])
    return hmac.new(
        str(secret or "").encode("utf-8"),
        canonical_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def receive_vtelemax_message_interaction_events(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
) -> MessageInteractionBatchResult:
    """Проверяет подписанный пакет и независимо обрабатывает каждый элемент."""

    _validate_transport(headers=headers, body=body)
    _validate_signature(method=method, path=path, headers=headers, body=body)
    payload = _parse_json_body(body)
    envelope = _validate_envelope(payload=payload, headers=headers)
    _enforce_rate_limit()

    received_at = timezone.now()
    results = tuple(
        _process_item_with_result(index=index, raw_item=raw_item, received_at=received_at)
        for index, raw_item in enumerate(envelope["items"])
    )
    return MessageInteractionBatchResult(
        request_id=envelope["request_id"],
        received_at=received_at,
        results=results,
    )


def _validate_transport(*, headers: Mapping[str, str], body: bytes) -> None:
    content_type = _get_header(headers, "Content-Type").split(";", maxsplit=1)[0].strip().lower()
    if content_type != "application/json":
        raise MessageInteractionCallbackError(
            "Ожидается Content-Type: application/json.",
            status_code=415,
            code="unsupported_content_type",
        )

    max_body_bytes = int(
        getattr(settings, "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_MAX_BODY_BYTES", 65536)
        or 65536
    )
    if len(body) > max_body_bytes:
        raise MessageInteractionCallbackError(
            "Тело пакета взаимодействий превышает допустимый размер.",
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
        raise MessageInteractionCallbackError(
            "Не переданы обязательные заголовки подписи vtelemax.",
            status_code=401,
            code="signature_headers_missing",
        )

    secret = str(
        getattr(settings, "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_HMAC_SECRET", "")
        or getattr(settings, "VTELEMAX_SYNC_HMAC_SECRET", "")
        or ""
    ).strip()
    if not secret:
        raise MessageInteractionCallbackError(
            "Приём событий взаимодействия не настроен: не задан HMAC-секрет.",
            status_code=503,
            code="callback_secret_missing",
        )

    try:
        timestamp_value = int(timestamp)
    except (TypeError, ValueError):
        raise MessageInteractionCallbackError(
            "Заголовок X-Vtelemax-Timestamp должен быть временем Unix.",
            status_code=401,
            code="timestamp_invalid",
        )

    tolerance_seconds = int(
        getattr(
            settings,
            "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_TIMESTAMP_TOLERANCE_SECONDS",
            300,
        )
        or 300
    )
    now_value = int(timezone.now().timestamp())
    if abs(now_value - timestamp_value) > tolerance_seconds:
        raise MessageInteractionCallbackError(
            "Временная метка подписи вне допустимого окна.",
            status_code=401,
            code="timestamp_out_of_window",
        )

    expected_signature = build_vtelemax_message_interaction_signature(
        secret=secret,
        method=method,
        path=path,
        timestamp=timestamp,
        body=body,
    )
    signature_has_valid_format = _LOWER_HEX_SHA256_RE.fullmatch(signature) is not None
    signature_matches = hmac.compare_digest(expected_signature, signature)
    if not signature_has_valid_format or not signature_matches:
        raise MessageInteractionCallbackError(
            "Подпись запроса не прошла проверку.",
            status_code=401,
            code="signature_invalid",
        )


def _parse_json_body(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MessageInteractionCallbackError(
            "Тело запроса должно быть корректным JSON.",
            status_code=400,
            code="json_invalid",
        )
    if not isinstance(payload, dict):
        raise MessageInteractionCallbackError(
            "Тело запроса должно быть JSON-объектом.",
            status_code=400,
            code="request_invalid",
        )
    return payload


def _validate_envelope(*, payload: dict[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
    if set(payload) != _ENVELOPE_FIELDS:
        raise MessageInteractionCallbackError(
            "Оболочка пакета содержит неизвестные или отсутствующие поля.",
            status_code=400,
            code="request_invalid",
        )

    request_id = _parse_uuid_text(payload.get("request_id"))
    if request_id is None:
        raise MessageInteractionCallbackError(
            "Поле request_id должно быть UUID.",
            status_code=400,
            code="request_invalid",
        )
    request_id_text = str(payload["request_id"])
    header_request_id = _get_header(headers, "X-Vtelemax-Request-Id")
    if header_request_id != request_id_text:
        raise MessageInteractionCallbackError(
            "request_id в заголовке и теле пакета не совпадает.",
            status_code=400,
            code="request_id_mismatch",
        )

    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise MessageInteractionCallbackError(
            "Поле schema_version должно быть целым числом.",
            status_code=400,
            code="request_invalid",
        )
    if schema_version != MESSAGE_INTERACTION_SCHEMA_VERSION:
        raise MessageInteractionCallbackError(
            "Версия пакетного контракта не поддерживается.",
            status_code=409,
            code="schema_version_unsupported",
        )

    sent_at = _parse_rfc3339_datetime(payload.get("sent_at"))
    if sent_at is None:
        raise MessageInteractionCallbackError(
            "Поле sent_at должно быть датой-временем RFC 3339 с часовым поясом.",
            status_code=400,
            code="request_invalid",
        )

    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH_ITEMS:
        raise MessageInteractionCallbackError(
            "Поле items должно содержать от 1 до 100 элементов.",
            status_code=400,
            code="request_invalid",
        )

    return {
        "request_id": request_id_text,
        "sent_at": sent_at,
        "items": items,
    }


def _process_item_with_result(
    *,
    index: int,
    raw_item: Any,
    received_at: datetime,
) -> dict[str, Any]:
    try:
        normalized = _validate_item(raw_item=raw_item, received_at=received_at)
        return _persist_item(index=index, item=normalized)
    except _ItemRejected as error:
        result: dict[str, Any] = {
            "index": index,
            "status": "rejected",
            "result": error.result,
            "message": error.message,
        }
        if error.event_id is not None:
            result["event_id"] = error.event_id
        return result


def _validate_item(*, raw_item: Any, received_at: datetime) -> _NormalizedEvent:
    if not isinstance(raw_item, dict):
        raise _ItemRejected("invalid_item", "Элемент пакета должен быть JSON-объектом.")

    item_fields = set(raw_item)
    if not _REQUIRED_ITEM_FIELDS.issubset(item_fields) or not item_fields.issubset(
        _REQUIRED_ITEM_FIELDS | _OPTIONAL_ITEM_FIELDS
    ):
        raise _ItemRejected(
            "invalid_item",
            "Элемент пакета содержит неизвестные или отсутствующие поля.",
            event_id=_valid_event_id_text(raw_item.get("event_id")),
        )

    event_uuid = _parse_uuid_text(raw_item.get("event_id"))
    if event_uuid is None:
        raise _ItemRejected("invalid_item", "Поле event_id должно быть UUID.")
    event_id_text = str(event_uuid)

    interaction_id = raw_item.get("interaction_id")
    if (
        isinstance(interaction_id, bool)
        or not isinstance(interaction_id, int)
        or not 1 <= interaction_id <= MAX_SIGNED_BIGINT
    ):
        raise _ItemRejected(
            "invalid_item",
            "Поле interaction_id должно быть положительным 64-битным числом.",
            event_id=event_id_text,
        )

    action = raw_item.get("action")
    if not isinstance(action, str) or action not in {"l", "d", "c", "m"}:
        raise _ItemRejected(
            "action_unsupported",
            "Код действия не поддерживается.",
            event_id=event_id_text,
        )

    occurred_at = _parse_rfc3339_datetime(raw_item.get("occurred_at"))
    if occurred_at is None:
        raise _ItemRejected(
            "invalid_item",
            "Поле occurred_at должно быть датой-временем RFC 3339 с часовым поясом.",
            event_id=event_id_text,
        )
    if occurred_at > received_at.astimezone(dt_timezone.utc) + timedelta(
        seconds=MAX_OCCURRED_AT_FUTURE_SECONDS
    ):
        raise _ItemRejected(
            "invalid_item",
            "Поле occurred_at более чем на пять минут опережает часы SAGUR.",
            event_id=event_id_text,
        )

    provider_message_id = _normalize_provider_message_id(
        raw_item=raw_item,
        event_id=event_id_text,
    )
    return _NormalizedEvent(
        event_id=event_uuid,
        interaction_id=interaction_id,
        action=action,
        occurred_at=occurred_at,
        provider_message_id=provider_message_id,
    )


def _persist_item(*, index: int, item: _NormalizedEvent) -> dict[str, Any]:
    existing = MessageInteractionEvent.objects.select_related("interaction").filter(
        event_id=item.event_id
    ).first()
    if existing is not None:
        return _existing_event_result(index=index, item=item, existing=existing)

    try:
        with transaction.atomic():
            interaction = MessageInteraction.objects.select_for_update().filter(
                id=item.interaction_id
            ).first()
            if interaction is None:
                raise _ItemRejected(
                    "interaction_not_found",
                    "Интерактивность сообщения не найдена.",
                    event_id=str(item.event_id),
                )

            # Повторная проверка нужна после ожидания блокировки интерактивности:
            # параллельная транзакция могла уже сохранить тот же event_id.
            existing = MessageInteractionEvent.objects.select_related("interaction").filter(
                event_id=item.event_id
            ).first()
            if existing is not None:
                return _existing_event_result(index=index, item=item, existing=existing)

            allowed_actions = _ACTIONS_BY_BUTTON_SET.get(interaction.button_set, set())
            if item.action not in allowed_actions:
                raise _ItemRejected(
                    "action_not_allowed_for_button_set",
                    "Действие не входит в сохранённый набор кнопок.",
                    event_id=str(item.event_id),
                )

            event_result = MessageInteractionEvent.Result.ACCEPTED
            if item.action in {
                MessageInteractionEvent.Action.LIKE,
                MessageInteractionEvent.Action.DISLIKE,
            }:
                rating_exists = MessageInteractionEvent.objects.filter(
                    interaction=interaction,
                    action__in=[
                        MessageInteractionEvent.Action.LIKE,
                        MessageInteractionEvent.Action.DISLIKE,
                    ],
                    result=MessageInteractionEvent.Result.ACCEPTED,
                ).exists()
                if rating_exists:
                    event_result = MessageInteractionEvent.Result.RATING_ALREADY_RECORDED

            MessageInteractionEvent.objects.create(
                event_id=item.event_id,
                interaction=interaction,
                action=item.action,
                occurred_at=item.occurred_at,
                result=event_result,
                provider_message_id=item.provider_message_id,
            )
    except IntegrityError:
        # Уникальный event_id остаётся последней защитой для параллельных
        # запросов, которые могли блокировать разные интерактивности.
        existing = MessageInteractionEvent.objects.select_related("interaction").filter(
            event_id=item.event_id
        ).first()
        if existing is None:
            raise
        return _existing_event_result(index=index, item=item, existing=existing)

    return {
        "index": index,
        "event_id": str(item.event_id),
        "status": "accepted",
        "result": event_result,
    }


def _existing_event_result(
    *,
    index: int,
    item: _NormalizedEvent,
    existing: MessageInteractionEvent,
) -> dict[str, Any]:
    if _event_matches(existing=existing, item=item):
        return {
            "index": index,
            "event_id": str(item.event_id),
            "status": "accepted",
            "result": "duplicate",
        }
    return {
        "index": index,
        "event_id": str(item.event_id),
        "status": "rejected",
        "result": "event_id_conflict",
        "message": "Событие с таким event_id уже принято с другим содержимым.",
    }


def _event_matches(*, existing: MessageInteractionEvent, item: _NormalizedEvent) -> bool:
    existing_occurred_at = existing.occurred_at.astimezone(dt_timezone.utc)
    existing_provider_message_id = str(existing.provider_message_id or "").strip() or None
    return (
        existing.interaction_id == item.interaction_id
        and existing.action == item.action
        and existing_occurred_at == item.occurred_at
        and existing_provider_message_id == item.provider_message_id
    )


def _normalize_provider_message_id(*, raw_item: dict[str, Any], event_id: str) -> str | None:
    if "provider_message_id" not in raw_item:
        return None
    value = raw_item["provider_message_id"]
    if not isinstance(value, str):
        raise _ItemRejected(
            "invalid_item",
            "Поле provider_message_id должно быть строкой.",
            event_id=event_id,
        )
    normalized = value.strip()
    if len(normalized) > 255:
        raise _ItemRejected(
            "invalid_item",
            "Поле provider_message_id превышает 255 символов.",
            event_id=event_id,
        )
    return normalized or None


def _enforce_rate_limit() -> None:
    limit = int(
        getattr(
            settings,
            "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_RATE_LIMIT_PER_MINUTE",
            60,
        )
        or 60
    )
    now_timestamp = int(timezone.now().timestamp())
    minute_bucket = now_timestamp // 60
    cache_key = f"vtelemax:message-interactions:rate:{minute_bucket}"
    if cache.add(cache_key, 1, timeout=70):
        request_count = 1
    else:
        request_count = int(cache.incr(cache_key))
    if request_count > limit:
        raise MessageInteractionCallbackError(
            "Превышено допустимое число пакетных запросов за минуту.",
            status_code=429,
            code="rate_limited",
            retry_after_seconds=max(1, 60 - (now_timestamp % 60)),
        )


def _parse_rfc3339_datetime(raw_value: Any) -> datetime | None:
    if not isinstance(raw_value, str):
        return None
    text = raw_value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(dt_timezone.utc)


def _parse_uuid_text(raw_value: Any) -> uuid.UUID | None:
    if not isinstance(raw_value, str):
        return None
    try:
        return uuid.UUID(raw_value)
    except (ValueError, AttributeError):
        return None


def _valid_event_id_text(raw_value: Any) -> str | None:
    parsed = _parse_uuid_text(raw_value)
    return str(parsed) if parsed is not None else None


def _format_rfc3339(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def _get_header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    if value is None:
        value = headers.get(name.upper())
    return str(value or "").strip()
