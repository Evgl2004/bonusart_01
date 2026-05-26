from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db.models import Q, QuerySet
from django.utils import timezone

from guests.models import (
    CouponAutomationConfig,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    NotificationScenario,
    VtelemaxRecipientChannel,
)
from guests.services.coupon_constants import COUPON_VENUE_GLOBAL_CODE, is_coupon_global_venue
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON
from guests.services.notification_scenarios import (
    _collect_candidate_guests,
    _extract_inactive_days,
)


SUPPORTED_COUPON_AUTOSCENARIOS = {SCENARIO_CODE_INACTIVE_30D_COUPON}
DEFAULT_PREVIEW_SCAN_LIMIT = 5000


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
    scan_limit: int
    scanned_guests: int
    matched_guests: int
    sendable_guests: int
    blocked_without_channel: int
    planned_recipients_for_run: int
    available_coupons: int
    coupon_shortage: int
    warnings: tuple[str, ...] = field(default_factory=tuple)
    sample_rows: tuple[CouponAutoscenarioAudienceRow, ...] = field(default_factory=tuple)
    sample_sendable_rows: tuple[CouponAutoscenarioAudienceRow, ...] = field(default_factory=tuple)
    sample_blocked_rows: tuple[CouponAutoscenarioAudienceRow, ...] = field(default_factory=tuple)

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
            "scan_limit": self.scan_limit,
            "scanned_guests": self.scanned_guests,
            "matched_guests": self.matched_guests,
            "sendable_guests": self.sendable_guests,
            "blocked_without_channel": self.blocked_without_channel,
            "planned_recipients_for_run": self.planned_recipients_for_run,
            "available_coupons": self.available_coupons,
            "coupon_shortage": self.coupon_shortage,
            "warnings": list(self.warnings),
            "sample_rows": [row.as_dict() for row in self.sample_rows],
            "sample_sendable_rows": [row.as_dict() for row in self.sample_sendable_rows],
            "sample_blocked_rows": [row.as_dict() for row in self.sample_blocked_rows],
        }


@dataclass(frozen=True, slots=True)
class CouponAutoscenarioPlanItem:
    guest_id: int
    phone: str
    first_name: str
    last_name: str
    sendable_channels: tuple[str, ...]
    coupon_id: int
    coupon_series: str
    coupon_code: str
    venue_code: str
    venue_name: str
    valid_until: datetime

    def as_dict(self) -> dict:
        return {
            "guest_id": self.guest_id,
            "phone": self.phone,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "sendable_channels": list(self.sendable_channels),
            "coupon_id": self.coupon_id,
            "coupon_series": self.coupon_series,
            "coupon_code": self.coupon_code,
            "venue_code": self.venue_code,
            "venue_name": self.venue_name,
            "valid_until": self.valid_until.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class CouponAutoscenarioExecutionPlan:
    scenario_id: int
    scenario_code: str
    execution_mode: str
    coupon_series: str
    venue_code: str
    venue_name: str
    inactive_days_threshold: int
    max_recipients_per_run: int
    scan_limit: int
    scanned_guests: int
    matched_guests: int
    sendable_guests: int
    blocked_without_channel: int
    blocked_existing_active_coupon: int
    blocked_by_cooldown: int
    eligible_guests: int
    planned_assignments: int
    available_coupons: int
    coupon_shortage: int
    can_execute: bool
    blockers: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    plan_items: tuple[CouponAutoscenarioPlanItem, ...] = field(default_factory=tuple)

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
            "scan_limit": self.scan_limit,
            "scanned_guests": self.scanned_guests,
            "matched_guests": self.matched_guests,
            "sendable_guests": self.sendable_guests,
            "blocked_without_channel": self.blocked_without_channel,
            "blocked_existing_active_coupon": self.blocked_existing_active_coupon,
            "blocked_by_cooldown": self.blocked_by_cooldown,
            "eligible_guests": self.eligible_guests,
            "planned_assignments": self.planned_assignments,
            "available_coupons": self.available_coupons,
            "coupon_shortage": self.coupon_shortage,
            "can_execute": self.can_execute,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "plan_items": [item.as_dict() for item in self.plan_items],
        }


