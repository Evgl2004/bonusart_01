"""
Сервис данных для экрана «Категории и цели».

Цель:
1. дать маркетологу рабочий обзор по целевым категориям;
2. показать покрытие по гостям и обороту за выбранное окно;
3. дать быстрый доступ к составу номенклатур в каждой целевой категории.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import Coalesce

from guests.models import (
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    GuestRestaurantDailyCategoryFact,
    OlapCategoryDict,
    OlapNomenclatureDict,
    OrderFact,
    VirtualCategory,
)
from guests.services.guest_workbench import WINDOW_OPTIONS, normalize_window_days

DEFAULT_WINDOW_DAYS = 30
NOMENCLATURE_PREVIEW_LIMIT = 200
NOMENCLATURE_CATALOG_LIMIT = 500


def build_focus_categories_workbench_payload(
    *,
    as_of_date: date | None = None,
    window_days: int | str | None = None,
    department_id: str | None = None,
    selected_focus_id: int | None = None,
    selected_virtual_category_id: int | None = None,
    nomenclature_query: str | None = None,
    nomenclature_group_query: str | None = None,
    nomenclature_olap_category_id: int | None = None,
) -> dict[str, Any]:
    """
    Формирует payload для страницы `focus-categories`.
    """
    selected_window_days = normalize_window_days(window_days)
    selected_department_id = (department_id or "").strip()
    selected_nomenclature_query = (nomenclature_query or "").strip()
    selected_nomenclature_group_query = (nomenclature_group_query or "").strip()
    selected_nomenclature_olap_category_id = int(nomenclature_olap_category_id or 0)
    selected_virtual_category_pk = int(selected_virtual_category_id or 0)

    target_as_of = as_of_date
    if target_as_of is None:
        target_as_of = GuestRestaurantDailyCategoryFact.objects.aggregate(v=Max("business_date")).get("v")

    if target_as_of is None:
        return _build_empty_payload(
            as_of_date=None,
            selected_window_days=selected_window_days,
            selected_department_id=selected_department_id,
            selected_focus_id=selected_focus_id,
            selected_virtual_category_id=selected_virtual_category_pk,
            selected_nomenclature_query=selected_nomenclature_query,
            selected_nomenclature_group_query=selected_nomenclature_group_query,
            selected_nomenclature_olap_category_id=selected_nomenclature_olap_category_id,
        )

    range_start = target_as_of - timedelta(days=selected_window_days - 1)
    daily_scope = GuestRestaurantDailyCategoryFact.objects.filter(
        business_date__gte=range_start,
        business_date__lte=target_as_of,
    )
    if selected_department_id:
        daily_scope = daily_scope.filter(department_id=selected_department_id)

    focus_coverage_rows = {
        int(row["focus_category_id"]): row
        for row in daily_scope.values("focus_category_id").annotate(
            guests_count=Count("guest_id", distinct=True),
            orders_count=Coalesce(Sum("orders_count"), 0),
            net_total=Coalesce(Sum("sum_net"), Decimal("0")),
        )
    }

    focus_rows = list(
        FocusCategory.objects.select_related("olap_category", "virtual_category")
        .annotate(resolved_links_count=Count("resolved_nomenclatures", distinct=True))
        .order_by("-is_enabled", "name", "id")
    )

    payload_rows: list[dict[str, Any]] = []
    for focus in focus_rows:
        coverage = focus_coverage_rows.get(int(focus.id), {})
        payload_rows.append(
            {
                "id": int(focus.id),
                "code": focus.code,
                "name": focus.name,
                "source_type": focus.source_type,
                "source_type_label": focus.get_source_type_display(),
                "source_name": _build_focus_source_name(focus),
                "is_enabled": bool(focus.is_enabled),
                "priority_weight": int(focus.priority_weight or 1),
                "tag_code": (focus.tag_code or "").strip(),
                "resolved_links_count": int(focus.resolved_links_count or 0),
                "guests_count": int(coverage.get("guests_count") or 0),
                "orders_count": int(coverage.get("orders_count") or 0),
                "net_total": _to_money_str(coverage.get("net_total")),
            }
        )

    selected_focus_data = _build_selected_focus_nomenclature(
        focus_id=selected_focus_id,
        limit=NOMENCLATURE_PREVIEW_LIMIT,
    )
    selected_virtual_category = _build_selected_virtual_category(
        virtual_category_id=selected_virtual_category_pk,
        limit=NOMENCLATURE_PREVIEW_LIMIT,
    )
    nomenclature_catalog = _build_nomenclature_catalog(
        query=selected_nomenclature_query,
        group_query=selected_nomenclature_group_query,
        olap_category_id=selected_nomenclature_olap_category_id if selected_nomenclature_olap_category_id > 0 else None,
        limit=NOMENCLATURE_CATALOG_LIMIT,
    )

    total_resolved_links = sum(item["resolved_links_count"] for item in payload_rows)
    enabled_focus_count = sum(1 for item in payload_rows if item["is_enabled"])

    return {
        "filters": {
            "as_of_date": target_as_of.isoformat(),
            "window_days": selected_window_days,
            "window_options": list(WINDOW_OPTIONS),
            "department_id": selected_department_id,
            "department_options": _build_department_options(),
            "selected_focus_id": int(selected_focus_id or 0),
            "selected_virtual_category_id": int(selected_virtual_category_pk or 0),
            "nomenclature_query": selected_nomenclature_query,
            "nomenclature_group_query": selected_nomenclature_group_query,
            "nomenclature_olap_category_id": selected_nomenclature_olap_category_id,
            "nomenclature_olap_category_options": _build_nomenclature_olap_category_options(),
        },
        "stats": {
            "focus_total": len(payload_rows),
            "focus_enabled": enabled_focus_count,
            "resolved_links_total": int(total_resolved_links),
        },
        "focus_rows": payload_rows,
        "available_sources": {
            "olap_categories": _build_available_olap_categories(),
            "virtual_categories": _build_available_virtual_categories(),
        },
        "virtual_categories": _build_virtual_categories_summary(),
        "selected_focus": selected_focus_data,
        "selected_virtual_category": selected_virtual_category,
        "nomenclature_catalog": nomenclature_catalog,
    }


def _build_empty_payload(
    *,
    as_of_date: date | None,
    selected_window_days: int,
    selected_department_id: str,
    selected_focus_id: int | None,
    selected_virtual_category_id: int,
    selected_nomenclature_query: str,
    selected_nomenclature_group_query: str,
    selected_nomenclature_olap_category_id: int,
) -> dict[str, Any]:
    """
    Возвращает пустой payload, если пока нет данных для аналитики.
    """
    return {
        "filters": {
            "as_of_date": as_of_date.isoformat() if as_of_date else "",
            "window_days": selected_window_days,
            "window_options": list(WINDOW_OPTIONS),
            "department_id": selected_department_id,
            "department_options": _build_department_options(),
            "selected_focus_id": int(selected_focus_id or 0),
            "selected_virtual_category_id": int(selected_virtual_category_id or 0),
            "nomenclature_query": selected_nomenclature_query,
            "nomenclature_group_query": selected_nomenclature_group_query,
            "nomenclature_olap_category_id": selected_nomenclature_olap_category_id,
            "nomenclature_olap_category_options": _build_nomenclature_olap_category_options(),
        },
        "stats": {
            "focus_total": 0,
            "focus_enabled": 0,
            "resolved_links_total": 0,
        },
        "focus_rows": [],
        "available_sources": {
            "olap_categories": _build_available_olap_categories(),
            "virtual_categories": _build_available_virtual_categories(),
        },
        "virtual_categories": _build_virtual_categories_summary(),
        "selected_focus": {
            "focus_id": int(selected_focus_id or 0),
            "focus_name": "",
            "total": 0,
            "limit": NOMENCLATURE_PREVIEW_LIMIT,
            "is_truncated": False,
            "rows": [],
        },
        "selected_virtual_category": {
            "id": int(selected_virtual_category_id or 0),
            "name": "",
            "code": "",
            "total": 0,
            "limit": NOMENCLATURE_PREVIEW_LIMIT,
            "is_truncated": False,
            "rows": [],
            "selected_ids": [],
        },
        "nomenclature_catalog": {
            "total": 0,
            "limit": NOMENCLATURE_CATALOG_LIMIT,
            "is_truncated": False,
            "rows": [],
        },
    }


def _build_department_options() -> list[dict[str, str]]:
    """
    Формирует список заведений для фильтра.
    """
    rows = (
        OrderFact.objects.exclude(department_id="")
        .values("department_id")
        .annotate(department_name=Max("department_name"))
        .order_by("department_name", "department_id")
    )
    result: list[dict[str, str]] = []
    for row in rows:
        dep_id = (row.get("department_id") or "").strip()
        if not dep_id:
            continue
        dep_name = (row.get("department_name") or "").strip() or dep_id
        result.append({"id": dep_id, "name": dep_name})
    return result


def _build_available_olap_categories() -> list[dict[str, Any]]:
    """
    Возвращает доступные OLAP-категории для создания новых фокусов.
    """
    used_ids = set(
        FocusCategory.objects.filter(source_type=FocusCategory.SourceType.OLAP_DIRECT)
        .exclude(olap_category_id__isnull=True)
        .values_list("olap_category_id", flat=True)
    )
    rows = (
        OlapCategoryDict.objects.filter(is_active=True)
        .exclude(id__in=used_ids)
        .order_by("category_name", "id")
        .values("id", "category_name", "iiko_category_external_id")
    )
    return [
        {
            "id": int(row["id"]),
            "name": (row.get("category_name") or "").strip(),
            "external_id": (row.get("iiko_category_external_id") or "").strip(),
        }
        for row in rows
    ]


def _build_available_virtual_categories() -> list[dict[str, Any]]:
    """
    Возвращает доступные виртуальные категории для создания фокусов.
    """
    used_ids = set(
        FocusCategory.objects.filter(source_type=FocusCategory.SourceType.VIRTUAL)
        .exclude(virtual_category_id__isnull=True)
        .values_list("virtual_category_id", flat=True)
    )
    rows = (
        VirtualCategory.objects.filter(is_active=True)
        .exclude(id__in=used_ids)
        .order_by("name", "id")
        .values("id", "name", "code")
    )
    return [
        {
            "id": int(row["id"]),
            "name": (row.get("name") or "").strip(),
            "code": (row.get("code") or "").strip(),
        }
        for row in rows
    ]


def _build_virtual_categories_summary() -> list[dict[str, Any]]:
    """
    Возвращает список всех виртуальных категорий для верхнего блока конструктора.

    Нужен для быстрого обзора:
    1. активна категория или архивная;
    2. сколько номенклатур в составе;
    3. используется ли категория в фокусных категориях.
    """
    rows = (
        VirtualCategory.objects.annotate(
            nomenclatures_count=Count("nomenclature_links", distinct=True),
            focus_categories_count=Count("focus_category_rows", distinct=True),
        )
        .order_by("-is_active", "name", "id")
        .values(
            "id",
            "name",
            "code",
            "is_active",
            "updated_at",
            "nomenclatures_count",
            "focus_categories_count",
        )
    )
    return [
        {
            "id": int(row["id"]),
            "name": (row.get("name") or "").strip(),
            "code": (row.get("code") or "").strip(),
            "is_active": bool(row.get("is_active")),
            "updated_at": row.get("updated_at"),
            "nomenclatures_count": int(row.get("nomenclatures_count") or 0),
            "focus_categories_count": int(row.get("focus_categories_count") or 0),
        }
        for row in rows
    ]


def _build_nomenclature_olap_category_options() -> list[dict[str, Any]]:
    """
    Возвращает список категорий OLAP для фильтра каталога номенклатуры.
    """
    rows = (
        OlapCategoryDict.objects.filter(is_active=True)
        .order_by("category_name", "id")
        .values("id", "category_name")
    )
    return [
        {
            "id": int(row["id"]),
            "name": (row.get("category_name") or "").strip(),
        }
        for row in rows
    ]


def _build_focus_source_name(focus: FocusCategory) -> str:
    """
    Формирует человеко-понятное название источника фокуса.
    """
    if focus.source_type == FocusCategory.SourceType.OLAP_DIRECT and focus.olap_category:
        return (focus.olap_category.category_name or "").strip() or f"OLAP #{focus.olap_category_id}"
    if focus.source_type == FocusCategory.SourceType.VIRTUAL and focus.virtual_category:
        return (focus.virtual_category.name or "").strip() or f"Virtual #{focus.virtual_category_id}"
    return "Источник не задан"


def _build_selected_focus_nomenclature(*, focus_id: int | None, limit: int) -> dict[str, Any]:
    """
    Возвращает состав номенклатур выбранной фокусной категории.
    """
    focus_pk = int(focus_id or 0)
    if focus_pk <= 0:
        return {
            "focus_id": 0,
            "focus_name": "",
            "total": 0,
            "limit": limit,
            "is_truncated": False,
            "rows": [],
        }

    focus = (
        FocusCategory.objects.filter(id=focus_pk)
        .only("id", "name")
        .first()
    )
    if focus is None:
        return {
            "focus_id": focus_pk,
            "focus_name": "",
            "total": 0,
            "limit": limit,
            "is_truncated": False,
            "rows": [],
        }

    queryset = (
        FocusCategoryNomenclatureResolved.objects.select_related("nomenclature")
        .filter(focus_category_id=focus_pk)
        .order_by("nomenclature__nomenclature_name", "id")
    )
    total = queryset.count()
    rows = list(
        queryset[:limit].values(
            "nomenclature__iiko_nomenclature_external_id",
            "nomenclature__nomenclature_name",
            "nomenclature__dish_group_name",
            "nomenclature__olap_category__category_name",
            "source_reason",
        )
    )
    source_reason_labels = dict(FocusCategoryNomenclatureResolved.SourceReason.choices)

    return {
        "focus_id": int(focus.id),
        "focus_name": focus.name,
        "total": int(total),
        "limit": int(limit),
        "is_truncated": total > limit,
        "rows": [
            {
                "dish_code": (row.get("nomenclature__iiko_nomenclature_external_id") or "").strip(),
                "dish_name": (row.get("nomenclature__nomenclature_name") or "").strip(),
                "dish_group_name": (row.get("nomenclature__dish_group_name") or "").strip(),
                "olap_category_name": (row.get("nomenclature__olap_category__category_name") or "").strip(),
                "source_reason": row.get("source_reason") or "",
                "source_reason_label": source_reason_labels.get(
                    row.get("source_reason") or "",
                    row.get("source_reason") or "",
                ),
            }
            for row in rows
        ],
    }


def _build_selected_virtual_category(*, virtual_category_id: int | None, limit: int) -> dict[str, Any]:
    """
    Возвращает состав выбранной виртуальной категории для режима редактирования.
    """
    virtual_pk = int(virtual_category_id or 0)
    if virtual_pk <= 0:
        return {
            "id": 0,
            "name": "",
            "code": "",
            "total": 0,
            "limit": limit,
            "is_truncated": False,
            "rows": [],
            "selected_ids": [],
        }

    virtual_category = (
        VirtualCategory.objects.filter(id=virtual_pk)
        .only("id", "name", "code")
        .first()
    )
    if virtual_category is None:
        return {
            "id": virtual_pk,
            "name": "",
            "code": "",
            "total": 0,
            "limit": limit,
            "is_truncated": False,
            "rows": [],
            "selected_ids": [],
        }

    queryset = (
        virtual_category.nomenclature_links.select_related("nomenclature", "nomenclature__olap_category")
        .order_by("nomenclature__nomenclature_name", "id")
    )
    total = queryset.count()
    rows = list(
        queryset[:limit].values(
            "nomenclature_id",
            "nomenclature__iiko_nomenclature_external_id",
            "nomenclature__nomenclature_name",
            "nomenclature__dish_group_name",
            "nomenclature__olap_category__category_name",
        )
    )

    return {
        "id": int(virtual_category.id),
        "name": (virtual_category.name or "").strip(),
        "code": (virtual_category.code or "").strip(),
        "total": int(total),
        "limit": int(limit),
        "is_truncated": total > limit,
        "rows": [
            {
                "id": int(row["nomenclature_id"]),
                "dish_code": (row.get("nomenclature__iiko_nomenclature_external_id") or "").strip(),
                "dish_name": (row.get("nomenclature__nomenclature_name") or "").strip(),
                "dish_group_name": (row.get("nomenclature__dish_group_name") or "").strip(),
                "olap_category_name": (row.get("nomenclature__olap_category__category_name") or "").strip(),
            }
            for row in rows
        ],
        "selected_ids": [int(row["nomenclature_id"]) for row in rows],
    }


def _build_nomenclature_catalog(
    *,
    query: str,
    group_query: str,
    olap_category_id: int | None,
    limit: int,
) -> dict[str, Any]:
    """
    Формирует каталог номенклатур с быстрым поиском для конструктора виртуальных категорий.
    """
    queryset = OlapNomenclatureDict.objects.select_related("olap_category").filter(is_active=True)
    if query:
        queryset = queryset.filter(
            Q(iiko_nomenclature_external_id__icontains=query)
            | Q(nomenclature_name__icontains=query)
        )
    if group_query:
        queryset = queryset.filter(dish_group_name__icontains=group_query)
    if olap_category_id is not None:
        queryset = queryset.filter(olap_category_id=int(olap_category_id))

    queryset = queryset.order_by("nomenclature_name", "id")
    total = queryset.count()
    rows = list(
        queryset[:limit].values(
            "id",
            "iiko_nomenclature_external_id",
            "nomenclature_name",
            "dish_group_name",
            "olap_category_id",
            "olap_category__category_name",
        )
    )
    return {
        "total": int(total),
        "limit": int(limit),
        "is_truncated": total > limit,
        "rows": [
            {
                "id": int(row["id"]),
                "dish_code": (row.get("iiko_nomenclature_external_id") or "").strip(),
                "dish_name": (row.get("nomenclature_name") or "").strip(),
                "dish_group_name": (row.get("dish_group_name") or "").strip(),
                "olap_category_id": int(row.get("olap_category_id") or 0),
                "olap_category_name": (row.get("olap_category__category_name") or "").strip(),
            }
            for row in rows
        ],
    }


def _to_money_str(value: Any) -> str:
    """
    Приводит денежное значение к строке с двумя знаками после запятой.
    """
    if value is None:
        return "0.00"
    return f"{Decimal(str(value)):.2f}"
