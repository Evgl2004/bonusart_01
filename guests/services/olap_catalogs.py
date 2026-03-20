from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Iterable, Optional

from django.db import transaction
from django.utils import timezone

from guests.models import (
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    OlapCategoryDict,
    OlapNomenclatureDict,
    OlapSalesRawLine,
    VirtualCategoryNomenclatureLink,
    VirtualCategoryOlapCategoryLink,
)

logger = logging.getLogger(__name__)

_CATEGORY_FALLBACK_PREFIX = "name::"


@dataclass
class OlapCatalogSyncStats:
    """
    Результаты синхронизации OLAP-справочников из сырых строк.
    """

    scanned_raw_lines: int = 0
    categories_created: int = 0
    categories_updated: int = 0
    nomenclatures_created: int = 0
    nomenclatures_updated: int = 0
    skipped_without_category: int = 0
    skipped_without_nomenclature: int = 0


@dataclass
class FocusResolvedRebuildStats:
    """
    Результаты пересборки предрассчитанных связей focus -> nomenclature.
    """

    scanned_focus_categories: int = 0
    rebuilt_focus_categories: int = 0
    disabled_focus_categories_cleared: int = 0
    written_links: int = 0
    deleted_links: int = 0
    skipped_invalid_focus_categories: int = 0


def _normalize_optional_text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_category_external_id(
    *,
    raw_category_external_id: object,
    raw_category_name: object,
) -> Optional[str]:
    """
    Возвращает внешний идентификатор категории для справочника.

    Приоритет:
    1. Явный `dish_category_id` из OLAP.
    2. Детерминированный fallback по названию `name::<lower(name)>`,
       если в выгрузке нет ID категории.
    """

    category_external_id = _normalize_optional_text(raw_category_external_id)
    if category_external_id:
        return category_external_id

    category_name = _normalize_optional_text(raw_category_name)
    if not category_name:
        return None

    return f"{_CATEGORY_FALLBACK_PREFIX}{category_name.lower()}"


