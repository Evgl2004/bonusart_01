from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

from guests.models import (
    CouponAutomationConfig,
    CouponRegistryEntry,
    NotificationScenario,
    VtelemaxRecipientChannel,
)
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON
from guests.services.notification_scenarios import (
    _collect_candidate_guests,
    _extract_inactive_days,
)


SUPPORTED_COUPON_AUTOSCENARIOS = {SCENARIO_CODE_INACTIVE_30D_COUPON}


class CouponAutoscenarioPreviewError(ValueError):
    """Ошибка подготовки безопасного расчёта купонного автосценария."""


@dataclass(frozen=True, slots=True)
class CouponAutoscenarioAudienceRow:
    guest_id: int
    phone: str
    first_name: str
    last_name: str
    last_visit_at: datetime | None
    sendable_channels: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_sendable_channel(self) -> bool:
        return bool(self.sendable_channels)

    def as_dict(self) -> dict:
        return {
            "guest_id": self.guest_id,
            "phone": self.phone,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "last_visit_at": self.last_visit_at.isoformat() if self.last_visit_at else None,
            "sendable_channels": list(self.sendable_channels),
            "has_sendable_channel": self.has_sendable_channel,
        }


@dataclass(frozen=True, slots=True)
class CouponAutoscenarioPreview:
    scenario_id: int
    scenario_code: str
    execution_mode: str
    coupon_series: str
    venue_code: str
    venue_name: str
    inactive_days_threshold: int
    max_recipients_per_run: int
    scanned_guests: int
    matched_guests: int
    sendable_guests: int
    blocked_without_channel: int
    available_coupons: int
    coupon_shortage: int
    warnings: tuple[str, ...] = field(default_factory=tuple)
    sample_rows: tuple[CouponAutoscenarioAudienceRow, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "scenario_code": self.scenario_code,
            "execution_mode": self.execution_mode,
            "coupon_series": self.coupon_series,
            "venue_code": self.venue_code,
            "venue_name": self.venue_name,
            "inactive_days_threshold": self.inactive_days_threshold,
            "max_recipients_per_run": self.max_recipients_per_run,
            "scanned_guests": self.scanned_guests,
            "matched_guests": self.matched_guests,
            "sendable_guests": self.sendable_guests,
            "blocked_without_channel": self.blocked_without_channel,
            "available_coupons": self.available_coupons,
            "coupon_shortage": self.coupon_shortage,
            "warnings": list(self.warnings),
            "sample_rows": [row.as_dict() for row in self.sample_rows],
        }


