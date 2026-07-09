from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from guests.models import (
    CouponAutoscenarioAssignment,
    GuestWelcomeRegistrationEvent,
    VtelemaxRecipientChannel,
)
from guests.services.coupon_autoscenarios import (
    WelcomeCouponReservationResult,
    reserve_welcome_registration_coupon,
)
from guests.services.vtelemax_recipients_sync import VtelemaxRecipientsApplyService


@dataclass(slots=True)
class WelcomeRegistrationProcessingStats:
    scanned: int = 0
    processed: int = 0
    channel_applied: int = 0
    coupon_reserved: int = 0
    skipped: int = 0
    failed: int = 0
    skipped_max_attempts: int = 0
    dry_run: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "scanned": int(self.scanned),
            "processed": int(self.processed),
            "channel_applied": int(self.channel_applied),
            "coupon_reserved": int(self.coupon_reserved),
            "skipped": int(self.skipped),
            "failed": int(self.failed),
            "skipped_max_attempts": int(self.skipped_max_attempts),
            "dry_run": bool(self.dry_run),
        }


class WelcomeRegistrationEventProcessor:
    """
    Обрабатывает журнал welcome-регистраций, принятых от vtelemax.

    Процесс намеренно отделён от HTTP-callback: входящий запрос быстро
    фиксирует событие, а эта очередь применяет профиль, резервирует купон и
    передаёт дальнейшую работу штатным очередям vtelemax/iikoCard/доставки.
    """

    def __init__(
        self,
        *,
        scenario_code: str,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
        bot_code_telegram: str = "",
        bot_code_max: str = "",
        bot_code_vk: str = "",
    ) -> None:
        self.scenario_code = str(scenario_code or "").strip()
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))
        self.apply_service = VtelemaxRecipientsApplyService(
            bot_code_telegram=str(bot_code_telegram or "").strip(),
            bot_code_max=str(bot_code_max or "").strip(),
            bot_code_vk=str(bot_code_vk or "").strip(),
            create_missing_guests=True,
        )

    @classmethod
    def from_settings(cls) -> "WelcomeRegistrationEventProcessor":
        """
        Собирает обработчик из Django settings.
        """
        return cls(
            scenario_code=str(getattr(settings, "WELCOME_COUPON_SCENARIO_CODE", "welcome_coupon") or "welcome_coupon"),
            max_attempts=int(getattr(settings, "WELCOME_COUPON_PROCESSING_MAX_ATTEMPTS", 8) or 8),
            retry_base_seconds=int(getattr(settings, "WELCOME_COUPON_PROCESSING_RETRY_BASE_SECONDS", 30) or 30),
            retry_max_seconds=int(getattr(settings, "WELCOME_COUPON_PROCESSING_RETRY_MAX_SECONDS", 3600) or 3600),
            bot_code_telegram=str(getattr(settings, "VTELEMAX_SYNC_BOT_CODE_TELEGRAM", "") or "").strip(),
            bot_code_max=str(getattr(settings, "VTELEMAX_SYNC_BOT_CODE_MAX", "") or "").strip(),
            bot_code_vk=str(getattr(settings, "VTELEMAX_SYNC_BOT_CODE_VK", "") or "").strip(),
        )

    def process_batch(
        self,
        *,
        limit: int,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> WelcomeRegistrationProcessingStats:
        """
        Обрабатывает пачку событий с retry/backoff.
        """
        current_now = now or timezone.now()
        safe_limit = max(1, int(limit))
        stats = WelcomeRegistrationProcessingStats(dry_run=bool(dry_run))

        candidate_qs = GuestWelcomeRegistrationEvent.objects.filter(
            status__in=[
                GuestWelcomeRegistrationEvent.Status.NEW,
                GuestWelcomeRegistrationEvent.Status.CHANNEL_APPLIED,
                GuestWelcomeRegistrationEvent.Status.ERROR,
            ],
            next_retry_at__lte=current_now,
        )
        stats.skipped_max_attempts = int(candidate_qs.filter(attempts__gte=self.max_attempts).count())
        event_ids = list(
            candidate_qs.filter(attempts__lt=self.max_attempts)
            .order_by("next_retry_at", "received_at", "id")
            .values_list("id", flat=True)[:safe_limit]
        )
        stats.scanned = len(event_ids) + stats.skipped_max_attempts
        if dry_run:
            return stats

        for event_id in event_ids:
            result = self._process_single_event(event_id=int(event_id), now=current_now)
            stats.processed += int(result.get("processed", 0))
            stats.channel_applied += int(result.get("channel_applied", 0))
            stats.coupon_reserved += int(result.get("coupon_reserved", 0))
            stats.skipped += int(result.get("skipped", 0))
            stats.failed += int(result.get("failed", 0))
        return stats

    def _process_single_event(self, *, event_id: int, now: datetime) -> dict[str, int]:
        counters = {
            "processed": 0,
            "channel_applied": 0,
            "coupon_reserved": 0,
            "skipped": 0,
            "failed": 0,
        }
        try:
            with transaction.atomic():
                event = (
                    GuestWelcomeRegistrationEvent.objects.select_for_update()
                    .select_related("guest", "vtelemax_channel", "coupon_assignment")
                    .get(pk=event_id)
                )
                if event.status in {
                    GuestWelcomeRegistrationEvent.Status.COUPON_RESERVED,
                    GuestWelcomeRegistrationEvent.Status.SKIPPED,
                }:
                    return counters
                if int(event.attempts or 0) >= self.max_attempts:
                    return counters

                counters["processed"] = 1
                if not bool(event.is_registered and event.notifications_allowed and event.external_id):
                    self._mark_event_skipped(
                        event=event,
                        reason="channel_not_sendable",
                        now=now,
                    )
                    counters["skipped"] = 1
                    return counters

                self.apply_service.apply_items(items=[self._event_to_vtelemax_item(event)], dry_run=False)

                channel = (
                    VtelemaxRecipientChannel.objects.select_related("guest", "guest_binding", "guest_binding__bot")
                    .filter(person_id=event.person_id, platform=event.platform)
                    .first()
                )
                if channel is None:
                    self._mark_event_error(
                        event=event,
                        message="После применения welcome-события не найден канал vtelemax.",
                        now=now,
                    )
                    counters["failed"] = 1
                    return counters
                if channel.guest_id is None or channel.guest is None:
                    self._mark_event_error(
                        event=event,
                        message="После применения welcome-события канал vtelemax не связан с гостем.",
                        now=now,
                    )
                    counters["failed"] = 1
                    return counters

                event.guest = channel.guest
                event.vtelemax_channel = channel
                event.status = GuestWelcomeRegistrationEvent.Status.CHANNEL_APPLIED
                event.error_text = None
                event.save(
                    update_fields=[
                        "guest",
                        "vtelemax_channel",
                        "status",
                        "error_text",
                        "updated_at",
                    ]
                )
                counters["channel_applied"] = 1

                reservation = reserve_welcome_registration_coupon(
                    guest=channel.guest,
                    channel=channel,
                    scenario_code=self.scenario_code,
                    event_id=event.event_id,
                    registered_at=event.registered_at,
                    current_now=now,
                )
                self._apply_reservation_result(event=event, result=reservation, now=now)
                if reservation.created:
                    counters["coupon_reserved"] = 1
                elif reservation.skipped:
                    counters["skipped"] = 1
                elif reservation.error_text:
                    counters["failed"] = 1
                return counters
        except Exception as exc:
            self._mark_event_error_by_id(
                event_id=event_id,
                message=f"Ошибка обработки welcome-события: {exc}",
                now=now,
            )
            counters["processed"] = 1
            counters["failed"] = 1
            return counters

    def _apply_reservation_result(
        self,
        *,
        event: GuestWelcomeRegistrationEvent,
        result: WelcomeCouponReservationResult,
        now: datetime,
    ) -> None:
        if result.created:
            event.coupon_assignment = result.assignment
            event.status = GuestWelcomeRegistrationEvent.Status.COUPON_RESERVED
            event.skip_reason = None
            event.error_text = None
            event.processed_at = now
            event.save(
                update_fields=[
                    "coupon_assignment",
                    "status",
                    "skip_reason",
                    "error_text",
                    "processed_at",
                    "updated_at",
                ]
            )
            return

        if result.skipped:
            if self._assignment_can_be_linked_to_event(event=event, assignment=result.assignment):
                event.coupon_assignment = result.assignment
                coupon_assignment_field = ["coupon_assignment"]
            else:
                coupon_assignment_field = []
            event.status = GuestWelcomeRegistrationEvent.Status.SKIPPED
            event.skip_reason = result.skip_reason or "welcome_coupon_skipped"
            event.error_text = None
            event.processed_at = now
            event.save(
                update_fields=[
                    *coupon_assignment_field,
                    "status",
                    "skip_reason",
                    "error_text",
                    "processed_at",
                    "updated_at",
                ]
            )
            return

        self._mark_event_error(
            event=event,
            message=result.error_text or "Welcome-купон не был зарезервирован по неизвестной причине.",
            now=now,
        )

    @staticmethod
    def _mark_event_skipped(
        *,
        event: GuestWelcomeRegistrationEvent,
        reason: str,
        now: datetime,
    ) -> None:
        event.status = GuestWelcomeRegistrationEvent.Status.SKIPPED
        event.skip_reason = str(reason or "").strip()[:120] or "welcome_coupon_skipped"
        event.error_text = None
        event.processed_at = now
        event.save(
            update_fields=[
                "status",
                "skip_reason",
                "error_text",
                "processed_at",
                "updated_at",
            ]
        )

    @staticmethod
    def _assignment_can_be_linked_to_event(
        *,
        event: GuestWelcomeRegistrationEvent,
        assignment: CouponAutoscenarioAssignment | None,
    ) -> bool:
        if assignment is None:
            return False
        return not GuestWelcomeRegistrationEvent.objects.filter(
            coupon_assignment=assignment,
        ).exclude(id=event.id).exists()

    def _mark_event_error(
        self,
        *,
        event: GuestWelcomeRegistrationEvent,
        message: str,
        now: datetime,
    ) -> None:
        attempts = int(event.attempts or 0) + 1
        event.status = GuestWelcomeRegistrationEvent.Status.ERROR
        event.error_text = str(message or "").strip()[:4000]
        event.attempts = attempts
        event.next_retry_at = now + timedelta(seconds=self._retry_delay_seconds(attempts=attempts))
        event.save(
            update_fields=[
                "status",
                "error_text",
                "attempts",
                "next_retry_at",
                "updated_at",
            ]
        )

    def _mark_event_error_by_id(self, *, event_id: int, message: str, now: datetime) -> None:
        with transaction.atomic():
            event = GuestWelcomeRegistrationEvent.objects.select_for_update().filter(pk=event_id).first()
            if event is None:
                return
            if event.status in {
                GuestWelcomeRegistrationEvent.Status.COUPON_RESERVED,
                GuestWelcomeRegistrationEvent.Status.SKIPPED,
            }:
                return
            self._mark_event_error(event=event, message=message, now=now)

    def _retry_delay_seconds(self, *, attempts: int) -> int:
        exponent = max(0, int(attempts) - 1)
        return min(self.retry_max_seconds, self.retry_base_seconds * (2**exponent))

    @staticmethod
    def _event_to_vtelemax_item(event: GuestWelcomeRegistrationEvent) -> dict[str, Any]:
        payload = dict(event.payload_json or {}) if isinstance(event.payload_json, dict) else {}
        profile = dict(event.profile or {}) if isinstance(event.profile, dict) else {}
        payload.update(
            {
                "person_id": str(event.person_id),
                "platform": event.platform,
                "phone_e164": event.phone_e164,
                "customerId": event.iiko_customer_id,
                "external_id": event.external_id,
                "rules_accepted": bool(event.rules_accepted),
                "notifications_allowed": bool(event.notifications_allowed),
                "is_registered": bool(event.is_registered),
                "registered_at": _datetime_to_iso(event.registered_at),
                "state_updated_at": _datetime_to_iso(event.state_updated_at),
                "account_created_at": _datetime_to_iso(event.account_created_at),
                "effective_updated_at": _datetime_to_iso(event.effective_updated_at),
                "profile": profile,
            }
        )
        return payload


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value.isoformat()