def sync_olap_catalogs_from_raw_lines(
    *,
    raw_line_id_from: int | None = None,
    raw_line_id_to: int | None = None,
    batch_size: int = 2000,
) -> OlapCatalogSyncStats:
    """
    Наполняет справочники `olap_category_dict` и `olap_nomenclature_dict`
    на основе `olap_sales_raw_line`.

    Важно:
    1. Сервис не удаляет старые справочники и не деактивирует записи.
    2. При отсутствии `dish_category_id` используется fallback-ключ по имени.
    """

    stats = OlapCatalogSyncStats()
    safe_batch_size = max(100, int(batch_size))

    query = OlapSalesRawLine.objects.all().values(
        "id",
        "created_at",
        "dish_category_id",
        "dish_category_name",
        "dish_code",
        "dish_name",
        "dish_group_id",
        "dish_group_name",
    )
    if raw_line_id_from is not None:
        query = query.filter(id__gte=int(raw_line_id_from))
    if raw_line_id_to is not None:
        query = query.filter(id__lte=int(raw_line_id_to))

    query = query.order_by("id")

    categories_buffer: dict[str, dict[str, object]] = {}
    nomenclature_buffer: dict[str, dict[str, object]] = {}

    for row in query.iterator(chunk_size=safe_batch_size):
        stats.scanned_raw_lines += 1
        row_created_at = row["created_at"] or timezone.now()

        category_external_id = _build_category_external_id(
            raw_category_external_id=row["dish_category_id"],
            raw_category_name=row["dish_category_name"],
        )
        category_name = _normalize_optional_text(row["dish_category_name"]) or "Категория не указана"

        if not category_external_id:
            stats.skipped_without_category += 1
            continue

        category_payload = categories_buffer.get(category_external_id)
        if category_payload is None:
            categories_buffer[category_external_id] = {
                "category_name": category_name,
                "first_seen_at": row_created_at,
                "last_seen_at": row_created_at,
            }
        else:
            if row_created_at < category_payload["first_seen_at"]:
                category_payload["first_seen_at"] = row_created_at
            if row_created_at > category_payload["last_seen_at"]:
                category_payload["last_seen_at"] = row_created_at
            if category_name and category_name != category_payload["category_name"]:
                # Берём последнее известное название категории.
                category_payload["category_name"] = category_name

        nomenclature_external_id = _normalize_optional_text(row["dish_code"])
        if not nomenclature_external_id:
            stats.skipped_without_nomenclature += 1
            continue

        nomenclature_name = _normalize_optional_text(row["dish_name"]) or "Номенклатура без названия"
        nomenclature_payload = nomenclature_buffer.get(nomenclature_external_id)
        if nomenclature_payload is None:
            nomenclature_buffer[nomenclature_external_id] = {
                "nomenclature_name": nomenclature_name,
                "category_external_id": category_external_id,
                "iiko_dish_group_external_id": _normalize_optional_text(row["dish_group_id"]),
                "dish_group_name": _normalize_optional_text(row["dish_group_name"]),
                "first_seen_at": row_created_at,
                "last_seen_at": row_created_at,
            }
        else:
            if row_created_at < nomenclature_payload["first_seen_at"]:
                nomenclature_payload["first_seen_at"] = row_created_at
            if row_created_at > nomenclature_payload["last_seen_at"]:
                nomenclature_payload["last_seen_at"] = row_created_at
            nomenclature_payload["category_external_id"] = category_external_id
            if nomenclature_name and nomenclature_name != nomenclature_payload["nomenclature_name"]:
                nomenclature_payload["nomenclature_name"] = nomenclature_name
            dish_group_external_id = _normalize_optional_text(row["dish_group_id"])
            dish_group_name = _normalize_optional_text(row["dish_group_name"])
            if dish_group_external_id:
                nomenclature_payload["iiko_dish_group_external_id"] = dish_group_external_id
            if dish_group_name:
                nomenclature_payload["dish_group_name"] = dish_group_name

    if not categories_buffer and not nomenclature_buffer:
        logger.info("sync_olap_catalogs_from_raw_lines: нет строк для обновления справочников")
        return stats

    with transaction.atomic():
        category_external_ids = list(categories_buffer.keys())
        existing_categories = {
            item.iiko_category_external_id: item
            for item in OlapCategoryDict.objects.filter(iiko_category_external_id__in=category_external_ids)
        }

        categories_to_create: list[OlapCategoryDict] = []
        categories_to_update: list[OlapCategoryDict] = []

        for category_external_id, payload in categories_buffer.items():
            existing = existing_categories.get(category_external_id)
            if existing is None:
                categories_to_create.append(
                    OlapCategoryDict(
                        iiko_category_external_id=category_external_id,
                        category_name=payload["category_name"],
                        first_seen_at=payload["first_seen_at"],
                        last_seen_at=payload["last_seen_at"],
                        is_active=True,
                    )
                )
                continue

            changed = False
            if existing.category_name != payload["category_name"]:
                existing.category_name = payload["category_name"]
                changed = True
            if existing.first_seen_at is None or payload["first_seen_at"] < existing.first_seen_at:
                existing.first_seen_at = payload["first_seen_at"]
                changed = True
            if existing.last_seen_at is None or payload["last_seen_at"] > existing.last_seen_at:
                existing.last_seen_at = payload["last_seen_at"]
                changed = True
            if not existing.is_active:
                existing.is_active = True
                changed = True
            if changed:
                categories_to_update.append(existing)

        if categories_to_create:
            OlapCategoryDict.objects.bulk_create(categories_to_create, batch_size=safe_batch_size)
        if categories_to_update:
            OlapCategoryDict.objects.bulk_update(
                categories_to_update,
                fields=["category_name", "first_seen_at", "last_seen_at", "is_active", "updated_at"],
                batch_size=safe_batch_size,
            )

        stats.categories_created = len(categories_to_create)
        stats.categories_updated = len(categories_to_update)

        all_categories = {
            item.iiko_category_external_id: item
            for item in OlapCategoryDict.objects.filter(iiko_category_external_id__in=category_external_ids)
        }

        nomenclature_external_ids = list(nomenclature_buffer.keys())
        existing_nomenclatures = {
            item.iiko_nomenclature_external_id: item
            for item in OlapNomenclatureDict.objects.select_related("olap_category").filter(
                iiko_nomenclature_external_id__in=nomenclature_external_ids
            )
        }

        nomenclature_to_create: list[OlapNomenclatureDict] = []
        nomenclature_to_update: list[OlapNomenclatureDict] = []

        for nomenclature_external_id, payload in nomenclature_buffer.items():
            category_obj = all_categories.get(payload["category_external_id"])
            if category_obj is None:
                stats.skipped_without_category += 1
                continue

            existing = existing_nomenclatures.get(nomenclature_external_id)
            if existing is None:
                nomenclature_to_create.append(
                    OlapNomenclatureDict(
                        iiko_nomenclature_external_id=nomenclature_external_id,
                        nomenclature_name=payload["nomenclature_name"],
                        olap_category=category_obj,
                        iiko_dish_group_external_id=payload["iiko_dish_group_external_id"],
                        dish_group_name=payload["dish_group_name"],
                        first_seen_at=payload["first_seen_at"],
                        last_seen_at=payload["last_seen_at"],
                        is_active=True,
                    )
                )
                continue

            changed = False
            if existing.nomenclature_name != payload["nomenclature_name"]:
                existing.nomenclature_name = payload["nomenclature_name"]
                changed = True
            if existing.olap_category_id != category_obj.id:
                existing.olap_category = category_obj
                changed = True
            if existing.iiko_dish_group_external_id != payload["iiko_dish_group_external_id"]:
                existing.iiko_dish_group_external_id = payload["iiko_dish_group_external_id"]
                changed = True
            if existing.dish_group_name != payload["dish_group_name"]:
                existing.dish_group_name = payload["dish_group_name"]
                changed = True
            if existing.first_seen_at is None or payload["first_seen_at"] < existing.first_seen_at:
                existing.first_seen_at = payload["first_seen_at"]
                changed = True
            if existing.last_seen_at is None or payload["last_seen_at"] > existing.last_seen_at:
                existing.last_seen_at = payload["last_seen_at"]
                changed = True
            if not existing.is_active:
                existing.is_active = True
                changed = True

            if changed:
                nomenclature_to_update.append(existing)

        if nomenclature_to_create:
            OlapNomenclatureDict.objects.bulk_create(nomenclature_to_create, batch_size=safe_batch_size)
        if nomenclature_to_update:
            OlapNomenclatureDict.objects.bulk_update(
                nomenclature_to_update,
                fields=[
                    "nomenclature_name",
                    "olap_category",
                    "iiko_dish_group_external_id",
                    "dish_group_name",
                    "first_seen_at",
                    "last_seen_at",
                    "is_active",
                    "updated_at",
                ],
                batch_size=safe_batch_size,
            )

        stats.nomenclatures_created = len(nomenclature_to_create)
        stats.nomenclatures_updated = len(nomenclature_to_update)

    logger.info(
        (
            "sync_olap_catalogs_from_raw_lines: scanned=%s, categories(created=%s, updated=%s), "
            "nomenclature(created=%s, updated=%s), skipped_without_category=%s, skipped_without_nomenclature=%s"
        ),
        stats.scanned_raw_lines,
        stats.categories_created,
        stats.categories_updated,
        stats.nomenclatures_created,
        stats.nomenclatures_updated,
        stats.skipped_without_category,
        stats.skipped_without_nomenclature,
    )
    return stats


