from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from guests.models import (
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    VtelemaxSyncState,
)
from guests.services.vtelemax_coupon_sync import VtelemaxCouponSyncService


class Command(BaseCommand):
    """
    Read-only аудит готовности купонного контура SAGUR к релизной проверке.
    """

    help = (
        "Проверяет готовность купонного контура к E2E/релизу: конфиг vtelemax, "
        "свежесть синка получателей, очередь событий, sync-gate и release-состояние."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--as-json",
            action="store_true",
            help="Вывести результат JSON-структурой.",
        )
        parser.add_argument(
            "--fail-on-blocked",
            action="store_true",
            help="Завершить команду с кодом 1, если итоговый статус blocked.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        checks: list[dict[str, Any]] = []

        self._collect_config_checks(checks=checks)
        self._collect_recipient_sync_check(checks=checks, now=now)
        self._collect_queue_checks(checks=checks, now=now)
        self._collect_assignment_sync_checks(checks=checks)
        self._collect_release_checks(checks=checks)

        overall_status = self._resolve_overall_status(checks)
        summary = {
            "overall_status": overall_status,
            "checks_total": len(checks),
            "checks_ok": sum(1 for item in checks if item["status"] == "ok"),
            "checks_warning": sum(1 for item in checks if item["status"] == "warning"),
            "checks_blocked": sum(1 for item in checks if item["status"] == "blocked"),
            "generated_at": now.isoformat(),
        }
        report = {
            "summary": summary,
            "checks": checks,
        }

        if bool(options.get("as_json")):
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            self._print_human_report(summary=summary, checks=checks)

        if bool(options.get("fail_on_blocked")) and overall_status == "blocked":
            raise SystemExit(1)

    def _collect_config_checks(self, *, checks: list[dict[str, Any]]) -> None:
        sync_enabled = bool(getattr(settings, "VTELEMAX_COUPON_SYNC_ENABLED", False))
        self._add_check(
            checks=checks,
            code="coupon_sync_enabled",
            status="ok" if sync_enabled else "blocked",
            message=(
                "Доставка купонных событий во vtelemax включена."
                if sync_enabled
                else "Доставка купонных событий во vtelemax отключена."
            ),
            details={"VTELEMAX_COUPON_SYNC_ENABLED": sync_enabled},
        )

        try:
            service = VtelemaxCouponSyncService.from_settings()
        except Exception as exc:  # noqa: BLE001
            self._add_check(
                checks=checks,
                code="coupon_sync_config",
                status="blocked",
                message="Конфигурация доставки купонов во vtelemax некорректна.",
                details={"error": str(exc)},
            )
        else:
            self._add_check(
                checks=checks,
                code="coupon_sync_config",
                status="ok",
                message="Конфигурация доставки купонов во vtelemax заполнена.",
                details={
                    "base_url": service.base_url,
                    "endpoint_path": service.endpoint_path,
                    "timeout_seconds": service.timeout_seconds,
                    "max_attempts": service.max_attempts,
                },
            )

        batch_size = int(getattr(settings, "VTELEMAX_COUPON_SYNC_BATCH_SIZE", 100) or 100)
        self._add_check(
            checks=checks,
            code="coupon_sync_batch_size",
            status="ok" if batch_size <= 100 else "warning",
            message=(
                "Размер batch для vtelemax не превышает согласованный лимит."
                if batch_size <= 100
                else "Размер batch больше согласованного значения 100; перед E2E лучше вернуть 100."
            ),
            details={"VTELEMAX_COUPON_SYNC_BATCH_SIZE": batch_size, "agreed_batch_size": 100},
        )

        sync_schedule_enabled = bool(getattr(settings, "VTELEMAX_COUPON_SYNC_SCHEDULE_ENABLED", False))
        self._add_check(
            checks=checks,
            code="coupon_sync_schedule",
            status="ok" if sync_schedule_enabled else "warning",
            message=(
                "Плановая доставка купонной очереди включена."
                if sync_schedule_enabled
                else "Плановая доставка купонной очереди выключена; потребуется ручной worker или отдельный запуск."
            ),
            details={"VTELEMAX_COUPON_SYNC_SCHEDULE_ENABLED": sync_schedule_enabled},
        )

        close_enabled = bool(getattr(settings, "COUPON_CAMPAIGN_CLOSE_ENABLED", True))
        self._add_check(
            checks=checks,
            code="coupon_campaign_close_enabled",
            status="ok" if close_enabled else "blocked",
            message=(
                "Post-campaign lifecycle купонов включён."
                if close_enabled
                else "Post-campaign lifecycle купонов отключён."
            ),
            details={"COUPON_CAMPAIGN_CLOSE_ENABLED": close_enabled},
        )

        close_schedule_enabled = bool(getattr(settings, "COUPON_CAMPAIGN_CLOSE_SCHEDULE_ENABLED", False))
        self._add_check(
            checks=checks,
            code="coupon_campaign_close_schedule",
            status="ok" if close_schedule_enabled else "warning",
            message=(
                "Плановое закрытие завершённых купонных кампаний включено."
                if close_schedule_enabled
                else "Плановое закрытие завершённых купонных кампаний выключено; потребуется ручной запуск."
            ),
            details={"COUPON_CAMPAIGN_CLOSE_SCHEDULE_ENABLED": close_schedule_enabled},
        )

        redemption_enabled = bool(getattr(settings, "COUPON_REDEMPTION_SYNC_ENABLED", True))
        self._add_check(
            checks=checks,
            code="coupon_redemption_sync_enabled",
            status="ok" if redemption_enabled else "warning",
            message=(
                "Автосинхронизация применений купонов после order_fact включена."
                if redemption_enabled
                else "Автосинхронизация применений купонов после order_fact выключена; потребуется ручной sync_coupon_redemptions."
            ),
            details={"COUPON_REDEMPTION_SYNC_ENABLED": redemption_enabled},
        )

    def _collect_recipient_sync_check(self, *, checks: list[dict[str, Any]], now) -> None:
        require_fresh_state = bool(getattr(settings, "VTELEMAX_COUPON_SYNC_GATE_REQUIRE_FRESH_STATE", True))
        max_age_minutes = int(getattr(settings, "VTELEMAX_COUPON_SYNC_GATE_MAX_SYNC_AGE_MINUTES", 120) or 120)
        state = VtelemaxSyncState.objects.filter(key="vtelemax_recipients").first()

        if not require_fresh_state:
            self._add_check(
                checks=checks,
                code="recipient_sync_freshness",
                status="warning",
                message="Проверка свежести синка получателей vtelemax отключена.",
                details={"VTELEMAX_COUPON_SYNC_GATE_REQUIRE_FRESH_STATE": False},
            )
            return

        if state is None or state.last_status != VtelemaxSyncState.Status.SUCCESS or state.last_success_at is None:
            self._add_check(
                checks=checks,
                code="recipient_sync_freshness",
                status="blocked",
                message="Нет успешного свежего синка получателей vtelemax для sync-gate.",
                details={
                    "state_exists": state is not None,
                    "last_status": state.last_status if state else None,
                    "last_success_at": state.last_success_at.isoformat() if state and state.last_success_at else None,
                    "max_age_minutes": max_age_minutes,
                },
            )
            return

        age_minutes = max(0, int((now - state.last_success_at).total_seconds() // 60))
        is_fresh = age_minutes <= max_age_minutes
        self._add_check(
            checks=checks,
            code="recipient_sync_freshness",
            status="ok" if is_fresh else "blocked",
            message=(
                "Синк получателей vtelemax свежий."
                if is_fresh
                else "Синк получателей vtelemax устарел для sync-gate."
            ),
            details={
                "last_status": state.last_status,
                "last_success_at": state.last_success_at.isoformat(),
                "age_minutes": age_minutes,
                "max_age_minutes": max_age_minutes,
            },
        )

    def _collect_queue_checks(self, *, checks: list[dict[str, Any]], now) -> None:
        max_attempts = int(getattr(settings, "VTELEMAX_COUPON_SYNC_MAX_ATTEMPTS", 8) or 8)
        raw_counts = (
            CouponVtelemaxSyncQueue.objects.values("status")
            .annotate(total=Count("id"))
            .order_by("status")
        )
        counts = {str(item["status"]): int(item["total"]) for item in raw_counts}
        retry_statuses = [
            CouponVtelemaxSyncQueue.Status.PENDING,
            CouponVtelemaxSyncQueue.Status.ERROR,
            CouponVtelemaxSyncQueue.Status.SENT,
        ]
        due_total = int(
            CouponVtelemaxSyncQueue.objects.filter(
                status__in=retry_statuses,
                attempts__lt=max_attempts,
                next_retry_at__lte=now,
            ).count()
        )
        max_attempts_total = int(
            CouponVtelemaxSyncQueue.objects.filter(
                status__in=retry_statuses,
                attempts__gte=max_attempts,
            ).count()
        )

        self._add_check(
            checks=checks,
            code="coupon_queue_max_attempts",
            status="ok" if max_attempts_total == 0 else "blocked",
            message=(
                "В купонной очереди нет событий, исчерпавших retry."
                if max_attempts_total == 0
                else "В купонной очереди есть события, исчерпавшие retry."
            ),
            details={"max_attempts": max_attempts, "max_attempts_total": max_attempts_total},
        )

        queue_has_work = due_total > 0 or int(counts.get(CouponVtelemaxSyncQueue.Status.ERROR, 0)) > 0
        self._add_check(
            checks=checks,
            code="coupon_queue_due",
            status="warning" if queue_has_work else "ok",
            message=(
                "В купонной очереди есть события, требующие обработки worker."
                if queue_has_work
                else "Купонная очередь не содержит срочных событий для обработки."
            ),
            details={
                "due_total": due_total,
                "pending": int(counts.get(CouponVtelemaxSyncQueue.Status.PENDING, 0)),
                "sent": int(counts.get(CouponVtelemaxSyncQueue.Status.SENT, 0)),
                "error": int(counts.get(CouponVtelemaxSyncQueue.Status.ERROR, 0)),
                "acked": int(counts.get(CouponVtelemaxSyncQueue.Status.ACKED, 0)),
            },
        )

    def _collect_assignment_sync_checks(self, *, checks: list[dict[str, Any]]) -> None:
        error_total = int(
            CouponCampaignAssignment.objects.filter(
                vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.ERROR
            ).count()
        )
        pending_total = int(
            CouponCampaignAssignment.objects.filter(
                vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.PENDING
            ).count()
        )
        self._add_check(
            checks=checks,
            code="coupon_assignment_sync_errors",
            status="ok" if error_total == 0 else "blocked",
            message=(
                "Нет назначений купонов с ошибкой синхронизации vtelemax."
                if error_total == 0
                else "Есть назначения купонов с ошибкой синхронизации vtelemax."
            ),
            details={"assignment_sync_error_total": error_total},
        )
        self._add_check(
            checks=checks,
            code="coupon_assignment_sync_pending",
            status="ok" if pending_total == 0 else "warning",
            message=(
                "Нет назначений купонов в ожидании синхронизации vtelemax."
                if pending_total == 0
                else "Есть назначения купонов в ожидании синхронизации vtelemax."
            ),
            details={"assignment_sync_pending_total": pending_total},
        )

    def _collect_release_checks(self, *, checks: list[dict[str, Any]]) -> None:
        canceled_assignments = list(
            CouponCampaignAssignment.objects.filter(status=CouponCampaignAssignment.Status.CANCELED)
            .select_related("coupon")
            .order_by("id")
        )
        latest_events = self._build_latest_status_update_map(
            assignment_ids=[int(item.id) for item in canceled_assignments]
        )

        waiting_ack_total = 0
        acked_not_released_total = 0
        release_requested_total = 0
        for assignment in canceled_assignments:
            event = latest_events.get(int(assignment.id))
            coupon = assignment.coupon
            release_requested = self._is_release_requested(event=event, coupon=coupon)
            if not release_requested:
                continue
            release_requested_total += 1
            if event is None or event.status != CouponVtelemaxSyncQueue.Status.ACKED:
                waiting_ack_total += 1
                continue
            if not self._is_coupon_released(coupon):
                acked_not_released_total += 1

        self._add_check(
            checks=checks,
            code="coupon_release_waiting_ack",
            status="ok" if waiting_ack_total == 0 else "warning",
            message=(
                "Нет release-событий, ожидающих ACK vtelemax."
                if waiting_ack_total == 0
                else "Есть release-события, ожидающие ACK vtelemax."
            ),
            details={
                "release_requested_total": release_requested_total,
                "release_waiting_ack_total": waiting_ack_total,
            },
        )
        self._add_check(
            checks=checks,
            code="coupon_release_ack_side_effects",
            status="ok" if acked_not_released_total == 0 else "blocked",
            message=(
                "Нет release-событий с ACK без фактического возврата купона в пул."
                if acked_not_released_total == 0
                else "Есть release-события с ACK, но купон не вернулся в пул."
            ),
            details={"release_acked_not_released_total": acked_not_released_total},
        )

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
            if event.assignment_id is None:
                continue
            assignment_id = int(event.assignment_id)
            if assignment_id not in mapping:
                mapping[assignment_id] = event
        return mapping

    @staticmethod
    def _is_release_requested(
        *,
        event: CouponVtelemaxSyncQueue | None,
        coupon: CouponRegistryEntry | None,
    ) -> bool:
        if event is not None:
            payload = event.payload_json if isinstance(event.payload_json, dict) else {}
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            if Command._bool_from_meta(meta.get("release_to_pool")):
                return True
        return bool(
            coupon
            and not bool(coupon.is_active)
            and coupon.pool_status == CouponRegistryEntry.PoolStatus.ASSIGNED
        )

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
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _add_check(
        *,
        checks: list[dict[str, Any]],
        code: str,
        status: str,
        message: str,
        details: dict[str, Any],
    ) -> None:
        checks.append(
            {
                "code": code,
                "status": status,
                "message": message,
                "details": details,
            }
        )

    @staticmethod
    def _resolve_overall_status(checks: list[dict[str, Any]]) -> str:
        if any(item["status"] == "blocked" for item in checks):
            return "blocked"
        if any(item["status"] == "warning" for item in checks):
            return "warning"
        return "ready"

    def _print_human_report(self, *, summary: dict[str, Any], checks: list[dict[str, Any]]) -> None:
        self.stdout.write("=== Готовность купонного контура SAGUR ===")
        self.stdout.write(
            "Итог: {label} (status={status})".format(
                label=self._overall_label(str(summary["overall_status"])),
                status=summary["overall_status"],
            )
        )
        self.stdout.write(
            "Проверки: ok={ok} warning={warning} blocked={blocked} total={total}".format(
                ok=summary["checks_ok"],
                warning=summary["checks_warning"],
                blocked=summary["checks_blocked"],
                total=summary["checks_total"],
            )
        )
        self.stdout.write("")
        for item in checks:
            self.stdout.write(
                "[{status}] {code}: {message} ({details})".format(
                    status=str(item["status"]).upper(),
                    code=item["code"],
                    message=item["message"],
                    details=self._format_details(item["details"]),
                )
            )

    @staticmethod
    def _overall_label(status: str) -> str:
        if status == "ready":
            return "готово"
        if status == "warning":
            return "требует внимания"
        return "заблокировано"

    @staticmethod
    def _format_details(details: dict[str, Any]) -> str:
        return " ".join(f"{key}={value}" for key, value in details.items())
