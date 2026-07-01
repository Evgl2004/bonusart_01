from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from guests.models import (
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Mailing,
    MailingGuest,
)
from guests.services.iiko_customer_category_sync import enqueue_iiko_category_remove_if_last_coupon


@dataclass(slots=True)
class CouponCampaignLifecycleStats:
    """
    Сводка по операциям жизненного цикла купонной кампании.

    Используется и для ручной отмены, и для post-campaign закрытия.
    """

    campaigns_scanned: int = 0
    campaigns_processed: int = 0
    campaigns_deactivated: int = 0

    rows_canceled: int = 0
    dispatch_tasks_canceled: int = 0

    assignments_scanned: int = 0
    assignments_canceled: int = 0
    assignments_expired: int = 0
    assignments_release_pending: int = 0
    assignments_released_to_pool: int = 0

    queue_events_created: int = 0
    queue_events_updated: int = 0
    iiko_category_events_created: int = 0
    iiko_category_events_skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "campaigns_scanned": int(self.campaigns_scanned),
            "campaigns_processed": int(self.campaigns_processed),
            "campaigns_deactivated": int(self.campaigns_deactivated),
            "rows_canceled": int(self.rows_canceled),
            "dispatch_tasks_canceled": int(self.dispatch_tasks_canceled),
            "assignments_scanned": int(self.assignments_scanned),
            "assignments_canceled": int(self.assignments_canceled),
            "assignments_expired": int(self.assignments_expired),
            "assignments_release_pending": int(self.assignments_release_pending),
            "assignments_released_to_pool": int(self.assignments_released_to_pool),
            "queue_events_created": int(self.queue_events_created),
            "queue_events_updated": int(self.queue_events_updated),
            "iiko_category_events_created": int(self.iiko_category_events_created),
            "iiko_category_events_skipped": int(self.iiko_category_events_skipped),
        }


