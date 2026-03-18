"""
Реестр обработчиков выполнения сценариев `NotificationScenario`.

Модуль описывает соответствие:
`scenario_code -> handler`, чтобы запуск сценариев выполнялся
через единый централизованный слой.

На текущем этапе реестр покрывает:
1. плановые (`trigger_type=schedule`) сценарии;
2. webhook-сценарий `balance_changed`.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, Optional

from guests.services.notification_registry import (
    SCENARIO_CODE_BALANCE_CHANGED,
    SCENARIO_CODE_INACTIVE_30D_COUPON,
    SCENARIO_CODE_INACTIVE_7D,
)
from guests.services.notification_scenarios import (
    CouponResolver,
    ScenarioRunStat,
    run_scheduled_inactive_scenario,
)

logger = logging.getLogger(__name__)


SCHEDULE_SCENARIO_HANDLERS = {
    SCENARIO_CODE_INACTIVE_7D: run_scheduled_inactive_scenario,
    SCENARIO_CODE_INACTIVE_30D_COUPON: run_scheduled_inactive_scenario,
}

DEFAULT_SCHEDULE_SCENARIO_CODES = tuple(SCHEDULE_SCENARIO_HANDLERS.keys())


def get_registered_schedule_scenario_codes() -> tuple[str, ...]:
    """
    Возвращает коды сценариев, у которых есть handler для планового запуска.
    """
    return tuple(SCHEDULE_SCENARIO_HANDLERS.keys())


def run_schedule_scenario_by_code(
    *,
    scenario_code: str,
    limit_per_scenario: int = 1000,
    coupon_resolver: Optional[CouponResolver] = None,
) -> ScenarioRunStat:
    """
    Запускает handler планового сценария по коду.

    Если handler не зарегистрирован, возвращает пустую статистику
    и пишет диагностический warning в лог.
    """
    safe_code = str(scenario_code or "").strip()
    if not safe_code:
        return ScenarioRunStat(scenario_code="")

    handler = SCHEDULE_SCENARIO_HANDLERS.get(safe_code)
    if handler is None:
        logger.warning(
            "Для scenario_code=%s не найден schedule-handler в реестре.",
            safe_code,
        )
        return ScenarioRunStat(scenario_code=safe_code)

    return handler(
        scenario_code=safe_code,
        limit_per_scenario=limit_per_scenario,
        coupon_resolver=coupon_resolver,
    )


def run_registered_schedule_scenarios(
    *,
    scenario_codes: Optional[Iterable[str]] = None,
    limit_per_scenario: int = 1000,
    coupon_resolver: Optional[CouponResolver] = None,
) -> Dict[str, ScenarioRunStat]:
    """
    Запускает набор плановых сценариев через реестр `code -> handler`.
    """
    safe_codes = [
        str(code).strip()
        for code in (scenario_codes or DEFAULT_SCHEDULE_SCENARIO_CODES)
        if str(code).strip()
    ]
    if not safe_codes:
        return {}

    result: Dict[str, ScenarioRunStat] = {}
    for code in safe_codes:
        result[code] = run_schedule_scenario_by_code(
            scenario_code=code,
            limit_per_scenario=limit_per_scenario,
            coupon_resolver=coupon_resolver,
        )
    return result


def _run_balance_changed_webhook_handler(
    *,
    webhook: dict,
    is_enabled: bool = True,
    priority: str = "high",
    primary_only: bool = True,
) -> int:
    """
    Handler webhook-сценария `balance_changed`.

    Импорт выполняется локально, чтобы избежать циклической зависимости
    между `webhooks.py` и реестром обработчиков.
    """
    from guests.services.balance_notifications import enqueue_balance_notification_from_webhook

    return int(
        enqueue_balance_notification_from_webhook(
            webhook=webhook,
            is_enabled=is_enabled,
            priority=priority,
            primary_only=primary_only,
        )
    )


WEBHOOK_SCENARIO_HANDLERS = {
    SCENARIO_CODE_BALANCE_CHANGED: _run_balance_changed_webhook_handler,
}


def run_webhook_scenario_by_code(
    *,
    scenario_code: str,
    webhook: dict,
    is_enabled: bool = True,
    priority: str = "high",
    primary_only: bool = True,
) -> int:
    """
    Запускает webhook-сценарий по коду через реестр `code -> handler`.

    Возвращает количество созданных задач DispatchTask.
    """
    safe_code = str(scenario_code or "").strip()
    if not safe_code:
        return 0

    handler = WEBHOOK_SCENARIO_HANDLERS.get(safe_code)
    if handler is None:
        logger.warning(
            "Для webhook scenario_code=%s не найден handler в реестре.",
            safe_code,
        )
        return 0

    return int(
        handler(
            webhook=webhook,
            is_enabled=is_enabled,
            priority=priority,
            primary_only=primary_only,
        )
    )
