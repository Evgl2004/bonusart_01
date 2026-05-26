from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from guests.models import (
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponAutomationConfig,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    NotificationEvent,
    NotificationScenario,
    VisitHistory,
    VtelemaxRecipientChannel,
)
from guests.services.coupon_constants import COUPON_VENUE_GLOBAL_CODE, is_coupon_global_venue
from guests.services.notification_events import ScenarioNotConfiguredError, create_notification_event
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON
from guests.services.notification_scenarios import (
    _collect_candidate_guests,
    _extract_inactive_days,
)
from guests.services.guest_resolution import normalize_phone_e164
from guests.services.template_render import render_message_for_guest


SUPPORTED_COUPON_AUTOSCENARIOS = {SCENARIO_CODE_INACTIVE_30D_COUPON}
DEFAULT_PREVIEW_SCAN_LIMIT = 5000
DEFAULT_PILOT_PHONE_E164 = "+79129923438"
PILOT_PHONE_SETTINGS_KEYS = ("pilot_phone_e164", "pilot_phone", "pilot_phones", "pilot_phone_e164s")
PILOT_GUEST_ID_SETTINGS_KEYS = ("pilot_guest_id", "pilot_guest_ids")
PILOT_INCLUDE_UNMATCHED_SETTINGS_KEYS = (
    "pilot_include_unmatched",
    "pilot_force_include",
    "pilot_force_include_recipients",
)


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
    blocked_by_pilot_filter: int
    pilot_phone_filters: tuple[str, ...]
    pilot_guest_id_filters: tuple[int, ...]
    used_default_pilot_phone: bool
    pilot_forced_guests: int
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
            "blocked_by_pilot_filter": self.blocked_by_pilot_filter,
            "pilot_phone_filters": list(self.pilot_phone_filters),
            "pilot_guest_id_filters": list(self.pilot_guest_id_filters),
            "used_default_pilot_phone": self.used_default_pilot_phone,
            "pilot_forced_guests": self.pilot_forced_guests,
            "eligible_guests": self.eligible_guests,
            "planned_assignments": self.planned_assignments,
            "available_coupons": self.available_coupons,
            "coupon_shortage": self.coupon_shortage,
            "can_execute": self.can_execute,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "plan_items": [item.as_dict() for item in self.plan_items],
        }


@dataclass(frozen=True, slots=True)
class CouponAutoscenarioExecutionResult:
    plan: CouponAutoscenarioExecutionPlan
    dry_run: bool
    confirmed: bool
    run_id: int | None = None
    created_assignments: int = 0
    queue_events_created: int = 0

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "confirmed": self.confirmed,
            "run_id": self.run_id,
            "created_assignments": self.created_assignments,
            "queue_events_created": self.queue_events_created,
            "plan": self.plan.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class PilotRecipientFilter:
    phones: tuple[str, ...] = field(default_factory=tuple)
    guest_ids: tuple[int, ...] = field(default_factory=tuple)
    used_default_phone: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.phones and not self.guest_ids


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
        if config.execution_mode == CouponAutomationConfig.ExecutionMode.PILOT:
            warnings.append(
                "NotificationScenario выключен; пилотный купонный автосценарий будет выполнен "
                "только через явный запуск, без старого планировщика уведомлений."
            )
        else:
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
    pilot_filter = _resolve_pilot_recipient_filter(config=config)
    matched_rows, pilot_forced_guests = _append_unmatched_pilot_rows_if_requested(
        matched_rows=matched_rows,
        config=config,
        pilot_filter=pilot_filter,
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
    if (
        config.execution_mode == CouponAutomationConfig.ExecutionMode.PILOT
        and pilot_filter.used_default_phone
    ):
        warnings.append(
            f"Пилотный список получателей не задан; используется безопасная заглушка {DEFAULT_PILOT_PHONE_E164}."
        )
    if pilot_forced_guests > 0:
        warnings.append(
            f"Для пилотной проверки дополнительно включено гостей вне основного сегмента: {pilot_forced_guests}."
        )

    eligible_rows: list[CouponAutoscenarioAudienceRow] = []
    blocked_existing_active_coupon = 0
    blocked_by_cooldown = 0
    blocked_by_pilot_filter = 0
    for row in sendable_rows:
        if row.guest_id in active_assignment_guest_ids:
            blocked_existing_active_coupon += 1
            continue
        if row.guest_id in cooldown_guest_ids:
            blocked_by_cooldown += 1
            continue
        if not _is_allowed_by_pilot_filter(
            row=row,
            config=config,
            pilot_filter=pilot_filter,
        ):
            blocked_by_pilot_filter += 1
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
        blocked_by_pilot_filter=blocked_by_pilot_filter,
        pilot_phone_filters=pilot_filter.phones,
        pilot_guest_id_filters=pilot_filter.guest_ids,
        used_default_pilot_phone=pilot_filter.used_default_phone,
        pilot_forced_guests=pilot_forced_guests,
        eligible_guests=len(eligible_rows),
        planned_assignments=len(plan_items),
        available_coupons=available_coupons_count,
        coupon_shortage=coupon_shortage,
        can_execute=can_execute,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        plan_items=tuple(plan_items),
    )


