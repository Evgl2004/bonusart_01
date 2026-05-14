"""
Общие константы купонного контура.

В модуле храним единые значения для "общих" купонов (доступных для всех заведений),
чтобы не дублировать магические строки в формах, сервисах и командах.
"""

from __future__ import annotations

COUPON_VENUE_GLOBAL_CODE = "__global__"
COUPON_VENUE_GLOBAL_NAME = "Общий"


def is_coupon_global_venue(venue_code: str | None) -> bool:
    """
    Проверяет, что код заведения относится к "общему" купону.
    """
    return str(venue_code or "").strip() == COUPON_VENUE_GLOBAL_CODE

