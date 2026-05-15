from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from guests.models import CouponCampaignAssignment, CouponRegistryEntry, CouponVtelemaxSyncQueue


class Command(BaseCommand):
    help = (
        "Аудит контура освобождения купонов после отмены кампаний: "
        "показывает ожидание ACK, аномалии после ACK и зависшие reserved."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--older-than-minutes",
            type=int,
            default=60,
            help="Порог давности для диагностики зависших reserved назначений.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Сколько строк-примеров выводить по каждому разделу.",
        )
        parser.add_argument(
            "--show-rows",
            action="store_true",
            help="Показать примеры проблемных строк.",
        )
        parser.add_argument(
            "--output-json",
            type=str,
            default="",
            help="Путь сохранения JSON-отчёта.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        older_than_minutes = max(1, int(options["older_than_minutes"]))
        limit = max(1, int(options["limit"]))
        show_rows = bool(options["show_rows"])
        output_json = str(options["output_json"] or "").strip()
        stale_before = now - timedelta(minutes=older_than_minutes)

        canceled_scope = list(
            CouponCampaignAssignment.objects.filter(status=CouponCampaignAssignment.Status.CANCELED)
            .select_related("coupon", "campaign", "guest")
            .order_by("-updated_at", "-id")
        )
        canceled_ids = [int(item.id) for item in canceled_scope]

        latest_events_map = self._build_latest_status_update_map(assignment_ids=canceled_ids)

        waiting_ack_rows: list[dict[str, Any]] = []
        acked_not_released_rows: list[dict[str, Any]] = []
        released_rows: list[dict[str, Any]] = []

        canceled_release_requested_total = 0
        release_waiting_ack = 0
        release_acked_not_released = 0
        release_done = 0

        for assignment in canceled_scope:
            event = latest_events_map.get(int(assignment.id))
            coupon = assignment.coupon

            event_payload = dict(event.payload_json or {}) if event else {}
            event_meta_raw = event_payload.get("meta")
            event_meta = event_meta_raw if isinstance(event_meta_raw, dict) else {}
            event_release_requested = self._bool_from_meta(event_meta.get("release_to_pool"))
            fallback_release_requested = bool(
                coupon
                and (not bool(coupon.is_active))
                and coupon.pool_status == CouponRegistryEntry.PoolStatus.ASSIGNED
            )
            release_requested = bool(event_release_requested or fallback_release_requested)
            if not release_requested:
                continue

            canceled_release_requested_total += 1
            event_status = str(event.status) if event is not None else "missing"
            row = {
                "assignment_id": int(assignment.id),
                "campaign_id": int(assignment.campaign_id),
                "guest_id": int(assignment.guest_id) if assignment.guest_id else None,
                "phone_e164": assignment.phone_e164,
                "coupon_series": assignment.coupon_series,
                "coupon_code": assignment.coupon_code,
                "event_status": event_status,
                "vtelemax_sync_status": assignment.vtelemax_sync_status,
                "coupon_pool_status": coupon.pool_status if coupon else None,
                "coupon_is_active": bool(coupon.is_active) if coupon else None,
                "coupon_assigned_at": coupon.assigned_at.isoformat() if coupon and coupon.assigned_at else None,
            }

            if event is None or event.status != CouponVtelemaxSyncQueue.Status.ACKED:
                release_waiting_ack += 1
                if len(waiting_ack_rows) < limit:
                    waiting_ack_rows.append(row)
                continue

            released = self._is_coupon_released(coupon)
            if released:
                release_done += 1
                if len(released_rows) < limit:
                    released_rows.append(row)
            else:
                release_acked_not_released += 1
                if len(acked_not_released_rows) < limit:
                    acked_not_released_rows.append(row)

        reserved_stale_scope = list(
            CouponCampaignAssignment.objects.filter(
                status=CouponCampaignAssignment.Status.RESERVED,
                assigned_at__lte=stale_before,
            )
            .select_related("coupon", "campaign", "guest")
            .order_by("assigned_at", "id")
        )
        reserved_stale_rows = [
            {
                "assignment_id": int(item.id),
                "campaign_id": int(item.campaign_id),
                "guest_id": int(item.guest_id) if item.guest_id else None,
                "phone_e164": item.phone_e164,
                "coupon_series": item.coupon_series,
                "coupon_code": item.coupon_code,
                "assigned_at": item.assigned_at.isoformat() if item.assigned_at else None,
                "vtelemax_sync_status": item.vtelemax_sync_status,
            }
            for item in reserved_stale_scope[:limit]
        ]

        summary = {
            "canceled_total": int(len(canceled_scope)),
            "canceled_release_requested_total": int(canceled_release_requested_total),
            "release_waiting_ack": int(release_waiting_ack),
            "release_acked_not_released": int(release_acked_not_released),
            "release_done": int(release_done),
            "reserved_stale_total": int(len(reserved_stale_scope)),
            "reserved_stale_threshold_minutes": int(older_than_minutes),
        }

        self.stdout.write("=== Coupon Release Sync Audit ===")
        for key, value in summary.items():
            self.stdout.write(f"{key}: {value}")

        if show_rows:
            self.stdout.write("")
            self.stdout.write(f"--- release_waiting_ack (top {limit}) ---")
            for row in waiting_ack_rows:
                self.stdout.write(str(row))

            self.stdout.write("")
            self.stdout.write(f"--- release_acked_not_released (top {limit}) ---")
            for row in acked_not_released_rows:
                self.stdout.write(str(row))

            self.stdout.write("")
            self.stdout.write(f"--- reserved_stale (top {limit}) ---")
            for row in reserved_stale_rows:
                self.stdout.write(str(row))

        if output_json:
            report = {
                "summary": summary,
                "release_waiting_ack_rows": waiting_ack_rows,
                "release_acked_not_released_rows": acked_not_released_rows,
                "release_done_rows": released_rows,
                "reserved_stale_rows": reserved_stale_rows,
            }
            output_path = Path(output_json)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self.stdout.write("")
            self.stdout.write(f"JSON report saved: {output_path}")

    @staticmethod
    def _build_latest_status_update_map(
        *,
        assignment_ids: list[int],
    ) -> dict[int, CouponVtelemaxSyncQueue]:
        if not assignment_ids:
            return {}
        events = (
            CouponVtelemaxSyncQueue.objects.filter(
                assignment_id__in=assignment_ids,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            )
            .order_by("assignment_id", "-id")
        )
        mapping: dict[int, CouponVtelemaxSyncQueue] = {}
        for event in events:
            if not event.assignment_id:
                continue
            assignment_id = int(event.assignment_id)
            if assignment_id in mapping:
                continue
            mapping[assignment_id] = event
        return mapping

    @staticmethod
    def _is_coupon_released(coupon: CouponRegistryEntry | None) -> bool:
        if coupon is None:
            return False
        return bool(
            coupon.is_active
            and coupon.pool_status == CouponRegistryEntry.PoolStatus.VERIFIED_LOADED
            and coupon.assigned_at is None
        )

    @staticmethod
    def _bool_from_meta(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        token = str(value).strip().lower()
        return token in {"1", "true", "yes", "y", "on"}