def execute_coupon_autoscenario_pilot(
    *,
    scenario_code: str,
    limit: int | None = None,
    scan_limit: int | None = None,
    confirm: bool = False,
    now: datetime | None = None,
) -> CouponAutoscenarioExecutionResult:
    """
    Выполняет безопасный пробный запуск купонного автосценария.

    Без `confirm=True` команда работает как сухой прогон: строит план и ничего
    не меняет в базе. При подтверждении создаёт техническую волну, резервирует
    купоны и ставит assignment-события в очередь vtelemax. Сообщения гостям на
    этом шаге не создаются и не отправляются.
    """
    current_now = now or timezone.now()
    scenario, config = _load_coupon_autoscenario_context(scenario_code=scenario_code)
    plan = build_coupon_autoscenario_execution_plan(
        scenario_code=scenario.code,
        limit=limit,
        scan_limit=scan_limit,
        now=current_now,
    )

    if not confirm:
        return CouponAutoscenarioExecutionResult(
            plan=plan,
            dry_run=True,
            confirmed=False,
        )

    if config.execution_mode != CouponAutomationConfig.ExecutionMode.PILOT:
        raise CouponAutoscenarioPreviewError(
            "Фактический пробный запуск разрешён только для режима 'Пилот'."
        )
    if not plan.can_execute:
        blockers = "; ".join(plan.blockers) or "план не готов к запуску"
        raise CouponAutoscenarioPreviewError(f"Нельзя выполнить пробный запуск: {blockers}.")

    with transaction.atomic():
        run = CouponAutoscenarioRun.objects.create(
            scenario=scenario,
            config=config,
            status=CouponAutoscenarioRun.Status.SYNC_PENDING,
            execution_mode=config.execution_mode,
            scan_limit=plan.scan_limit,
            max_recipients_per_run=plan.max_recipients_per_run,
            scanned_guests=plan.scanned_guests,
            matched_guests=plan.matched_guests,
            sendable_guests=plan.sendable_guests,
            blocked_without_channel=plan.blocked_without_channel,
            blocked_existing_active_coupon=plan.blocked_existing_active_coupon,
            blocked_by_cooldown=plan.blocked_by_cooldown,
            eligible_guests=plan.eligible_guests,
            planned_assignments=plan.planned_assignments,
            coupon_shortage=plan.coupon_shortage,
            warnings=list(plan.warnings),
            blockers=list(plan.blockers),
        )

        coupon_ids = [item.coupon_id for item in plan.plan_items]
        locked_coupons = {
            int(coupon.id): coupon
            for coupon in CouponRegistryEntry.objects.select_for_update().filter(id__in=coupon_ids)
        }
        guests = {
            int(guest.id): guest
            for guest in Guest.objects.filter(id__in=[item.guest_id for item in plan.plan_items])
        }
        primary_channels = _build_primary_channel_map(
            guest_ids=[item.guest_id for item in plan.plan_items]
        )

        created_assignments = 0
        queue_events_created = 0
        for item in plan.plan_items:
            coupon = locked_coupons.get(item.coupon_id)
            guest = guests.get(item.guest_id)
            if coupon is None or guest is None:
                raise CouponAutoscenarioPreviewError(
                    f"План устарел: не найден купон или гость для coupon_id={item.coupon_id}, guest_id={item.guest_id}."
                )
            if not _is_coupon_still_available(coupon=coupon, config=config):
                raise CouponAutoscenarioPreviewError(
                    f"План устарел: купон {coupon.series}:{coupon.code} уже недоступен."
                )

            channel = primary_channels.get(item.guest_id)
            rendered_promo_text = _render_autoscenario_coupon_text(
                scenario=scenario,
                config=config,
                guest=guest,
                coupon_code=item.coupon_code,
                coupon_series=item.coupon_series,
                venue_code=item.venue_code,
                venue_name=item.venue_name,
                valid_until=item.valid_until,
            )
            assignment = CouponAutoscenarioAssignment.objects.create(
                run=run,
                scenario=scenario,
                config=config,
                guest=guest,
                coupon=coupon,
                person_id=channel.person_id if channel else None,
                phone_e164=str(channel.phone_e164 or "").strip() if channel else item.phone,
                coupon_series=item.coupon_series,
                coupon_code=item.coupon_code,
                venue_code=item.venue_code or None,
                venue_name=item.venue_name or None,
                promo_text=rendered_promo_text or None,
                assigned_at=current_now,
                lifetime_expires_at=item.valid_until,
                status=CouponAutoscenarioAssignment.Status.RESERVED,
                vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
            )
            created_assignments += 1

            coupon.is_active = False
            coupon.pool_status = CouponRegistryEntry.PoolStatus.ASSIGNED
            coupon.assigned_at = current_now
            coupon.save(update_fields=["is_active", "pool_status", "assigned_at", "updated_at"])

            CouponVtelemaxSyncQueue.objects.create(
                direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
                autoscenario_assignment=assignment,
                payload_json=_build_autoscenario_assignment_payload(
                    run=run,
                    assignment=assignment,
                ),
                status=CouponVtelemaxSyncQueue.Status.PENDING,
                next_retry_at=current_now,
            )
            queue_events_created += 1

        run.created_assignments = created_assignments
        run.queue_events_created = queue_events_created
        run.save(update_fields=["created_assignments", "queue_events_created", "updated_at"])

    return CouponAutoscenarioExecutionResult(
        plan=plan,
        dry_run=False,
        confirmed=True,
        run_id=int(run.id),
        created_assignments=created_assignments,
        queue_events_created=queue_events_created,
    )


