"""
Сервис планового запуска автоматизированных сценариев уведомлений.

Назначение модуля:
1. Поддержать каркас авто-сценариев `inactive_7d` и `inactive_30d_coupon`;
2. Находить гостей, которые давно не посещали заведение;
3. Создавать `NotificationEvent` и `DispatchTask` через единый контур
   `Scenario -> Event -> DispatchTask`.

Важно:
1. Интеграция с реальной генерацией купонов iiko на этом шаге не выполняется;
2. Для купонного сценария предусмотрена точка расширения через `coupon_resolver`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import OuterRef, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from guests.models import DispatchTask, Guest, NotificationScenario, VisitHistory
from guests.services.notification_events import (
    SCENARIO_CODE_INACTIVE_30D_COUPON,
    SCENARIO_CODE_INACTIVE_7D,
    enqueue_notification_event_from_scenario,
)

logger = logging.getLogger(__name__)


DEFAULT_SCENARIO_CODES = (
    SCENARIO_CODE_INACTIVE_7D,
    SCENARIO_CODE_INACTIVE_30D_COUPON,
)


@dataclass
class ScenarioRunStat:
    """
    Сводка выполнения одного сценария авто-уведомления.
    """

    scenario_code: str
    inactive_days_threshold: int = 0
    scanned_guests: int = 0
    matched_guests: int = 0
    created_tasks: int = 0
    skipped_without_coupon: int = 0
    skipped_duplicate_or_no_targets: int = 0


CouponResolver = Callable[[Guest, NotificationScenario], Optional[Dict[str, Any]]]


def _resolve_zoneinfo(timezone_name: str | None) -> ZoneInfo:
    """
    Возвращает корректную временную зону для сценария.
    """
    if timezone_name:
        try:
            return ZoneInfo(str(timezone_name).strip())
        except ZoneInfoNotFoundError:
            logger.warning(
                "Неизвестная timezone '%s' в NotificationScenario. "
                "Используется timezone Django.",
                timezone_name,
            )

    current_tz = timezone.get_current_timezone()
    current_tz_name = getattr(current_tz, "key", None) or str(current_tz)
    try:
        return ZoneInfo(current_tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _default_inactive_days_for_code(scenario_code: str) -> int:
    """
    Базовые пороги неактивности по коду сценария.
    """
    if scenario_code == SCENARIO_CODE_INACTIVE_7D:
        return 7
    if scenario_code == SCENARIO_CODE_INACTIVE_30D_COUPON:
        return 30
    return 7


def _extract_inactive_days(scenario: NotificationScenario) -> int:
    """
    Читает порог неактивности из `scenario.settings`.

    Если параметр отсутствует или некорректен, применяется значение по умолчанию
    для конкретного кода сценария.
    """
    default_value = _default_inactive_days_for_code(scenario.code)
    raw_value = (scenario.settings or {}).get("inactive_days", default_value)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default_value
    return value if value > 0 else default_value


def _is_coupon_required(scenario: NotificationScenario) -> bool:
    """
    Определяет, требуется ли купон для сценария.
    """
    raw_value = (scenario.settings or {}).get("coupon_required")
    if raw_value is None:
        return scenario.code == SCENARIO_CODE_INACTIVE_30D_COUPON
    return bool(raw_value)


def _parse_coupon_expires_at(value: Any) -> Optional[datetime]:
    """
    Нормализует дату истечения купона в timezone-aware datetime.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            return timezone.make_aware(value, timezone.get_current_timezone())
        return value

    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _build_coupon_payload(
    *,
    guest: Guest,
    scenario: NotificationScenario,
    coupon_resolver: Optional[CouponResolver],
) -> Dict[str, Any]:
    """
    Возвращает данные купона для сценария.

    Источники:
    1. Внешний `coupon_resolver` (приоритетно);
    2. `scenario.settings` (временный fallback-каркас).
    """
    if coupon_resolver is not None:
        resolved = coupon_resolver(guest, scenario)
        if isinstance(resolved, dict):
            return dict(resolved)

    settings_payload = dict((scenario.settings or {}).get("coupon_payload", {}))
    if settings_payload:
        return settings_payload
    return {}


def _collect_candidate_guests(*, inactive_days: int, limit: int, now: datetime) -> Iterable[Guest]:
    """
    Возвращает гостей, у которых последний визит старше порога `inactive_days`.
    """
    cutoff = now - timedelta(days=max(1, int(inactive_days)))
    last_visit_subquery = (
        VisitHistory.objects.filter(guest_id=OuterRef("pk"))
        .order_by("-visit_date")
        .values("visit_date")[:1]
    )

    queryset = (
        Guest.objects.annotate(last_visit_at=Subquery(last_visit_subquery))
        .filter(last_visit_at__isnull=False, last_visit_at__lte=cutoff)
        .order_by("id")
    )

    safe_limit = max(1, int(limit))
    return queryset[:safe_limit]


def _local_bucket_date_iso(*, scenario: NotificationScenario, now: datetime) -> str:
    """
    Возвращает локальную дату сценария в формате YYYY-MM-DD для dedupe-ключа.
    """
    tzinfo = _resolve_zoneinfo(scenario.timezone)
    return timezone.localtime(now, timezone=tzinfo).date().isoformat()


