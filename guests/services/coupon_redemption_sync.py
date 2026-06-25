from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from django.db import transaction
from django.utils import timezone

from guests.models import (
    CouponAutoscenarioAssignment,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    OrderFact,
)

RedemptionAssignmentKind = Literal["campaign", "autoscenario"]
RedemptionAssignment = CouponCampaignAssignment | CouponAutoscenarioAssignment


_REDEEMABLE_ASSIGNMENT_STATUSES = [
    CouponCampaignAssignment.Status.RESERVED,
    CouponCampaignAssignment.Status.SENT,
    CouponCampaignAssignment.Status.EXPIRED,
    CouponCampaignAssignment.Status.USED,
    CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN,
]

_ASSIGNMENT_STATUS_PRIORITY = {
    CouponCampaignAssignment.Status.SENT: 0,
    CouponCampaignAssignment.Status.RESERVED: 1,
    CouponCampaignAssignment.Status.EXPIRED: 2,
    CouponCampaignAssignment.Status.USED: 3,
    CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN: 3,
}


@dataclass(slots=True)
class CouponRedemptionSyncStats:
    """
    Сводная статистика синхронизации факта применения купонов из OLAP.
    """

    order_facts_total: int = 0
    order_facts_with_coupon: int = 0
    assignments_matched: int = 0
    campaign_assignments_matched: int = 0
    autoscenario_assignments_matched: int = 0
    assignments_marked_used: int = 0
    assignments_marked_used_after_campaign: int = 0
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
            "campaign_assignments_matched": int(self.campaign_assignments_matched),
            "autoscenario_assignments_matched": int(self.autoscenario_assignments_matched),
            "assignments_marked_used": int(self.assignments_marked_used),
            "assignments_marked_used_after_campaign": int(self.assignments_marked_used_after_campaign),
            "assignments_already_used": int(self.assignments_already_used),
            "assignments_guest_mismatch": int(self.assignments_guest_mismatch),
            "assignments_missing": int(self.assignments_missing),
            "queue_events_created": int(self.queue_events_created),
            "queue_events_updated": int(self.queue_events_updated),
            "registry_marked_used": int(self.registry_marked_used),
        }


@dataclass(slots=True)
class _RedemptionAssignmentCandidate:
    kind: RedemptionAssignmentKind
    assignment: RedemptionAssignment