def create_autoscenario_dispatch_after_vtelemax_ack(
    *,
    assignment_id: int,
    now: datetime | None = None,
) -> int:
    """
    Создаёт задачу отправки гостю после ACK assignment-события во vtelemax.

    До ACK купон уже зарезервирован в SAGUR, но сообщение гостю не ставится в
    очередь. Эта функция является вторым шагом: vtelemax подтвердил карточку
    купона, значит гостю можно отправлять уведомление.
    """
    try:
        safe_assignment_id = int(assignment_id)
    except (TypeError, ValueError):
        return 0
    if safe_assignment_id <= 0:
        return 0

    current_now = now or timezone.now()
    with transaction.atomic():
        assignment = (
            _autoscenario_assignments_for_update_queryset()
            .select_related("guest", "scenario", "scenario__template", "coupon", "run", "config")
            .filter(id=safe_assignment_id)
            .first()
        )
        if assignment is None:
            return 0
        if assignment.status != CouponAutoscenarioAssignment.Status.RESERVED:
            _refresh_autoscenario_run_status(run_id=assignment.run_id)
            return 0
        if assignment.vtelemax_sync_status != CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK:
            return 0
        if assignment.guest_id is None or assignment.guest is None:
            _mark_autoscenario_assignment_dispatch_error(
                assignment=assignment,
                error_text="Нельзя создать отправку: у назначения нет гостя.",
            )
            return 0

        dedupe_key = _autoscenario_dispatch_dedupe_key(assignment_id=assignment.id)
        existing_tasks = _autoscenario_dispatch_task_count(
            scenario=assignment.scenario,
            dedupe_key=dedupe_key,
        )
        if existing_tasks > 0:
            _mark_autoscenario_assignment_sent(assignment=assignment, sent_at=current_now)
            _refresh_autoscenario_run_status(run_id=assignment.run_id)
            return 0

        payload = _build_autoscenario_dispatch_payload(assignment=assignment)
        template_context = _build_autoscenario_template_context(
            coupon_code=assignment.coupon_code,
            coupon_series=assignment.coupon_series,
            venue_code=assignment.venue_code or "",
            venue_name=assignment.venue_name or "",
            valid_until=assignment.lifetime_expires_at,
        )
        try:
            created_count = create_notification_event(
                scenario_code=assignment.scenario.code,
                guest=assignment.guest,
                dedupe_key=dedupe_key,
                source_ref=f"coupon_autoscenario_assignment:{assignment.id}",
                event_source_type=NotificationEvent.SourceType.SCHEDULE,
                task_source_type=DispatchTask.SourceType.SYSTEM,
                payload=payload,
                template_context=template_context,
                fallback_message_text=assignment.promo_text or "",
                event_at=current_now,
                coupon_code=assignment.coupon_code,
                coupon_external_id=f"{assignment.coupon_series}:{assignment.coupon_code}",
                coupon_expires_at=assignment.lifetime_expires_at,
                allow_inactive_scenario=(
                    assignment.config.execution_mode == CouponAutomationConfig.ExecutionMode.PILOT
                ),
            )
        except ScenarioNotConfiguredError as exc:
            _mark_autoscenario_assignment_dispatch_error(
                assignment=assignment,
                error_text=str(exc),
            )
            return 0

        total_tasks = _autoscenario_dispatch_task_count(
            scenario=assignment.scenario,
            dedupe_key=dedupe_key,
        )
        if created_count > 0 or total_tasks > 0:
            _mark_autoscenario_assignment_sent(assignment=assignment, sent_at=current_now)
        else:
            event = NotificationEvent.objects.filter(
                scenario=assignment.scenario,
                dedupe_key=dedupe_key,
            ).first()
            error_text = (
                getattr(event, "error_text", None)
                or "Не удалось поставить задачу отправки после ACK vtelemax."
            )
            _mark_autoscenario_assignment_dispatch_error(
                assignment=assignment,
                error_text=error_text,
            )
        _refresh_autoscenario_run_status(run_id=assignment.run_id)
        return int(created_count or 0)


