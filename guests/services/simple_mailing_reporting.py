from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import ceil
from typing import Any

from django.db.models import Count, Exists, Min, OuterRef, Q, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Trim
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Mailing,
    MailingGuest,
    OlapNomenclatureDict,
    OlapSalesRawLine,
    OrderFact,
)


ALLOWED_PERIOD_DAYS = (7, 14, 30)
DEFAULT_PERIOD_DAYS = 7
DEFAULT_ORDER_PAGE_SIZE = 50
MAX_ORDER_PAGE_SIZE = 100
PURCHASE_ROWS_LIMIT = 10
VENUE_ROWS_LIMIT = 3


class SimpleMailingReportError(ValueError):
    """Ошибка нарушения контракта отчёта по простой рассылке."""


@dataclass(slots=True)
class SimpleMailingReportSnapshot:
    """Сериализуемый снимок агрегатов выбранной простой рассылки."""

    mailing: dict[str, Any]
    period: dict[str, Any]
    audience: dict[str, Any]
    orders: dict[str, Any]
    daily_rows: list[dict[str, Any]] = field(default_factory=list)
    period_summary_rows: list[dict[str, Any]] = field(default_factory=list)
    channel_rows: list[dict[str, Any]] = field(default_factory=list)
    venue_rows: list[dict[str, Any]] = field(default_factory=list)
    purchase_rows: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Возвращает секции снимка без преобразования денежных значений в строки."""

        return {
            "mailing": dict(self.mailing),
            "period": dict(self.period),
            "audience": dict(self.audience),
            "orders": dict(self.orders),
            "daily_rows": list(self.daily_rows),
            "period_summary_rows": list(self.period_summary_rows),
            "channel_rows": list(self.channel_rows),
            "venue_rows": list(self.venue_rows),
            "purchase_rows": list(self.purchase_rows),
            "limitations": list(self.limitations),
        }


@dataclass(slots=True)
class SimpleMailingOrderPage:
    """Ограниченная серверная страница деталей заказов."""

    rows: list[dict[str, Any]]
    page: int
    page_size: int
    num_pages: int
    total: int
    has_next: bool
    has_previous: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": list(self.rows),
            "page": self.page,
            "page_size": self.page_size,
            "num_pages": self.num_pages,
            "total": self.total,
            "has_next": self.has_next,
            "has_previous": self.has_previous,
        }


def simple_mailings_queryset(queryset: QuerySet[Mailing] | None = None) -> QuerySet[Mailing]:
    """Возвращает только рассылки без серии купонов."""

    source = queryset if queryset is not None else Mailing.objects.all()
    return source.filter(Q(coupon_series__isnull=True) | Q(coupon_series=""))


def normalize_simple_mailing_period_days(value: int | str | None) -> int:
    """Оставляет только согласованные периоды, иначе возвращает 7 дней."""

    try:
        normalized = int(value) if value is not None else DEFAULT_PERIOD_DAYS
    except (TypeError, ValueError):
        return DEFAULT_PERIOD_DAYS
    return normalized if normalized in ALLOWED_PERIOD_DAYS else DEFAULT_PERIOD_DAYS


def normalize_order_page_number(value: int | str | None) -> int:
    """Нормализует номер страницы к положительному целому числу."""

    try:
        normalized = int(value) if value is not None else 1
    except (TypeError, ValueError):
        return 1
    return normalized if normalized > 0 else 1


def normalize_order_page_size(value: int | str | None) -> int:
    """Нормализует размер страницы и применяет верхний предел 100 строк."""

    try:
        normalized = int(value) if value is not None else DEFAULT_ORDER_PAGE_SIZE
    except (TypeError, ValueError):
        return DEFAULT_ORDER_PAGE_SIZE
    if normalized <= 0:
        return DEFAULT_ORDER_PAGE_SIZE
    return min(normalized, MAX_ORDER_PAGE_SIZE)


def search_simple_mailings(query: str | None = None, *, limit: int = 10) -> QuerySet[Mailing]:
    """Ищет не более десяти простых рассылок по точному идентификатору или названию."""

    safe_limit = max(1, min(int(limit or 10), 10))
    search_query = str(query or "").strip()[:150]
    queryset = simple_mailings_queryset().order_by("-scheduled_date", "-id")
    if not search_query:
        return queryset[:safe_limit]
    if search_query.isascii() and search_query.isdecimal():
        try:
            mailing_id = int(search_query)
        except (TypeError, ValueError, OverflowError):
            return queryset.none()
        if mailing_id <= 0 or mailing_id > 9223372036854775807:
            return queryset.none()
        return queryset.filter(id=mailing_id)[:safe_limit]
    return queryset.filter(name__icontains=search_query)[:safe_limit]


def build_simple_mailing_report_snapshot(
    *,
    mailing: Mailing,
    period_days: int | str | None = None,
) -> SimpleMailingReportSnapshot:
    """Строит все агрегаты отчёта, кроме лениво загружаемых деталей заказов."""

    _validate_simple_mailing(mailing)
    selected_days = normalize_simple_mailing_period_days(period_days)
    start_date = mailing.scheduled_date
    end_date = start_date + timedelta(days=selected_days - 1)
    end_30_date = start_date + timedelta(days=29)

    audience_scope = MailingGuest.objects.filter(mailing_id=mailing.id)
    successful_rows = _build_successful_mailing_guests_scope(mailing.id)
    successful_guest_ids = successful_rows.order_by().values("guest_id")

    recipients_total = int(audience_scope.values("guest_id").distinct().count())
    sent_total = int(successful_rows.values("guest_id").distinct().count())
    not_sent_total = max(0, recipients_total - sent_total)
    send_share_percent = _percent(sent_total, recipients_total)

    max_orders_scope = _build_eligible_orders_scope(
        successful_guest_ids=successful_guest_ids,
        start_date=start_date,
        end_date=end_30_date,
    )
    period_summary_rows = _build_period_summary_rows(
        orders_scope=max_orders_scope,
        start_date=start_date,
        sent_total=sent_total,
        selected_days=selected_days,
    )
    selected_period_row = next(
        row for row in period_summary_rows if row["period_days"] == selected_days
    )
    selected_orders_scope = max_orders_scope.filter(business_date__lte=end_date)

    average_first_order_days = _build_average_first_order_days(
        orders_scope=selected_orders_scope,
        start_date=start_date,
    )
    daily_rows = _build_daily_rows(
        orders_scope=selected_orders_scope,
        start_date=start_date,
        end_date=end_date,
    )
    channel_rows, channel_names, tasks_total = _build_channel_rows(
        mailing_id=mailing.id,
        recipients_total=recipients_total,
        sent_total=sent_total,
    )
    venue_rows = _build_venue_rows(selected_orders_scope)
    purchase_rows = _build_purchase_rows(
        orders_scope=selected_orders_scope,
        sent_total=sent_total,
    )
    limitations = _build_limitations(
        mailing=mailing,
        recipients_total=recipients_total,
        tasks_total=tasks_total,
    )

    return SimpleMailingReportSnapshot(
        mailing={
            "id": int(mailing.id),
            "name": mailing.name,
            "scheduled_date": mailing.scheduled_date,
            "scheduled_time_begin": mailing.scheduled_time_begin,
            "scheduled_time_end": mailing.scheduled_time_end,
        },
        period={
            "days": selected_days,
            "start_date": start_date,
            "end_date": end_date,
            "allowed_days": ALLOWED_PERIOD_DAYS,
        },
        audience={
            "recipients_total": recipients_total,
            "sent_total": sent_total,
            "not_sent_total": not_sent_total,
            "send_share_percent": send_share_percent,
            "channels": channel_names,
        },
        orders={
            "guests_count": selected_period_row["guests_count"],
            "orders_count": selected_period_row["orders_count"],
            "net_sum": selected_period_row["net_sum"],
            "average_check": selected_period_row["average_check"],
            "guest_share_percent": selected_period_row["guest_share_percent"],
            "average_first_order_days": average_first_order_days,
        },
        daily_rows=daily_rows,
        period_summary_rows=period_summary_rows,
        channel_rows=channel_rows,
        venue_rows=venue_rows,
        purchase_rows=purchase_rows,
        limitations=limitations,
    )


def build_simple_mailing_order_details_page(
    *,
    mailing: Mailing,
    period_days: int | str | None = None,
    page_number: int | str | None = None,
    page_size: int | str | None = None,
) -> SimpleMailingOrderPage:
    """Возвращает одну ограниченную страницу заказов с однозначным каналом отправки."""

    _validate_simple_mailing(mailing)
    selected_days = normalize_simple_mailing_period_days(period_days)
    normalized_page = normalize_order_page_number(page_number)
    normalized_page_size = normalize_order_page_size(page_size)
    start_date = mailing.scheduled_date
    end_date = start_date + timedelta(days=selected_days - 1)

    successful_rows = _build_successful_mailing_guests_scope(mailing.id)
    successful_guest_ids = successful_rows.order_by().values("guest_id")
    orders_scope = _build_eligible_orders_scope(
        successful_guest_ids=successful_guest_ids,
        start_date=start_date,
        end_date=end_date,
    ).order_by(
        "business_date",
        "department_id",
        "order_number",
        "uniq_order_id",
        "id",
    )

    total = int(orders_scope.count())
    num_pages = ceil(total / normalized_page_size) if total else 0
    offset = (normalized_page - 1) * normalized_page_size
    order_rows = list(
        orders_scope.values(
            "business_date",
            "department_id",
            "department_name",
            "order_number",
            "uniq_order_id",
            "guest_id",
            "net_sum",
        )[offset : offset + normalized_page_size]
    ) if offset < total else []

    page_guest_ids = {int(row["guest_id"]) for row in order_rows if row.get("guest_id") is not None}
    channel_by_guest = _build_unambiguous_channel_by_guest(
        mailing_id=mailing.id,
        guest_ids=page_guest_ids,
    )
    rows = []
    for row in order_rows:
        guest_id = int(row["guest_id"])
        delay_days = (row["business_date"] - start_date).days
        rows.append(
            {
                "business_date": row["business_date"],
                "order_number": int(row["order_number"]),
                "guest_id": guest_id,
                "net_sum": _decimal_or_zero(row["net_sum"]),
                "venue_name": _venue_name(row),
                "calendar_delay_days": delay_days,
                "calendar_delay_label": _calendar_delay_label(delay_days),
                "channel": channel_by_guest.get(guest_id, ""),
            }
        )

    return SimpleMailingOrderPage(
        rows=rows,
        page=normalized_page,
        page_size=normalized_page_size,
        num_pages=num_pages,
        total=total,
        has_next=normalized_page < num_pages,
        has_previous=normalized_page > 1 and total > 0,
    )


def _validate_simple_mailing(mailing: Mailing) -> None:
    if mailing.pk is None:
        raise SimpleMailingReportError("Рассылка должна быть сохранена перед построением отчёта.")
    if mailing.coupon_series not in (None, ""):
        raise SimpleMailingReportError("Отчёт доступен только для простой рассылки без купонов.")
    if mailing.scheduled_date is None:
        raise SimpleMailingReportError("У рассылки отсутствует дата начала календарного отчёта.")


def _build_successful_mailing_guests_scope(mailing_id: int) -> QuerySet[MailingGuest]:
    successful_task = DispatchTask.objects.filter(
        mailing_guest_id=OuterRef("pk"),
        source_type=DispatchTask.SourceType.MAILING,
        status=DispatchTask.Status.DONE,
    )
    return (
        MailingGuest.objects.filter(mailing_id=mailing_id)
        .annotate(has_successful_task=Exists(successful_task))
        .filter(has_successful_task=True)
    )


def _build_eligible_orders_scope(
    *,
    successful_guest_ids: QuerySet,
    start_date: date,
    end_date: date,
) -> QuerySet[OrderFact]:
    return OrderFact.objects.filter(
        guest_id__in=Subquery(successful_guest_ids),
        business_date__gte=start_date,
        business_date__lte=end_date,
    )


def _build_period_summary_rows(
    *,
    orders_scope: QuerySet[OrderFact],
    start_date: date,
    sent_total: int,
    selected_days: int,
) -> list[dict[str, Any]]:
    aggregate_expressions: dict[str, Any] = {}
    for days in ALLOWED_PERIOD_DAYS:
        period_filter = Q(business_date__lte=start_date + timedelta(days=days - 1))
        aggregate_expressions[f"guests_{days}"] = Count(
            "guest_id",
            distinct=True,
            filter=period_filter,
        )
        aggregate_expressions[f"orders_{days}"] = Count("id", filter=period_filter)
        aggregate_expressions[f"net_sum_{days}"] = Sum("net_sum", filter=period_filter)

    aggregates = orders_scope.aggregate(**aggregate_expressions)
    rows = []
    for days in ALLOWED_PERIOD_DAYS:
        guests_count = int(aggregates.get(f"guests_{days}") or 0)
        orders_count = int(aggregates.get(f"orders_{days}") or 0)
        net_sum = _decimal_or_zero(aggregates.get(f"net_sum_{days}"))
        rows.append(
            {
                "period_days": days,
                "start_date": start_date,
                "end_date": start_date + timedelta(days=days - 1),
                "guests_count": guests_count,
                "guest_share_percent": _percent(guests_count, sent_total),
                "orders_count": orders_count,
                "net_sum": net_sum,
                "average_check": _average(net_sum, orders_count, places="0.01"),
                "is_selected": days == selected_days,
            }
        )
    return rows


def _build_average_first_order_days(
    *,
    orders_scope: QuerySet[OrderFact],
    start_date: date,
) -> Decimal:
    rows = orders_scope.values("guest_id").annotate(first_order_date=Min("business_date"))
    total_days = 0
    guests_count = 0
    for row in rows.iterator():
        first_order_date = row.get("first_order_date")
        if first_order_date is None:
            continue
        total_days += max(0, (first_order_date - start_date).days)
        guests_count += 1
    return _average(Decimal(total_days), guests_count, places="0.1")


def _build_daily_rows(
    *,
    orders_scope: QuerySet[OrderFact],
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    source_rows = {
        row["business_date"]: row
        for row in orders_scope.values("business_date").annotate(
            guests_count=Count("guest_id", distinct=True),
            orders_count=Count("id"),
            net_sum=Sum("net_sum"),
        )
    }
    rows = []
    current_date = start_date
    while current_date <= end_date:
        source = source_rows.get(current_date, {})
        rows.append(
            {
                "business_date": current_date,
                "guests_count": int(source.get("guests_count") or 0),
                "orders_count": int(source.get("orders_count") or 0),
                "net_sum": _decimal_or_zero(source.get("net_sum")),
            }
        )
        current_date += timedelta(days=1)
    return rows


def _build_channel_rows(
    *,
    mailing_id: int,
    recipients_total: int,
    sent_total: int,
) -> tuple[list[dict[str, Any]], list[str], int]:
    tasks_scope = DispatchTask.objects.filter(
        mailing_guest__mailing_id=mailing_id,
        source_type=DispatchTask.SourceType.MAILING,
    )
    tasks_total = int(tasks_scope.count())
    provider_labels = dict(BotProfile.ProviderType.choices)
    source_rows = list(
        tasks_scope.values("provider_type")
        .annotate(
            recipients_count=Count("mailing_guest__guest_id", distinct=True),
            sent_count=Count(
                "mailing_guest__guest_id",
                distinct=True,
                filter=Q(status=DispatchTask.Status.DONE),
            ),
        )
        .order_by("provider_type")
    )
    rows = []
    channel_names = []
    for source in source_rows:
        provider_type = str(source.get("provider_type") or "")
        recipients_count = int(source.get("recipients_count") or 0)
        sent_count = int(source.get("sent_count") or 0)
        channel_name = str(provider_labels.get(provider_type, provider_type or "Неизвестный канал"))
        channel_names.append(channel_name)
        rows.append(
            {
                "provider_type": provider_type,
                "channel_name": channel_name,
                "recipients_count": recipients_count,
                "sent_count": sent_count,
                "not_sent_count": max(0, recipients_count - sent_count),
                "send_share_percent": _percent(sent_count, recipients_count),
                "is_total": False,
            }
        )
    rows.append(
        {
            "provider_type": "total",
            "channel_name": "Итого",
            "recipients_count": recipients_total,
            "sent_count": sent_total,
            "not_sent_count": max(0, recipients_total - sent_total),
            "send_share_percent": _percent(sent_total, recipients_total),
            "is_total": True,
        }
    )
    return rows, channel_names, tasks_total


def _build_venue_rows(orders_scope: QuerySet[OrderFact]) -> list[dict[str, Any]]:
    source_rows = list(
        orders_scope.values("department_id", "department_name")
        .annotate(
            guests_count=Count("guest_id", distinct=True),
            orders_count=Count("id"),
            net_sum=Sum("net_sum"),
        )
        .order_by("-net_sum", "department_name", "department_id")
    )
    rows = [_venue_aggregate_row(source) for source in source_rows[:VENUE_ROWS_LIMIT]]
    if len(source_rows) <= VENUE_ROWS_LIMIT:
        return rows

    top_venue_filter = Q()
    for source in source_rows[:VENUE_ROWS_LIMIT]:
        top_venue_filter |= Q(
            department_id=source.get("department_id"),
            department_name=source.get("department_name"),
        )
    other = orders_scope.exclude(top_venue_filter).aggregate(
        guests_count=Count("guest_id", distinct=True),
        orders_count=Count("id"),
        net_sum=Sum("net_sum"),
    )
    other_orders_count = int(other.get("orders_count") or 0)
    other_net_sum = _decimal_or_zero(other.get("net_sum"))
    rows.append(
        {
            "venue_name": "Другие заведения",
            "guests_count": int(other.get("guests_count") or 0),
            "orders_count": other_orders_count,
            "net_sum": other_net_sum,
            "average_check": _average(other_net_sum, other_orders_count, places="0.01"),
            "is_other": True,
        }
    )
    return rows


def _venue_aggregate_row(source: dict[str, Any]) -> dict[str, Any]:
    orders_count = int(source.get("orders_count") or 0)
    net_sum = _decimal_or_zero(source.get("net_sum"))
    return {
        "venue_name": _venue_name(source),
        "guests_count": int(source.get("guests_count") or 0),
        "orders_count": orders_count,
        "net_sum": net_sum,
        "average_check": _average(net_sum, orders_count, places="0.01"),
        "is_other": False,
    }


def _build_purchase_rows(
    *,
    orders_scope: QuerySet[OrderFact],
    sent_total: int,
) -> list[dict[str, Any]]:
    matching_order = orders_scope.filter(
        guest_id=OuterRef("guest_id"),
        business_date=OuterRef("business_date"),
        department_id=Trim(Coalesce(OuterRef("department_id"), Value(""))),
        order_number=OuterRef("order_number"),
        uniq_order_id=Trim(Coalesce(OuterRef("uniq_order_id"), Value(""))),
    )
    raw_rows = (
        OlapSalesRawLine.objects.annotate(has_matching_order=Exists(matching_order))
        .filter(has_matching_order=True)
        .exclude(dish_code__isnull=True)
        .exclude(dish_code="")
        .values(
            "guest_id",
            "business_date",
            "department_id",
            "order_number",
            "uniq_order_id",
            "dish_code",
            "dish_name",
            "dish_category_name",
            "dish_group_name",
            "dish_amount",
            "dish_sum_before_discount",
            "dish_sum_after_discount",
        )
    )
    item_aggregates: dict[str, dict[str, Any]] = {}
    for row in raw_rows.iterator():
        dish_code = str(row.get("dish_code") or "").strip()
        if not dish_code:
            continue
        item = item_aggregates.setdefault(
            dish_code,
            {
                "dish_code": dish_code,
                "dish_name": str(row.get("dish_name") or "").strip() or dish_code,
                "category_name": str(row.get("dish_category_name") or "").strip(),
                "group_name": str(row.get("dish_group_name") or "").strip(),
                "quantity": Decimal("0"),
                "guest_ids": set(),
                "order_keys": set(),
                "net_sum": Decimal("0"),
            },
        )
        if not item["dish_name"] and row.get("dish_name"):
            item["dish_name"] = str(row["dish_name"]).strip()
        if not item["category_name"] and row.get("dish_category_name"):
            item["category_name"] = str(row["dish_category_name"]).strip()
        if not item["group_name"] and row.get("dish_group_name"):
            item["group_name"] = str(row["dish_group_name"]).strip()

        guest_id = row.get("guest_id")
        if guest_id is not None:
            item["guest_ids"].add(int(guest_id))
        item["order_keys"].add(_raw_order_identity(row))
        item["quantity"] += _raw_line_quantity(row)
        item["net_sum"] += _raw_line_net_sum(row)

    sorted_items = sorted(
        item_aggregates.values(),
        key=lambda item: (
            -item["quantity"],
            -len(item["guest_ids"]),
            -item["net_sum"],
            str(item["dish_name"]).casefold(),
        ),
    )[:PURCHASE_ROWS_LIMIT]
    _enrich_purchase_items(sorted_items)

    rows = []
    for rank, item in enumerate(sorted_items, start=1):
        guests_count = len(item["guest_ids"])
        rows.append(
            {
                "rank": rank,
                "dish_code": item["dish_code"],
                "dish_name": item["dish_name"] or item["dish_code"],
                "category_name": item["category_name"] or "Без категории",
                "group_name": item["group_name"] or "Без группы",
                "quantity": item["quantity"],
                "guests_count": guests_count,
                "orders_count": len(item["order_keys"]),
                "guest_share_percent": _percent(guests_count, sent_total),
                "net_sum": item["net_sum"],
            }
        )
    return rows


def _enrich_purchase_items(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    item_by_code = {str(item["dish_code"]): item for item in items}
    dictionary_rows = (
        OlapNomenclatureDict.objects.filter(
            iiko_nomenclature_external_id__in=list(item_by_code),
        )
        .values(
            "iiko_nomenclature_external_id",
            "nomenclature_name",
            "dish_group_name",
            "olap_category__category_name",
        )
    )
    for row in dictionary_rows:
        dish_code = str(row.get("iiko_nomenclature_external_id") or "").strip()
        item = item_by_code.get(dish_code)
        if item is None:
            continue
        if row.get("nomenclature_name"):
            item["dish_name"] = str(row["nomenclature_name"]).strip()
        if row.get("olap_category__category_name"):
            item["category_name"] = str(row["olap_category__category_name"]).strip()
        if row.get("dish_group_name"):
            item["group_name"] = str(row["dish_group_name"]).strip()


def _build_unambiguous_channel_by_guest(
    *,
    mailing_id: int,
    guest_ids: set[int],
) -> dict[int, str]:
    if not guest_ids:
        return {}
    provider_labels = dict(BotProfile.ProviderType.choices)
    channels_by_guest: dict[int, set[str]] = {}
    rows = DispatchTask.objects.filter(
        mailing_guest__mailing_id=mailing_id,
        mailing_guest__guest_id__in=guest_ids,
        source_type=DispatchTask.SourceType.MAILING,
        status=DispatchTask.Status.DONE,
    ).values_list("mailing_guest__guest_id", "provider_type").distinct()
    for guest_id, provider_type in rows:
        channels_by_guest.setdefault(int(guest_id), set()).add(str(provider_type or ""))
    return {
        guest_id: str(provider_labels.get(next(iter(provider_types)), next(iter(provider_types))))
        for guest_id, provider_types in channels_by_guest.items()
        if len(provider_types) == 1
    }


def _build_limitations(
    *,
    mailing: Mailing,
    recipients_total: int,
    tasks_total: int,
) -> list[str]:
    limitations = [
        (
            "Учитываются заказы в календарном окне, начинающемся датой выбранной "
            "рассылки. Заказ в первый день мог быть совершён как до, так и после "
            "сообщения. Отчёт не доказывает влияние сообщения на заказ."
        )
    ]
    scheduled_begin_date = timezone.localtime(mailing.scheduled_time_begin).date()
    if scheduled_begin_date != mailing.scheduled_date:
        limitations.append(
            "Дата планового начала не совпадает с датой рассылки; календарный отчёт "
            "всё равно построен от даты рассылки."
        )
    if recipients_total > 0 and tasks_total == 0:
        limitations.append(
            "Для аудитории не найдены связанные задачи отправки; исторический статус "
            "строки аудитории не используется как недоказанная подстановка успеха."
        )
    return limitations


def _raw_order_identity(row: dict[str, Any]) -> tuple[date, str, int, str]:
    return (
        row["business_date"],
        str(row.get("department_id") or "").strip(),
        int(row.get("order_number") or 0),
        str(row.get("uniq_order_id") or "").strip(),
    )


def _raw_line_net_sum(row: dict[str, Any]) -> Decimal:
    if row.get("dish_sum_after_discount") in (None, ""):
        return _decimal_or_zero(row.get("dish_sum_before_discount"))
    return _decimal_or_zero(row.get("dish_sum_after_discount"))


def _raw_line_quantity(row: dict[str, Any]) -> Decimal:
    quantity = _decimal_or_zero(row.get("dish_amount"))
    return quantity if quantity > 0 else Decimal("1")


def _decimal_or_zero(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _average(value: Decimal, denominator: int, *, places: str) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (value / Decimal(denominator)).quantize(
        Decimal(places),
        rounding=ROUND_HALF_UP,
    )


def _percent(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (Decimal(numerator) * Decimal("100") / Decimal(denominator)).quantize(
        Decimal("0.1"),
        rounding=ROUND_HALF_UP,
    )


def _venue_name(row: dict[str, Any]) -> str:
    return (
        str(row.get("department_name") or "").strip()
        or str(row.get("department_id") or "").strip()
        or "Без заведения"
    )


def _calendar_delay_label(delay_days: int) -> str:
    if delay_days <= 0:
        return "день отправки"
    if delay_days == 1:
        return "через 1 день"
    if 2 <= delay_days <= 4:
        return f"через {delay_days} дня"
    return f"через {delay_days} дней"