def rebuild_focus_category_nomenclature_resolved(
    *,
    focus_codes: Optional[Iterable[str]] = None,
) -> FocusResolvedRebuildStats:
    """
    Пересобирает `focus_category_nomenclature_resolved`.

    Алгоритм:
    1. Для `olap_direct` берём все активные номенклатуры категории OLAP.
    2. Для `virtual` объединяем:
       - прямые номенклатуры виртуальной категории;
       - номенклатуры из связанных категорий OLAP.
    3. Для неактивных фокусных категорий удаляем старые связи.
    """

    stats = FocusResolvedRebuildStats()
    query = FocusCategory.objects.select_related("olap_category", "virtual_category").all().order_by("id")

    if focus_codes:
        normalized_codes = [str(code).strip() for code in focus_codes if str(code).strip()]
        if normalized_codes:
            query = query.filter(code__in=normalized_codes)

    focus_rows = list(query)
    stats.scanned_focus_categories = len(focus_rows)

    for focus in focus_rows:
        if not focus.is_enabled:
            deleted_count, _ = FocusCategoryNomenclatureResolved.objects.filter(
                focus_category=focus
            ).delete()
            stats.disabled_focus_categories_cleared += 1
            stats.deleted_links += int(deleted_count)
            continue

        reason_by_nomenclature_id: dict[int, str] = {}

        if focus.source_type == FocusCategory.SourceType.OLAP_DIRECT:
            if not focus.olap_category_id:
                stats.skipped_invalid_focus_categories += 1
                continue

            direct_ids = list(
                OlapNomenclatureDict.objects.filter(
                    olap_category_id=focus.olap_category_id,
                    is_active=True,
                ).values_list("id", flat=True)
            )
            for nomenclature_id in direct_ids:
                reason_by_nomenclature_id[nomenclature_id] = (
                    FocusCategoryNomenclatureResolved.SourceReason.DIRECT_OLAP
                )

        elif focus.source_type == FocusCategory.SourceType.VIRTUAL:
            if not focus.virtual_category_id:
                stats.skipped_invalid_focus_categories += 1
                continue

            category_ids = list(
                VirtualCategoryOlapCategoryLink.objects.filter(
                    virtual_category_id=focus.virtual_category_id
                ).values_list("olap_category_id", flat=True)
            )
            category_nomenclature_ids = []
            if category_ids:
                category_nomenclature_ids = list(
                    OlapNomenclatureDict.objects.filter(
                        olap_category_id__in=category_ids,
                        is_active=True,
                    ).values_list("id", flat=True)
                )
            for nomenclature_id in category_nomenclature_ids:
                reason_by_nomenclature_id[nomenclature_id] = (
                    FocusCategoryNomenclatureResolved.SourceReason.VIRTUAL_OLAP_CATEGORY
                )

            direct_nomenclature_ids = list(
                VirtualCategoryNomenclatureLink.objects.filter(
                    virtual_category_id=focus.virtual_category_id
                ).values_list("nomenclature_id", flat=True)
            )
            for nomenclature_id in direct_nomenclature_ids:
                # Прямое явное включение маркетологом важнее category-включения.
                reason_by_nomenclature_id[nomenclature_id] = (
                    FocusCategoryNomenclatureResolved.SourceReason.VIRTUAL_NOMENCLATURE
                )

        else:
            stats.skipped_invalid_focus_categories += 1
            continue

        with transaction.atomic():
            deleted_count, _ = FocusCategoryNomenclatureResolved.objects.filter(
                focus_category=focus
            ).delete()
            stats.deleted_links += int(deleted_count)

            if reason_by_nomenclature_id:
                payload = [
                    FocusCategoryNomenclatureResolved(
                        focus_category=focus,
                        nomenclature_id=nomenclature_id,
                        source_reason=reason,
                    )
                    for nomenclature_id, reason in reason_by_nomenclature_id.items()
                ]
                FocusCategoryNomenclatureResolved.objects.bulk_create(payload, batch_size=2000)
                stats.written_links += len(payload)

        stats.rebuilt_focus_categories += 1

    logger.info(
        (
            "rebuild_focus_category_nomenclature_resolved: focus_scanned=%s, rebuilt=%s, "
            "disabled_cleared=%s, written_links=%s, deleted_links=%s, skipped_invalid=%s"
        ),
        stats.scanned_focus_categories,
        stats.rebuilt_focus_categories,
        stats.disabled_focus_categories_cleared,
        stats.written_links,
        stats.deleted_links,
        stats.skipped_invalid_focus_categories,
    )
    return stats
