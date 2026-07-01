from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from guests.models import (
    CouponAutoscenarioAssignment,
    CouponCampaignAssignment,
    IikoCustomerCategorySyncEvent,
)
from guests.services.iiko_customer_category_client import (
    IikoCustomerCategoryApiError,
    IikoCustomerCategoryClient,
)

logger = logging.getLogger(__name__)

CouponAssignment = CouponCampaignAssignment | CouponAutoscenarioAssignment

LIVE_COUPON_ASSIGNMENT_STATUSES = [
    CouponCampaignAssignment.Status.RESERVED,
    CouponCampaignAssignment.Status.SENT,
]
IIKO_CUSTOMER_HAS_NO_CATEGORY_ERROR_CODE = "Customer_CustomerHasNoCategory"


class IikoCustomerCategorySyncError(Exception):
    """Ошибка доставки события категории гостя в iikoCard."""


@dataclass(slots=True)
class IikoCustomerCategoryEnqueueResult:
    """
    Результат постановки события iikoCard в очередь.
    """

    event: IikoCustomerCategorySyncEvent | None
    created: bool = False
    skipped: bool = False
    reason: str = ""


@dataclass(slots=True)
class IikoCustomerCategorySyncBatchStats:
    """
    Результат одного прохода синхронизации категорий гостей iikoCard.
    """

    scanned: int = 0
    processed: int = 0
    acked: int = 0
    failed: int = 0
    skipped: int = 0
    skipped_max_attempts: int = 0
    add_acked: int = 0
    remove_acked: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "scanned": int(self.scanned),
            "processed": int(self.processed),
            "acked": int(self.acked),
            "failed": int(self.failed),
            "skipped": int(self.skipped),
            "skipped_max_attempts": int(self.skipped_max_attempts),
            "add_acked": int(self.add_acked),
            "remove_acked": int(self.remove_acked),
        }


