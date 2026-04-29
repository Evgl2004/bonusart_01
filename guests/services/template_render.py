from datetime import date
from typing import Any

from django.template import Context, Template


class _SafeTemplateContext(dict):
    """
    Безопасный контекст для format_map.

    Если ключ отсутствует, оставляем плейсхолдер без изменений.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"

def calc_age(birthdate):
    if not birthdate:
        return ""
    today = date.today()
    years = today.year - birthdate.year
    if (today.month, today.day) < (birthdate.month, birthdate.day):
        years -= 1
    return years

def render_message_for_guest(message_text, guest, extra_context: dict[str, Any] | None = None):
    """
    Рендерит сообщение для гостя.

    Поддерживает оба исторически используемых формата:
    1. Django-стиль: `{{ first_name }}`;
    2. format-стиль: `{first_name}`.
    """
    context = {
        "first_name": guest.first_name or "",
        "last_name": guest.last_name or "",
        "phone": guest.phone or "",
        "email": guest.email or "",
        "birthdate": guest.birthdate.strftime("%d.%m.%Y") if guest.birthdate else "",
        "age": calc_age(guest.birthdate),
    }
    if extra_context:
        context.update(extra_context)

    normalized_context = {
        key: ("" if value is None else value)
        for key, value in context.items()
    }

    # Почтовые/рассылочные шаблоны часто используют эти переменные.
    # Заполняем безопасные значения, чтобы в сообщениях не оставались "сырые" маркеры.
    coupon_value = str(
        normalized_context.get("coupon_code")
        or normalized_context.get("courpon_code")  # legacy-опечатка в части шаблонов
        or ""
    )
    normalized_context.setdefault("coupon_code", coupon_value)
    normalized_context.setdefault("courpon_code", coupon_value)
    normalized_context.setdefault(
        "days_without_visits",
        normalized_context.get("days_without_visits") or "",
    )

    django_rendered = Template(message_text).render(Context(normalized_context))

    try:
        return django_rendered.format_map(_SafeTemplateContext(normalized_context))
    except Exception:
        # Если текст содержит неподдерживаемую комбинацию фигурных скобок,
        # возвращаем результат Django-рендера без падения предпросмотра.
        return django_rendered
