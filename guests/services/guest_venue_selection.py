"""
Сервис отбора гостей по связи с заведением.

Используется как общий слой для разовых маркетинговых рассылок:
экран «Гости» собирает аудиторию, а обычная рассылка отправляет сообщение.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from django.db.models import Max, Sum

from guests.models import GuestRestaurantDailyOrderFact


VENUE_SELECTION_VISITED_ONCE = "visited_once"
VENUE_SELECTION_FAVORITE = "favorite"
VENUE_SELECTION_LAST_VISIT = "last_visit"

VENUE_SELECTION_MODE_CHOICES = (
    (VENUE_SELECTION_VISITED_ONCE, "Был хотя бы 1 раз"),
    (VENUE_SELECTION_FAVORITE, "Любимое заведение"),
    (VENUE_SELECTION_LAST_VISIT, "Самое последнее посещение"),
)

VENUE_SELECTION_MODE_LABELS = dict(VENUE_SELECTION_MODE_CHOICES)
_ALLOWED_VENUE_SELECTION_MODES = {code for code, _label in VENUE_SELECTION_MODE_CHOICES}


@dataclass(frozen=True)
class GuestVenueSelectionRow:
    """
    Одна строка результата отбора гостя по заведению.
    """

    guest_id: int
    department_id: str
    orders_count: int
    last_visit_date: date | None
    selection_mode: str


@dataclass(frozen=True)
class GuestVenueSelectionResult:
    """
    Результат отбора гостей по заведению.
    """

    department_id: str
    selection_mode: str
    rows: tuple[GuestVenueSelectionRow, ...]
    total_before_limit: int
    limit_enabled: bool
    limit_value: int | None

    @property
    def guest_ids(self) -> tuple[int, ...]:
        return tuple(row.guest_id for row in self.rows)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def truncated(self) -> bool:
        return self.limit_enabled and self.total_before_limit > self.total


def normalize_venue_selection_mode(raw_value: str | None) -> str:
    """
    Нормализует способ отбора гостей по заведению.
    """

    value = str(raw_value or "").strip()
    if value in _ALLOWED_VENUE_SELECTION_MODES:
        return value
    return VENUE_SELECTION_VISITED_ONCE


def build_guest_venue_selection(
    *,
    department_id: str,
    selection_mode: str = VENUE_SELECTION_VISITED_ONCE,
    date_from: date | None = None,
    date_to: date | None = None,
    limit_enabled: bool = True,
    limit_value: int | None = 200,
) -> GuestVenueSelectionResult:
    """
    Возвращает гостей, связанных с выбранным заведением.

    Способы отбора:
    1. `visited_once` - гость был в заведении хотя бы один раз;
    2. `favorite` - выбранное заведение лидирует по числу заказов гостя;
    3. `last_visit` - последнее известное посещение гостя относится к заведению.
    """

    safe_department_id = str(department_id or "").strip()
    mode = normalize_venue_selection_mode(selection_mode)
    safe_limit = _normalize_limit(limit_value)

    if not safe_department_id:
        return GuestVenueSelectionResult(
            department_id="",
            selection_mode=mode,
            rows=(),
            total_before_limit=0,
            limit_enabled=bool(limit_enabled),
            limit_value=safe_limit if limit_enabled else None,
        )

    if mode == VENUE_SELECTION_FAVORITE:
        rows = _select_favorite_venue_rows(
            department_id=safe_department_id,
            date_from=date_from,
            date_to=date_to,
        )
    elif mode == VENUE_SELECTION_LAST_VISIT:
        rows = _select_last_visit_venue_rows(
            department_id=safe_department_id,
            date_from=date_from,
            date_to=date_to,
        )
    else:
        rows = _select_visited_once_rows(
            department_id=safe_department_id,
            date_from=date_from,
            date_to=date_to,
        )

    total_before_limit = len(rows)
    if limit_enabled and safe_limit is not None:
        rows = rows[:safe_limit]

    return GuestVenueSelectionResult(
        department_id=safe_department_id,
        selection_mode=mode,
        rows=tuple(rows),
        total_before_limit=total_before_limit,
        limit_enabled=bool(limit_enabled),
        limit_value=safe_limit if limit_enabled else None,
    )


def _normalize_limit(raw_value: int | str | None) -> int | None:
    """
    Нормализует пользовательский лимит.
    """

    if raw_value in (None, ""):
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 200
    if value <= 0:
        return None
    return value


def _daily_scope(*, date_from: date | None, date_to: date | None):
    """
    Возвращает базовый queryset дневных фактов с учетом периода.
    """

    scope = GuestRestaurantDailyOrderFact.objects.filter(orders_count__gt=0)
    if date_from is not None:
        scope = scope.filter(business_date__gte=date_from)
    if date_to is not None:
        scope = scope.filter(business_date__lte=date_to)
    return scope


def _select_visited_once_rows(
    *,
    department_id: str,
    date_from: date | None,
    date_to: date | None,
) -> list[GuestVenueSelectionRow]:
    rows = (
        _daily_scope(date_from=date_from, date_to=date_to)
        .filter(department_id=department_id)
        .values("guest_id", "department_id")
        .annotate(
            orders_total=Sum("orders_count"),
            last_visit_date=Max("business_date"),
        )
        .order_by("-last_visit_date", "guest_id")
    )
    return [
        GuestVenueSelectionRow(
            guest_id=int(row["guest_id"]),
            department_id=str(row["department_id"] or "").strip(),
            orders_count=int(row.get("orders_total") or 0),
            last_visit_date=row.get("last_visit_date"),
            selection_mode=VENUE_SELECTION_VISITED_ONCE,
        )
        for row in rows
        if row.get("guest_id")
    ]


def _select_favorite_venue_rows(
    *,
    department_id: str,
    date_from: date | None,
    date_to: date | None,
) -> list[GuestVenueSelectionRow]:
    best_by_guest: dict[int, GuestVenueSelectionRow] = {}
    for row in _iter_guest_department_aggregates(date_from=date_from, date_to=date_to):
        guest_id = int(row["guest_id"])
        candidate = GuestVenueSelectionRow(
            guest_id=guest_id,
            department_id=str(row["department_id"] or "").strip(),
            orders_count=int(row.get("orders_total") or 0),
            last_visit_date=row.get("last_visit_date"),
            selection_mode=VENUE_SELECTION_FAVORITE,
        )
        current = best_by_guest.get(guest_id)
        if current is None or _favorite_sort_key(candidate) > _favorite_sort_key(current):
            best_by_guest[guest_id] = candidate

    result = [
        row
        for row in best_by_guest.values()
        if row.department_id == department_id
    ]
    result.sort(key=lambda item: (-item.orders_count, item.last_visit_date or date.min, -item.guest_id), reverse=True)
    return result


def _select_last_visit_venue_rows(
    *,
    department_id: str,
    date_from: date | None,
    date_to: date | None,
) -> list[GuestVenueSelectionRow]:
    latest_by_guest: dict[int, GuestVenueSelectionRow] = {}
    for row in _iter_guest_department_aggregates(date_from=date_from, date_to=date_to):
        guest_id = int(row["guest_id"])
        candidate = GuestVenueSelectionRow(
            guest_id=guest_id,
            department_id=str(row["department_id"] or "").strip(),
            orders_count=int(row.get("orders_total") or 0),
            last_visit_date=row.get("last_visit_date"),
            selection_mode=VENUE_SELECTION_LAST_VISIT,
        )
        current = latest_by_guest.get(guest_id)
        if current is None or _last_visit_sort_key(candidate) > _last_visit_sort_key(current):
            latest_by_guest[guest_id] = candidate

    result = [
        row
        for row in latest_by_guest.values()
        if row.department_id == department_id
    ]
    result.sort(key=lambda item: (item.last_visit_date or date.min, item.orders_count, -item.guest_id), reverse=True)
    return result


def _iter_guest_department_aggregates(
    *,
    date_from: date | None,
    date_to: date | None,
) -> Iterable[dict]:
    return (
        _daily_scope(date_from=date_from, date_to=date_to)
        .values("guest_id", "department_id")
        .annotate(
            orders_total=Sum("orders_count"),
            last_visit_date=Max("business_date"),
        )
        .order_by("guest_id", "department_id")
    )


def _favorite_sort_key(row: GuestVenueSelectionRow) -> tuple[int, date, str]:
    return (
        int(row.orders_count or 0),
        row.last_visit_date or date.min,
        row.department_id,
    )


def _last_visit_sort_key(row: GuestVenueSelectionRow) -> tuple[date, int, str]:
    return (
        row.last_visit_date or date.min,
        int(row.orders_count or 0),
        row.department_id,
    )