def _autoscenario_assignments_for_update_queryset():
    """
    Блокирует только строку назначения автосценария, не nullable-связи из select_related.
    """
    return CouponAutoscenarioAssignment.objects.select_for_update(of=("self",))


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


def _resolve_pilot_recipient_filter(*, config: CouponAutomationConfig) -> PilotRecipientFilter:
    if config.execution_mode != CouponAutomationConfig.ExecutionMode.PILOT:
        return PilotRecipientFilter()

    settings = config.settings if isinstance(config.settings, dict) else {}

    phones: list[str] = []
    for key in PILOT_PHONE_SETTINGS_KEYS:
        for value in _iter_setting_values(settings.get(key)):
            normalized = normalize_phone_e164(value)
            if normalized:
                phones.append(normalized)

    guest_ids: list[int] = []
    for key in PILOT_GUEST_ID_SETTINGS_KEYS:
        for value in _iter_setting_values(settings.get(key)):
            try:
                guest_id = int(value)
            except (TypeError, ValueError):
                continue
            if guest_id > 0:
                guest_ids.append(guest_id)

    unique_phones = tuple(dict.fromkeys(phones))
    unique_guest_ids = tuple(dict.fromkeys(guest_ids))
    used_default_phone = False
    if (
        config.execution_mode == CouponAutomationConfig.ExecutionMode.PILOT
        and not unique_phones
        and not unique_guest_ids
    ):
        unique_phones = (DEFAULT_PILOT_PHONE_E164,)
        used_default_phone = True

    return PilotRecipientFilter(
        phones=unique_phones,
        guest_ids=unique_guest_ids,
        used_default_phone=used_default_phone,
    )


