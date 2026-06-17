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

from guests.models import (
    DispatchTask,
    Guest,
    GuestRestaurantWindowMetrics,
    NotificationEvent,
    NotificationScenario,
    VisitHistory,
)
from guests.services.notification_registry import (
    SCENARIO_CODE_FILL_BIRTHDAY_REQUEST,
    SCENARIO_CODE_INACTIVE_30D_COUPON,
    SCENARIO_CODE_INACTIVE_7D,
    SCENARIO_CODE_MEAT_LOVER_30D,
)
from guests.services.notification_events import (
    enqueue_notification_event_from_scenario,
)

logger = logging.getLogger(__name__)


DEFAULT_SCENARIO_CODES = (
    SCENARIO_CODE_INACTIVE_7D,
    SCENARIO_CODE_FILL_BIRTHDAY_REQUEST,
    SCENARIO_CODE_INACTIVE_30D_COUPON,
    SCENARIO_CODE_MEAT_LOVER_30D,
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


def _extract_positive_int_setting(settings: dict, key: str, default: int) -> int:
    raw_value = settings.get(key, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _extract_decimal_setting(settings: dict, key: str, default: float) -> float:
    raw_value = settings.get(key, default)
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return float(default)
    return value if value >= 0 else float(default)


def _collect_fill_birthday_request_guests(
    *,
    scenario: NotificationScenario,
    limit: int,
) -> list[Guest]:
    selected_bot_ids = list(
        scenario.bot_profiles.filter(is_active=True).values_list("id", flat=True)
    )
    if not selected_bot_ids:
        return []

    from guests.models import GuestBotBinding

    binding_query = GuestBotBinding.objects.filter(
        guest__birthdate__isnull=True,
        bot_id__in=selected_bot_ids,
        bot__is_active=True,
        bot__provider_type__in=["telegram", "max", "vk"],
        is_active=True,
        is_opt_in=True,
        is_stop_sending=False,
    ).exclude(
        external_chat_id__isnull=True,
    ).exclude(
        external_chat_id="",
    )
    if scenario.target_mode == NotificationScenario.TargetMode.PRIMARY_ONLY:
        binding_query = binding_query.filter(is_primary=True)

    guest_ids = list(
        binding_query.order_by("guest_id")
        .values_list("guest_id", flat=True)
        .distinct()[: max(1, int(limit))]
    )
    if not guest_ids:
        return []
    guests_by_id = {
        int(guest.id): guest
        for guest in Guest.objects.filter(id__in=guest_ids).order_by("id")
    }
    return [guests_by_id[int(guest_id)] for guest_id in guest_ids if int(guest_id) in guests_by_id]


def run_scheduled_fill_birthday_request_scenario(
    *,
    scenario_code: str,
    limit_per_scenario: int = 1000,
    coupon_resolver: Optional[CouponResolver] = None,
    now: Optional[datetime] = None,
) -> ScenarioRunStat:
    """
    Планово просит гостей заполнить дату рождения.

    Купон здесь не выдаётся. Купонный сценарий сработает позже, когда дата рождения
    действительно появится в карточке гостя после синхронизации vtelemax.
    """
    del coupon_resolver

    safe_code = str(scenario_code or "").strip()
    if not safe_code:
        return ScenarioRunStat(scenario_code="")

    safe_limit = max(1, int(limit_per_scenario))
    current_now = now or timezone.now()
    scenario = (
        NotificationScenario.objects.select_related("template")
        .filter(
            code=safe_code,
            is_active=True,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
        )
        .first()
    )
    if scenario is None:
        logger.info(
            "Сценарий %s не активен, не найден или не относится к планировщику.",
            safe_code,
        )
        return ScenarioRunStat(scenario_code=safe_code)

    stat = ScenarioRunStat(scenario_code=safe_code)
    guests = _collect_fill_birthday_request_guests(
        scenario=scenario,
        limit=safe_limit,
    )
    stat.scanned_guests = len(guests)
    stat.matched_guests = len(guests)
    repeat_days = _extract_positive_int_setting(scenario.settings or {}, "request_repeat_days", 30)
    recent_request_after = current_now - timedelta(days=repeat_days)
    dedupe_bucket = _local_bucket_date_iso(scenario=scenario, now=current_now)

    for guest in guests:
        if repeat_days > 0 and NotificationEvent.objects.filter(
            scenario=scenario,
            guest=guest,
            created_at__gte=recent_request_after,
        ).exists():
            stat.skipped_duplicate_or_no_targets += 1
            continue

        dedupe_key = f"{scenario.code}:{guest.id}:{dedupe_bucket}" if repeat_days > 0 else f"{scenario.code}:{guest.id}"
        payload = {
            "kind": scenario.code,
            "source": "scheduled_missing_birthdate_scan",
            "request_repeat_days": repeat_days,
        }
        template_context = {
            "first_name": (guest.first_name or "").strip(),
        }
        created_tasks = enqueue_notification_event_from_scenario(
            scenario_code=scenario.code,
            guest=guest,
            dedupe_key=dedupe_key,
            source_ref=f"scheduled:{scenario.code}",
            event_source_type=NotificationEvent.SourceType.SCHEDULE,
            task_source_type=DispatchTask.SourceType.SYSTEM,
            payload=payload,
            template_context=template_context,
            fallback_message_text=(
                "Укажите дату рождения в боте, чтобы мы могли подготовить персональный подарок."
            ),
            event_at=current_now,
        )
        if created_tasks > 0:
            stat.created_tasks += int(created_tasks)
        else:
            stat.skipped_duplicate_or_no_targets += 1

    logger.info(
        "Сценарий %s: missing_birthdate_guests=%s created_tasks=%s skipped=%s",
        stat.scenario_code,
        stat.matched_guests,
        stat.created_tasks,
        stat.skipped_duplicate_or_no_targets,
    )
    return stat


def run_scheduled_meat_lover_scenario(
    *,
    scenario_code: str,
    limit_per_scenario: int = 1000,
    now: Optional[datetime] = None,
) -> ScenarioRunStat:
    """
    Выполняет schedule-сценарий сегментации "любитель мяса".

    Отбор выполняется по `guest_restaurant_window_metrics`:
    1. окно `window_days` (по умолчанию 30);
    2. минимальное число заказов `min_orders_count`;
    3. минимальный средний чек `min_avg_check_net`.
    """
    safe_code = str(scenario_code or "").strip()
    if not safe_code:
        return ScenarioRunStat(scenario_code="")

    safe_limit = max(1, int(limit_per_scenario))
    current_now = now or timezone.now()
    scenario = (
        NotificationScenario.objects.select_related("template")
        .filter(
            code=safe_code,
            is_active=True,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
        )
        .first()
    )
    if scenario is None:
        logger.info(
            "Сценарий %s не активен/не найден или не относится к trigger_type=schedule.",
            safe_code,
        )
        return ScenarioRunStat(scenario_code=safe_code)

    settings_payload = scenario.settings or {}
    window_days = _extract_positive_int_setting(settings_payload, "window_days", 30)
    min_orders_count = _extract_positive_int_setting(settings_payload, "min_orders_count", 3)
    min_avg_check_net = _extract_decimal_setting(settings_payload, "min_avg_check_net", 5000.0)
    department_id = str(settings_payload.get("department_id") or "").strip()

    stat = ScenarioRunStat(
        scenario_code=safe_code,
        inactive_days_threshold=window_days,
    )
    as_of_date = timezone.localdate()
    metrics_query = (
        GuestRestaurantWindowMetrics.objects.select_related("guest")
        .filter(
            as_of_date=as_of_date,
            window_days=window_days,
            orders_count__gte=min_orders_count,
            avg_check_net__gte=min_avg_check_net,
        )
        .order_by("id")
    )
    if department_id:
        metrics_query = metrics_query.filter(department_id=department_id)

    dedupe_bucket = _local_bucket_date_iso(scenario=scenario, now=current_now)
    for metric in metrics_query[:safe_limit]:
        if metric.guest_id is None:
            continue

        stat.scanned_guests += 1
        guest = metric.guest
        if guest is None:
            continue
        stat.matched_guests += 1

        source_ref = f"scheduled:{scenario.code}:{metric.department_id}:{dedupe_bucket}"
        dedupe_key = (
            f"{scenario.code}:{guest.id}:{metric.department_id}:"
            f"window{window_days}:{dedupe_bucket}"
        )
        payload = {
            "kind": scenario.code,
            "source": "scheduled_window_metrics",
            "window_days": window_days,
            "department_id": metric.department_id,
            "orders_count": metric.orders_count,
            "visits_count": metric.visits_count,
            "avg_check_net": str(metric.avg_check_net),
            "rating_score": str(metric.rating_score),
            "as_of_date": as_of_date.isoformat(),
        }
        template_context = {
            "first_name": (guest.first_name or "").strip(),
            "orders_count": metric.orders_count,
            "avg_check_net": str(metric.avg_check_net),
            "window_days": window_days,
            "department_id": metric.department_id,
            "rating_score": str(metric.rating_score),
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
            fallback_message_text=(
                f"У вас {metric.orders_count} заказа(ов) за {window_days} дней. "
                f"Средний чек: {metric.avg_check_net}. Приглашаем на вечер шашлыка."
            ),
            event_at=current_now,
        )

        if created_tasks > 0:
            stat.created_tasks += int(created_tasks)
        else:
            stat.skipped_duplicate_or_no_targets += 1

    logger.info(
        "Сценарий %s: window_days=%s min_orders=%s min_avg_check=%s scanned=%s matched=%s created_tasks=%s skipped=%s",
        stat.scenario_code,
        window_days,
        min_orders_count,
        min_avg_check_net,
        stat.scanned_guests,
        stat.matched_guests,
        stat.created_tasks,
        stat.skipped_duplicate_or_no_targets,
    )
    return stat


def run_scheduled_inactive_scenario(
    *,
    scenario_code: str,
    limit_per_scenario: int = 1000,
    coupon_resolver: Optional[CouponResolver] = None,
    now: Optional[datetime] = None,
) -> ScenarioRunStat:
    """
    Выполняет один schedule-сценарий неактивности по коду.

    Возвращает агрегированную статистику по одному сценарию.
    """
    safe_code = str(scenario_code or "").strip()
    if not safe_code:
        return ScenarioRunStat(scenario_code="")

    safe_limit = max(1, int(limit_per_scenario))
    current_now = now or timezone.now()

    scenario = (
        NotificationScenario.objects.select_related("template")
        .filter(
            code=safe_code,
            is_active=True,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
        )
        .first()
    )
    if scenario is None:
        logger.info(
            "Сценарий %s не активен/не найден или не относится к trigger_type=schedule.",
            safe_code,
        )
        return ScenarioRunStat(
            scenario_code=safe_code,
            inactive_days_threshold=_default_inactive_days_for_code(safe_code),
        )

    inactive_days = _extract_inactive_days(scenario)
    stat = ScenarioRunStat(
        scenario_code=scenario.code,
        inactive_days_threshold=inactive_days,
    )

    guests = _collect_candidate_guests(
        inactive_days=inactive_days,
        limit=safe_limit,
        now=current_now,
    )

    dedupe_bucket = _local_bucket_date_iso(scenario=scenario, now=current_now)
    for guest in guests:
        stat.scanned_guests += 1
        last_visit_at = getattr(guest, "last_visit_at", None)
        if last_visit_at is None:
            continue

        days_without_visits = max(0, int((current_now - last_visit_at).days))
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
            event_at=current_now,
            coupon_code=coupon_code or None,
            coupon_external_id=coupon_external_id,
            coupon_expires_at=coupon_expires_at,
        )

        if created_tasks > 0:
            stat.created_tasks += int(created_tasks)
        else:
            stat.skipped_duplicate_or_no_targets += 1

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
    return stat


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

    result: Dict[str, ScenarioRunStat] = {}
    now = timezone.now()
    for scenario_code in safe_codes:
        result[scenario_code] = run_scheduled_inactive_scenario(
            scenario_code=scenario_code,
            limit_per_scenario=safe_limit,
            coupon_resolver=coupon_resolver,
            now=now,
        )

    return result