def iiko_customer_category_sync_enabled() -> bool:
    """
    Проверяет, включён ли контур синхронизации общей категории iikoCard.
    """
    return bool(getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED", False))


def iiko_customer_category_gate_required() -> bool:
    """
    Проверяет, должен ли шлюз перед отправкой ждать подтверждение от iikoCard.
    """
    return iiko_customer_category_sync_enabled() and bool(
        getattr(settings, "IIKO_CUSTOMER_CATEGORY_GATE_REQUIRE_ACK", True)
    )


def get_iiko_active_coupon_category_id() -> str:
    """
    Возвращает ID общей категории iikoCard «Активный купон SAGUR».
    """
    return str(
        getattr(settings, "IIKO_ACTIVE_COUPON_CATEGORY_ID", "")
        or getattr(settings, "IIKO_CUSTOMER_CATEGORY_ID", "")
        or ""
    ).strip()


def get_iiko_active_coupon_category_name() -> str:
    """
    Возвращает человекочитаемое имя общей категории iikoCard.
    """
    return str(
        getattr(settings, "IIKO_ACTIVE_COUPON_CATEGORY_NAME", "")
        or "Активный купон SAGUR"
    ).strip()


def guest_has_live_sagur_coupon(*, guest_id: int | None, now=None) -> bool:
    """
    Проверяет, есть ли у гостя хотя бы один живой купон SAGUR.

    Живой купон — это назначение в статусе `reserved` или `sent` в любой из
    двух купонных таблиц: ручная кампания или автосценарий. Фильтра по серии
    здесь нет намеренно: категория iikoCard общая для всех купонов SAGUR.
    """
    if not guest_id:
        return False
    current_now = now or timezone.now()
    live_filter = Q(lifetime_expires_at__isnull=True) | Q(lifetime_expires_at__gt=current_now)

    campaign_exists = CouponCampaignAssignment.objects.filter(
        guest_id=int(guest_id),
        status__in=LIVE_COUPON_ASSIGNMENT_STATUSES,
    ).filter(live_filter).exists()
    if campaign_exists:
        return True

    return CouponAutoscenarioAssignment.objects.filter(
        guest_id=int(guest_id),
        status__in=LIVE_COUPON_ASSIGNMENT_STATUSES,
    ).filter(live_filter).exists()


def build_iiko_category_add_events_map(
    *,
    campaign_assignment_ids: list[int] | None = None,
    autoscenario_assignment_ids: list[int] | None = None,
) -> dict[tuple[str, int], IikoCustomerCategorySyncEvent]:
    """
    Возвращает последние `add`-события iikoCard по назначениям купонов.
    """
    filters = Q()
    has_filter = False
    if campaign_assignment_ids:
        filters |= Q(campaign_assignment_id__in=campaign_assignment_ids)
        has_filter = True
    if autoscenario_assignment_ids:
        filters |= Q(autoscenario_assignment_id__in=autoscenario_assignment_ids)
        has_filter = True
    if not has_filter:
        return {}

    events = (
        IikoCustomerCategorySyncEvent.objects.filter(
            filters,
            action=IikoCustomerCategorySyncEvent.Action.ADD,
        )
        .order_by("campaign_assignment_id", "autoscenario_assignment_id", "-id")
    )
    mapping: dict[tuple[str, int], IikoCustomerCategorySyncEvent] = {}
    for event in events:
        key = _event_assignment_key(event=event)
        if key is None or key in mapping:
            continue
        mapping[key] = event
    return mapping


def enqueue_iiko_category_add_for_assignment(
    *,
    assignment: CouponAssignment,
    now=None,
    dry_run: bool = False,
) -> IikoCustomerCategoryEnqueueResult:
    """
    Ставит `add` в очередь iikoCard для активного назначения купона.
    """
    if not iiko_customer_category_sync_enabled():
        return IikoCustomerCategoryEnqueueResult(event=None, skipped=True, reason="sync_disabled")
    current_now = now or timezone.now()
    category_id = get_iiko_active_coupon_category_id()
    if not category_id:
        if not dry_run:
            _mark_assignment_iiko_error(
                assignment=assignment,
                error_text="Не задан IIKO_ACTIVE_COUPON_CATEGORY_ID для категории «Активный купон SAGUR».",
            )
        return IikoCustomerCategoryEnqueueResult(event=None, skipped=True, reason="category_id_missing")
    if not assignment.guest_id:
        if not dry_run:
            _mark_assignment_iiko_error(
                assignment=assignment,
                error_text="Нельзя добавить категорию iikoCard: у назначения нет гостя.",
            )
        return IikoCustomerCategoryEnqueueResult(event=None, skipped=True, reason="guest_missing")

    existing = _find_existing_event(
        assignment=assignment,
        action=IikoCustomerCategorySyncEvent.Action.ADD,
        category_id=category_id,
    )
    if existing is not None:
        if existing.status == IikoCustomerCategorySyncEvent.Status.ACKED:
            if not dry_run:
                _mark_assignment_iiko_ok(assignment=assignment, synced_at=existing.ack_at or current_now)
        elif existing.status == IikoCustomerCategorySyncEvent.Status.PENDING and not dry_run:
            _mark_assignment_iiko_pending(assignment=assignment)
        return IikoCustomerCategoryEnqueueResult(event=existing, created=False)

    payload = _build_event_payload(
        assignment=assignment,
        action=IikoCustomerCategorySyncEvent.Action.ADD,
        category_id=category_id,
    )
    if dry_run:
        return IikoCustomerCategoryEnqueueResult(event=None, created=True)

    event = IikoCustomerCategorySyncEvent.objects.create(
        action=IikoCustomerCategorySyncEvent.Action.ADD,
        source_type=_assignment_source_type(assignment),
        guest_id=assignment.guest_id,
        iiko_customer_id=_assignment_iiko_customer_id(assignment),
        category_id=category_id,
        organization_id=str(getattr(settings, "IIKO_ORGANIZATION_ID", "") or "").strip() or None,
        payload_json=payload,
        status=IikoCustomerCategorySyncEvent.Status.PENDING,
        next_retry_at=current_now,
        **_assignment_event_create_kwargs(assignment),
    )
    _mark_assignment_iiko_pending(assignment=assignment)
    return IikoCustomerCategoryEnqueueResult(event=event, created=True)


def enqueue_iiko_category_remove_if_last_coupon(
    *,
    assignment: CouponAssignment,
    now=None,
    dry_run: bool = False,
) -> IikoCustomerCategoryEnqueueResult:
    """
    Ставит `remove` в очередь iikoCard только если у гостя больше нет живых купонов.
    """
    if not iiko_customer_category_sync_enabled():
        return IikoCustomerCategoryEnqueueResult(event=None, skipped=True, reason="sync_disabled")
    current_now = now or timezone.now()
    category_id = get_iiko_active_coupon_category_id()
    if not category_id:
        return IikoCustomerCategoryEnqueueResult(event=None, skipped=True, reason="category_id_missing")
    if not assignment.guest_id:
        return IikoCustomerCategoryEnqueueResult(event=None, skipped=True, reason="guest_missing")
    if guest_has_live_sagur_coupon(guest_id=int(assignment.guest_id), now=current_now):
        return IikoCustomerCategoryEnqueueResult(
            event=None,
            skipped=True,
            reason="guest_has_another_live_coupon",
        )

    payload = _build_event_payload(
        assignment=assignment,
        action=IikoCustomerCategorySyncEvent.Action.REMOVE,
        category_id=category_id,
    )
    existing = _find_existing_event(
        assignment=assignment,
        action=IikoCustomerCategorySyncEvent.Action.REMOVE,
        category_id=category_id,
    )
    if dry_run:
        return IikoCustomerCategoryEnqueueResult(event=existing, created=existing is None)

    if existing is None:
        event = IikoCustomerCategorySyncEvent.objects.create(
            action=IikoCustomerCategorySyncEvent.Action.REMOVE,
            source_type=_assignment_source_type(assignment),
            guest_id=assignment.guest_id,
            iiko_customer_id=_assignment_iiko_customer_id(assignment),
            category_id=category_id,
            organization_id=str(getattr(settings, "IIKO_ORGANIZATION_ID", "") or "").strip() or None,
            payload_json=payload,
            status=IikoCustomerCategorySyncEvent.Status.PENDING,
            next_retry_at=current_now,
            **_assignment_event_create_kwargs(assignment),
        )
        return IikoCustomerCategoryEnqueueResult(event=event, created=True)

    if existing.status not in [
        IikoCustomerCategorySyncEvent.Status.ACKED,
        IikoCustomerCategorySyncEvent.Status.SKIPPED,
    ]:
        existing.payload_json = payload
        existing.status = IikoCustomerCategorySyncEvent.Status.PENDING
        existing.last_error = None
        existing.next_retry_at = current_now
        existing.sent_at = None
        existing.ack_at = None
        existing.save(
            update_fields=[
                "payload_json",
                "status",
                "last_error",
                "next_retry_at",
                "sent_at",
                "ack_at",
                "updated_at",
            ]
        )
    return IikoCustomerCategoryEnqueueResult(event=existing, created=False)


class IikoCustomerCategorySyncService:
    """
    Delivery-контур очереди `IikoCustomerCategorySyncEvent` -> iikoCard.
    """

    def __init__(
        self,
        *,
        client: IikoCustomerCategoryClient,
        category_id: str,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
        request_interval_seconds: float = 0.0,
    ) -> None:
        self.client = client
        self.category_id = str(category_id or "").strip()
        if not self.category_id:
            raise ValueError("Не задан IIKO_ACTIVE_COUPON_CATEGORY_ID.")
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))
        self.request_interval_seconds = max(0.0, float(request_interval_seconds or 0.0))

    @classmethod
    def from_settings(cls) -> "IikoCustomerCategorySyncService":
        """
        Собирает service-конфигурацию из Django settings.
        """
        api_key = str(getattr(settings, "IIKO_API_KEY", "") or "").strip()
        base_url = str(getattr(settings, "IIKO_API_BASE_URL", "") or "").strip()
        organization_id = str(getattr(settings, "IIKO_ORGANIZATION_ID", "") or "").strip()
        if not api_key:
            raise ValueError("Не задан IIKO_API_KEY для iikoCard.")
        if not base_url:
            raise ValueError("Не задан IIKO_API_BASE_URL для iikoCard.")
        if not organization_id:
            raise ValueError("Не задан IIKO_ORGANIZATION_ID для iikoCard.")
        timeout_seconds = float(
            getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_HTTP_TIMEOUT_SECONDS", 15.0) or 15.0
        )
        client = IikoCustomerCategoryClient(
            api_key=api_key,
            base_url=base_url,
            organization_id=organization_id,
            timeout_seconds=timeout_seconds,
        )
        return cls(
            client=client,
            category_id=get_iiko_active_coupon_category_id(),
            max_attempts=int(getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_MAX_ATTEMPTS", 8) or 8),
            retry_base_seconds=int(
                getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_RETRY_BASE_SECONDS", 30) or 30
            ),
            retry_max_seconds=int(
                getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_RETRY_MAX_SECONDS", 3600) or 3600
            ),
            request_interval_seconds=float(
                getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_REQUEST_INTERVAL_SECONDS", 0.0)
                or 0.0
            ),
        )

    @staticmethod
    def _queue_events_for_update_queryset():
        """
        Блокирует только строки очереди iikoCard для защиты от параллельных воркеров.
        """
        return IikoCustomerCategorySyncEvent.objects.select_for_update(of=("self",))

    def process_batch(self, *, limit: int, now=None) -> IikoCustomerCategorySyncBatchStats:
        """
        Обрабатывает пачку событий iikoCard с retry и backoff.
        """
        safe_limit = max(1, int(limit))
        now_value = now or timezone.now()
        stats = IikoCustomerCategorySyncBatchStats()

        candidate_qs = IikoCustomerCategorySyncEvent.objects.filter(
            status__in=[
                IikoCustomerCategorySyncEvent.Status.PENDING,
                IikoCustomerCategorySyncEvent.Status.ERROR,
                IikoCustomerCategorySyncEvent.Status.SENT,
            ],
            next_retry_at__lte=now_value,
        )
        stats.skipped_max_attempts = int(
            candidate_qs.filter(attempts__gte=self.max_attempts).count()
        )
        events = list(
            candidate_qs.filter(attempts__lt=self.max_attempts)
            .select_related("guest", "campaign_assignment", "autoscenario_assignment")
            .order_by("next_retry_at", "id")[:safe_limit]
        )
        stats.scanned = len(events) + stats.skipped_max_attempts

        for index, event in enumerate(events):
            result = self._process_single_event(event_id=int(event.id), now=now_value)
            stats.processed += int(result["processed"])
            stats.acked += int(result["acked"])
            stats.failed += int(result["failed"])
            stats.skipped += int(result["skipped"])
            stats.add_acked += int(result["add_acked"])
            stats.remove_acked += int(result["remove_acked"])
            if self.request_interval_seconds > 0 and index < len(events) - 1:
                time.sleep(self.request_interval_seconds)

        return stats

    def _process_single_event(self, *, event_id: int, now) -> dict[str, int]:
        result = {
            "processed": 0,
            "acked": 0,
            "failed": 0,
            "skipped": 0,
            "add_acked": 0,
            "remove_acked": 0,
        }
        attempt_time = now or timezone.now()
        with transaction.atomic():
            event = (
                self._queue_events_for_update_queryset()
                .select_related("guest", "campaign_assignment", "autoscenario_assignment")
                .filter(id=int(event_id))
                .first()
            )
            if event is None or int(event.attempts or 0) >= self.max_attempts:
                return result
            event.attempts = int(event.attempts or 0) + 1
            event.status = IikoCustomerCategorySyncEvent.Status.SENT
            event.sent_at = attempt_time
            event.last_error = None
            event.save(
                update_fields=[
                    "attempts",
                    "status",
                    "sent_at",
                    "last_error",
                    "updated_at",
                ]
            )

        result["processed"] = 1
        try:
            if (
                event.action == IikoCustomerCategorySyncEvent.Action.ADD
                and not _event_assignment_is_live(event=event, now=attempt_time)
            ):
                self._mark_event_skipped(
                    event_id=event_id,
                    reason="Добавление категории пропущено: назначение купона уже не живое.",
                    skipped_at=attempt_time,
                )
                result["skipped"] = 1
                return result

            if (
                event.action == IikoCustomerCategorySyncEvent.Action.REMOVE
                and event.guest_id
                and guest_has_live_sagur_coupon(guest_id=int(event.guest_id), now=attempt_time)
            ):
                self._mark_event_skipped(
                    event_id=event_id,
                    reason="Удаление категории пропущено: у гостя есть другой живой купон SAGUR.",
                    skipped_at=attempt_time,
                )
                result["skipped"] = 1
                return result

            customer_id = self._resolve_iiko_customer_id(event=event)
            if event.action == IikoCustomerCategorySyncEvent.Action.ADD:
                self.client.add_customer_category(customer_id=customer_id, category_id=event.category_id)
            elif event.action == IikoCustomerCategorySyncEvent.Action.REMOVE:
                self.client.remove_customer_category(customer_id=customer_id, category_id=event.category_id)
            else:
                raise IikoCustomerCategorySyncError(f"Неизвестное действие iikoCard: {event.action}")

            self._mark_event_acked(
                event_id=event_id,
                customer_id=customer_id,
                acked_at=attempt_time,
            )
            result["acked"] = 1
            if event.action == IikoCustomerCategorySyncEvent.Action.ADD:
                result["add_acked"] = 1
            else:
                result["remove_acked"] = 1
            return result
        except IikoCustomerCategoryApiError as exc:
            if (
                event.action == IikoCustomerCategorySyncEvent.Action.REMOVE
                and _is_customer_has_no_category_error(exc)
            ):
                self._mark_event_skipped(
                    event_id=event_id,
                    reason=(
                        "Удаление категории пропущено: iikoCard сообщил "
                        f"{IIKO_CUSTOMER_HAS_NO_CATEGORY_ERROR_CODE}, у гостя уже нет категории "
                        f"{event.category_id}."
                    ),
                    skipped_at=attempt_time,
                )
                result["skipped"] = 1
                return result

            error_text = self._truncate_error(str(exc))
            self._mark_event_error(event_id=event_id, error_text=error_text, failed_at=attempt_time)
            result["failed"] = 1
            return result
        except Exception as exc:
            error_text = self._truncate_error(str(exc))
            self._mark_event_error(event_id=event_id, error_text=error_text, failed_at=attempt_time)
            result["failed"] = 1
            return result

    def _resolve_iiko_customer_id(self, *, event: IikoCustomerCategorySyncEvent) -> str:
        customer_id = str(event.iiko_customer_id or "").strip()
        if customer_id:
            return customer_id

        guest = event.guest
        if guest is not None:
            customer_id = str(getattr(guest, "iiko_id", "") or "").strip()
            if customer_id:
                return customer_id

        payload = event.payload_json if isinstance(event.payload_json, dict) else {}
        phone = (
            str(payload.get("phone_e164") or "").strip()
            or str(payload.get("guest_phone") or "").strip()
            or str(getattr(guest, "phone", "") or "").strip()
        )
        if not phone:
            raise IikoCustomerCategorySyncError(
                "Не найден iiko customerId: у события нет iiko_id и телефона гостя."
            )

        try:
            body = self.client.get_customer_by_phone(phone=phone)
        except IikoCustomerCategoryApiError:
            raise
        except Exception as exc:
            raise IikoCustomerCategorySyncError(f"Не удалось получить гостя iikoCard по телефону: {exc}") from exc

        resolved_id = _extract_iiko_customer_id(body)
        if not resolved_id:
            raise IikoCustomerCategorySyncError(
                f"iikoCard не вернул customerId для телефона {phone}."
            )
        return resolved_id

    def _mark_event_acked(self, *, event_id: int, customer_id: str, acked_at) -> None:
        with transaction.atomic():
            event = (
                self._queue_events_for_update_queryset()
                .select_related("campaign_assignment", "autoscenario_assignment")
                .filter(id=int(event_id))
                .first()
            )
            if event is None:
                return
            event.status = IikoCustomerCategorySyncEvent.Status.ACKED
            event.iiko_customer_id = str(customer_id or "").strip() or event.iiko_customer_id
            event.ack_at = acked_at
            event.last_error = None
            event.next_retry_at = acked_at
            event.save(
                update_fields=[
                    "status",
                    "iiko_customer_id",
                    "ack_at",
                    "last_error",
                    "next_retry_at",
                    "updated_at",
                ]
            )
            if event.action == IikoCustomerCategorySyncEvent.Action.ADD:
                assignment = event.campaign_assignment or event.autoscenario_assignment
                if assignment is not None:
                    _mark_assignment_iiko_ok(assignment=assignment, synced_at=acked_at)
                self._apply_post_add_ack_effects(event=event)

    def _mark_event_error(self, *, event_id: int, error_text: str, failed_at) -> None:
        with transaction.atomic():
            event = (
                self._queue_events_for_update_queryset()
                .select_related("campaign_assignment", "autoscenario_assignment")
                .filter(id=int(event_id))
                .first()
            )
            if event is None:
                return
            retry_seconds = self._calculate_retry_seconds(attempt_no=int(event.attempts or 1))
            event.status = IikoCustomerCategorySyncEvent.Status.ERROR
            event.last_error = error_text
            event.next_retry_at = failed_at + timedelta(seconds=retry_seconds)
            event.save(
                update_fields=[
                    "status",
                    "last_error",
                    "next_retry_at",
                    "updated_at",
                ]
            )
            if event.action == IikoCustomerCategorySyncEvent.Action.ADD:
                assignment = event.campaign_assignment or event.autoscenario_assignment
                if assignment is not None:
                    _mark_assignment_iiko_error(assignment=assignment, error_text=error_text)

    def _mark_event_skipped(self, *, event_id: int, reason: str, skipped_at) -> None:
        with transaction.atomic():
            event = self._queue_events_for_update_queryset().filter(id=int(event_id)).first()
            if event is None:
                return
            event.status = IikoCustomerCategorySyncEvent.Status.SKIPPED
            event.last_error = str(reason or "").strip()[:2000]
            event.ack_at = skipped_at
            event.next_retry_at = skipped_at
            event.save(
                update_fields=[
                    "status",
                    "last_error",
                    "ack_at",
                    "next_retry_at",
                    "updated_at",
                ]
            )

    def _apply_post_add_ack_effects(self, *, event: IikoCustomerCategorySyncEvent) -> None:
        if not event.autoscenario_assignment_id:
            return
        assignment = event.autoscenario_assignment
        if assignment is None:
            return
        if assignment.vtelemax_sync_status != CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK:
            return
        try:
            from guests.services.coupon_autoscenarios import create_autoscenario_dispatch_after_vtelemax_ack

            create_autoscenario_dispatch_after_vtelemax_ack(
                assignment_id=int(assignment.id),
                now=event.ack_at,
                days_without_visits=_optional_int((event.payload_json or {}).get("days_without_visits")),
            )
        except Exception:
            logger.exception(
                "Не удалось создать dispatch-задачу автосценария после подтверждения iikoCard: event_id=%s assignment_id=%s",
                event.event_id,
                event.autoscenario_assignment_id,
            )

    def _calculate_retry_seconds(self, *, attempt_no: int) -> int:
        exponent = max(0, int(attempt_no) - 1)
        candidate = int(self.retry_base_seconds * (2 ** exponent))
        return min(self.retry_max_seconds, max(self.retry_base_seconds, candidate))

    @staticmethod
    def _truncate_error(raw_error: str) -> str:
        return str(raw_error or "").strip()[:2000] or "неизвестная ошибка iikoCard"


def _find_existing_event(
    *,
    assignment: CouponAssignment,
    action: str,
    category_id: str,
) -> IikoCustomerCategorySyncEvent | None:
    query = IikoCustomerCategorySyncEvent.objects.filter(
        action=action,
        category_id=category_id,
    )
    if isinstance(assignment, CouponAutoscenarioAssignment):
        query = query.filter(autoscenario_assignment=assignment)
    else:
        query = query.filter(campaign_assignment=assignment)
    return query.order_by("-id").first()


def _is_customer_has_no_category_error(exc: IikoCustomerCategoryApiError) -> bool:
    """
    Проверяет ответ iikoCard: категория уже отсутствует у гостя.

    Для операции `remove` это конечное состояние без повторов, но не полноценный
    подтверждение: событие переводим в `skipped` и сохраняем причину для диагностики.
    """
    error_code = str(getattr(exc, "error_code", "") or "").strip()
    if error_code == IIKO_CUSTOMER_HAS_NO_CATEGORY_ERROR_CODE:
        return True

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        body_code = str(body.get("errorCode") or body.get("code") or "").strip()
        if body_code == IIKO_CUSTOMER_HAS_NO_CATEGORY_ERROR_CODE:
            return True

    return IIKO_CUSTOMER_HAS_NO_CATEGORY_ERROR_CODE in str(exc)


def _assignment_source_type(assignment: CouponAssignment) -> str:
    if isinstance(assignment, CouponAutoscenarioAssignment):
        return IikoCustomerCategorySyncEvent.SourceType.AUTOSCENARIO
    return IikoCustomerCategorySyncEvent.SourceType.CAMPAIGN


def _assignment_event_create_kwargs(assignment: CouponAssignment) -> dict[str, Any]:
    if isinstance(assignment, CouponAutoscenarioAssignment):
        return {"autoscenario_assignment": assignment}
    return {"campaign_assignment": assignment}


def _assignment_iiko_customer_id(assignment: CouponAssignment) -> str | None:
    guest = getattr(assignment, "guest", None)
    value = str(getattr(guest, "iiko_id", "") or "").strip()
    return value or None


def _assignment_guest_phone(assignment: CouponAssignment) -> str:
    guest = getattr(assignment, "guest", None)
    return (
        str(getattr(assignment, "phone_e164", "") or "").strip()
        or str(getattr(guest, "phone", "") or "").strip()
    )


def _build_event_payload(
    *,
    assignment: CouponAssignment,
    action: str,
    category_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": _assignment_source_type(assignment),
        "action": str(action),
        "guest_id": int(assignment.guest_id) if assignment.guest_id else None,
        "guest_phone": str(getattr(getattr(assignment, "guest", None), "phone", "") or "").strip() or None,
        "phone_e164": getattr(assignment, "phone_e164", None),
        "iiko_customer_id": _assignment_iiko_customer_id(assignment),
        "category_id": str(category_id or "").strip(),
        "category_name": get_iiko_active_coupon_category_name(),
        "coupon_series": assignment.coupon_series,
        "coupon_code": assignment.coupon_code,
        "venue_code": assignment.venue_code,
        "venue_name": assignment.venue_name,
        "coupon_title": assignment.coupon_title,
        "promo_text": assignment.promo_text,
        "status": assignment.status,
    }
    if isinstance(assignment, CouponAutoscenarioAssignment):
        payload.update(
            {
                "autoscenario_run_id": int(assignment.run_id),
                "autoscenario_assignment_id": int(assignment.id),
                "assignment_id": int(assignment.id),
                "scenario_id": int(assignment.scenario_id),
                "trigger_key": assignment.trigger_key,
            }
        )
    else:
        payload.update(
            {
                "campaign_id": int(assignment.campaign_id),
                "assignment_id": int(assignment.id),
            }
        )
    return payload


def _event_assignment_key(event: IikoCustomerCategorySyncEvent) -> tuple[str, int] | None:
    if event.campaign_assignment_id:
        return ("campaign", int(event.campaign_assignment_id))
    if event.autoscenario_assignment_id:
        return ("autoscenario", int(event.autoscenario_assignment_id))
    return None


def _event_assignment_is_live(*, event: IikoCustomerCategorySyncEvent, now) -> bool:
    assignment = event.campaign_assignment or event.autoscenario_assignment
    if assignment is None:
        return False
    if assignment.status not in LIVE_COUPON_ASSIGNMENT_STATUSES:
        return False
    if assignment.lifetime_expires_at and assignment.lifetime_expires_at <= now:
        return False
    return True


def _mark_assignment_iiko_pending(*, assignment: CouponAssignment) -> None:
    assignment.iiko_category_add_status = CouponCampaignAssignment.IikoCategorySyncStatus.PENDING
    assignment.iiko_category_add_synced_at = None
    assignment.iiko_category_add_error = None
    assignment.save(
        update_fields=[
            "iiko_category_add_status",
            "iiko_category_add_synced_at",
            "iiko_category_add_error",
            "updated_at",
        ]
    )


def _mark_assignment_iiko_ok(*, assignment: CouponAssignment, synced_at) -> None:
    assignment.iiko_category_add_status = CouponCampaignAssignment.IikoCategorySyncStatus.OK
    assignment.iiko_category_add_synced_at = synced_at
    assignment.iiko_category_add_error = None
    assignment.save(
        update_fields=[
            "iiko_category_add_status",
            "iiko_category_add_synced_at",
            "iiko_category_add_error",
            "updated_at",
        ]
    )


def _mark_assignment_iiko_error(*, assignment: CouponAssignment, error_text: str) -> None:
    assignment.iiko_category_add_status = CouponCampaignAssignment.IikoCategorySyncStatus.ERROR
    assignment.iiko_category_add_synced_at = None
    assignment.iiko_category_add_error = str(error_text or "").strip()[:2000]
    assignment.save(
        update_fields=[
            "iiko_category_add_status",
            "iiko_category_add_synced_at",
            "iiko_category_add_error",
            "updated_at",
        ]
    )


def _extract_iiko_customer_id(body: dict[str, Any] | None) -> str:
    if not isinstance(body, dict):
        return ""

    for key in ("id", "customerId", "customer_id"):
        value = str(body.get(key) or "").strip()
        if value:
            return value

    for container_key in ("customer", "guest", "client"):
        container = body.get(container_key)
        if isinstance(container, dict):
            for key in ("id", "customerId", "customer_id"):
                value = str(container.get(key) or "").strip()
                if value:
                    return value

    for list_key in ("customers", "guests", "items"):
        rows = body.get(list_key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("id", "customerId", "customer_id"):
                value = str(row.get(key) or "").strip()
                if value:
                    return value
    return ""


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
