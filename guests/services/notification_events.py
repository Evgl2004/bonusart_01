"""
Сервисы работы с NotificationScenario/NotificationEvent.

Модуль реализует:
1. дедупликацию событий по `(scenario, dedupe_key)`;
2. расчёт `planned_send_at` (immediate/uniform);
3. создание `DispatchTask` через унифицированный producer.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import F
from django.template import Context, Template
from django.utils import timezone

from guests.models import DispatchTask, Guest, NotificationEvent, NotificationScenario
from guests.services.notification_registry import (
    SCENARIO_CODE_BALANCE_CHANGED,
    SCENARIO_CODE_INACTIVE_30D_COUPON,
    SCENARIO_CODE_INACTIVE_7D,
)
from guests.services.template_render import render_message_for_guest
from guests.services.universal_queue.notification_producer import enqueue_guest_notification_tasks

logger = logging.getLogger(__name__)


class ScenarioNotConfiguredError(RuntimeError):
    """Сценарий не найден или выключен."""


class _SafeTemplateContext(dict):
    """
    Безопасный словарь для форматирования шаблона.

    Если ключ не найден, плейсхолдер остаётся в тексте как есть.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _resolve_zoneinfo(timezone_name: str | None) -> ZoneInfo:
    """
    Возвращает tzinfo по имени временной зоны.

    При ошибке используем текущую таймзону Django.
    """
    if timezone_name:
        try:
            return ZoneInfo(str(timezone_name).strip())
        except ZoneInfoNotFoundError:
            logger.warning("Неизвестная timezone '%s', используется текущая timezone Django.", timezone_name)

    current_tz = timezone.get_current_timezone()
    current_tz_name = getattr(current_tz, "key", None) or str(current_tz)
    try:
        return ZoneInfo(current_tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _calculate_planned_send_at(
    *,
    scenario: NotificationScenario,
    now: datetime,
) -> datetime:
    """
    Рассчитывает плановое время отправки события.

    Правила:
    1. `immediate` -> отправка сразу (`now`);
    2. `uniform` -> случайное время в окне `send_window_begin/send_window_end`.
       Если окно не задано, отправка сразу.
    """
    if scenario.distribution_mode == NotificationScenario.DistributionMode.IMMEDIATE:
        return now

    if scenario.distribution_mode != NotificationScenario.DistributionMode.UNIFORM:
        return now

    if scenario.send_window_begin is None or scenario.send_window_end is None:
        return now

    tzinfo = _resolve_zoneinfo(scenario.timezone)
    now_local = timezone.localtime(now, timezone=tzinfo)

    window_start_local = datetime.combine(now_local.date(), scenario.send_window_begin, tzinfo=tzinfo)
    window_end_local = datetime.combine(now_local.date(), scenario.send_window_end, tzinfo=tzinfo)

    # Окно может переходить через полночь.
    if window_end_local <= window_start_local:
        window_end_local += timedelta(days=1)

    # Если сегодня окно уже закончено, планируем на следующее окно.
    if now_local > window_end_local:
        window_start_local += timedelta(days=1)
        window_end_local += timedelta(days=1)

    # Если сейчас до начала окна, базовая точка — старт окна.
    base_local = window_start_local if now_local < window_start_local else now_local
    delta_seconds = int((window_end_local - base_local).total_seconds())
    if delta_seconds <= 0:
        planned_local = base_local
    else:
        planned_local = base_local + timedelta(seconds=random.randint(0, delta_seconds))

    return planned_local.astimezone(timezone.get_current_timezone())


def _render_scenario_message(
    *,
    scenario: NotificationScenario,
    guest: Guest,
    template_context: Optional[Dict[str, Any]],
    fallback_message_text: str,
) -> str:
    """
    Рендерит текст уведомления по шаблону сценария.

    Если шаблон пустой или форматирование упало — используем fallback-текст.
    """
    template_text = str(getattr(scenario.template, "message_text", "") or "").strip()
    if not template_text:
        return str(fallback_message_text or "").strip()

    try:
        rendered = render_message_for_guest(
            template_text,
            guest,
            extra_context=template_context,
        ).strip()
        if rendered:
            return rendered
    except Exception:
        logger.exception(
            "Notification template render failed with guest context for scenario_code=%s. Falling back to legacy render.",
            scenario.code,
        )

    raw_context = template_context or {}
    safe_context = _SafeTemplateContext(raw_context)
    try:
        django_rendered = Template(template_text).render(Context(raw_context))
    except Exception:
        logger.exception(
            "Ошибка Django-рендера шаблона scenario_code=%s. Пробуем format_map напрямую.",
            scenario.code,
        )
        django_rendered = template_text

    try:
        rendered = django_rendered.format_map(safe_context).strip()
        if rendered:
            return rendered
    except Exception:
        logger.exception(
            "Ошибка форматирования шаблона scenario_code=%s. Используем fallback-текст.",
            scenario.code,
        )
    return str(fallback_message_text or "").strip()


def _normalize_event_source_type(source_type: str) -> str:
    allowed = {
        NotificationEvent.SourceType.WEBHOOK,
        NotificationEvent.SourceType.SCHEDULE,
        NotificationEvent.SourceType.MANUAL,
    }
    value = str(source_type or "").strip().lower()
    return value if value in allowed else NotificationEvent.SourceType.WEBHOOK


def _normalize_route_priority(value: Optional[str]) -> Optional[str]:
    """
    Нормализует override-приоритет маршрутизации.
    """
    if value is None:
        return None
    normalized = str(value or "").strip().lower()
    allowed = {
        NotificationScenario.Priority.HIGH,
        NotificationScenario.Priority.NORMAL,
        NotificationScenario.Priority.BULK,
    }
    return normalized if normalized in allowed else None


def _normalize_route_target_mode(value: Optional[str]) -> Optional[str]:
    """
    Нормализует override-режим выбора целей.
    """
    if value is None:
        return None
    normalized = str(value or "").strip().lower()
    allowed = {
        NotificationScenario.TargetMode.PRIMARY_ONLY,
        NotificationScenario.TargetMode.ALL_BOTS,
    }
    return normalized if normalized in allowed else None


def _normalize_route_bot_profile_ids(value: Optional[Iterable[int]]) -> Optional[list[int]]:
    """
    Нормализует список bot_profile_id для явного override.
    """
    if value is None:
        return None

    if isinstance(value, (int, str)):
        raw_values: Iterable[Any] = [value]
    else:
        raw_values = value

    normalized: list[int] = []
    for raw in raw_values:
        try:
            bot_profile_id = int(raw)
        except (TypeError, ValueError):
            continue
        if bot_profile_id <= 0:
            continue
        if bot_profile_id not in normalized:
            normalized.append(bot_profile_id)
    return normalized


def _resolve_effective_routing(
    *,
    scenario: NotificationScenario,
    route_priority: Optional[str],
    route_target_mode: Optional[str],
    route_allowed_bot_profile_ids: Optional[Iterable[int]],
) -> tuple[str, str, Optional[list[int]]]:
    """
    Возвращает эффективные параметры маршрутизации.

    Правило приоритета:
    1. Явный override из вызова (если валиден);
    2. Значение из NotificationScenario.
    """
    effective_priority = scenario.priority
    normalized_route_priority = _normalize_route_priority(route_priority)
    if normalized_route_priority is not None:
        effective_priority = normalized_route_priority
    elif route_priority is not None:
        logger.warning(
            "Передан невалидный route_priority='%s' для scenario=%s. Используется приоритет сценария '%s'.",
            route_priority,
            scenario.code,
            scenario.priority,
        )

    effective_target_mode = scenario.target_mode
    normalized_route_target_mode = _normalize_route_target_mode(route_target_mode)
    if normalized_route_target_mode is not None:
        effective_target_mode = normalized_route_target_mode
    elif route_target_mode is not None:
        logger.warning(
            "Передан невалидный route_target_mode='%s' для scenario=%s. Используется режим сценария '%s'.",
            route_target_mode,
            scenario.code,
            scenario.target_mode,
        )

    scenario_bot_profile_ids = list(
        scenario.bot_profile_links.values_list("bot_profile_id", flat=True)
    )
    effective_allowed_bot_profile_ids: Optional[list[int]] = (
        scenario_bot_profile_ids if scenario_bot_profile_ids else None
    )
    normalized_route_bot_ids = _normalize_route_bot_profile_ids(route_allowed_bot_profile_ids)
    if route_allowed_bot_profile_ids is not None:
        if normalized_route_bot_ids:
            effective_allowed_bot_profile_ids = normalized_route_bot_ids
        else:
            logger.warning(
                "Передан пустой/некорректный route_allowed_bot_profile_ids для scenario=%s. "
                "Используется список ботов сценария.",
                scenario.code,
            )

    return effective_priority, effective_target_mode, effective_allowed_bot_profile_ids


def _normalize_event_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Нормализует payload события в безопасный словарь для JSONField.

    Если передан неподдерживаемый тип, сохраняем диагностическую структуру,
    чтобы не ронять создание события и сохранить контекст в БД.
    """
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)

    logger.warning(
        "NotificationEvent payload имеет неподдерживаемый тип '%s', используется fallback-структура.",
        type(payload).__name__,
    )
    return {
        "payload_error": "invalid_payload_type",
        "payload_type": type(payload).__name__,
        "payload_preview": str(payload)[:500],
    }


def _scenario_day_bounds(
    *,
    timezone_name: str | None,
    dt: datetime,
) -> tuple[datetime, datetime]:
    """
    Возвращает границы суток сценария в текущей timezone Django.

    Нужен для корректного подсчёта дневных лимитов в локальной зоне сценария.
    """
    tzinfo = _resolve_zoneinfo(timezone_name)
    local_dt = timezone.localtime(dt, timezone=tzinfo)
    day_start_local = datetime.combine(local_dt.date(), dt_time.min, tzinfo=tzinfo)
    day_end_local = day_start_local + timedelta(days=1)
    current_tz = timezone.get_current_timezone()
    return day_start_local.astimezone(current_tz), day_end_local.astimezone(current_tz)


def _apply_scenario_send_limits(
    *,
    scenario: NotificationScenario,
    guest: Guest,
    planned_send_at: datetime,
) -> tuple[datetime, Optional[str]]:
    """
    Проверяет ограничения сценария отправки для одного гостя.

    Возвращает:
    1. Итоговое время `planned_send_at` с учётом ограничений;
    2. Текст причины переноса (если перенос применён).
    """
    candidate = planned_send_at
    defer_reasons: list[str] = []

    base_queryset = NotificationEvent.objects.filter(
        scenario=scenario,
        guest=guest,
        status=NotificationEvent.Status.TASK_CREATED,
        planned_send_at__isnull=False,
    )

    cooldown_minutes = int(getattr(scenario, "cooldown_minutes", 0) or 0)
    if cooldown_minutes > 0:
        latest_planned_send_at = (
            base_queryset.order_by("-planned_send_at")
            .values_list("planned_send_at", flat=True)
            .first()
        )
        if latest_planned_send_at is not None:
            cooldown_allowed_at = latest_planned_send_at + timedelta(minutes=cooldown_minutes)
            if candidate < cooldown_allowed_at:
                candidate = cooldown_allowed_at
                defer_reasons.append(f"cooldown:{cooldown_minutes}m")

    max_per_day = getattr(scenario, "max_per_day_per_guest", None)
    if max_per_day is not None:
        try:
            max_per_day_int = int(max_per_day)
        except (TypeError, ValueError):
            max_per_day_int = 0

        if max_per_day_int > 0:
            for _ in range(366):
                day_start, day_end = _scenario_day_bounds(
                    timezone_name=getattr(scenario, "timezone", None),
                    dt=candidate,
                )
                sent_on_day = base_queryset.filter(
                    planned_send_at__gte=day_start,
                    planned_send_at__lt=day_end,
                ).count()
                if sent_on_day < max_per_day_int:
                    break
                candidate = candidate + timedelta(days=1)
                if f"max_per_day:{max_per_day_int}" not in defer_reasons:
                    defer_reasons.append(f"max_per_day:{max_per_day_int}")

    if candidate <= planned_send_at:
        return planned_send_at, None

    return candidate, ", ".join(defer_reasons) if defer_reasons else "send_limits"


@transaction.atomic
def create_notification_event(
    *,
    scenario_code: str,
    guest: Guest,
    dedupe_key: str,
    source_ref: str = "",
    event_source_type: str = NotificationEvent.SourceType.WEBHOOK,
    task_source_type: str = DispatchTask.SourceType.SYSTEM,
    payload: Optional[Dict[str, Any]] = None,
    template_context: Optional[Dict[str, Any]] = None,
    fallback_message_text: str = "",
    event_at: Optional[datetime] = None,
    coupon_code: Optional[str] = None,
    coupon_external_id: Optional[str] = None,
    coupon_expires_at: Optional[datetime] = None,
    route_priority: Optional[str] = None,
    route_target_mode: Optional[str] = None,
    route_allowed_bot_profile_ids: Optional[Iterable[int]] = None,
    allow_inactive_scenario: bool = False,
    planned_send_at_override: Optional[datetime] = None,
    skip_send_limits: bool = False,
) -> int:
    """
    Создаёт NotificationEvent и ставит задачи в DispatchTask по сценарию.

    Режим маршрутизации:
    1. По умолчанию используются настройки NotificationScenario из БД;
    2. Любой параметр `route_*` (если передан и валиден) переопределяет
       соответствующее значение сценария в рамках текущего вызова.

    Возвращает:
    1. `0`, если событие дублируется или нет целей отправки;
    2. количество созданных задач DispatchTask в остальных случаях.
    """
    if guest is None:
        return 0

    safe_scenario_code = str(scenario_code or "").strip()
    safe_dedupe_key = str(dedupe_key or "").strip()
    if not safe_scenario_code or not safe_dedupe_key:
        logger.warning("Сценарий/ключ дедупликации не задан: scenario='%s', dedupe='%s'", scenario_code, dedupe_key)
        return 0

    scenario_filter = {"code": safe_scenario_code}
    if not allow_inactive_scenario:
        scenario_filter["is_active"] = True

    scenario = (
        NotificationScenario.objects.select_related("template")
        .prefetch_related("bot_profile_links")
        .filter(**scenario_filter)
        .first()
    )
    if scenario is None:
        if allow_inactive_scenario:
            message = f"Сценарий '{safe_scenario_code}' не найден."
        else:
            message = f"Сценарий '{safe_scenario_code}' не найден или выключен."
        raise ScenarioNotConfiguredError(
            message
        )

    now = timezone.now()
    safe_event_at = event_at or now
    if timezone.is_naive(safe_event_at):
        safe_event_at = timezone.make_aware(safe_event_at, timezone.get_current_timezone())

    if planned_send_at_override is not None:
        planned_send_at = planned_send_at_override
        if timezone.is_naive(planned_send_at):
            planned_send_at = timezone.make_aware(planned_send_at, timezone.get_current_timezone())
    else:
        planned_send_at = _calculate_planned_send_at(scenario=scenario, now=now)
    safe_payload = _normalize_event_payload(payload)
    safe_event_source_type = _normalize_event_source_type(event_source_type)

    event_defaults = {
        "guest": guest,
        "source_type": safe_event_source_type,
        "source_ref": str(source_ref or "").strip() or None,
        "status": NotificationEvent.Status.NEW,
        "event_at": safe_event_at,
        "planned_send_at": planned_send_at,
        "payload": safe_payload,
        "coupon_code": (str(coupon_code).strip() if coupon_code else None),
        "coupon_external_id": (str(coupon_external_id).strip() if coupon_external_id else None),
        "coupon_expires_at": coupon_expires_at,
    }
    event, created = NotificationEvent.objects.get_or_create(
        scenario=scenario,
        dedupe_key=safe_dedupe_key,
        defaults=event_defaults,
    )
    if not created:
        NotificationEvent.objects.filter(pk=event.pk).update(
            duplicate_hits=F("duplicate_hits") + 1,
            last_duplicate_at=now,
            updated_at=now,
        )
        logger.info(
            "NotificationEvent duplicate: scenario=%s dedupe_key=%s guest_id=%s",
            scenario.code,
            safe_dedupe_key,
            guest.id,
        )
        return 0

    if not skip_send_limits:
        adjusted_planned_send_at, defer_reason = _apply_scenario_send_limits(
            scenario=scenario,
            guest=guest,
            planned_send_at=planned_send_at,
        )
        if adjusted_planned_send_at != planned_send_at:
            planned_send_at = adjusted_planned_send_at
            event_payload = dict(event.payload or {})
            event_payload["deferred"] = {
                "reason": defer_reason or "send_limits",
                "deferred_until": planned_send_at.isoformat(),
            }
            event.planned_send_at = planned_send_at
            event.payload = event_payload
            event.save(update_fields=["planned_send_at", "payload", "updated_at"])
            logger.info(
                "NotificationEvent id=%s scenario=%s guest_id=%s отложено по лимитам до %s (%s)",
                event.id,
                scenario.code,
                guest.id,
                planned_send_at.isoformat(),
                defer_reason or "send_limits",
            )

    message_text = _render_scenario_message(
        scenario=scenario,
        guest=guest,
        template_context=template_context,
        fallback_message_text=fallback_message_text,
    )
    if not message_text:
        event.status = NotificationEvent.Status.ERROR
        event.error_text = "Пустой текст уведомления после рендера шаблона."
        event.save(update_fields=["status", "error_text", "updated_at"])
        logger.warning(
            "NotificationEvent id=%s: пустой текст, задачи DispatchTask не создавались.",
            event.id,
        )
        return 0

    effective_priority, effective_target_mode, effective_allowed_bot_profile_ids = _resolve_effective_routing(
        scenario=scenario,
        route_priority=route_priority,
        route_target_mode=route_target_mode,
        route_allowed_bot_profile_ids=route_allowed_bot_profile_ids,
    )

    dispatch_payload = {
        **safe_payload,
        "notification_event_id": event.id,
        "notification_scenario_code": scenario.code,
        "effective_routing": {
            "priority": effective_priority,
            "target_mode": effective_target_mode,
            "allowed_bot_profile_ids": effective_allowed_bot_profile_ids or [],
        },
    }
    created_count = enqueue_guest_notification_tasks(
        guest=guest,
        message_text=message_text,
        source_type=task_source_type,
        source_key=f"{scenario.code}:{safe_dedupe_key}",
        priority=effective_priority,
        primary_only=(effective_target_mode == NotificationScenario.TargetMode.PRIMARY_ONLY),
        payload=dispatch_payload,
        notification_scenario=scenario,
        notification_event=event,
        available_at=planned_send_at,
        allowed_bot_profile_ids=effective_allowed_bot_profile_ids,
    )

    if created_count > 0:
        event.status = NotificationEvent.Status.TASK_CREATED
        event.error_text = None
    else:
        event.status = NotificationEvent.Status.SKIPPED
        event.error_text = "Нет доступных целей отправки (binding/bot profile)."
    event.save(update_fields=["status", "error_text", "updated_at"])
    return created_count


def enqueue_notification_event_from_scenario(
    *,
    scenario_code: str,
    guest: Guest,
    dedupe_key: str,
    source_ref: str = "",
    event_source_type: str = NotificationEvent.SourceType.WEBHOOK,
    task_source_type: str = DispatchTask.SourceType.SYSTEM,
    payload: Optional[Dict[str, Any]] = None,
    template_context: Optional[Dict[str, Any]] = None,
    fallback_message_text: str = "",
    event_at: Optional[datetime] = None,
    coupon_code: Optional[str] = None,
    coupon_external_id: Optional[str] = None,
    coupon_expires_at: Optional[datetime] = None,
) -> int:
    """
    Совместимый адаптер старого API.

    Использует только настройки сценария из БД (без явных route-override).
    """
    return create_notification_event(
        scenario_code=scenario_code,
        guest=guest,
        dedupe_key=dedupe_key,
        source_ref=source_ref,
        event_source_type=event_source_type,
        task_source_type=task_source_type,
        payload=payload,
        template_context=template_context,
        fallback_message_text=fallback_message_text,
        event_at=event_at,
        coupon_code=coupon_code,
        coupon_external_id=coupon_external_id,
        coupon_expires_at=coupon_expires_at,
    )