def preview_coupon_autoscenario_audience(
    *,
    scenario_code: str,
    limit: int | None = None,
    sample_limit: int = 20,
    now: datetime | None = None,
) -> CouponAutoscenarioPreview:
    """
    Безопасно считает аудиторию купонного автосценария без побочных эффектов.

    Функция не создаёт `NotificationEvent`, `DispatchTask`, назначения купонов и
    события vtelemax-очереди. Это только операционный черновик перед запуском
    настоящего executor.
    """
    safe_code = str(scenario_code or "").strip()
    if not safe_code:
        raise CouponAutoscenarioPreviewError("Не указан код сценария.")
    if safe_code not in SUPPORTED_COUPON_AUTOSCENARIOS:
        raise CouponAutoscenarioPreviewError(
            f"Купонный preview пока поддерживает только сценарии: {', '.join(sorted(SUPPORTED_COUPON_AUTOSCENARIOS))}."
        )

    scenario = NotificationScenario.objects.filter(code=safe_code).first()
    if scenario is None:
        raise CouponAutoscenarioPreviewError(f"Сценарий '{safe_code}' не найден.")

    try:
        config = scenario.coupon_automation_config
    except CouponAutomationConfig.DoesNotExist as exc:
        raise CouponAutoscenarioPreviewError(
            f"Для сценария '{safe_code}' не настроен CouponAutomationConfig."
        ) from exc

    warnings: list[str] = []
    if not scenario.is_active:
        warnings.append("Сценарий сейчас выключен; расчёт выполнен как черновик.")
    if scenario.trigger_type != NotificationScenario.TriggerType.SCHEDULE:
        warnings.append("Сценарий не относится к планировщику; автоматический запуск невозможен без смены trigger_type.")
    if config.execution_mode == CouponAutomationConfig.ExecutionMode.PAUSED:
        warnings.append("Купонная настройка в режиме 'Пауза'.")

    inactive_days = _extract_inactive_days(scenario)
    safe_limit = max(1, int(limit or config.max_recipients_per_run or 100))
    safe_sample_limit = max(0, int(sample_limit))
    current_now = now or timezone.now()

    candidates = list(
        _collect_candidate_guests(
            inactive_days=inactive_days,
            limit=safe_limit,
            now=current_now,
        )
    )
    channels_map = _build_sendable_channels_map(guest_ids=[candidate.id for candidate in candidates])

    matched_rows: list[CouponAutoscenarioAudienceRow] = []
    scanned_guests = 0
    for guest in candidates:
        scanned_guests += 1
        last_visit_at = getattr(guest, "last_visit_at", None)
        if last_visit_at is None:
            continue
        days_without_visits = max(0, int((current_now - last_visit_at).days))
        if days_without_visits < inactive_days:
            continue
        sendable_platforms = tuple(channels_map.get(guest.id, ()))
        matched_rows.append(
            CouponAutoscenarioAudienceRow(
                guest_id=int(guest.id),
                phone=str(guest.phone or ""),
                first_name=str(guest.first_name or ""),
                last_name=str(guest.last_name or ""),
                last_visit_at=last_visit_at,
                sendable_channels=sendable_platforms,
            )
        )

    sendable_guests = sum(1 for row in matched_rows if row.has_sendable_channel)
    blocked_without_channel = len(matched_rows) - sendable_guests
    available_coupons = _count_available_coupons(config=config)
    coupon_shortage = max(sendable_guests - available_coupons, 0)

    if not str(config.coupon_series or "").strip():
        warnings.append("Серия купонов не указана; назначение купонов невозможно.")
    if coupon_shortage > 0:
        warnings.append(
            f"Доступных купонов меньше, чем гостей с каналом доставки: не хватает {coupon_shortage}."
        )

    return CouponAutoscenarioPreview(
        scenario_id=int(scenario.id),
        scenario_code=scenario.code,
        execution_mode=config.execution_mode,
        coupon_series=str(config.coupon_series or "").strip(),
        venue_code=str(config.venue_code or "").strip(),
        venue_name=str(config.venue_name or "").strip(),
        inactive_days_threshold=inactive_days,
        max_recipients_per_run=safe_limit,
        scanned_guests=scanned_guests,
        matched_guests=len(matched_rows),
        sendable_guests=sendable_guests,
        blocked_without_channel=blocked_without_channel,
        available_coupons=available_coupons,
        coupon_shortage=coupon_shortage,
        warnings=tuple(warnings),
        sample_rows=tuple(matched_rows[:safe_sample_limit]),
    )


def _build_sendable_channels_map(*, guest_ids: list[int]) -> dict[int, tuple[str, ...]]:
    if not guest_ids:
        return {}

    result: dict[int, list[str]] = {}
    for channel in (
        VtelemaxRecipientChannel.objects.filter(guest_id__in=guest_ids)
        .order_by("guest_id", "platform", "id")
        .only("guest_id", "platform", "is_registered", "notifications_allowed", "external_id")
    ):
        if not _is_channel_sendable(channel):
            continue
        result.setdefault(int(channel.guest_id), []).append(str(channel.platform))
    return {guest_id: tuple(platforms) for guest_id, platforms in result.items()}


def _is_channel_sendable(channel: VtelemaxRecipientChannel | None) -> bool:
    if channel is None:
        return False
    if not bool(channel.is_registered):
        return False
    if not bool(channel.notifications_allowed):
        return False
    if not str(channel.external_id or "").strip():
        return False
    return True


def _count_available_coupons(*, config: CouponAutomationConfig) -> int:
    coupon_series = str(config.coupon_series or "").strip()
    if not coupon_series:
        return 0

    queryset = CouponRegistryEntry.objects.filter(
        series=coupon_series,
        is_active=True,
        pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
    )
    venue_code = str(config.venue_code or "").strip()
    if venue_code:
        queryset = queryset.filter(venue_code=venue_code)
    return queryset.count()
