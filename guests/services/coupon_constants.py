"""
Общие константы купонного контура.

В модуле храним единые значения для "общих" купонов (доступных для всех заведений),
чтобы не дублировать магические строки в формах, сервисах и командах.
"""

from __future__ import annotations

COUPON_VENUE_GLOBAL_CODE = "__global__"
COUPON_VENUE_GLOBAL_NAME = "Вся сеть"
COUPON_MESSAGE_FOOTER = 'Доступные вам купоны можно посмотреть в меню "Купоны" раздела "Профиль".'


def is_coupon_global_venue(venue_code: str | None) -> bool:
    """
    Проверяет, что код заведения относится к "общему" купону.
    """
    return str(venue_code or "").strip() == COUPON_VENUE_GLOBAL_CODE


def append_coupon_message_footer(message_text: str | None) -> str:
    """
    Добавляет к тексту купонной рассылки стандартную подсказку про меню купонов.

    Функция идемпотентна: если подсказка уже есть в тексте, повторно она
    не добавляется.
    """
    normalized_text = str(message_text or "").strip()
    if not normalized_text:
        return COUPON_MESSAGE_FOOTER
    if COUPON_MESSAGE_FOOTER in normalized_text:
        return normalized_text
    return f"{normalized_text}\n\n{COUPON_MESSAGE_FOOTER}"