def _iter_setting_values(value) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    if isinstance(value, str):
        normalized = value.replace(";", ",").replace("\n", ",")
        return tuple(part.strip() for part in normalized.split(",") if part.strip())
    return (value,)


def _is_allowed_by_pilot_filter(
    *,
    row: CouponAutoscenarioAudienceRow,
    config: CouponAutomationConfig,
    pilot_filter: PilotRecipientFilter,
) -> bool:
    if config.execution_mode != CouponAutomationConfig.ExecutionMode.PILOT:
        return True
    if pilot_filter.is_empty:
        return False
    if int(row.guest_id) in set(pilot_filter.guest_ids):
        return True
    normalized_phone = normalize_phone_e164(row.phone)
    return bool(normalized_phone and normalized_phone in set(pilot_filter.phones))


def _append_unmatched_pilot_rows_if_requested(
    *,
    matched_rows: list[CouponAutoscenarioAudienceRow],
    config: CouponAutomationConfig,
    pilot_filter: PilotRecipientFilter,
) -> tuple[list[CouponAutoscenarioAudienceRow], int]:
    if config.execution_mode != CouponAutomationConfig.ExecutionMode.PILOT:
        return matched_rows, 0
    if not _settings_bool(config.settings, PILOT_INCLUDE_UNMATCHED_SETTINGS_KEYS):
        return matched_rows, 0
    if pilot_filter.is_empty:
        return matched_rows, 0

    existing_guest_ids = {row.guest_id for row in matched_rows}
    filters = Q()
    if pilot_filter.guest_ids:
        filters |= Q(id__in=pilot_filter.guest_ids)
    if pilot_filter.phones:
        filters |= Q(phone__in=pilot_filter.phones)
    if not filters:
        return matched_rows, 0

    last_visit_subquery = (
        VisitHistory.objects.filter(guest_id=OuterRef("pk"))
        .order_by("-visit_date")
        .values("visit_date")[:1]
    )
    guests = list(
        Guest.objects.annotate(last_visit_at=Subquery(last_visit_subquery))
        .filter(filters)
        .order_by("id")
    )
    channels_map = _build_sendable_channels_map(guest_ids=[int(guest.id) for guest in guests])

    extra_rows: list[CouponAutoscenarioAudienceRow] = []
    for guest in guests:
        guest_id = int(guest.id)
        if guest_id in existing_guest_ids:
            continue
        extra_rows.append(
            CouponAutoscenarioAudienceRow(
                guest_id=guest_id,
                phone=str(guest.phone or ""),
                first_name=str(guest.first_name or ""),
                last_name=str(guest.last_name or ""),
                last_visit_at=getattr(guest, "last_visit_at", None),
                sendable_channels=tuple(channels_map.get(guest_id, ())),
            )
        )
        existing_guest_ids.add(guest_id)

    if not extra_rows:
        return matched_rows, 0
    return [*matched_rows, *extra_rows], len(extra_rows)


def _settings_bool(settings, keys: tuple[str, ...]) -> bool:
    payload = settings if isinstance(settings, dict) else {}
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "да"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "нет"}:
            return False
    return False


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


def _is_coupon_still_available(*, coupon: CouponRegistryEntry, config: CouponAutomationConfig) -> bool:
    if not bool(coupon.is_active):
        return False
    if coupon.pool_status != CouponRegistryEntry.PoolStatus.VERIFIED_LOADED:
        return False
    if str(coupon.series or "").strip() != str(config.coupon_series or "").strip():
        return False
    venue_code = str(config.venue_code or "").strip()
    coupon_venue_code = str(coupon.venue_code or "").strip()
    if is_coupon_global_venue(venue_code):
        return coupon_venue_code in {"", COUPON_VENUE_GLOBAL_CODE}
    if venue_code:
        return coupon_venue_code == venue_code
    return True


