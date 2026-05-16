from __future__ import annotations

from django.db.models import Max

from guests.models import TerminalDepartmentMap
from guests.services.coupon_constants import COUPON_VENUE_GLOBAL_CODE, COUPON_VENUE_GLOBAL_NAME


def build_coupon_venue_choices(
    *,
    include_empty: bool = True,
    existing_venue_code: str | None = None,
    existing_venue_name: str | None = None,
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """
    Формирует единый список заведений для купонных форм.

    Источник данных:
    1. активные сопоставления `TerminalDepartmentMap`;
    2. служебный вариант "общий купон" для всех заведений;
    3. текущее сохранённое значение, если оно уже есть в объекте, но отсутствует в справочнике.
    """

    rows = (
        TerminalDepartmentMap.objects.filter(is_active=True)
        .exclude(department_id="")
        .values("department_id")
        .annotate(department_name=Max("department_name"))
        .order_by("department_name", "department_id")
    )

    choices: list[tuple[str, str]] = []
    if include_empty:
        choices.append(("", "— Выберите заведение —"))

    venue_map: dict[str, str] = {
        COUPON_VENUE_GLOBAL_CODE: COUPON_VENUE_GLOBAL_NAME,
    }
    choices.append((COUPON_VENUE_GLOBAL_CODE, f"{COUPON_VENUE_GLOBAL_NAME} (для всех заведений)"))

    for row in rows:
        dep_id = str(row.get("department_id") or "").strip()
        if not dep_id:
            continue
        dep_name = str(row.get("department_name") or "").strip() or dep_id
        venue_map[dep_id] = dep_name
        choices.append((dep_id, f"{dep_name} ({dep_id})"))

    current_code = str(existing_venue_code or "").strip()
    if current_code and current_code not in venue_map:
        current_name = str(existing_venue_name or "").strip() or current_code
        venue_map[current_code] = current_name
        choices.append((current_code, f"{current_name} ({current_code})"))

    return choices, venue_map
