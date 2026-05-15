from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from guests.models import CouponCampaignAssignment, CouponRegistryEntry, CouponVtelemaxSyncQueue, OrderFact


@dataclass(slots=True)
class CouponRedemptionSyncStats:
    """
    Сводная статистика синхронизации факта применения купонов из OLAP.
    """

    order_facts_total: int = 0
    order_facts_with_coupon: int = 0
    assignments_matched: int = 0
    assignments_marked_used: int = 0
    assignments_already_used: int = 0
    assignments_guest_mismatch: int = 0
    assignments_missing: int = 0
    queue_events_created: int = 0
    queue_events_updated: int = 0
    registry_marked_used: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "order_facts_total": int(self.order_facts_total),
            "order_facts_with_coupon": int(self.order_facts_with_coupon),
            "assignments_matched": int(self.assignments_matched),
            "assignments_marked_used": int(self.assignments_marked_used),
            "assignments_already_used": int(self.assignments_already_used),
            "assignments_guest_mismatch": int(self.assignments_guest_mismatch),
            "assignments_missing": int(self.assignments_missing),
            "queue_events_created": int(self.queue_events_created),
            "queue_events_updated": int(self.queue_events_updated),
            "registry_marked_used": int(self.registry_marked_used),
        }


class CouponRedemptionSyncService:
    """
    Сервис фиксации применения купонов по данным `order_fact`.

    Назначение:
    1. найти в `order_fact` чеки с применённым купоном (`coupon_series` + `coupon_number`);
    2. сопоставить купон с назначением кампании (`CouponCampaignAssignment`);
    3. перевести назначение и запись реестра в состояние `used`;
    4. поставить тех-событие в очередь `CouponVtelemaxSyncQueue` для последующего скрытия купона в vtelemax.
    """

    def sync_from_order_facts(
        self,
        *,
        business_date_from: date | None = None,
        business_date_to: date | None = None,
        order_fact_id_from: int | None = None,
        order_fact_id_to: int | None = None,
        limit: int = 0,
        dry_run: bool = False,
    ) -> CouponRedemptionSyncStats:
        stats = CouponRedemptionSyncStats()

        facts_query = OrderFact.objects.all()
        if order_fact_id_from is not None:
            facts_query = facts_query.filter(id__gte=int(order_fact_id_from))
        if order_fact_id_to is not None:
            facts_query = facts_query.filter(id__lte=int(order_fact_id_to))
        if business_date_from is not None:
            facts_query = facts_query.filter(business_date__gte=business_date_from)
        if business_date_to is not None:
            facts_query = facts_query.filter(business_date__lte=business_date_to)

        stats.order_facts_total = int(facts_query.count())

        coupon_facts_query = (
            facts_query.filter(coupon_used=True)
            .exclude(coupon_series__isnull=True)
            .exclude(coupon_series="")
            .exclude(coupon_number__isnull=True)
            .exclude(coupon_number="")
            .order_by("business_date", "id")
            .values(
                "id",
                "guest_id",
                "business_date",
                "order_number",
                "coupon_series",
                "coupon_number",
                "first_seen_at",
            )
        )
        if limit > 0:
            coupon_facts_query = coupon_facts_query[: int(limit)]

        coupon_facts = list(coupon_facts_query)
        stats.order_facts_with_coupon = int(len(coupon_facts))
        if not coupon_facts:
            return stats

        keys: set[tuple[str, str]] = set()
        for fact in coupon_facts:
            series = str(fact.get("coupon_series") or "").strip()
            code = str(fact.get("coupon_number") or "").strip()
            if series and code:
                keys.add((series, code))

        if not keys:
            return stats

        series_values = sorted({item[0] for item in keys})
        code_values = sorted({item[1] for item in keys})

        assignments = list(
            CouponCampaignAssignment.objects.filter(
                coupon_series__in=series_values,
                coupon_code__in=code_values,
            )
            .select_related("coupon")
            .order_by("id")
        )

        assignment_by_key: dict[tuple[str, str], CouponCampaignAssignment] = {}
        for assignment in assignments:
            key = (
                str(assignment.coupon_series or "").strip(),
                str(assignment.coupon_code or "").strip(),
            )
            if not key[0] or not key[1]:
                continue
            assignment_by_key.setdefault(key, assignment)

        now = timezone.now()

        with transaction.atomic():
            for fact in coupon_facts:
                key = (
                    str(fact.get("coupon_series") or "").strip(),
                    str(fact.get("coupon_number") or "").strip(),
                )
                if not key[0] or not key[1]:
                    continue

                assignment = assignment_by_key.get(key)
                if assignment is None:
                    stats.assignments_missing += 1
                    continue

                stats.assignments_matched += 1

                fact_guest_id = fact.get("guest_id")
                if assignment.guest_id and fact_guest_id and int(assignment.guest_id) != int(fact_guest_id):
                    stats.assignments_guest_mismatch += 1

                fact_order_number = fact.get("order_number")
                used_at = fact.get("first_seen_at") or now
                assignment_changed = False

                if assignment.status == CouponCampaignAssignment.Status.USED and assignment.used_order_id:
                    stats.assignments_already_used += 1
                else:
                    assignment.status = CouponCampaignAssignment.Status.USED
                    assignment.used_at = used_at
                    assignment.used_order_id = int(fact_order_number) if fact_order_number is not None else None
                    assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.PENDING
                    assignment.vtelemax_sync_error = None
                    assignment.vtelemax_synced_at = None
                    assignment_changed = True
                    stats.assignments_marked_used += 1

                registry_entry = assignment.coupon
                if registry_entry and (
                    registry_entry.pool_status != CouponRegistryEntry.PoolStatus.USED or registry_entry.is_active
                ):
                    registry_entry.pool_status = CouponRegistryEntry.PoolStatus.USED
                    registry_entry.is_active = False
                    if not dry_run:
                        registry_entry.save(update_fields=["pool_status", "is_active", "updated_at"])
                    stats.registry_marked_used += 1

                if assignment_changed and not dry_run:
                    assignment.save(
                        update_fields=[
                            "status",
                            "used_at",
                            "used_order_id",
                            "vtelemax_sync_status",
                            "vtelemax_sync_error",
                            "vtelemax_synced_at",
                            "updated_at",
                        ]
                    )

                queue_created = self._upsert_status_update_event(
                    assignment=assignment,
                    used_order_id=int(fact_order_number) if fact_order_number is not None else None,
                    used_business_date=fact.get("business_date"),
                    now=now,
                    dry_run=dry_run,
                )
                if queue_created:
                    stats.queue_events_created += 1
                else:
                    stats.queue_events_updated += 1

        return stats

    @staticmethod
    def _upsert_status_update_event(
        *,
        assignment: CouponCampaignAssignment,
        used_order_id: int | None,
        used_business_date: date | None,
        now,
        dry_run: bool,
    ) -> bool:
        """
        Обновляет или создаёт событие `status_update` для передачи статуса купона в vtelemax.

        Возвращает:
        1. `True` — если создана новая запись очереди;
        2. `False` — если существующая запись обновлена.
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
            "promo_text": assignment.promo_text,
            "status": CouponCampaignAssignment.Status.USED,
            "used_order_id": used_order_id,
            "used_business_date": used_business_date.isoformat() if used_business_date else None,
            "meta": {
                "remove_from_guest": True,
                "release_to_pool": False,
            },
        }

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