def _build_fallback_message(
    *,
    scenario: NotificationScenario,
    days_without_visits: int,
    coupon_code: str,
) -> str:
    """
    Формирует fallback-текст, если шаблон сценария пустой или невалидный.
    """
    if scenario.code == SCENARIO_CODE_INACTIVE_30D_COUPON:
        if coupon_code:
            return (
                f"Мы соскучились. Вы не были у нас {days_without_visits} дней. "
                f"Ваш персональный купон: {coupon_code}"
            )
        return f"Мы соскучились. Вы не были у нас {days_without_visits} дней."

    return f"Мы соскучились. Вы не были у нас {days_without_visits} дней."


def run_scheduled_inactive_scenarios(
    *,
    scenario_codes: Optional[Iterable[str]] = None,
    limit_per_scenario: int = 1000,
    coupon_resolver: Optional[CouponResolver] = None,
) -> Dict[str, ScenarioRunStat]:
    """
    Выполняет плановый запуск авто-сценариев для неактивных гостей.

    Механика:
    1. Берёт только активные `NotificationScenario` с `trigger_type=schedule`;
    2. Находит целевых гостей по порогу неактивности;
    3. Ставит события в новую цепочку `NotificationEvent -> DispatchTask`.
    """
    safe_limit = max(1, int(limit_per_scenario))
    safe_codes = [str(code).strip() for code in (scenario_codes or DEFAULT_SCENARIO_CODES) if str(code).strip()]
    if not safe_codes:
        return {}

    now = timezone.now()
    scenarios = {
        scenario.code: scenario
        for scenario in NotificationScenario.objects.select_related("template").filter(
            code__in=safe_codes,
            is_active=True,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
        )
    }

    result: Dict[str, ScenarioRunStat] = {}
    for scenario_code in safe_codes:
        scenario = scenarios.get(scenario_code)
        if scenario is None:
            result[scenario_code] = ScenarioRunStat(
                scenario_code=scenario_code,
                inactive_days_threshold=_default_inactive_days_for_code(scenario_code),
            )
            logger.info(
                "Сценарий %s не активен/не найден или не относится к trigger_type=schedule.",
                scenario_code,
            )
            continue

        inactive_days = _extract_inactive_days(scenario)
        stat = ScenarioRunStat(
            scenario_code=scenario.code,
            inactive_days_threshold=inactive_days,
        )

        guests = _collect_candidate_guests(
            inactive_days=inactive_days,
            limit=safe_limit,
            now=now,
        )

        dedupe_bucket = _local_bucket_date_iso(scenario=scenario, now=now)
        for guest in guests:
            stat.scanned_guests += 1
            last_visit_at = getattr(guest, "last_visit_at", None)
            if last_visit_at is None:
                continue

            days_without_visits = max(0, int((now - last_visit_at).days))
            if days_without_visits < inactive_days:
                continue
            stat.matched_guests += 1

            coupon_payload = _build_coupon_payload(
                guest=guest,
                scenario=scenario,
                coupon_resolver=coupon_resolver,
            )
            coupon_code = str(coupon_payload.get("coupon_code") or "").strip()
            if _is_coupon_required(scenario) and not coupon_code:
                stat.skipped_without_coupon += 1
                continue

            coupon_external_id = str(coupon_payload.get("coupon_external_id") or "").strip() or None
            coupon_expires_at = _parse_coupon_expires_at(coupon_payload.get("coupon_expires_at"))

            source_ref = f"scheduled:{scenario.code}:{dedupe_bucket}"
            dedupe_key = f"{scenario.code}:{guest.id}:{dedupe_bucket}"
            payload = {
                "kind": scenario.code,
                "source": "scheduled_inactive_scan",
                "inactive_days_threshold": inactive_days,
                "days_without_visits": days_without_visits,
                "last_visit_at": last_visit_at.isoformat() if hasattr(last_visit_at, "isoformat") else str(last_visit_at),
            }
            template_context = {
                "first_name": (guest.first_name or "").strip(),
                "days_without_visits": days_without_visits,
                "coupon_code": coupon_code,
            }

            created_tasks = enqueue_notification_event_from_scenario(
                scenario_code=scenario.code,
                guest=guest,
                dedupe_key=dedupe_key,
                source_ref=source_ref,
                event_source_type="schedule",
                task_source_type=DispatchTask.SourceType.SYSTEM,
                payload=payload,
                template_context=template_context,
                fallback_message_text=_build_fallback_message(
                    scenario=scenario,
                    days_without_visits=days_without_visits,
                    coupon_code=coupon_code,
                ),
                event_at=now,
                coupon_code=coupon_code or None,
                coupon_external_id=coupon_external_id,
                coupon_expires_at=coupon_expires_at,
            )

            if created_tasks > 0:
                stat.created_tasks += int(created_tasks)
            else:
                stat.skipped_duplicate_or_no_targets += 1

        result[scenario_code] = stat

        logger.info(
            "Сценарий %s: threshold=%s scanned=%s matched=%s created_tasks=%s "
            "skipped_without_coupon=%s skipped_duplicate_or_no_targets=%s",
            stat.scenario_code,
            stat.inactive_days_threshold,
            stat.scanned_guests,
            stat.matched_guests,
            stat.created_tasks,
            stat.skipped_without_coupon,
            stat.skipped_duplicate_or_no_targets,
        )

    return result