def _build_primary_channel_map(*, guest_ids: list[int]) -> dict[int, VtelemaxRecipientChannel]:
    if not guest_ids:
        return {}
    result: dict[int, VtelemaxRecipientChannel] = {}
    for channel in (
        VtelemaxRecipientChannel.objects.filter(guest_id__in=guest_ids)
        .order_by("guest_id", "platform", "id")
        .only(
            "guest_id",
            "person_id",
            "platform",
            "phone_e164",
            "external_id",
            "is_registered",
            "notifications_allowed",
        )
    ):
        guest_id = int(channel.guest_id)
        if guest_id in result:
            continue
        if not _is_channel_sendable(channel):
            continue
        result[guest_id] = channel
    return result


def _render_autoscenario_coupon_text(
    *,
    scenario: NotificationScenario,
    config: CouponAutomationConfig,
    guest: Guest,
    coupon_code: str,
    coupon_series: str,
    venue_code: str,
    venue_name: str,
    valid_until: datetime,
) -> str:
    template_text = str(config.coupon_promo_text_template or "").strip()
    if not template_text:
        template_text = str(getattr(scenario.template, "message_text", "") or "").strip()
    if not template_text:
        return ""
    return render_message_for_guest(
        template_text,
        guest,
        extra_context=_build_autoscenario_template_context(
            coupon_code=coupon_code,
            coupon_series=coupon_series,
            venue_code=venue_code,
            venue_name=venue_name,
            valid_until=valid_until,
        ),
    )


def _build_autoscenario_template_context(
    *,
    coupon_code: str,
    coupon_series: str,
    venue_code: str,
    venue_name: str,
    valid_until: datetime | None,
) -> dict[str, str]:
    return {
        "coupon_code": str(coupon_code or "").strip(),
        "coupon_series": str(coupon_series or "").strip(),
        "coupon_venue_code": str(venue_code or "").strip(),
        "coupon_venue_name": str(venue_name or "").strip(),
        "coupon_expires_at": timezone.localtime(valid_until).strftime("%d.%m.%Y") if valid_until else "",
        "valid_until": _format_valid_until(valid_until) or "",
    }


def _autoscenario_dispatch_dedupe_key(*, assignment_id: int) -> str:
    return f"coupon_autoscenario_assignment:{int(assignment_id)}"


def _autoscenario_dispatch_task_count(
    *,
    scenario: NotificationScenario,
    dedupe_key: str,
) -> int:
    source_key_fragment = f"{scenario.code}:{dedupe_key}"
    return DispatchTask.objects.filter(
        notification_scenario=scenario,
        source_type=DispatchTask.SourceType.SYSTEM,
        idempotency_key__contains=source_key_fragment,
    ).count()


def _build_autoscenario_dispatch_payload(*, assignment: CouponAutoscenarioAssignment) -> dict:
    return {
        "source": "coupon_autoscenario",
        "autoscenario_run_id": int(assignment.run_id),
        "autoscenario_assignment_id": int(assignment.id),
        "scenario_id": int(assignment.scenario_id),
        "scenario_code": assignment.scenario.code,
        "coupon_series": assignment.coupon_series,
        "coupon_code": assignment.coupon_code,
        "coupon_venue_code": assignment.venue_code,
        "coupon_venue_name": assignment.venue_name,
        "coupon_valid_until": _format_valid_until(assignment.lifetime_expires_at),
    }


def _mark_autoscenario_assignment_sent(
    *,
    assignment: CouponAutoscenarioAssignment,
    sent_at: datetime,
) -> None:
    assignment.status = CouponAutoscenarioAssignment.Status.SENT
    assignment.sent_at = sent_at
    assignment.save(update_fields=["status", "sent_at", "updated_at"])


