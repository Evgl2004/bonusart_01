from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.db.models import F, OuterRef, Q, QuerySet, Subquery, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from guests.models import (
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponAutomationConfig,
    CouponAutomationRule,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    NotificationEvent,
    NotificationScenario,
    OrderFact,
    VtelemaxRecipientChannel,
)
from guests.services.coupon_constants import COUPON_VENUE_GLOBAL_CODE, is_coupon_global_venue
from guests.services.notification_events import ScenarioNotConfiguredError, create_notification_event
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON
from guests.services.notification_scenarios import _extract_inactive_days
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
PILOT_DAYS_WITHOUT_VISITS_SETTINGS_KEYS = (
    "pilot_days_without_visits",
    "test_days_without_visits",
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
    is_pilot_forced: bool = False

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
            "is_pilot_forced": self.is_pilot_forced,
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
    last_visit_at: datetime | None = None
    days_without_visits: int | None = None
    is_pilot_forced: bool = False
    coupon_rule_id: int | None = None
    coupon_rule_label: str = ""
    coupon_selection_source: str = ""
    last_order_department_id: str = ""
    last_order_department_name: str = ""

    def as_dict(self) -> dict:
        valid_until_local = timezone.localtime(self.valid_until)
        last_visit_local = timezone.localtime(self.last_visit_at) if self.last_visit_at else None
        if self.days_without_visits is None:
            days_without_visits_label = "—"
        elif self.is_pilot_forced:
            days_without_visits_label = f"{self.days_without_visits} (пилотное значение)"
        else:
            days_without_visits_label = str(self.days_without_visits)
        selection_source_labels = {
            "last_order_department": "по последнему заведению",
            "global_fallback": "резерв: Вся сеть (global)",
            "legacy_config": "старый режим настройки",
            "pilot_rule_fallback": "пилотный резерв",
        }
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
            "valid_until_display": valid_until_local.strftime("%d.%m.%Y %H:%M"),
            "last_visit_at": self.last_visit_at.isoformat() if self.last_visit_at else None,
            "last_visit_at_display": last_visit_local.strftime("%d.%m.%Y %H:%M") if last_visit_local else "—",
            "days_without_visits": self.days_without_visits,
            "days_without_visits_label": days_without_visits_label,
            "is_pilot_forced": self.is_pilot_forced,
            "coupon_rule_id": self.coupon_rule_id,
            "coupon_rule_label": self.coupon_rule_label,
            "coupon_selection_source": self.coupon_selection_source,
            "coupon_selection_source_display": selection_source_labels.get(
                self.coupon_selection_source,
                self.coupon_selection_source,
            ),
            "last_order_department_id": self.last_order_department_id,
            "last_order_department_name": self.last_order_department_name,
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
            "segment_matched_guests": max(self.matched_guests - self.pilot_forced_guests, 0),
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
class CouponAutoscenarioCleanupResult:
    assignment_id: int
    queue_event_id: int
    queue_event_created: bool
    coupon_series: str
    coupon_code: str


@dataclass(frozen=True, slots=True)
class PilotRecipientFilter:
    phones: tuple[str, ...] = field(default_factory=tuple)
    guest_ids: tuple[int, ...] = field(default_factory=tuple)
    used_default_phone: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.phones and not self.guest_ids


@dataclass(frozen=True, slots=True)
class CouponRuleOption:
    key: str
    coupon_series: str
    venue_code: str
    venue_name: str
    scope_type: str
    priority: int
    rule_id: int | None = None
    coupon_validity_days: int | None = None
    coupon_promo_text_template: str = ""
    is_legacy_fallback: bool = False

    @property
    def is_global(self) -> bool:
        return self.scope_type == CouponAutomationRule.ScopeType.GLOBAL or is_coupon_global_venue(self.venue_code)

    @property
    def label(self) -> str:
        if self.is_global:
            return "Вся сеть (global)"
        return self.venue_name or self.venue_code or "-"


@dataclass(frozen=True, slots=True)
class GuestLastOrderVenue:
    department_id: str
    department_name: str
    business_date: date | None = None
    first_seen_at: datetime | None = None
    last_visit_at: datetime | None = None


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
    coupon_rules = _effective_coupon_rules(config=config)
    available_coupons = sum(
        len(coupons)
        for coupons in _available_coupons_by_rule(
            rules=coupon_rules,
            limit=safe_limit,
        ).values()
    )
    coupon_shortage = max(planned_recipients_for_run - available_coupons, 0)

    if not coupon_rules:
        warnings.append("Не настроено ни одно купонное правило; назначение купонов невозможно.")
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
        coupon_series=_format_coupon_series_summary(coupon_rules),
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

    coupon_rules = _effective_coupon_rules(config=config)
    coupon_series_values = tuple(dict.fromkeys(rule.coupon_series for rule in coupon_rules if rule.coupon_series))
    if not coupon_rules:
        blockers.append("Не настроено ни одно купонное правило.")

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
        coupon_series=coupon_series_values,
    )
    cooldown_guest_ids = _cooldown_guest_ids(
        guest_ids=[row.guest_id for row in sendable_rows],
        coupon_series=coupon_series_values,
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

    last_order_venues = _last_order_venue_map(guest_ids=[row.guest_id for row in eligible_rows])
    coupons_by_rule = _available_coupons_by_rule(rules=coupon_rules, limit=safe_limit)
    available_coupons_count = sum(len(coupons) for coupons in coupons_by_rule.values())
    planned_recipients = min(len(eligible_rows), safe_limit)
    plan_items: list[CouponAutoscenarioPlanItem] = []
    for row in eligible_rows[:planned_recipients]:
        rule, coupon, selection_source = _select_coupon_for_row(
            row=row,
            rules=coupon_rules,
            coupons_by_rule=coupons_by_rule,
            last_order_venues=last_order_venues,
        )
        if rule is None or coupon is None:
            continue
        venue_code = str(coupon.venue_code or rule.venue_code or "").strip()
        venue_name = str(coupon.venue_name or rule.venue_name or "").strip()
        validity_days = rule.coupon_validity_days or config.coupon_validity_days or 1
        valid_until = current_now + timedelta(days=max(1, int(validity_days)))
        days_without_visits = _days_without_visits_from_last_visit(
            last_visit_at=row.last_visit_at,
            now=current_now,
        )
        if days_without_visits is None and config.execution_mode == CouponAutomationConfig.ExecutionMode.PILOT:
            days_without_visits = _resolve_pilot_days_without_visits(
                config=config,
                default_days=inactive_days,
            )
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
                last_visit_at=row.last_visit_at,
                days_without_visits=days_without_visits,
                is_pilot_forced=row.is_pilot_forced,
                coupon_rule_id=rule.rule_id,
                coupon_rule_label=rule.label,
                coupon_selection_source=selection_source,
                last_order_department_id=last_order_venues.get(
                    row.guest_id,
                    GuestLastOrderVenue("", ""),
                ).department_id,
                last_order_department_name=last_order_venues.get(
                    row.guest_id,
                    GuestLastOrderVenue("", ""),
                ).department_name,
            )
        )

    coupon_shortage = max(planned_recipients - len(plan_items), 0)
    if coupon_shortage > 0:
        blockers.append(f"Не хватает подходящих купонов для ближайшего запуска: {coupon_shortage}.")

    if planned_recipients == 0:
        warnings.append("Нет достижимых гостей для ближайшего запуска после фильтров.")

    can_execute = not blockers and bool(plan_items)
    return CouponAutoscenarioExecutionPlan(
        scenario_id=int(scenario.id),
        scenario_code=scenario.code,
        execution_mode=config.execution_mode,
        coupon_series=_format_coupon_series_summary(coupon_rules),
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
            if not _is_coupon_available_for_plan_item(coupon=coupon, item=item):
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
                now=current_now,
                days_without_visits=item.days_without_visits,
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
                coupon_rule_id=item.coupon_rule_id,
                coupon_selection_source=item.coupon_selection_source or None,
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
                    days_without_visits=item.days_without_visits,
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


def cleanup_coupon_autoscenario_pilot_assignment(
    *,
    assignment_id: int,
    reason: str = "pilot_cleanup_from_ui",
    now: datetime | None = None,
) -> CouponAutoscenarioCleanupResult:
    """
    Ставит пилотное назначение автосценария на безопасную очистку.

    Функция не возвращает купон в пул мгновенно. Она создаёт
    `status_update:canceled` с `release_to_pool=true`; фактический release
    выполняется общим post-ACK механизмом после подтверждения vtelemax.
    """
    try:
        safe_assignment_id = int(assignment_id)
    except (TypeError, ValueError) as exc:
        raise CouponAutoscenarioPreviewError("Некорректный id назначения автосценария.") from exc
    if safe_assignment_id <= 0:
        raise CouponAutoscenarioPreviewError("Некорректный id назначения автосценария.")

    current_now = now or timezone.now()
    with transaction.atomic():
        assignment = (
            _autoscenario_assignments_for_update_queryset()
            .select_related("coupon", "scenario", "run", "config", "guest")
            .filter(id=safe_assignment_id)
            .first()
        )
        if assignment is None:
            raise CouponAutoscenarioPreviewError(
                f"Назначение автосценария #{safe_assignment_id} не найдено."
            )
        if assignment.config.execution_mode != CouponAutomationConfig.ExecutionMode.PILOT:
            raise CouponAutoscenarioPreviewError(
                "Очистка из UI разрешена только для автосценариев в режиме 'Пилот'."
            )
        if assignment.status in {
            CouponAutoscenarioAssignment.Status.USED,
            CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN,
        }:
            raise CouponAutoscenarioPreviewError(
                "Нельзя очистить пилот: купон уже отмечен использованным."
            )
        if assignment.status not in {
            CouponAutoscenarioAssignment.Status.RESERVED,
            CouponAutoscenarioAssignment.Status.SENT,
            CouponAutoscenarioAssignment.Status.CANCELED,
        }:
            raise CouponAutoscenarioPreviewError(
                f"Нельзя очистить пилот из статуса '{assignment.status}'."
            )
        if (
            assignment.status != CouponAutoscenarioAssignment.Status.CANCELED
            and assignment.vtelemax_sync_status != CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK
        ):
            raise CouponAutoscenarioPreviewError(
                "Нельзя очистить пилот: assignment-событие ещё не подтверждено vtelemax."
            )

        if assignment.status != CouponAutoscenarioAssignment.Status.CANCELED:
            assignment.status = CouponAutoscenarioAssignment.Status.CANCELED
            assignment.vtelemax_sync_status = CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING
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

        payload = _build_autoscenario_status_update_payload(
            assignment=assignment,
            status=CouponAutoscenarioAssignment.Status.CANCELED,
            now=current_now,
            meta={
                "cancel_reason": str(reason or "pilot_cleanup_from_ui"),
                "remove_from_guest": True,
                "release_to_pool": True,
            },
        )
        existing_event = _find_autoscenario_status_update_event(
            assignment=assignment,
            status=CouponAutoscenarioAssignment.Status.CANCELED,
        )
        if existing_event is None:
            event = CouponVtelemaxSyncQueue.objects.create(
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
                autoscenario_assignment=assignment,
                payload_json=payload,
                status=CouponVtelemaxSyncQueue.Status.PENDING,
                next_retry_at=current_now,
            )
            created = True
        else:
            event = existing_event
            created = False
            if event.status != CouponVtelemaxSyncQueue.Status.ACKED:
                event.payload_json = payload
                event.status = CouponVtelemaxSyncQueue.Status.PENDING
                event.last_error = None
                event.next_retry_at = current_now
                event.sent_at = None
                event.ack_at = None
                event.save(
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
        _refresh_autoscenario_run_status(run_id=assignment.run_id)

    return CouponAutoscenarioCleanupResult(
        assignment_id=int(assignment.id),
        queue_event_id=int(event.id),
        queue_event_created=created,
        coupon_series=assignment.coupon_series,
        coupon_code=assignment.coupon_code,
    )


def create_autoscenario_dispatch_after_vtelemax_ack(
    *,
    assignment_id: int,
    now: datetime | None = None,
    days_without_visits: int | None = None,
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
            days_without_visits=(
                days_without_visits
                if days_without_visits is not None
                else _calculate_days_without_visits(
                    guest_id=int(assignment.guest_id),
                    now=current_now,
                )
            ),
        )
        is_pilot_execution = assignment.config.execution_mode == CouponAutomationConfig.ExecutionMode.PILOT
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
                allow_inactive_scenario=is_pilot_execution,
                planned_send_at_override=current_now if is_pilot_execution else None,
                skip_send_limits=is_pilot_execution,
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


def _latest_order_fact_query_for_guest() -> QuerySet:
    return OrderFact.objects.filter(guest_id=OuterRef("pk")).order_by(
        F("business_date").desc(nulls_last=True),
        F("first_seen_at").desc(nulls_last=True),
        F("id").desc(),
    )


def _order_fact_visit_datetime(
    *,
    business_date: date | datetime | None,
    first_seen_at: datetime | None = None,
) -> datetime | None:
    if business_date is not None:
        if isinstance(business_date, datetime):
            if timezone.is_aware(business_date):
                return business_date
            return timezone.make_aware(business_date, timezone.get_current_timezone())
        if isinstance(business_date, date):
            return timezone.make_aware(
                datetime.combine(business_date, time.min),
                timezone.get_current_timezone(),
            )
    if first_seen_at is None:
        return None
    if timezone.is_aware(first_seen_at):
        return first_seen_at
    return timezone.make_aware(first_seen_at, timezone.get_current_timezone())


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

    guests = list(Guest.objects.filter(filters).order_by("id"))
    guest_ids = [int(guest.id) for guest in guests]
    channels_map = _build_sendable_channels_map(guest_ids=guest_ids)
    last_order_venues = _last_order_venue_map(guest_ids=guest_ids)
    last_order_visits = _last_order_visit_at_map(guest_ids=guest_ids)

    extra_rows: list[CouponAutoscenarioAudienceRow] = []
    for guest in guests:
        guest_id = int(guest.id)
        if guest_id in existing_guest_ids:
            continue
        last_order_venue = last_order_venues.get(guest_id)
        extra_rows.append(
            CouponAutoscenarioAudienceRow(
                guest_id=guest_id,
                phone=str(guest.phone or ""),
                first_name=str(guest.first_name or ""),
                last_name=str(guest.last_name or ""),
                last_visit_at=last_order_visits.get(guest_id)
                or (last_order_venue.last_visit_at if last_order_venue else None),
                sendable_channels=tuple(channels_map.get(guest_id, ())),
                is_pilot_forced=True,
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
    cutoff_date = timezone.localtime(now).date() - timedelta(days=max(1, int(inactive_days)))
    latest_order_query = _latest_order_fact_query_for_guest()
    candidates = list(
        Guest.objects.annotate(
            last_order_business_date=Subquery(latest_order_query.values("business_date")[:1]),
            last_order_first_seen_at=Subquery(latest_order_query.values("first_seen_at")[:1]),
        )
        .filter(last_order_business_date__isnull=False, last_order_business_date__lte=cutoff_date)
        .order_by("id")[: max(1, int(scan_limit))]
    )
    channels_map = _build_sendable_channels_map(guest_ids=[candidate.id for candidate in candidates])

    matched_rows: list[CouponAutoscenarioAudienceRow] = []
    scanned_guests = 0
    for guest in candidates:
        scanned_guests += 1
        last_visit_at = _order_fact_visit_datetime(
            business_date=getattr(guest, "last_order_business_date", None),
            first_seen_at=getattr(guest, "last_order_first_seen_at", None),
        )
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


def _effective_coupon_rules(*, config: CouponAutomationConfig) -> tuple[CouponRuleOption, ...]:
    rules = list(
        config.coupon_rules.filter(is_active=True)
        .exclude(coupon_series="")
        .order_by("priority", "id")
    )
    if rules:
        result: list[CouponRuleOption] = []
        for rule in rules:
            scope_type = str(rule.scope_type or "").strip()
            venue_code = str(rule.venue_code or "").strip()
            venue_name = str(rule.venue_name or "").strip()
            if scope_type == CouponAutomationRule.ScopeType.GLOBAL:
                venue_code = COUPON_VENUE_GLOBAL_CODE
                venue_name = venue_name or "Вся сеть"
            result.append(
                CouponRuleOption(
                    key=f"rule:{rule.id}",
                    rule_id=int(rule.id),
                    coupon_series=str(rule.coupon_series or "").strip(),
                    venue_code=venue_code,
                    venue_name=venue_name,
                    scope_type=scope_type,
                    priority=int(rule.priority or 100),
                    coupon_validity_days=rule.coupon_validity_days,
                    coupon_promo_text_template=str(rule.coupon_promo_text_template or "").strip(),
                )
            )
        return tuple(result)

    coupon_series = str(config.coupon_series or "").strip()
    if not coupon_series:
        return tuple()

    venue_code = str(config.venue_code or "").strip()
    venue_name = str(config.venue_name or "").strip()
    scope_type = (
        CouponAutomationRule.ScopeType.GLOBAL
        if is_coupon_global_venue(venue_code)
        else CouponAutomationRule.ScopeType.VENUE
    )
    if scope_type == CouponAutomationRule.ScopeType.GLOBAL:
        venue_code = COUPON_VENUE_GLOBAL_CODE
        venue_name = venue_name or "Вся сеть"
    return (
        CouponRuleOption(
            key=f"legacy:{config.id}",
            coupon_series=coupon_series,
            venue_code=venue_code,
            venue_name=venue_name,
            scope_type=scope_type,
            priority=100,
            coupon_validity_days=config.coupon_validity_days,
            coupon_promo_text_template=str(config.coupon_promo_text_template or "").strip(),
            is_legacy_fallback=True,
        ),
    )


def _format_coupon_series_summary(rules: tuple[CouponRuleOption, ...]) -> str:
    series_values = tuple(dict.fromkeys(rule.coupon_series for rule in rules if rule.coupon_series))
    if not series_values:
        return ""
    if len(series_values) == 1:
        return series_values[0]
    visible = ", ".join(series_values[:3])
    if len(series_values) > 3:
        visible = f"{visible}, ..."
    return visible


def _available_coupon_queryset_for_rule(*, rule: CouponRuleOption) -> QuerySet[CouponRegistryEntry]:
    if not rule.coupon_series:
        return CouponRegistryEntry.objects.none()
    queryset = CouponRegistryEntry.objects.filter(
        series=rule.coupon_series,
        is_active=True,
        pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
    )
    if rule.is_global:
        queryset = queryset.filter(
            Q(venue_code=COUPON_VENUE_GLOBAL_CODE) | Q(venue_code__isnull=True) | Q(venue_code="")
        )
    elif rule.venue_code:
        queryset = queryset.filter(venue_code=rule.venue_code)
    return queryset.order_by("id")


def _available_coupons_by_rule(
    *,
    rules: tuple[CouponRuleOption, ...],
    limit: int,
) -> dict[str, list[CouponRegistryEntry]]:
    result: dict[str, list[CouponRegistryEntry]] = {}
    used_coupon_ids: set[int] = set()
    safe_limit = max(1, int(limit or 1))
    for rule in rules:
        coupons: list[CouponRegistryEntry] = []
        for coupon in _available_coupon_queryset_for_rule(rule=rule)[:safe_limit]:
            coupon_id = int(coupon.id)
            if coupon_id in used_coupon_ids:
                continue
            used_coupon_ids.add(coupon_id)
            coupons.append(coupon)
        result[rule.key] = coupons
    return result


def _last_order_venue_map(*, guest_ids: list[int]) -> dict[int, GuestLastOrderVenue]:
    unique_guest_ids = [int(guest_id) for guest_id in dict.fromkeys(guest_ids) if guest_id]
    if not unique_guest_ids:
        return {}

    latest_rows = (
        OrderFact.objects.filter(guest_id__in=unique_guest_ids)
        .exclude(department_id="")
        .annotate(
            rn=Window(
                expression=RowNumber(),
                partition_by=[F("guest_id")],
                order_by=[
                    F("business_date").desc(nulls_last=True),
                    F("first_seen_at").desc(nulls_last=True),
                    F("id").desc(),
                ],
            )
        )
        .filter(rn=1)
        .values("guest_id", "department_id", "department_name", "business_date", "first_seen_at")
    )
    return {
        int(row["guest_id"]): GuestLastOrderVenue(
            department_id=str(row.get("department_id") or "").strip(),
            department_name=str(row.get("department_name") or "").strip(),
            business_date=row.get("business_date"),
            first_seen_at=row.get("first_seen_at"),
            last_visit_at=_order_fact_visit_datetime(
                business_date=row.get("business_date"),
                first_seen_at=row.get("first_seen_at"),
            ),
        )
        for row in latest_rows
    }


def _last_order_visit_at_map(*, guest_ids: list[int]) -> dict[int, datetime]:
    unique_guest_ids = [int(guest_id) for guest_id in dict.fromkeys(guest_ids) if guest_id]
    if not unique_guest_ids:
        return {}

    latest_rows = (
        OrderFact.objects.filter(guest_id__in=unique_guest_ids)
        .annotate(
            rn=Window(
                expression=RowNumber(),
                partition_by=[F("guest_id")],
                order_by=[
                    F("business_date").desc(nulls_last=True),
                    F("first_seen_at").desc(nulls_last=True),
                    F("id").desc(),
                ],
            )
        )
        .filter(rn=1)
        .values("guest_id", "business_date", "first_seen_at")
    )
    result: dict[int, datetime] = {}
    for row in latest_rows:
        last_visit_at = _order_fact_visit_datetime(
            business_date=row.get("business_date"),
            first_seen_at=row.get("first_seen_at"),
        )
        if last_visit_at is not None:
            result[int(row["guest_id"])] = last_visit_at
    return result


def _select_coupon_for_row(
    *,
    row: CouponAutoscenarioAudienceRow,
    rules: tuple[CouponRuleOption, ...],
    coupons_by_rule: dict[str, list[CouponRegistryEntry]],
    last_order_venues: dict[int, GuestLastOrderVenue],
) -> tuple[CouponRuleOption | None, CouponRegistryEntry | None, str]:
    last_order_venue = last_order_venues.get(row.guest_id)
    last_department_id = str(last_order_venue.department_id if last_order_venue else "").strip()

    candidates: list[tuple[CouponRuleOption, str]] = []
    for rule in rules:
        if rule.is_legacy_fallback:
            candidates.append((rule, "legacy_config"))
        elif last_department_id and not rule.is_global and rule.venue_code == last_department_id:
            candidates.append((rule, "last_order_department"))
    for rule in rules:
        if rule.is_global:
            candidates.append((rule, "global_fallback"))
    if row.is_pilot_forced and not candidates:
        candidates.extend((rule, "pilot_rule_fallback") for rule in rules if not rule.is_global)

    seen_keys: set[str] = set()
    for rule, selection_source in candidates:
        if rule.key in seen_keys:
            continue
        seen_keys.add(rule.key)
        coupons = coupons_by_rule.get(rule.key) or []
        if coupons:
            return rule, coupons.pop(0), selection_source
    return None, None, ""


def _is_coupon_available_for_plan_item(
    *,
    coupon: CouponRegistryEntry,
    item: CouponAutoscenarioPlanItem,
) -> bool:
    if not bool(coupon.is_active):
        return False
    if coupon.pool_status != CouponRegistryEntry.PoolStatus.VERIFIED_LOADED:
        return False
    if str(coupon.series or "").strip() != item.coupon_series:
        return False
    expected_venue_code = str(item.venue_code or "").strip()
    coupon_venue_code = str(coupon.venue_code or "").strip()
    if is_coupon_global_venue(expected_venue_code):
        return coupon_venue_code in {"", COUPON_VENUE_GLOBAL_CODE}
    if expected_venue_code:
        return coupon_venue_code == expected_venue_code
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
    now: datetime | None = None,
    days_without_visits: int | None = None,
) -> str:
    template_text = str(config.coupon_promo_text_template or "").strip()
    if not template_text:
        template_text = str(getattr(scenario.template, "message_text", "") or "").strip()
    if not template_text:
        return ""
    current_now = now or timezone.now()
    return render_message_for_guest(
        template_text,
        guest,
        extra_context=_build_autoscenario_template_context(
            coupon_code=coupon_code,
            coupon_series=coupon_series,
            venue_code=venue_code,
            venue_name=venue_name,
            valid_until=valid_until,
            days_without_visits=(
                days_without_visits
                if days_without_visits is not None
                else _calculate_days_without_visits(
                    guest_id=int(guest.id),
                    now=current_now,
                )
            ),
        ),
    )


def _days_without_visits_from_last_visit(
    *,
    last_visit_at: datetime | None,
    now: datetime,
) -> int | None:
    if last_visit_at is None:
        return None
    return max(0, int((now - last_visit_at).days))


def _resolve_pilot_days_without_visits(
    *,
    config: CouponAutomationConfig,
    default_days: int,
) -> int:
    payload = config.settings if isinstance(config.settings, dict) else {}
    for key in PILOT_DAYS_WITHOUT_VISITS_SETTINGS_KEYS:
        if key not in payload:
            continue
        try:
            return max(0, int(payload.get(key)))
        except (TypeError, ValueError):
            continue
    return max(0, int(default_days or 0))


def _calculate_days_without_visits(*, guest_id: int | None, now: datetime | None = None) -> int | None:
    if not guest_id:
        return None
    current_now = now or timezone.now()
    last_visit_at = _last_order_visit_at_map(guest_ids=[int(guest_id)]).get(int(guest_id))
    if last_visit_at is None:
        return None
    return max(0, int((current_now - last_visit_at).days))


def _build_autoscenario_template_context(
    *,
    coupon_code: str,
    coupon_series: str,
    venue_code: str,
    venue_name: str,
    valid_until: datetime | None,
    days_without_visits: int | None = None,
) -> dict[str, str]:
    return {
        "coupon_code": str(coupon_code or "").strip(),
        "coupon_series": str(coupon_series or "").strip(),
        "coupon_venue_code": str(venue_code or "").strip(),
        "coupon_venue_name": str(venue_name or "").strip(),
        "coupon_expires_at": timezone.localtime(valid_until).strftime("%d.%m.%Y") if valid_until else "",
        "valid_until": _format_valid_until(valid_until) or "",
        "days_without_visits": str(days_without_visits) if days_without_visits is not None else "",
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
    days_without_visits: int | None = None,
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
        "days_without_visits": days_without_visits,
        "status": assignment.status,
        "vtelemax_sync_status": assignment.vtelemax_sync_status,
    }


def _build_autoscenario_status_update_payload(
    *,
    assignment: CouponAutoscenarioAssignment,
    status: str,
    now: datetime,
    meta: dict,
) -> dict:
    return {
        "source": "autoscenario",
        "autoscenario_run_id": int(assignment.run_id),
        "autoscenario_assignment_id": int(assignment.id),
        "scenario_id": int(assignment.scenario_id),
        "scenario_code": assignment.scenario.code,
        "assignment_id": int(assignment.id),
        "guest_id": int(assignment.guest_id) if assignment.guest_id else None,
        "person_id": str(assignment.person_id) if assignment.person_id else None,
        "phone_e164": assignment.phone_e164,
        "coupon_series": assignment.coupon_series,
        "coupon_code": assignment.coupon_code,
        "venue_code": assignment.venue_code,
        "venue_name": assignment.venue_name,
        "promo_text": assignment.promo_text,
        "status": status,
        "status_at": timezone.localtime(now).isoformat(timespec="seconds"),
        "meta": meta,
    }


def _find_autoscenario_status_update_event(
    *,
    assignment: CouponAutoscenarioAssignment,
    status: str,
) -> CouponVtelemaxSyncQueue | None:
    events = CouponVtelemaxSyncQueue.objects.filter(
        autoscenario_assignment=assignment,
        direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
    ).order_by("-id")
    for event in events:
        payload = event.payload_json if isinstance(event.payload_json, dict) else {}
        if str(payload.get("status") or "").strip() == status:
            return event
    return None


def _normalize_coupon_series_filter(coupon_series: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(coupon_series, str):
        values = [coupon_series]
    else:
        values = list(coupon_series or [])
    return tuple(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def _active_assignment_guest_ids(
    *,
    guest_ids: list[int],
    coupon_series: str | tuple[str, ...] | list[str],
) -> set[int]:
    series_values = _normalize_coupon_series_filter(coupon_series)
    if not guest_ids or not series_values:
        return set()
    campaign_guest_ids = set(
        CouponCampaignAssignment.objects.filter(
            guest_id__in=guest_ids,
            coupon_series__in=series_values,
            status__in=[
                CouponCampaignAssignment.Status.RESERVED,
                CouponCampaignAssignment.Status.SENT,
            ],
        ).values_list("guest_id", flat=True)
    )
    autoscenario_guest_ids = set(
        CouponAutoscenarioAssignment.objects.filter(
            guest_id__in=guest_ids,
            coupon_series__in=series_values,
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
    coupon_series: str | tuple[str, ...] | list[str],
    cooldown_days: int,
    now: datetime,
) -> set[int]:
    series_values = _normalize_coupon_series_filter(coupon_series)
    if not guest_ids or not series_values or cooldown_days <= 0:
        return set()
    cutoff = now - timedelta(days=cooldown_days)
    campaign_guest_ids = set(
        CouponCampaignAssignment.objects.filter(
            guest_id__in=guest_ids,
            coupon_series__in=series_values,
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
            coupon_series__in=series_values,
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
