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
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from guests.models import DispatchTask, Guest, NotificationEvent, NotificationScenario
from guests.services.universal_queue.notification_producer import enqueue_guest_notification_tasks

logger = logging.getLogger(__name__)


SCENARIO_CODE_BALANCE_CHANGED = "balance_changed"
SCENARIO_CODE_INACTIVE_7D = "inactive_7d"
SCENARIO_CODE_INACTIVE_30D_COUPON = "inactive_30d_coupon"


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

    context = _SafeTemplateContext(template_context or {})
    try:
        rendered = template_text.format_map(context).strip()
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


@transaction.atomic
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
    Создаёт NotificationEvent и ставит задачи в DispatchTask по сценарию.

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

    scenario = (
        NotificationScenario.objects.select_related("template")
        .prefetch_related("bot_profile_links")
        .filter(code=safe_scenario_code, is_active=True)
        .first()
    )
    if scenario is None:
        raise ScenarioNotConfiguredError(
            f"Сценарий '{safe_scenario_code}' не найден или выключен."
        )

    now = timezone.now()
    safe_event_at = event_at or now
    if timezone.is_naive(safe_event_at):
        safe_event_at = timezone.make_aware(safe_event_at, timezone.get_current_timezone())

    planned_send_at = _calculate_planned_send_at(scenario=scenario, now=now)
    safe_payload = dict(payload or {})
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

    message_text = _render_scenario_message(
        scenario=scenario,
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

    allowed_bot_profile_ids = list(
        scenario.bot_profile_links.values_list("bot_profile_id", flat=True)
    )

    dispatch_payload = {
        **safe_payload,
        "notification_event_id": event.id,
        "notification_scenario_code": scenario.code,
    }
    created_count = enqueue_guest_notification_tasks(
        guest=guest,
        message_text=message_text,
        source_type=task_source_type,
        source_key=f"{scenario.code}:{safe_dedupe_key}",
        priority=scenario.priority,
        primary_only=(scenario.target_mode == NotificationScenario.TargetMode.PRIMARY_ONLY),
        payload=dispatch_payload,
        notification_scenario=scenario,
        notification_event=event,
        available_at=planned_send_at,
        allowed_bot_profile_ids=allowed_bot_profile_ids or None,
    )

    if created_count > 0:
        event.status = NotificationEvent.Status.TASK_CREATED
        event.error_text = None
    else:
        event.status = NotificationEvent.Status.SKIPPED
        event.error_text = "Нет доступных целей отправки (binding/bot profile)."
    event.save(update_fields=["status", "error_text", "updated_at"])
    return created_count