def _mark_autoscenario_assignment_dispatch_error(
    *,
    assignment: CouponAutoscenarioAssignment,
    error_text: str,
) -> None:
    assignment.status = CouponAutoscenarioAssignment.Status.ERROR
    assignment.vtelemax_sync_error = str(error_text or "").strip()[:2000]
    assignment.save(update_fields=["status", "vtelemax_sync_error", "updated_at"])
    _refresh_autoscenario_run_status(run_id=assignment.run_id)


def _refresh_autoscenario_run_status(*, run_id: int | None) -> None:
    if not run_id:
        return
    run = CouponAutoscenarioRun.objects.select_for_update().filter(id=run_id).first()
    if run is None:
        return
    assignments = CouponAutoscenarioAssignment.objects.filter(run=run)
    if assignments.filter(status=CouponAutoscenarioAssignment.Status.ERROR).exists():
        next_status = CouponAutoscenarioRun.Status.ERROR
    elif assignments.filter(
        vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING
    ).exists() or assignments.filter(status=CouponAutoscenarioAssignment.Status.RESERVED).exists():
        next_status = CouponAutoscenarioRun.Status.SYNC_PENDING
    elif assignments.exists():
        next_status = CouponAutoscenarioRun.Status.COMPLETED
    else:
        next_status = CouponAutoscenarioRun.Status.PLANNED
    if run.status != next_status:
        run.status = next_status
        run.save(update_fields=["status", "updated_at"])


def _format_valid_until(value) -> str | None:
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return timezone.localtime(value).isoformat(timespec="seconds")


def _build_autoscenario_assignment_payload(
    *,
    run: CouponAutoscenarioRun,
    assignment: CouponAutoscenarioAssignment,
) -> dict:
    return {
        "source": "autoscenario",
        "autoscenario_run_id": int(run.id),
        "scenario_id": int(assignment.scenario_id),
        "scenario_code": assignment.scenario.code,
        "assignment_id": int(assignment.id),
        "guest_id": int(assignment.guest_id) if assignment.guest_id else None,
        "person_id": str(assignment.person_id) if assignment.person_id else None,
        "phone_e164": assignment.phone_e164,
        "coupon_series": assignment.coupon_series,
        "coupon_code": assignment.coupon_code,
        "valid_until": _format_valid_until(assignment.lifetime_expires_at),
        "venue_code": assignment.venue_code,
        "venue_name": assignment.venue_name,
        "promo_text": assignment.promo_text,
        "status": assignment.status,
        "vtelemax_sync_status": assignment.vtelemax_sync_status,
    }


def _active_assignment_guest_ids(*, guest_ids: list[int], coupon_series: str) -> set[int]:
    if not guest_ids or not coupon_series:
        return set()
    campaign_guest_ids = set(
        CouponCampaignAssignment.objects.filter(
            guest_id__in=guest_ids,
            coupon_series=coupon_series,
            status__in=[
                CouponCampaignAssignment.Status.RESERVED,
                CouponCampaignAssignment.Status.SENT,
            ],
        ).values_list("guest_id", flat=True)
    )
    autoscenario_guest_ids = set(
        CouponAutoscenarioAssignment.objects.filter(
            guest_id__in=guest_ids,
            coupon_series=coupon_series,
            status__in=[
                CouponAutoscenarioAssignment.Status.RESERVED,
                CouponAutoscenarioAssignment.Status.SENT,
            ],
        ).values_list("guest_id", flat=True)
    )
    return campaign_guest_ids | autoscenario_guest_ids


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
    campaign_guest_ids = set(
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
    autoscenario_guest_ids = set(
        CouponAutoscenarioAssignment.objects.filter(
            guest_id__in=guest_ids,
            coupon_series=coupon_series,
            assigned_at__gte=cutoff,
            status__in=[
                CouponAutoscenarioAssignment.Status.SENT,
                CouponAutoscenarioAssignment.Status.USED,
                CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN,
                CouponAutoscenarioAssignment.Status.EXPIRED,
            ],
        ).values_list("guest_id", flat=True)
    )
    return campaign_guest_ids | autoscenario_guest_ids