def preview_coupon_autoscenario_audience(
    *,
    scenario_code: str,
    limit: int | None = None,
    scan_limit: int | None = None,
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
    if config.execution_mode == CouponAutomationConfig.ExecutionMode.REPORT_ONLY:
        warnings.append("Купонная настройка в режиме 'Только отчёт'; автоматическая выдача купонов не должна запускаться.")
    if config.execution_mode == CouponAutomationConfig.ExecutionMode.PAUSED:
        warnings.append("Купонная настройка в режиме 'Пауза'.")

    inactive_days = _extract_inactive_days(scenario)
    safe_limit = max(1, int(limit or config.max_recipients_per_run or 100))
    safe_scan_limit = _resolve_preview_scan_limit(scan_limit=scan_limit, run_limit=safe_limit)
    safe_sample_limit = max(0, int(sample_limit))
    current_now = now or timezone.now()

    scanned_guests, matched_rows = _build_candidate_rows(
        inactive_days=inactive_days,
        scan_limit=safe_scan_limit,
        now=current_now,
    )

    sendable_guests = sum(1 for row in matched_rows if row.has_sendable_channel)
    blocked_without_channel = len(matched_rows) - sendable_guests
    planned_recipients_for_run = min(sendable_guests, safe_limit)
    available_coupons = _count_available_coupons(config=config)
    coupon_shortage = max(planned_recipients_for_run - available_coupons, 0)

    if not str(config.coupon_series or "").strip():
        warnings.append("Серия купонов не указана; назначение купонов невозможно.")
    if coupon_shortage > 0:
        warnings.append(
            f"Для ближайшего запуска не хватает купонов: {coupon_shortage}."
        )
    if sendable_guests > planned_recipients_for_run:
        warnings.append(
            f"Достижимых гостей больше лимита одного прохода: за запуск будет взято не более {planned_recipients_for_run}."
        )

    sendable_rows = [row for row in matched_rows if row.has_sendable_channel]
    blocked_rows = [row for row in matched_rows if not row.has_sendable_channel]

    return CouponAutoscenarioPreview(
        scenario_id=int(scenario.id),
        scenario_code=scenario.code,
        execution_mode=config.execution_mode,
        coupon_series=str(config.coupon_series or "").strip(),
        venue_code=str(config.venue_code or "").strip(),
        venue_name=str(config.venue_name or "").strip(),
        inactive_days_threshold=inactive_days,
        max_recipients_per_run=safe_limit,
        scan_limit=safe_scan_limit,
        scanned_guests=scanned_guests,
        matched_guests=len(matched_rows),
        sendable_guests=sendable_guests,
        blocked_without_channel=blocked_without_channel,
        planned_recipients_for_run=planned_recipients_for_run,
        available_coupons=available_coupons,
        coupon_shortage=coupon_shortage,
        warnings=tuple(warnings),
        sample_rows=tuple(matched_rows[:safe_sample_limit]),
        sample_sendable_rows=tuple(sendable_rows[:safe_sample_limit]),
        sample_blocked_rows=tuple(blocked_rows[:safe_sample_limit]),
    )


def build_coupon_autoscenario_execution_plan(
    *,
    scenario_code: str,
    limit: int | None = None,
    scan_limit: int | None = None,
    now: datetime | None = None,
) -> CouponAutoscenarioExecutionPlan:
    """
    Готовит безопасный план ближайшего запуска купонного автосценария.

    Функция не резервирует купоны, не создаёт события и не ставит сообщения в
    очередь. Она только соединяет аудиторию с конкретными свободными купонами,
    чтобы следующий шаг executor можно было включать уже поверх проверенного
    плана.
    """
    scenario, config = _load_coupon_autoscenario_context(scenario_code=scenario_code)
    inactive_days = _extract_inactive_days(scenario)
    safe_limit = max(1, int(limit or config.max_recipients_per_run or 100))
    safe_scan_limit = _resolve_preview_scan_limit(scan_limit=scan_limit, run_limit=safe_limit)
    current_now = now or timezone.now()

    blockers: list[str] = []
    warnings: list[str] = []

    if not scenario.is_active:
        blockers.append("Сценарий выключен.")
    if scenario.trigger_type != NotificationScenario.TriggerType.SCHEDULE:
        blockers.append("Сценарий не относится к планировщику.")
    if config.execution_mode == CouponAutomationConfig.ExecutionMode.REPORT_ONLY:
        blockers.append("Купонная настройка находится в режиме 'Только отчёт'.")
    if config.execution_mode == CouponAutomationConfig.ExecutionMode.PAUSED:
        blockers.append("Купонная настройка находится в режиме 'Пауза'.")
    if config.execution_mode not in {
        CouponAutomationConfig.ExecutionMode.PILOT,
        CouponAutomationConfig.ExecutionMode.AUTOMATIC,
    }:
        warnings.append("План построен без права фактического запуска.")

    coupon_series = str(config.coupon_series or "").strip()
    if not coupon_series:
        blockers.append("Не указана серия купонов.")

    scanned_guests, matched_rows = _build_candidate_rows(
        inactive_days=inactive_days,
        scan_limit=safe_scan_limit,
        now=current_now,
    )
    sendable_rows = [row for row in matched_rows if row.has_sendable_channel]
    blocked_without_channel = len(matched_rows) - len(sendable_rows)

    active_assignment_guest_ids = _active_assignment_guest_ids(
        guest_ids=[row.guest_id for row in sendable_rows],
        coupon_series=coupon_series,
    )
    cooldown_guest_ids = _cooldown_guest_ids(
        guest_ids=[row.guest_id for row in sendable_rows],
        coupon_series=coupon_series,
        cooldown_days=int(config.cooldown_days or 0),
        now=current_now,
    )

    eligible_rows: list[CouponAutoscenarioAudienceRow] = []
    blocked_existing_active_coupon = 0
    blocked_by_cooldown = 0
    for row in sendable_rows:
        if row.guest_id in active_assignment_guest_ids:
            blocked_existing_active_coupon += 1
            continue
        if row.guest_id in cooldown_guest_ids:
            blocked_by_cooldown += 1
            continue
        eligible_rows.append(row)

    available_coupons = list(_available_coupon_queryset(config=config)[:safe_limit])
    available_coupons_count = _count_available_coupons(config=config)
    planned_recipients = min(len(eligible_rows), safe_limit)
    coupon_shortage = max(planned_recipients - len(available_coupons), 0)
    if coupon_shortage > 0:
        blockers.append(f"Не хватает купонов для ближайшего запуска: {coupon_shortage}.")

    valid_until = current_now + timedelta(days=max(1, int(config.coupon_validity_days or 1)))
    plan_items: list[CouponAutoscenarioPlanItem] = []
    for row, coupon in zip(eligible_rows[:planned_recipients], available_coupons):
        venue_code = str(coupon.venue_code or config.venue_code or "").strip()
        venue_name = str(coupon.venue_name or config.venue_name or "").strip()
        plan_items.append(
            CouponAutoscenarioPlanItem(
                guest_id=row.guest_id,
                phone=row.phone,
                first_name=row.first_name,
                last_name=row.last_name,
                sendable_channels=row.sendable_channels,
                coupon_id=int(coupon.id),
                coupon_series=str(coupon.series or "").strip(),
                coupon_code=str(coupon.code or "").strip(),
                venue_code=venue_code,
                venue_name=venue_name,
                valid_until=valid_until,
            )
        )

    if planned_recipients == 0:
        warnings.append("Нет достижимых гостей для ближайшего запуска после фильтров.")

    can_execute = not blockers and bool(plan_items)
    return CouponAutoscenarioExecutionPlan(
        scenario_id=int(scenario.id),
        scenario_code=scenario.code,
        execution_mode=config.execution_mode,
        coupon_series=coupon_series,
        venue_code=str(config.venue_code or "").strip(),
        venue_name=str(config.venue_name or "").strip(),
        inactive_days_threshold=inactive_days,
        max_recipients_per_run=safe_limit,
        scan_limit=safe_scan_limit,
        scanned_guests=scanned_guests,
        matched_guests=len(matched_rows),
        sendable_guests=len(sendable_rows),
        blocked_without_channel=blocked_without_channel,
        blocked_existing_active_coupon=blocked_existing_active_coupon,
        blocked_by_cooldown=blocked_by_cooldown,
        eligible_guests=len(eligible_rows),
        planned_assignments=len(plan_items),
        available_coupons=available_coupons_count,
        coupon_shortage=coupon_shortage,
        can_execute=can_execute,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        plan_items=tuple(plan_items),
    )


def _load_coupon_autoscenario_context(
    *,
    scenario_code: str,
) -> tuple[NotificationScenario, CouponAutomationConfig]:
    safe_code = str(scenario_code or "").strip()
    if not safe_code:
        raise CouponAutoscenarioPreviewError("Не указан код сценария.")
    if safe_code not in SUPPORTED_COUPON_AUTOSCENARIOS:
        raise CouponAutoscenarioPreviewError(
            f"Купонный backend пока поддерживает только сценарии: {', '.join(sorted(SUPPORTED_COUPON_AUTOSCENARIOS))}."
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
    return scenario, config


def _build_candidate_rows(
    *,
    inactive_days: int,
    scan_limit: int,
    now: datetime,
) -> tuple[int, list[CouponAutoscenarioAudienceRow]]:
    candidates = list(
        _collect_candidate_guests(
            inactive_days=inactive_days,
            limit=scan_limit,
            now=now,
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
        days_without_visits = max(0, int((now - last_visit_at).days))
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
    return scanned_guests, matched_rows


def _resolve_preview_scan_limit(*, scan_limit: int | None, run_limit: int) -> int:
    if scan_limit is None:
        return max(DEFAULT_PREVIEW_SCAN_LIMIT, run_limit)
    return max(run_limit, max(1, int(scan_limit)))


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
    return _available_coupon_queryset(config=config).count()


def _available_coupon_queryset(*, config: CouponAutomationConfig) -> QuerySet[CouponRegistryEntry]:
    coupon_series = str(config.coupon_series or "").strip()
    if not coupon_series:
        return CouponRegistryEntry.objects.none()

    queryset = CouponRegistryEntry.objects.filter(
        series=coupon_series,
        is_active=True,
        pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
    )
    venue_code = str(config.venue_code or "").strip()
    if is_coupon_global_venue(venue_code):
        queryset = queryset.filter(
            Q(venue_code=COUPON_VENUE_GLOBAL_CODE) | Q(venue_code__isnull=True) | Q(venue_code="")
        )
    elif venue_code:
        queryset = queryset.filter(venue_code=venue_code)
    return queryset.order_by("id")


def _active_assignment_guest_ids(*, guest_ids: list[int], coupon_series: str) -> set[int]:
    if not guest_ids or not coupon_series:
        return set()
    return set(
        CouponCampaignAssignment.objects.filter(
            guest_id__in=guest_ids,
            coupon_series=coupon_series,
            status__in=[
                CouponCampaignAssignment.Status.RESERVED,
                CouponCampaignAssignment.Status.SENT,
            ],
        ).values_list("guest_id", flat=True)
    )


def _cooldown_guest_ids(
    *,
    guest_ids: list[int],
    coupon_series: str,
    cooldown_days: int,
    now: datetime,
) -> set[int]:
    if not guest_ids or not coupon_series or cooldown_days <= 0:
        return set()
    cutoff = now - timedelta(days=cooldown_days)
    return set(
        CouponCampaignAssignment.objects.filter(
            guest_id__in=guest_ids,
            coupon_series=coupon_series,
            assigned_at__gte=cutoff,
            status__in=[
                CouponCampaignAssignment.Status.SENT,
                CouponCampaignAssignment.Status.USED,
                CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN,
                CouponCampaignAssignment.Status.EXPIRED,
            ],
        ).values_list("guest_id", flat=True)
    )