class CouponCampaignLifecycleService:
    """
    Операции жизненного цикла купонных кампаний.

    Задачи:
    1. безопасная отмена кампании оператором;
    2. post-campaign закрытие купонов после завершения окна кампании.
    """

    def cancel_campaign(
        self,
        *,
        mailing: Mailing,
        reason: str = "campaign_canceled_by_operator",
        now=None,
        dry_run: bool = False,
    ) -> CouponCampaignLifecycleStats:
        """
        Безопасно отменяет кампанию и освобождает неотправленные купоны.

        Правила:
        1. Строки `planned/in_progress` переводятся в ошибку с признаком отмены;
        2. Dispatch-задачи `pending/queued/in_progress` переводятся в `canceled`;
        3. Только купоны в `reserved` помечаются к освобождению;
        4. Фактическое освобождение в пул выполняется только после подтверждения по `status_update(canceled)`.
        """
        stats = CouponCampaignLifecycleStats(campaigns_scanned=1)
        now_value = now or timezone.now()
        reason_text = str(reason or "campaign_canceled_by_operator").strip()[:200]
        is_coupon_campaign = bool(str(getattr(mailing, "coupon_series", "") or "").strip())

        with transaction.atomic():
            if mailing.is_active:
                stats.campaigns_deactivated = 1
                if not dry_run:
                    mailing.is_active = False
                    if hasattr(mailing, "updated_at"):
                        mailing.updated_at = now_value
                        mailing.save(update_fields=["is_active", "updated_at"])
                    else:
                        mailing.save(update_fields=["is_active"])

            rows_qs = MailingGuest.objects.filter(
                mailing=mailing,
                status__in=[MailingGuest.Status.PLANNED, MailingGuest.Status.IN_PROGRESS],
            )
            stats.rows_canceled = int(rows_qs.count())
            if stats.rows_canceled > 0 and not dry_run:
                rows_qs.update(
                    status=MailingGuest.Status.ERROR,
                    delivery_status="campaign_canceled",
                    error_description="Кампания остановлена оператором.",
                )

            dispatch_qs = DispatchTask.objects.filter(
                mailing_guest__mailing=mailing,
                status__in=[
                    DispatchTask.Status.PENDING,
                    DispatchTask.Status.QUEUED,
                    DispatchTask.Status.IN_PROGRESS,
                ],
            )
            stats.dispatch_tasks_canceled = int(dispatch_qs.count())
            if stats.dispatch_tasks_canceled > 0 and not dry_run:
                dispatch_qs.update(
                    status=DispatchTask.Status.CANCELED,
                    finished_at=now_value,
                    last_error="Кампания отменена оператором.",
                    updated_at=now_value,
                )

            if is_coupon_campaign:
                assignments = list(
                    CouponCampaignAssignment.objects.select_for_update()
                    .select_related("coupon")
                    .filter(
                        campaign=mailing,
                        status=CouponCampaignAssignment.Status.RESERVED,
                    )
                    .order_by("id")
                )
                stats.assignments_scanned = int(len(assignments))

                for assignment in assignments:
                    stats.assignments_canceled += 1

                    if assignment.coupon_id and assignment.coupon is not None:
                        # Освобождение не делаем мгновенно: ждём подтверждение от vtelemax,
                        # чтобы избежать повторной выдачи купона до скрытия у текущего гостя.
                        stats.assignments_release_pending += 1

                    if not dry_run:
                        assignment.status = CouponCampaignAssignment.Status.CANCELED
                        assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.PENDING
                        assignment.vtelemax_synced_at = None
                        assignment.vtelemax_sync_error = None
                        assignment.save(
                            update_fields=[
                                "status",
                                "vtelemax_sync_status",
                                "vtelemax_synced_at",
                                "vtelemax_sync_error",
                                "updated_at",
                            ]
                        )

                    created = self._upsert_status_update_event(
                        assignment=assignment,
                        status=CouponCampaignAssignment.Status.CANCELED,
                        now=now_value,
                        dry_run=dry_run,
                        meta={
                            "cancel_reason": reason_text,
                            "remove_from_guest": True,
                            "release_to_pool": True,
                        },
                    )
                    if created:
                        stats.queue_events_created += 1
                    else:
                        stats.queue_events_updated += 1

                    iiko_result = enqueue_iiko_category_remove_if_last_coupon(
                        assignment=assignment,
                        now=now_value,
                        dry_run=dry_run,
                    )
                    if iiko_result.created:
                        stats.iiko_category_events_created += 1
                    elif iiko_result.skipped and iiko_result.reason == "guest_has_another_live_coupon":
                        stats.iiko_category_events_skipped += 1

        stats.campaigns_processed = 1
        return stats

    def close_finished_campaigns(
        self,
        *,
        close_before: datetime | None = None,
        campaign_ids: list[int] | None = None,
        limit: int = 100,
        dry_run: bool = False,
    ) -> CouponCampaignLifecycleStats:
        """
        Выполняет post-campaign закрытие купонов после завершения окна кампании.

        Правила закрытия:
        1. `sent` -> `expired`;
        2. `reserved` -> `canceled` (без возврата в пул, т.к. кампания уже завершилась);
        3. по каждому изменению ставится `status_update` событие в очередь vtelemax.
        """
        stats = CouponCampaignLifecycleStats()
        cutoff = close_before or timezone.now()
        safe_limit = max(1, int(limit))

        campaigns_qs = Mailing.objects.filter(
            scheduled_time_end__lt=cutoff,
        )
        if campaign_ids:
            normalized_ids = sorted({int(value) for value in campaign_ids if int(value) > 0})
            if normalized_ids:
                campaigns_qs = campaigns_qs.filter(id__in=normalized_ids)

        campaigns = list(
            campaigns_qs
            .exclude(coupon_series__isnull=True)
            .exclude(coupon_series="")
            .order_by("scheduled_time_end", "id")[:safe_limit]
        )
        stats.campaigns_scanned = len(campaigns)
        if not campaigns:
            return stats

        for mailing in campaigns:
            with transaction.atomic():
                assignments = list(
                    CouponCampaignAssignment.objects.select_for_update()
                    .select_related("coupon")
                    .filter(
                        campaign=mailing,
                        status__in=[
                            CouponCampaignAssignment.Status.RESERVED,
                            CouponCampaignAssignment.Status.SENT,
                        ],
                    )
                    .order_by("id")
                )
                if not assignments:
                    continue

                stats.campaigns_processed += 1
                stats.assignments_scanned += len(assignments)

                if mailing.is_active:
                    stats.campaigns_deactivated += 1
                    if not dry_run:
                        mailing.is_active = False
                        if hasattr(mailing, "updated_at"):
                            mailing.updated_at = cutoff
                            mailing.save(update_fields=["is_active", "updated_at"])
                        else:
                            mailing.save(update_fields=["is_active"])

                for assignment in assignments:
                    if assignment.status == CouponCampaignAssignment.Status.SENT:
                        new_status = CouponCampaignAssignment.Status.EXPIRED
                        stats.assignments_expired += 1
                    else:
                        new_status = CouponCampaignAssignment.Status.CANCELED
                        stats.assignments_canceled += 1

                    if assignment.coupon_id and assignment.coupon is not None and not dry_run:
                        assignment.coupon.is_active = False
                        assignment.coupon.pool_status = (
                            CouponRegistryEntry.PoolStatus.EXPIRED
                            if new_status == CouponCampaignAssignment.Status.EXPIRED
                            else CouponRegistryEntry.PoolStatus.CANCELED
                        )
                        assignment.coupon.save(update_fields=["is_active", "pool_status", "updated_at"])

                    if not dry_run:
                        assignment.status = new_status
                        assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.PENDING
                        assignment.vtelemax_synced_at = None
                        assignment.vtelemax_sync_error = None
                        assignment.save(
                            update_fields=[
                                "status",
                                "vtelemax_sync_status",
                                "vtelemax_synced_at",
                                "vtelemax_sync_error",
                                "updated_at",
                            ]
                        )

                    created = self._upsert_status_update_event(
                        assignment=assignment,
                        status=new_status,
                        now=cutoff,
                        dry_run=dry_run,
                        meta={
                            "post_campaign_close": True,
                            "remove_from_guest": True,
                            "release_to_pool": False,
                        },
                    )
                    if created:
                        stats.queue_events_created += 1
                    else:
                        stats.queue_events_updated += 1

                    iiko_result = enqueue_iiko_category_remove_if_last_coupon(
                        assignment=assignment,
                        now=cutoff,
                        dry_run=dry_run,
                    )
                    if iiko_result.created:
                        stats.iiko_category_events_created += 1
                    elif iiko_result.skipped and iiko_result.reason == "guest_has_another_live_coupon":
                        stats.iiko_category_events_skipped += 1

        return stats

    @staticmethod
    def _upsert_status_update_event(
        *,
        assignment: CouponCampaignAssignment,
        status: str,
        now,
        dry_run: bool,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        """
        Обновляет или создаёт `status_update` событие синка в vtelemax.

        Возвращает:
        1. `True` — создано новое событие;
        2. `False` — обновлено существующее событие.
        """
        payload: dict[str, Any] = {
            "campaign_id": int(assignment.campaign_id),
            "assignment_id": int(assignment.id),
            "guest_id": int(assignment.guest_id) if assignment.guest_id else None,
            "person_id": str(assignment.person_id) if assignment.person_id else None,
            "phone_e164": assignment.phone_e164,
            "coupon_series": assignment.coupon_series,
            "coupon_code": assignment.coupon_code,
            "venue_code": assignment.venue_code,
            "venue_name": assignment.venue_name,
            "coupon_title": assignment.coupon_title,
            "promo_text": assignment.promo_text,
            "status": status,
            "status_at": now.isoformat(),
        }
        if meta:
            payload["meta"] = dict(meta)

        existing = (
            CouponVtelemaxSyncQueue.objects.filter(
                assignment=assignment,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            )
            .order_by("-id")
            .first()
        )
        if existing is None:
            if not dry_run:
                CouponVtelemaxSyncQueue.objects.create(
                    direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
                    assignment=assignment,
                    payload_json=payload,
                    status=CouponVtelemaxSyncQueue.Status.PENDING,
                    attempts=0,
                    next_retry_at=now,
                    last_error=None,
                    sent_at=None,
                    ack_at=None,
                )
            return True

        if not dry_run:
            existing.payload_json = payload
            existing.status = CouponVtelemaxSyncQueue.Status.PENDING
            existing.last_error = None
            existing.next_retry_at = now
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
        return False
