from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone as django_timezone

from guests.models import CouponCampaignAssignment, CouponRegistryEntry, CouponVtelemaxSyncQueue

logger = logging.getLogger(__name__)


class VtelemaxCouponSyncError(Exception):
    """Ошибка доставки события купона в API vtelemax."""


@dataclass(slots=True)
class CouponVtelemaxSyncBatchStats:
    """
    Результат одного прохода синхронизации событий купонов в vtelemax.
    """

    scanned: int = 0
    processed: int = 0
    acked: int = 0
    failed: int = 0
    skipped_max_attempts: int = 0
    assignments_acked: int = 0
    status_updates_acked: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "scanned": int(self.scanned),
            "processed": int(self.processed),
            "acked": int(self.acked),
            "failed": int(self.failed),
            "skipped_max_attempts": int(self.skipped_max_attempts),
            "assignments_acked": int(self.assignments_acked),
            "status_updates_acked": int(self.status_updates_acked),
        }


class VtelemaxCouponSyncService:
    """
    Delivery-контур очереди `CouponVtelemaxSyncQueue` -> vtelemax.

    Поддерживает:
    1. batched-обработку pending/error/sent событий;
    2. HMAC-подпись запроса;
    3. retry с exponential backoff;
    4. обновление статусов `CouponCampaignAssignment.vtelemax_sync_status`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        endpoint_path: str,
        hmac_secret: str,
        timeout_seconds: float,
        require_https: bool,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ):
        normalized_base_url = str(base_url or "").strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError("VTELEMAX_COUPON_SYNC_BASE_URL is empty.")
        if require_https and not normalized_base_url.lower().startswith("https://"):
            raise ValueError("VTELEMAX_COUPON_SYNC_BASE_URL must use HTTPS.")

        normalized_endpoint = str(endpoint_path or "").strip()
        if not normalized_endpoint:
            raise ValueError("VTELEMAX_COUPON_SYNC_ENDPOINT is empty.")
        if not normalized_endpoint.startswith("/"):
            normalized_endpoint = f"/{normalized_endpoint}"

        normalized_secret = str(hmac_secret or "").strip()
        if not normalized_secret:
            raise ValueError("VTELEMAX_COUPON_SYNC_HMAC_SECRET is empty.")

        self.base_url = normalized_base_url
        self.endpoint_path = normalized_endpoint
        self.hmac_secret = normalized_secret
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))

    @classmethod
    def from_settings(cls) -> "VtelemaxCouponSyncService":
        """
        Собирает service-конфигурацию из Django settings.
        """

        base_url = str(
            getattr(settings, "VTELEMAX_COUPON_SYNC_BASE_URL", "")
            or getattr(settings, "VTELEMAX_SYNC_BASE_URL", "")
            or ""
        ).strip()
        hmac_secret = str(
            getattr(settings, "VTELEMAX_COUPON_SYNC_HMAC_SECRET", "")
            or getattr(settings, "VTELEMAX_SYNC_HMAC_SECRET", "")
            or ""
        ).strip()
        endpoint_path = str(
            getattr(settings, "VTELEMAX_COUPON_SYNC_ENDPOINT", "/internal/integration/v1/sagur/coupons/events")
            or "/internal/integration/v1/sagur/coupons/events"
        ).strip()
        timeout_seconds = float(
            getattr(settings, "VTELEMAX_COUPON_SYNC_HTTP_TIMEOUT_SECONDS", 20.0) or 20.0
        )
        require_https = bool(getattr(settings, "VTELEMAX_COUPON_SYNC_REQUIRE_HTTPS", True))
        max_attempts = int(getattr(settings, "VTELEMAX_COUPON_SYNC_MAX_ATTEMPTS", 8) or 8)
        retry_base_seconds = int(
            getattr(settings, "VTELEMAX_COUPON_SYNC_RETRY_BASE_SECONDS", 30) or 30
        )
        retry_max_seconds = int(
            getattr(settings, "VTELEMAX_COUPON_SYNC_RETRY_MAX_SECONDS", 3600) or 3600
        )
        return cls(
            base_url=base_url,
            endpoint_path=endpoint_path,
            hmac_secret=hmac_secret,
            timeout_seconds=timeout_seconds,
            require_https=require_https,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
        )

    def process_batch(self, *, limit: int, now=None) -> CouponVtelemaxSyncBatchStats:
        """
        Обрабатывает пачку событий очереди.

        Повторно берет в работу также `sent` события (если не были подтверждены), чтобы
        не терять события после аварийного падения процесса между отправкой и ack.
        """

        safe_limit = max(1, int(limit))
        now_value = now or django_timezone.now()
        stats = CouponVtelemaxSyncBatchStats()

        candidate_qs = CouponVtelemaxSyncQueue.objects.filter(
            status__in=[
                CouponVtelemaxSyncQueue.Status.PENDING,
                CouponVtelemaxSyncQueue.Status.ERROR,
                CouponVtelemaxSyncQueue.Status.SENT,
            ],
            next_retry_at__lte=now_value,
        )
        stats.skipped_max_attempts = int(
            candidate_qs.filter(attempts__gte=self.max_attempts).count()
        )

        events = list(
            candidate_qs.filter(attempts__lt=self.max_attempts)
            .select_related("assignment")
            .order_by("next_retry_at", "id")[:safe_limit]
        )
        stats.scanned = len(events) + stats.skipped_max_attempts

        for event in events:
            stats.processed += 1
            if self._process_event(event=event):
                stats.acked += 1
                if event.direction == CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS:
                    stats.assignments_acked += 1
                elif event.direction == CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE:
                    stats.status_updates_acked += 1
            else:
                stats.failed += 1

        return stats

    def _process_event(self, *, event: CouponVtelemaxSyncQueue) -> bool:
        """
        Отправляет единичное событие и фиксирует итог в очереди/назначении.
        """

        attempt_time = django_timezone.now()
        attempt_no = int(event.attempts or 0) + 1
        with transaction.atomic():
            # Берем строку под lock, чтобы конкурирующие воркеры не отправляли один event дважды.
            locked = (
                CouponVtelemaxSyncQueue.objects.select_for_update()
                .select_related("assignment")
                .filter(id=event.id)
                .first()
            )
            if locked is None:
                return False
            if int(locked.attempts or 0) >= self.max_attempts:
                return False
            locked.attempts = attempt_no
            locked.status = CouponVtelemaxSyncQueue.Status.SENT
            locked.sent_at = attempt_time
            locked.last_error = None
            locked.save(
                update_fields=[
                    "attempts",
                    "status",
                    "sent_at",
                    "last_error",
                    "updated_at",
                ]
            )
            event = locked

        try:
            self._send_event(event=event, sent_at=attempt_time)
        except Exception as exc:
            error_text = self._truncate_error(str(exc))
            retry_seconds = self._calculate_retry_seconds(attempt_no=attempt_no)
            next_retry_at = attempt_time + timedelta(seconds=retry_seconds)

            with transaction.atomic():
                failed = (
                    CouponVtelemaxSyncQueue.objects.select_for_update()
                    .select_related("assignment")
                    .filter(id=event.id)
                    .first()
                )
                if failed is None:
                    return False
                failed.status = CouponVtelemaxSyncQueue.Status.ERROR
                failed.last_error = error_text
                failed.next_retry_at = next_retry_at
                failed.save(
                    update_fields=[
                        "status",
                        "last_error",
                        "next_retry_at",
                        "updated_at",
                    ]
                )
                self._mark_assignment_error(
                    assignment=failed.assignment,
                    error_text=error_text,
                    failed_at=attempt_time,
                )
            return False

        with transaction.atomic():
            succeeded = (
                CouponVtelemaxSyncQueue.objects.select_for_update()
                .select_related("assignment")
                .filter(id=event.id)
                .first()
            )
            if succeeded is None:
                return False
            succeeded.status = CouponVtelemaxSyncQueue.Status.ACKED
            succeeded.ack_at = attempt_time
            succeeded.last_error = None
            succeeded.next_retry_at = attempt_time
            succeeded.save(
                update_fields=[
                    "status",
                    "ack_at",
                    "last_error",
                    "next_retry_at",
                    "updated_at",
                ]
            )
            self._mark_assignment_ok(assignment=succeeded.assignment, synced_at=attempt_time)
            self._apply_post_ack_effects(event=succeeded)
        return True

    def _send_event(self, *, event: CouponVtelemaxSyncQueue, sent_at) -> None:
        """
        Выполняет HTTP POST события в vtelemax endpoint.
        """

        payload = {
            "event_id": str(event.event_id),
            "direction": str(event.direction),
            "sent_at": sent_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "payload": dict(event.payload_json or {}),
        }
        body_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(datetime.now(tz=timezone.utc).timestamp()))
        signature = self._build_signature(
            method="POST",
            path=self.endpoint_path,
            timestamp=timestamp,
            body_text=body_text,
        )
        headers = {
            "Content-Type": "application/json",
            "X-Sagur-Timestamp": timestamp,
            "X-Sagur-Signature": signature,
            "X-Sagur-Event-Id": str(event.event_id),
        }
        url = f"{self.base_url}{self.endpoint_path}"

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(url, headers=headers, content=body_text.encode("utf-8"))
        except httpx.HTTPError as exc:
            raise VtelemaxCouponSyncError(f"HTTP request failed: {exc}") from exc

        response_payload = self._safe_json_dict(response)
        if response.status_code >= 400:
            error_message = str(response_payload.get("message") or response.text or "HTTP error").strip()
            raise VtelemaxCouponSyncError(
                f"vtelemax API error status={response.status_code}: {error_message[:500]}"
            )
        if response_payload.get("ok") is False:
            error_message = str(response_payload.get("message") or "ok=false").strip()
            raise VtelemaxCouponSyncError(f"vtelemax API negative ack: {error_message[:500]}")

    def _build_signature(self, *, method: str, path: str, timestamp: str, body_text: str) -> str:
        """
        Формирует HMAC подпись запроса для vtelemax.
        """

        body_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        canonical_payload = "\n".join([method.upper(), path, timestamp, body_hash])
        return hmac.new(
            self.hmac_secret.encode("utf-8"),
            canonical_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _safe_json_dict(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except Exception:
            return {"message": response.text[:1000]}
        if isinstance(parsed, dict):
            return parsed
        return {"message": str(parsed)}

    @staticmethod
    def _truncate_error(raw_error: str) -> str:
        return str(raw_error or "").strip()[:2000] or "unknown sync error"

    def _calculate_retry_seconds(self, *, attempt_no: int) -> int:
        exponent = max(0, int(attempt_no) - 1)
        candidate = int(self.retry_base_seconds * (2 ** exponent))
        return min(self.retry_max_seconds, max(self.retry_base_seconds, candidate))

    @staticmethod
    def _bool_from_meta(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        token = str(value).strip().lower()
        return token in {"1", "true", "yes", "y", "on"}

    def _apply_post_ack_effects(self, *, event: CouponVtelemaxSyncQueue) -> None:
        """
        Применяет побочные эффекты после подтверждённой отправки события в vtelemax.

        Важный кейс:
        1. `status_update` со статусом `canceled` и `meta.release_to_pool=true`;
        2. купон освобождается обратно в пул ТОЛЬКО после ACK, чтобы исключить
           повторную выдачу до фактического скрытия купона у предыдущего гостя.
        """
        if event.direction != CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE:
            return
        assignment = event.assignment
        if assignment is None:
            return

        payload = dict(event.payload_json or {})
        status_value = str(payload.get("status") or "").strip().lower()
        meta_raw = payload.get("meta")
        meta = meta_raw if isinstance(meta_raw, dict) else {}
        release_to_pool = self._bool_from_meta(meta.get("release_to_pool"))
        if status_value != CouponCampaignAssignment.Status.CANCELED or not release_to_pool:
            return

        assignment_for_update = (
            CouponCampaignAssignment.objects.select_for_update()
            .select_related("coupon")
            .filter(id=assignment.id)
            .first()
        )
        if assignment_for_update is None or assignment_for_update.coupon_id is None:
            return
        if assignment_for_update.status != CouponCampaignAssignment.Status.CANCELED:
            return

        coupon = assignment_for_update.coupon
        if coupon is None:
            return
        already_released = (
            bool(coupon.is_active)
            and coupon.pool_status == CouponRegistryEntry.PoolStatus.VERIFIED_LOADED
            and coupon.assigned_at is None
        )
        if already_released:
            return

        coupon.is_active = True
        coupon.pool_status = CouponRegistryEntry.PoolStatus.VERIFIED_LOADED
        coupon.assigned_at = None
        coupon.save(update_fields=["is_active", "pool_status", "assigned_at", "updated_at"])

    @staticmethod
    def _mark_assignment_ok(*, assignment: CouponCampaignAssignment | None, synced_at) -> None:
        if assignment is None:
            return
        assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.OK
        assignment.vtelemax_synced_at = synced_at
        assignment.vtelemax_sync_error = None
        assignment.save(
            update_fields=[
                "vtelemax_sync_status",
                "vtelemax_synced_at",
                "vtelemax_sync_error",
                "updated_at",
            ]
        )

    @staticmethod
    def _mark_assignment_error(
        *,
        assignment: CouponCampaignAssignment | None,
        error_text: str,
        failed_at,
    ) -> None:
        if assignment is None:
            return
        assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.ERROR
        assignment.vtelemax_synced_at = None
        assignment.vtelemax_sync_error = error_text
        assignment.save(
            update_fields=[
                "vtelemax_sync_status",
                "vtelemax_synced_at",
                "vtelemax_sync_error",
                "updated_at",
            ]
        )