class CouponRedemptionSyncService:
    """
    Сервис фиксации применения купонов по данным `order_fact`.

    Назначение:
    1. найти в `order_fact` чеки с применённым купоном (`coupon_series` + `coupon_number`);
    2. сопоставить купон с назначением кампании (`CouponCampaignAssignment`);
    3. перевести назначение и запись реестра в состояние `used` или `used_after_campaign`;
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

        assignment_by_key = self._load_assignment_candidates(
            series_values=series_values,
            code_values=code_values,
        )

        now = timezone.now()

        with transaction.atomic():
            for fact in coupon_facts:
                key = (
                    str(fact.get("coupon_series") or "").strip(),
                    str(fact.get("coupon_number") or "").strip(),
                )
                if not key[0] or not key[1]:
                    continue

                candidate = assignment_by_key.get(key)
                if candidate is None:
                    stats.assignments_missing += 1
                    continue

                assignment = candidate.assignment
                stats.assignments_matched += 1
                if candidate.kind == "autoscenario":
                    stats.autoscenario_assignments_matched += 1
                else:
                    stats.campaign_assignments_matched += 1

                fact_guest_id = fact.get("guest_id")
                if assignment.guest_id and fact_guest_id and int(assignment.guest_id) != int(fact_guest_id):
                    stats.assignments_guest_mismatch += 1

                fact_order_number = fact.get("order_number")
                used_at = fact.get("first_seen_at") or now
                used_business_date = fact.get("business_date")
                target_status = self._resolve_used_status(
                    assignment=assignment,
                    used_at=used_at,
                    used_business_date=used_business_date,
                )
                assignment_changed = False

                usage_is_complete = (
                    assignment.status == target_status
                    and assignment.used_order_id
                    and assignment.used_at
                    and (
                        not isinstance(assignment, CouponAutoscenarioAssignment)
                        or assignment.used_business_date is not None
                    )
                )
                if usage_is_complete:
                    stats.assignments_already_used += 1
                else:
                    assignment.status = target_status
                    assignment.used_at = used_at
                    assignment.used_order_id = int(fact_order_number) if fact_order_number is not None else None
                    if isinstance(assignment, CouponAutoscenarioAssignment):
                        assignment.used_business_date = used_business_date
                    assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.PENDING
                    assignment.vtelemax_sync_error = None
                    assignment.vtelemax_synced_at = None
                    assignment_changed = True
                    stats.assignments_marked_used += 1
                    if target_status == CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN:
                        stats.assignments_marked_used_after_campaign += 1

                registry_entry = assignment.coupon
                target_pool_status = self._resolve_registry_used_status(status=target_status)
                if registry_entry and (
                    registry_entry.pool_status != target_pool_status or registry_entry.is_active
                ):
                    registry_entry.pool_status = target_pool_status
                    registry_entry.is_active = False
                    if not dry_run:
                        registry_entry.save(update_fields=["pool_status", "is_active", "updated_at"])
                    stats.registry_marked_used += 1

                if assignment_changed and not dry_run:
                    update_fields = [
                        "status",
                        "used_at",
                        "used_order_id",
                        "vtelemax_sync_status",
                        "vtelemax_sync_error",
                        "vtelemax_synced_at",
                        "updated_at",
                    ]
                    if isinstance(assignment, CouponAutoscenarioAssignment):
                        update_fields.insert(3, "used_business_date")
                    assignment.save(
                        update_fields=update_fields,
                    )

                queue_result = self._upsert_status_update_event(
                    assignment=assignment,
                    status=target_status,
                    used_order_id=int(fact_order_number) if fact_order_number is not None else None,
                    used_business_date=used_business_date,
                    now=now,
                    dry_run=dry_run,
                )
                if queue_result is True:
                    stats.queue_events_created += 1
                elif queue_result is False:
                    stats.queue_events_updated += 1

        return stats

    @staticmethod
    def _load_assignment_candidates(
        *,
        series_values: list[str],
        code_values: list[str],
    ) -> dict[tuple[str, str], _RedemptionAssignmentCandidate]:
        candidates: list[_RedemptionAssignmentCandidate] = []

        campaign_assignments = (
            CouponCampaignAssignment.objects.filter(
                coupon_series__in=series_values,
                coupon_code__in=code_values,
                status__in=_REDEEMABLE_ASSIGNMENT_STATUSES,
            )
            .select_related("coupon", "campaign")
            .order_by("id")
        )
        candidates.extend(
            _RedemptionAssignmentCandidate(kind="campaign", assignment=assignment)
            for assignment in campaign_assignments
        )

        autoscenario_assignments = (
            CouponAutoscenarioAssignment.objects.filter(
                coupon_series__in=series_values,
                coupon_code__in=code_values,
                status__in=_REDEEMABLE_ASSIGNMENT_STATUSES,
            )
            .select_related("coupon", "run", "scenario", "config")
            .order_by("id")
        )
        candidates.extend(
            _RedemptionAssignmentCandidate(kind="autoscenario", assignment=assignment)
            for assignment in autoscenario_assignments
        )

        assignment_by_key: dict[tuple[str, str], _RedemptionAssignmentCandidate] = {}
        for candidate in candidates:
            assignment = candidate.assignment
            key = (
                str(assignment.coupon_series or "").strip(),
                str(assignment.coupon_code or "").strip(),
            )
            if not key[0] or not key[1]:
                continue
            current = assignment_by_key.get(key)
            if current is None or CouponRedemptionSyncService._candidate_rank(
                candidate
            ) < CouponRedemptionSyncService._candidate_rank(current):
                assignment_by_key[key] = candidate
        return assignment_by_key

    @staticmethod
    def _candidate_rank(candidate: _RedemptionAssignmentCandidate) -> tuple[int, float, int]:
        assignment = candidate.assignment
        priority = _ASSIGNMENT_STATUS_PRIORITY.get(assignment.status, 99)
        assigned_at = assignment.assigned_at
        timestamp = assigned_at.timestamp() if assigned_at else 0.0
        return (priority, -timestamp, -int(assignment.id or 0))

    @staticmethod
    def _resolve_used_status(
        *,
        assignment: RedemptionAssignment,
        used_at,
        used_business_date: date | None,
    ) -> str:
        """
        Возвращает статус применения купона с учётом окна кампании.

        Если купон уже был закрыт как истёкший/поздний или бизнес-дата заказа позже
        окончания кампании, фиксируем отдельный статус позднего использования.
        """
        if assignment.status in {
            CouponCampaignAssignment.Status.EXPIRED,
            CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN,
        }:
            return CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN

        if isinstance(assignment, CouponAutoscenarioAssignment):
            expires_at = assignment.lifetime_expires_at
            if expires_at is None:
                return CouponAutoscenarioAssignment.Status.USED

            if used_business_date is not None:
                if used_business_date > expires_at.date():
                    return CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN
                if used_business_date < expires_at.date():
                    return CouponAutoscenarioAssignment.Status.USED

            if used_at is not None and used_at > expires_at:
                return CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN
            return CouponAutoscenarioAssignment.Status.USED

        campaign_end = getattr(getattr(assignment, "campaign", None), "scheduled_time_end", None)
        if campaign_end is None:
            return CouponCampaignAssignment.Status.USED

        if used_business_date is not None:
            if used_business_date > campaign_end.date():
                return CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN
            if used_business_date < campaign_end.date():
                return CouponCampaignAssignment.Status.USED

        if used_at is not None and used_at > campaign_end:
            return CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN
        return CouponCampaignAssignment.Status.USED

    @staticmethod
    def _resolve_registry_used_status(*, status: str) -> str:
        if status == CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN:
            return CouponRegistryEntry.PoolStatus.USED_AFTER_CAMPAIGN
        return CouponRegistryEntry.PoolStatus.USED

    @staticmethod
    def _upsert_status_update_event(
        *,
        assignment: RedemptionAssignment,
        status: str,
        used_order_id: int | None,
        used_business_date: date | None,
        now,
        dry_run: bool,
    ) -> bool | None:
        """
        Обновляет или создаёт событие `status_update` для передачи статуса купона в vtelemax.

        Возвращает:
        1. `True` — если создана новая запись очереди;
        2. `False` — если существующая запись обновлена;
        3. `None` — если уже подтверждённое событие полностью покрывает текущее состояние.
        """
        payload = CouponRedemptionSyncService._build_status_update_payload(
            assignment=assignment,
            status=status,
            used_order_id=used_order_id,
            used_business_date=used_business_date,
        )

        existing = (
            CouponRedemptionSyncService._status_update_event_query(assignment=assignment)
            .order_by("-id")
            .first()
        )
        if existing is None:
            if not dry_run:
                create_kwargs = CouponRedemptionSyncService._status_update_event_create_kwargs(
                    assignment=assignment
                )
                CouponVtelemaxSyncQueue.objects.create(
                    direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
                    payload_json=payload,
                    status=CouponVtelemaxSyncQueue.Status.PENDING,
                    attempts=0,
                    next_retry_at=now,
                    last_error=None,
                    sent_at=None,
                    ack_at=None,
                    **create_kwargs,
                )
            return True

        if existing.status == CouponVtelemaxSyncQueue.Status.ACKED:
            existing_identity = CouponRedemptionSyncService._status_update_payload_identity(existing.payload_json)
            payload_identity = CouponRedemptionSyncService._status_update_payload_identity(payload)
            if existing_identity == payload_identity:
                return None

            if not dry_run:
                create_kwargs = CouponRedemptionSyncService._status_update_event_create_kwargs(
                    assignment=assignment
                )
                CouponVtelemaxSyncQueue.objects.create(
                    direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
                    payload_json=payload,
                    status=CouponVtelemaxSyncQueue.Status.PENDING,
                    attempts=0,
                    next_retry_at=now,
                    last_error=None,
                    sent_at=None,
                    ack_at=None,
                    **create_kwargs,
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

    @staticmethod
    def _build_status_update_payload(
        *,
        assignment: RedemptionAssignment,
        status: str,
        used_order_id: int | None,
        used_business_date: date | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
            "used_order_id": used_order_id,
            "used_business_date": used_business_date.isoformat() if used_business_date else None,
            "meta": {
                "remove_from_guest": True,
                "release_to_pool": False,
                "used_after_campaign": status == CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN,
            },
        }
        if isinstance(assignment, CouponAutoscenarioAssignment):
            payload.update(
                {
                    "source": "autoscenario",
                    "autoscenario_run_id": int(assignment.run_id),
                    "autoscenario_assignment_id": int(assignment.id),
                    "scenario_id": int(assignment.scenario_id),
                    "scenario_code": assignment.scenario.code,
                }
            )
        else:
            payload["campaign_id"] = int(assignment.campaign_id)
        return payload

    @staticmethod
    def _status_update_event_query(*, assignment: RedemptionAssignment):
        if isinstance(assignment, CouponAutoscenarioAssignment):
            return CouponVtelemaxSyncQueue.objects.filter(
                autoscenario_assignment=assignment,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            )
        return CouponVtelemaxSyncQueue.objects.filter(
            assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
        )

    @staticmethod
    def _status_update_event_create_kwargs(*, assignment: RedemptionAssignment) -> dict[str, Any]:
        if isinstance(assignment, CouponAutoscenarioAssignment):
            return {"autoscenario_assignment": assignment}
        return {"assignment": assignment}

    @staticmethod
    def _status_update_payload_identity(payload: dict[str, Any]) -> tuple[str, ...]:
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}

        def normalized(value: Any) -> str:
            if value is None:
                return ""
            return str(value)

        return (
            normalized(payload.get("source")),
            normalized(payload.get("campaign_id")),
            normalized(payload.get("autoscenario_run_id")),
            normalized(payload.get("autoscenario_assignment_id")),
            normalized(payload.get("scenario_id")),
            normalized(payload.get("scenario_code")),
            normalized(payload.get("assignment_id")),
            normalized(payload.get("coupon_series")),
            normalized(payload.get("coupon_code")),
            normalized(payload.get("status")),
            normalized(payload.get("used_order_id")),
            normalized(payload.get("used_business_date")),
            normalized(meta.get("used_after_campaign")),
        )
