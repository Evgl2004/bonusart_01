"""
Сервис отбора гостей по связи с заведением.

Используется как общий слой для разовых маркетинговых рассылок:
экран «Гости» собирает аудиторию, а обычная рассылка отправляет сообщение.
Агрегация, ранжирование заведений, выбор победителя, финальная сортировка и
лимит выполняются одним основным Django ORM-запросом на стороне СУБД. Если
нужно одновременно вернуть полный размер ограниченной аудитории, выполняется
отдельный ``COUNT``. Прямой SQL не используется.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from django.db.models import F, Max, Sum, Window
from django.db.models.functions import FirstValue, RowNumber
from django.db.models.query import QuerySet

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
    guest_ids: Iterable[int] | None = None,
    limit_enabled: bool = True,
    limit_value: int | None = 200,
) -> GuestVenueSelectionResult:
    """
    Возвращает гостей, связанных с выбранным заведением.

    Способы отбора:
    1. `visited_once` - гость был в заведении хотя бы один раз;
    2. `favorite` - выбранное заведение лидирует по числу заказов гостя;
    3. `last_visit` - последнее известное посещение гостя относится к заведению.

    Если переданы ``guest_ids``, дневные факты ограничиваются этой аудиторией
    до агрегации. Пустой список возвращает пустой результат, а ``None`` означает
    расчёт по всем гостям.
    """

    safe_department_id = str(department_id or "").strip()
    mode = normalize_venue_selection_mode(selection_mode)
    safe_limit = _normalize_limit(limit_value)
    safe_guest_ids = _normalize_guest_ids(guest_ids)

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
        rows_query = _build_competitive_venue_query(
            department_id=safe_department_id,
            date_from=date_from,
            date_to=date_to,
            guest_ids=safe_guest_ids,
            selection_mode=mode,
        )
    elif mode == VENUE_SELECTION_LAST_VISIT:
        rows_query = _build_competitive_venue_query(
            department_id=safe_department_id,
            date_from=date_from,
            date_to=date_to,
            guest_ids=safe_guest_ids,
            selection_mode=mode,
        )
    else:
        rows_query = _build_visited_once_query(
            department_id=safe_department_id,
            date_from=date_from,
            date_to=date_to,
            guest_ids=safe_guest_ids,
        )

    rows, total_before_limit = _materialize_selection_rows(
        rows_query=rows_query,
        selection_mode=mode,
        limit_enabled=bool(limit_enabled),
        limit_value=safe_limit,
    )

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


def _normalize_guest_ids(raw_values: Iterable[int] | None) -> tuple[int, ...] | None:
    """
    Нормализует необязательное ограничение по гостям.

    Значение ``None`` означает отсутствие ограничения. Пустая коллекция означает,
    что результат должен быть пустым. Это различие важно для безопасного пересечения
    отбора по заведению с исторической Telegram-аудиторией.
    """

    if raw_values is None:
        return None

    result: set[int] = set()
    for raw_value in raw_values:
        try:
            guest_id = int(raw_value)
        except (TypeError, ValueError):
            continue
        if guest_id > 0:
            result.add(guest_id)
    return tuple(sorted(result))


def _daily_scope(
    *,
    date_from: date | None,
    date_to: date | None,
    guest_ids: tuple[int, ...] | None,
):
    """
    Возвращает базовый набор дневных фактов с учётом периода и аудитории.

    Ограничение по гостям применяется до группировки. Благодаря этому режимы
    «Любимое заведение» и «Самое последнее посещение» не агрегируют факты гостей,
    которые заведомо не относятся к выбранной аудитории.
    """

    scope = GuestRestaurantDailyOrderFact.objects.filter(orders_count__gt=0)
    if guest_ids is not None:
        if not guest_ids:
            return scope.none()
        scope = scope.filter(guest_id__in=guest_ids)
    if date_from is not None:
        scope = scope.filter(business_date__gte=date_from)
    if date_to is not None:
        scope = scope.filter(business_date__lte=date_to)
    return scope


def _build_visited_once_query(
    *,
    department_id: str,
    date_from: date | None,
    date_to: date | None,
    guest_ids: tuple[int, ...] | None,
) -> QuerySet:
    """
    Строит ORM-запрос для режима «Был хотя бы 1 раз».
    """

    return (
        _daily_scope(date_from=date_from, date_to=date_to, guest_ids=guest_ids)
        .filter(department_id=department_id)
        .values("guest_id", "department_id")
        .annotate(
            orders_total=Sum("orders_count"),
            last_visit_date=Max("business_date"),
        )
        .order_by("-last_visit_date", "guest_id")
    )


def _build_competitive_venue_query(
    *,
    department_id: str,
    date_from: date | None,
    date_to: date | None,
    guest_ids: tuple[int, ...] | None,
    selection_mode: str,
) -> QuerySet:
    """
    Строит оконный ORM-отбор для «Любимого» или «Последнего» заведения.

    ``ROW_NUMBER`` ранжирует агрегаты внутри каждого гостя. ``FIRST_VALUE``
    переносит идентификатор победителя в оконное выражение, поэтому Django
    применяет проверку целевого заведения во внешнем квалифицирующем запросе,
    уже после сравнения всех заведений гостя.

    Обычный фильтр целевого заведения нельзя применять до окна: иначе остальные
    заведения исчезнут до определения победителя, и любое посещённое заведение
    ошибочно станет «Любимым» или «Последним».
    """

    if selection_mode == VENUE_SELECTION_FAVORITE:
        winner_ordering = (
            F("orders_total").desc(),
            F("last_visit_date").desc(),
            F("department_id").desc(),
        )
        result_ordering = ("-orders_total", "-last_visit_date", "guest_id")
    elif selection_mode == VENUE_SELECTION_LAST_VISIT:
        winner_ordering = (
            F("last_visit_date").desc(),
            F("orders_total").desc(),
            F("department_id").desc(),
        )
        result_ordering = ("-last_visit_date", "-orders_total", "guest_id")
    else:
        raise ValueError(f"Режим не поддерживает сравнение заведений: {selection_mode!r}.")

    # Обе оконные функции обязаны использовать одну и ту же спецификацию.
    # Единый словарь исключает незаметное расхождение бизнес-приоритетов при
    # последующем сопровождении и соответствует примеру документации Django.
    winner_window = {
        "partition_by": [F("guest_id")],
        "order_by": winner_ordering,
    }

    return (
        _build_guest_department_aggregates(
            date_from=date_from,
            date_to=date_to,
            guest_ids=guest_ids,
        )
        .annotate(
            venue_rank=Window(
                expression=RowNumber(),
                **winner_window,
            ),
            winning_department=Window(
                expression=FirstValue("department_id"),
                **winner_window,
            ),
        )
        # Условия должны оставаться объединёнными через И (AND): смешанный
        # ИЛИ (OR) с оконными аннотациями и агрегацией Django не поддерживает.
        .filter(
            venue_rank=1,
            winning_department=department_id,
        )
        .order_by(*result_ordering)
    )


def _build_guest_department_aggregates(
    *,
    date_from: date | None,
    date_to: date | None,
    guest_ids: tuple[int, ...] | None,
) -> QuerySet:
    """
    Группирует положительные дневные факты по гостю и заведению средствами ORM.
    """

    return (
        _daily_scope(date_from=date_from, date_to=date_to, guest_ids=guest_ids)
        .values("guest_id", "department_id")
        .annotate(
            orders_total=Sum("orders_count"),
            last_visit_date=Max("business_date"),
        )
    )


def _materialize_selection_rows(
    *,
    rows_query: QuerySet,
    selection_mode: str,
    limit_enabled: bool,
    limit_value: int | None,
) -> tuple[list[GuestVenueSelectionRow], int]:
    """
    Выполняет ORM-запрос и преобразует результат в неизменяемые строки сервиса.

    При включённом лимите отдельный ``COUNT`` получает полный размер аудитории,
    а срез QuerySet превращается в SQL ``LIMIT``. Без лимита выполняется один
    запрос, и полный размер берётся из уже материализованного результата.
    """

    if limit_enabled and limit_value is not None:
        total_before_limit = int(rows_query.count())
        raw_rows = list(rows_query[:limit_value])
    else:
        raw_rows = list(rows_query)
        total_before_limit = len(raw_rows)

    rows = [
        GuestVenueSelectionRow(
            guest_id=int(row["guest_id"]),
            department_id=str(row["department_id"] or "").strip(),
            orders_count=int(row.get("orders_total") or 0),
            last_visit_date=row.get("last_visit_date"),
            selection_mode=selection_mode,
        )
        for row in raw_rows
        if row.get("guest_id")
    ]
    return rows, int(total_before_limit)
