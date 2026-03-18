"""
Реестр допустимых кодов сценариев авто-уведомлений.

Модуль хранит единый список кодов, которые разрешено использовать
в `NotificationScenario.code`, и предоставляет вспомогательные функции:
1. получение кодов;
2. получение choices для форм/админки;
3. проверка, зарегистрирован ли код.
"""

from __future__ import annotations

SCENARIO_CODE_BALANCE_CHANGED = "balance_changed"
SCENARIO_CODE_INACTIVE_7D = "inactive_7d"
SCENARIO_CODE_INACTIVE_30D_COUPON = "inactive_30d_coupon"


REGISTERED_NOTIFICATION_SCENARIOS: tuple[tuple[str, str], ...] = (
    (SCENARIO_CODE_BALANCE_CHANGED, "Изменение баланса"),
    (SCENARIO_CODE_INACTIVE_7D, "Не был 7 дней"),
    (SCENARIO_CODE_INACTIVE_30D_COUPON, "Не был 30 дней + купон"),
)


def get_registered_notification_scenario_codes() -> tuple[str, ...]:
    """
    Возвращает кортеж зарегистрированных кодов сценариев.
    """
    return tuple(code for code, _ in REGISTERED_NOTIFICATION_SCENARIOS)


def get_registered_notification_scenario_code_choices() -> tuple[tuple[str, str], ...]:
    """
    Возвращает choices для UI-форм и админки.
    """
    return REGISTERED_NOTIFICATION_SCENARIOS


def is_registered_notification_scenario_code(code: str | None) -> bool:
    """
    Проверяет, зарегистрирован ли код сценария в реестре.
    """
    normalized = str(code or "").strip()
    if not normalized:
        return False
    return normalized in get_registered_notification_scenario_codes()
