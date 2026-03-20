"""
Сервис дозагрузки чеков из журнала синхронизации в сырой OLAP-слой.

Назначение:
1. Забрать из `olap_check_sync_journal` задания со статусом `new|retry`.
2. Запросить строки чека в iiko OLAP порциями по `order_number`.
3. Идемпотентно записать результат в `olap_sales_raw_line`.
4. Обновить статусы журнала (`loaded|retry|failed|skipped`).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
from typing import Any, Iterable, Sequence

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from guests.models import OlapCheckSyncJournal, OlapSalesRawLine
from guests.services.iiko_olap_client import IikoOlapClient, IikoOlapError

logger = logging.getLogger(__name__)


@dataclass
class OlapSyncIterationStats:
    """
    Сводная статистика одного прохода воркера синхронизации OLAP.
    """

    recovered_stale_rows: int = 0
    claimed_rows: int = 0
    processed_groups: int = 0
    loaded_rows: int = 0
    retry_rows: int = 0
    failed_rows: int = 0
    skipped_rows: int = 0
    raw_rows_planned: int = 0
    raw_rows_created: int = 0
    raw_rows_duplicates: int = 0
    requested_portions: int = 0
    successful_portions: int = 0
    failed_portions: int = 0


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _row_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            return row.get(key)
    return None


def _build_row_fingerprint(*, business_date: date, row_payload: dict[str, Any]) -> str:
    """
    Возвращает идемпотентный отпечаток OLAP-строки.

    В ключ включается:
    1. дата бизнес-дня;
    2. нормализованный JSON строки OLAP с сортировкой ключей.
    """

    payload = {
        "business_date": business_date.isoformat(),
        "row": row_payload,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class OlapCheckSyncWorkerService:
    """
    Воркерный сервис дозагрузки чеков из журнала `olap_check_sync_journal`.
    """

    def __init__(
        self,
        *,
        client: IikoOlapClient,
        claim_limit: int = 200,
        portion_size: int = 200,
        max_attempts: int = 5,
        retry_base_seconds: int = 120,
        lock_timeout_seconds: int = 15 * 60,
    ) -> None:
        self.client = client
        self.claim_limit = max(1, int(claim_limit))
        self.portion_size = max(1, int(portion_size))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.lock_timeout_seconds = max(60, int(lock_timeout_seconds))

    def run_iteration(self) -> OlapSyncIterationStats:
        """
        Выполняет один проход:
        1. реанимация зависших `in_progress`;
        2. захват новых задач;
        3. запрос OLAP и запись сырых строк;
        4. обновление статусов журнала.
        """

        stats = OlapSyncIterationStats()
        now = timezone.now()
        stats.recovered_stale_rows = self._recover_stale_rows(now=now)

        claimed_rows = self._claim_rows(now=now)
        stats.claimed_rows = len(claimed_rows)
        if not claimed_rows:
            return stats

        changed_rows: dict[int, OlapCheckSyncJournal] = {}
        raw_lines_to_create: list[OlapSalesRawLine] = []

        valid_rows: list[OlapCheckSyncJournal] = []
        for row in claimed_rows:
            if row.order_number is None or row.business_date is None:
                self._mark_failed(
                    row=row,
                    now=now,
                    error_text="Нельзя запросить OLAP: в журнале отсутствуют order_number или business_date.",
                )
                changed_rows[row.id] = row
                stats.failed_rows += 1
                continue
            valid_rows.append(row)

        grouped_rows: dict[date, list[OlapCheckSyncJournal]] = defaultdict(list)
        for row in valid_rows:
            grouped_rows[row.business_date].append(row)

        for business_day, group_rows in grouped_rows.items():
            stats.processed_groups += 1
            try:
                self._process_business_day_group(
                    business_day=business_day,
                    group_rows=group_rows,
                    now=now,
                    changed_rows=changed_rows,
                    raw_lines_to_create=raw_lines_to_create,
                    stats=stats,
                )
            except Exception as err:  # noqa: BLE001
                logger.exception(
                    "OLAP sync: фатальная ошибка обработки группы business_day=%s: %s",
                    business_day,
                    err,
                )
                for row in group_rows:
                    self._mark_retry_or_terminal(
                        row=row,
                        now=now,
                        error_text=f"Групповая ошибка обработки: {err}",
                        not_found=False,
                    )
                    changed_rows[row.id] = row

        if raw_lines_to_create:
            self._bulk_create_raw_lines(raw_lines_to_create=raw_lines_to_create, stats=stats)

        if changed_rows:
            OlapCheckSyncJournal.objects.bulk_update(
                list(changed_rows.values()),
                fields=[
                    "status",
                    "attempt_count",
                    "next_try_at",
                    "last_error",
                    "locked_at",
                    "loaded_at",
                    "updated_at",
                ],
                batch_size=1000,
            )

        return stats

    def _recover_stale_rows(self, *, now: datetime) -> int:
        stale_before = now - timedelta(seconds=self.lock_timeout_seconds)
        return OlapCheckSyncJournal.objects.filter(
            status=OlapCheckSyncJournal.Status.IN_PROGRESS,
            locked_at__lt=stale_before,
        ).update(
            status=OlapCheckSyncJournal.Status.RETRY,
            next_try_at=now,
            locked_at=None,
            last_error="Задача возвращена в retry после тайм-аута блокировки.",
            updated_at=now,
        )

    def _claim_rows(self, *, now: datetime) -> list[OlapCheckSyncJournal]:
        claim_filter = (
            Q(status=OlapCheckSyncJournal.Status.NEW)
            | Q(status=OlapCheckSyncJournal.Status.RETRY, next_try_at__isnull=True)
            | Q(status=OlapCheckSyncJournal.Status.RETRY, next_try_at__lte=now)
        )

        with transaction.atomic():
            rows = list(
                OlapCheckSyncJournal.objects.select_for_update(skip_locked=True)
                .filter(claim_filter)
                .order_by("created_at", "id")[: self.claim_limit]
            )
            if not rows:
                return []

            row_ids = [row.id for row in rows]
            OlapCheckSyncJournal.objects.filter(id__in=row_ids).update(
                status=OlapCheckSyncJournal.Status.IN_PROGRESS,
                locked_at=now,
                updated_at=now,
            )

            for row in rows:
                row.status = OlapCheckSyncJournal.Status.IN_PROGRESS
                row.locked_at = now
                row.updated_at = now

            return rows

    def _process_business_day_group(
        self,
        *,
        business_day: date,
        group_rows: Sequence[OlapCheckSyncJournal],
        now: datetime,
        changed_rows: dict[int, OlapCheckSyncJournal],
        raw_lines_to_create: list[OlapSalesRawLine],
        stats: OlapSyncIterationStats,
    ) -> None:
        order_numbers = sorted({int(row.order_number) for row in group_rows if row.order_number is not None})
        if not order_numbers:
            for row in group_rows:
                self._mark_failed(
                    row=row,
                    now=now,
                    error_text="Группа не содержит валидных order_number для OLAP-запроса.",
                )
                changed_rows[row.id] = row
                stats.failed_rows += 1
            return

        department_ids = sorted(
            {
                department_id
                for department_id in (_normalize_text(row.department_id) for row in group_rows)
                if department_id
            }
        )

        try:
            olap_rows, _summary_rows, portion_stats = self.client.fetch_sales_in_portions(
                date_from=business_day,
                date_to=business_day,
                order_numbers=order_numbers,
                department_ids=department_ids or None,
                portion_size=self.portion_size,
                fail_fast=False,
            )
        except IikoOlapError as err:
            for row in group_rows:
                self._mark_retry_or_terminal(
                    row=row,
                    now=now,
                    error_text=f"OLAP-ошибка: {err}",
                    not_found=False,
                )
                changed_rows[row.id] = row
                if row.status == OlapCheckSyncJournal.Status.RETRY:
                    stats.retry_rows += 1
                else:
                    stats.failed_rows += 1
            return

        stats.requested_portions += int(portion_stats.requested_portions)
        stats.successful_portions += int(portion_stats.successful_portions)
        stats.failed_portions += int(portion_stats.failed_portions)

        failed_orders: set[int] = set()
        for failed_part in portion_stats.failed_order_number_portions:
            for order_number in failed_part:
                failed_orders.add(int(order_number))

        olap_rows_by_order: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for payload in olap_rows:
            order_number = _to_int(_row_value(payload, "OrderNum"))
            if order_number is None:
                continue
            olap_rows_by_order[order_number].append(payload)

        journal_rows_by_order: dict[int, list[OlapCheckSyncJournal]] = defaultdict(list)
        for row in group_rows:
            if row.order_number is None:
                continue
            journal_rows_by_order[int(row.order_number)].append(row)

        for order_number, order_rows in journal_rows_by_order.items():
            if order_number in failed_orders:
                for row in order_rows:
                    self._mark_retry_or_terminal(
                        row=row,
                        now=now,
                        error_text=f"Чек {order_number}: не удалось загрузить часть порций OLAP.",
                        not_found=False,
                    )
                    changed_rows[row.id] = row
                    if row.status == OlapCheckSyncJournal.Status.RETRY:
                        stats.retry_rows += 1
                    else:
                        stats.failed_rows += 1
                continue

            matched_payloads = olap_rows_by_order.get(order_number, [])
            if not matched_payloads:
                for row in order_rows:
                    self._mark_retry_or_terminal(
                        row=row,
                        now=now,
                        error_text=f"Чек {order_number}: в OLAP пока нет строк по этому заказу.",
                        not_found=True,
                    )
                    changed_rows[row.id] = row
                    if row.status == OlapCheckSyncJournal.Status.RETRY:
                        stats.retry_rows += 1
                    elif row.status == OlapCheckSyncJournal.Status.SKIPPED:
                        stats.skipped_rows += 1
                    else:
                        stats.failed_rows += 1
                continue

            owner_row = sorted(order_rows, key=lambda item: item.id)[0]
            mapped_raw_lines = self._map_payloads_to_raw_lines(
                owner_row=owner_row,
                payload_rows=matched_payloads,
            )
            raw_lines_to_create.extend(mapped_raw_lines)
            stats.raw_rows_planned += len(mapped_raw_lines)

            for row in order_rows:
                self._mark_loaded(row=row, now=now)
                changed_rows[row.id] = row
                stats.loaded_rows += 1

    def _map_payloads_to_raw_lines(
        self,
        *,
        owner_row: OlapCheckSyncJournal,
        payload_rows: Iterable[dict[str, Any]],
    ) -> list[OlapSalesRawLine]:
        result: list[OlapSalesRawLine] = []
        default_business_date = owner_row.business_date or timezone.localdate()

        for payload in payload_rows:
            business_day = _to_date(_row_value(payload, "OpenDate.Typed")) or default_business_date
            order_number = _to_int(_row_value(payload, "OrderNum")) or int(owner_row.order_number or 0)
            uniq_order_id = _normalize_text(_row_value(payload, "UniqOrderId.Id", "UniqOrderId"))
            item_sale_event_id = _normalize_text(_row_value(payload, "ItemSaleEvent.Id"))

            dish_code = _normalize_text(_row_value(payload, "DishCode"))
            dish_name = _normalize_text(_row_value(payload, "DishName"))
            dish_category_id = _normalize_text(_row_value(payload, "DishCategory.Id"))
            dish_category_name = _normalize_text(_row_value(payload, "DishCategory"))
            dish_group_id = _normalize_text(_row_value(payload, "DishGroup.Id"))
            dish_group_name = _normalize_text(_row_value(payload, "DishGroup"))

            department_id = _normalize_text(_row_value(payload, "Department.Id")) or _normalize_text(owner_row.department_id)
            department_code = _normalize_text(_row_value(payload, "Department.Code")) or _normalize_text(owner_row.department_code)
            department_name = _normalize_text(_row_value(payload, "Department"))

            restaurant_section_id = _normalize_text(_row_value(payload, "RestaurantSection.Id"))
            restoraunt_group_id = _normalize_text(_row_value(payload, "RestorauntGroup.Id")) or _normalize_text(
                owner_row.restoraunt_group_id
            )
            restoraunt_group_name = _normalize_text(_row_value(payload, "RestorauntGroup"))

            dish_amount = _to_decimal(_row_value(payload, "DishAmountInt", "DishAmount"))
            dish_sum_int = _to_decimal(_row_value(payload, "DishSumInt"))
            discount_sum = _to_decimal(_row_value(payload, "DishDiscountSumInt", "DiscountSumInt"))
            bonus_sum = _to_decimal(_row_value(payload, "PayedByBonus", "PayedByBonuses"))

            coupon_series = _normalize_text(_row_value(payload, "CouponInfo.Series"))
            coupon_number = _normalize_text(_row_value(payload, "CouponInfo.Number"))

            row_data_for_fingerprint = {
                "OrderNum": order_number,
                "UniqOrderId": uniq_order_id,
                "ItemSaleEventId": item_sale_event_id,
                "DishCode": dish_code,
                "DishName": dish_name,
                "DishCategoryId": dish_category_id,
                "DishGroupId": dish_group_id,
                "DepartmentId": department_id,
                "RestaurantSectionId": restaurant_section_id,
                "RestorauntGroupId": restoraunt_group_id,
                "DishAmount": str(dish_amount) if dish_amount is not None else None,
                "DishSumInt": str(dish_sum_int) if dish_sum_int is not None else None,
                "DiscountSum": str(discount_sum) if discount_sum is not None else None,
                "BonusSum": str(bonus_sum) if bonus_sum is not None else None,
                "CouponSeries": coupon_series,
                "CouponNumber": coupon_number,
            }

            fingerprint = _build_row_fingerprint(
                business_date=business_day,
                row_payload=row_data_for_fingerprint,
            )

            result.append(
                OlapSalesRawLine(
                    row_fingerprint=fingerprint,
                    sync_journal=owner_row,
                    guest=owner_row.guest,
                    business_date=business_day,
                    department_id=department_id,
                    department_code=department_code,
                    department_name=department_name,
                    restaurant_section_id=restaurant_section_id,
                    restoraunt_group_id=restoraunt_group_id,
                    restoraunt_group_name=restoraunt_group_name,
                    order_number=order_number,
                    uniq_order_id=uniq_order_id,
                    item_sale_event_id=item_sale_event_id,
                    dish_code=dish_code,
                    dish_name=dish_name,
                    dish_category_id=dish_category_id,
                    dish_category_name=dish_category_name,
                    dish_group_id=dish_group_id,
                    dish_group_name=dish_group_name,
                    dish_amount=dish_amount,
                    dish_sum_before_discount=dish_sum_int,
                    dish_sum_after_discount=dish_sum_int,
                    discount_sum=discount_sum,
                    bonus_sum=bonus_sum,
                    coupon_series=coupon_series,
                    coupon_number=coupon_number,
                    raw_payload=payload,
                )
            )

        return result

    def _bulk_create_raw_lines(
        self,
        *,
        raw_lines_to_create: Sequence[OlapSalesRawLine],
        stats: OlapSyncIterationStats,
    ) -> None:
        if not raw_lines_to_create:
            return

        unique_by_fingerprint: dict[str, OlapSalesRawLine] = {}
        for item in raw_lines_to_create:
            unique_by_fingerprint[item.row_fingerprint] = item

        fingerprints = list(unique_by_fingerprint.keys())
        existing_fingerprints = set(
            OlapSalesRawLine.objects.filter(row_fingerprint__in=fingerprints).values_list(
                "row_fingerprint",
                flat=True,
            )
        )

        create_payload = [
            row
            for fingerprint, row in unique_by_fingerprint.items()
            if fingerprint not in existing_fingerprints
        ]

        if create_payload:
            OlapSalesRawLine.objects.bulk_create(
                create_payload,
                batch_size=1000,
                ignore_conflicts=False,
            )

        stats.raw_rows_created += len(create_payload)
        stats.raw_rows_duplicates += len(unique_by_fingerprint) - len(create_payload)

    def _mark_loaded(self, *, row: OlapCheckSyncJournal, now: datetime) -> None:
        row.status = OlapCheckSyncJournal.Status.LOADED
        row.locked_at = None
        row.next_try_at = None
        row.last_error = None
        row.loaded_at = now
        row.updated_at = now

    def _mark_failed(self, *, row: OlapCheckSyncJournal, now: datetime, error_text: str) -> None:
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.status = OlapCheckSyncJournal.Status.FAILED
        row.next_try_at = None
        row.last_error = (error_text or "")[:2000]
        row.locked_at = None
        row.updated_at = now

    def _mark_retry_or_terminal(
        self,
        *,
        row: OlapCheckSyncJournal,
        now: datetime,
        error_text: str,
        not_found: bool,
    ) -> None:
        next_attempt = int(row.attempt_count or 0) + 1
        row.attempt_count = next_attempt
        row.last_error = (error_text or "")[:2000]
        row.locked_at = None
        row.updated_at = now

        if next_attempt >= self.max_attempts:
            row.next_try_at = None
            row.status = (
                OlapCheckSyncJournal.Status.SKIPPED
                if not_found
                else OlapCheckSyncJournal.Status.FAILED
            )
            return

        delay_seconds = self.retry_base_seconds * (2 ** max(0, next_attempt - 1))
        row.status = OlapCheckSyncJournal.Status.RETRY
        row.next_try_at = now + timedelta(seconds=delay_seconds)

