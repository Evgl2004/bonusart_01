"""
Базовый каркас нового UI раздела рассылок (mailings-v2).

Задача этапа:
1. дать единую точку входа для маркетолога;
2. не ломать legacy формы, а использовать их как bridge;
3. показывать ключевые операционные метрики по текущему состоянию рассылок.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Max, Prefetch, Q
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DetailView, FormView, TemplateView, UpdateView

from guests.forms import (
    COUPON_CODE_PLACEHOLDER,
    CouponAutomationConfigForm,
    CouponAutomationRuleFormSet,
    CouponAutomationScenarioCreateForm,
    FillBirthdayRequestScenarioForm,
    MailingForm,
    MessageTemplateForm,
    validate_coupon_code_placeholder,
)
from guests.management.commands import mailing_worker as mailing_worker_cmd
from guests.models import (
    BotProfile,
    CouponAutomationConfig,
    CouponAutomationRule,
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponCampaignAssignment,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    IikoCustomerCategorySyncEvent,
    Mailing,
    MailingGuest,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
    OlapSalesRawLine,
)
from guests.services.mailing_delivery_targets import build_mailing_delivery_plan
from guests.services.template_render import render_message_for_guest
from guests.services.notification_handler_registry import (
    get_registered_schedule_scenario_codes,
    run_registered_schedule_scenarios,
)
from guests.services.coupon_campaign_reporting import build_coupon_campaign_performance_snapshot
from guests.services.coupon_campaign_lifecycle import CouponCampaignLifecycleService
from guests.services.coupon_autoscenarios import (
    CouponAutoscenarioPreviewError,
    build_coupon_autoscenario_execution_plan,
    cleanup_coupon_autoscenario_pilot_assignment,
    execute_coupon_autoscenario_automatic,
    execute_coupon_autoscenario_pilot,
    format_coupon_autoscenario_audience_venue_filter,
    resolve_coupon_autoscenario_type,
)
from guests.services.notification_registry import (
    SCENARIO_CODE_BIRTHDAY_COUPON,
    SCENARIO_CODE_FILL_BIRTHDAY_COUPON,
    SCENARIO_CODE_FILL_BIRTHDAY_REQUEST,
)

MAILINGS_V2_RUN_NOW_MAX_BATCHES = 5
COUPON_AUTOSCENARIO_CONTROL_SCAN_LIMIT = 5000
logger = logging.getLogger(__name__)

COUPON_AUTOSCENARIO_STATE_LABELS = {
    CouponAutomationConfig.ExecutionMode.REPORT_ONLY: "Черновик",
    CouponAutomationConfig.ExecutionMode.PILOT: "Пилот",
    CouponAutomationConfig.ExecutionMode.AUTOMATIC: "Активен",
    CouponAutomationConfig.ExecutionMode.PAUSED: "Пауза",
}

COUPON_TEMPLATE_LOCK_EXECUTION_MODES = {
    CouponAutomationConfig.ExecutionMode.PILOT,
    CouponAutomationConfig.ExecutionMode.AUTOMATIC,
}

COUPON_AUTOSCENARIO_STATE_HINTS = {
    CouponAutomationConfig.ExecutionMode.REPORT_ONLY: "Можно смотреть расчёт, купоны не выдаются.",
    CouponAutomationConfig.ExecutionMode.PILOT: "Пробный запуск разрешён только для контрольных телефонов.",
    CouponAutomationConfig.ExecutionMode.AUTOMATIC: "Готов к боевому запуску после включения расписания.",
    CouponAutomationConfig.ExecutionMode.PAUSED: "Автосценарий временно остановлен.",
}


def _coupon_autoscenario_state_label(mode: str) -> str:
    return COUPON_AUTOSCENARIO_STATE_LABELS.get(str(mode or ""), str(mode or "—"))


def _coupon_autoscenario_state_hint(mode: str) -> str:
    return COUPON_AUTOSCENARIO_STATE_HINTS.get(str(mode or ""), "")


def _build_coupon_autoscenario_urls(config: CouponAutomationConfig) -> dict[str, str]:
    """
    Возвращает основные переходы внутри рабочего контура купонного автосценария.
    """
    scenario = getattr(config, "scenario", None)
    scenario_code = str(getattr(scenario, "code", "") or "").strip()
    hub_url = reverse("mailings_v2_scenarios")
    settings_url = reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk})
    preview_query = urlencode(
        {
            "coupon_scenario_code": scenario_code,
            "coupon_check": "1",
        }
    )
    report_query = urlencode({"scenario_code": scenario_code})
    return {
        "control": reverse("mailings_v2_coupon_autoscenario_control", kwargs={"pk": config.pk}),
        "control_v2": reverse("mailings_v2_coupon_autoscenario_control_v2", kwargs={"pk": config.pk}),
        "settings": settings_url,
        "settings_state": f"{settings_url}#settings-state",
        "settings_messages": f"{settings_url}#settings-messages",
        "settings_chain": f"{settings_url}#settings-chain",
        "settings_coupons": f"{settings_url}#settings-coupons",
        "settings_pilot": f"{settings_url}#settings-pilot",
        "settings_advanced": f"{settings_url}#settings-advanced",
        "hub": hub_url,
        "preview": f"{hub_url}?{preview_query}" if scenario_code else hub_url,
        "report": f"{reverse('reports_coupon_autoscenarios')}?{report_query}",
    }


def _decorate_coupon_autoscenario_config(config: CouponAutomationConfig) -> CouponAutomationConfig:
    """
    Добавляет к настройке автосценария поля, удобные для шаблонов mailings-v2.
    """
    effective_scenario_type = resolve_coupon_autoscenario_type(config)
    config.effective_scenario_type = effective_scenario_type
    config.effective_scenario_type_label = dict(CouponAutomationConfig.ScenarioType.choices).get(
        effective_scenario_type,
        effective_scenario_type or "—",
    )
    template_obj = getattr(config.scenario, "template", None)
    display_name, technical_name = _resolve_template_title(template_obj)
    config.template_display_name = display_name
    config.template_technical_name = technical_name
    config.execution_state_label = _coupon_autoscenario_state_label(config.execution_mode)
    config.execution_state_hint = _coupon_autoscenario_state_hint(config.execution_mode)
    config.active_coupon_rules = [rule for rule in config.coupon_rules.all() if rule.is_active]
    config.has_rule_based_coupon_selection = bool(config.active_coupon_rules)
    config.coupon_selection_policy_label = _coupon_autoscenario_policy_label(
        venue_selection_mode=config.venue_selection_mode
    )
    config.coupon_selection_policy_rows = _coupon_autoscenario_policy_rows(
        cooldown_days=config.cooldown_days,
        scenario_type=effective_scenario_type,
        scenario_code=config.scenario.code,
        birthday_window_days=(config.settings or {}).get("birthday_preparation_window_days"),
        venue_selection_mode=config.venue_selection_mode,
    )
    config.audience_venue_filter_label = format_coupon_autoscenario_audience_venue_filter(
        config.audience_venue_filter_mode
    )
    config.audience_venue_filter_summary = _coupon_autoscenario_audience_venue_filter_summary(
        mode=config.audience_venue_filter_mode,
        venue_code=config.audience_venue_code,
        venue_name=config.audience_venue_name,
        inactive_days=(config.scenario.settings or {}).get("inactive_days"),
    )
    selected_bots = list(config.scenario.bot_profiles.all())
    config.notification_target_mode_label = config.scenario.get_target_mode_display()
    config.notification_bot_profiles_summary = (
        ", ".join(f"{bot.name} ({bot.get_provider_type_display()})" for bot in selected_bots)
        if selected_bots
        else "боты не выбраны"
    )
    return config


def _build_coupon_autoscenario_readiness(config: CouponAutomationConfig) -> dict[str, object]:
    """
    Выполняет лёгкую структурную проверку без построения аудитории и резервирования купонов.
    """
    scenario = config.scenario
    settings_url = reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk})
    rows: list[dict[str, object]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    def settings_tab_url(tab_name: str) -> str:
        return f"{settings_url}#settings-{tab_name}"

    def add_row(
        label: str,
        ok: bool,
        detail: str,
        *,
        blocker: str = "",
        warning: str = "",
        settings_tab: str = "",
    ) -> None:
        row = {"label": label, "ok": bool(ok), "detail": detail}
        if settings_tab:
            row["action_url"] = settings_tab_url(settings_tab)
            row["action_label"] = "Настроить"
        rows.append(row)
        if not ok and blocker:
            blockers.append(blocker)
        if not ok and warning:
            warnings.append(warning)

    template_obj = getattr(scenario, "template", None)
    add_row(
        "Шаблон сообщения",
        template_obj is not None,
        getattr(template_obj, "name", "") or "шаблон не выбран",
        blocker="У сценария не выбран шаблон сообщения.",
        settings_tab="messages",
    )
    if template_obj is not None:
        try:
            validate_coupon_code_placeholder(getattr(template_obj, "message_text", ""))
        except ValidationError as exc:
            add_row(
                "Параметр купона",
                False,
                "; ".join(exc.messages),
                blocker="В шаблоне нет корректного параметра купона.",
                settings_tab="messages",
            )
        else:
            add_row(
                "Параметр купона",
                True,
                f"Параметр {COUPON_CODE_PLACEHOLDER} найден.",
                settings_tab="messages",
            )

    active_bot_count = scenario.bot_profiles.filter(is_active=True).count()
    add_row(
        "Разрешённые боты",
        active_bot_count > 0,
        f"активных ботов: {active_bot_count}",
        blocker="Не выбран ни один активный бот для отправки сообщений.",
        settings_tab="messages",
    )
    add_row(
        "Тип запуска",
        scenario.trigger_type == NotificationScenario.TriggerType.SCHEDULE,
        scenario.get_trigger_type_display(),
        blocker="Сценарий не относится к планировщику.",
        settings_tab="state",
    )

    has_coupon_source = bool(str(config.coupon_series or "").strip()) or any(
        rule.is_active and str(rule.coupon_series or "").strip()
        for rule in config.coupon_rules.all()
    )
    add_row(
        "Источник купонов",
        has_coupon_source,
        "правила заведений или резервная серия заданы" if has_coupon_source else "серии купонов не заданы",
        blocker="Не настроено ни одно правило купонов или резервная серия.",
        settings_tab="coupons",
    )

    planner_ok = bool(scenario.is_active)
    add_row(
        "Планировщик уведомлений",
        planner_ok,
        "включён" if planner_ok else "выключен",
        warning="Планировщик выключен; автоматический режим не будет выполняться по расписанию.",
    )
    if config.execution_mode == CouponAutomationConfig.ExecutionMode.REPORT_ONLY:
        warnings.append("Автосценарий находится в черновике: фактическая выдача купонов отключена.")
    if config.execution_mode == CouponAutomationConfig.ExecutionMode.PAUSED:
        warnings.append("Автосценарий поставлен на паузу.")

    return {
        "rows": rows,
        "blockers": blockers,
        "warnings": warnings,
        "is_ready": not blockers,
    }


def _build_coupon_autoscenario_chain_steps(config: CouponAutomationConfig) -> list[dict[str, object]]:
    """
    Описывает этапы автосценария для операторского пульта.
    """
    scenario = config.scenario
    settings_url = reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk})

    def settings_tab_url(tab_name: str) -> str:
        return f"{settings_url}#settings-{tab_name}"

    def step_payload(
        number: int,
        title: str,
        scenario_obj: NotificationScenario | None,
        description: str,
        *,
        settings_tab: str = "messages",
    ) -> dict:
        template_obj = getattr(scenario_obj, "template", None) if scenario_obj else None
        display_name, technical_name = _resolve_template_title(template_obj)
        return {
            "number": number,
            "title": title,
            "description": description,
            "scenario": scenario_obj,
            "code": str(getattr(scenario_obj, "code", "") or ""),
            "name": str(getattr(scenario_obj, "name", "") or ""),
            "is_active": bool(getattr(scenario_obj, "is_active", False)),
            "template_display_name": display_name or getattr(template_obj, "name", "") or "—",
            "template_technical_name": technical_name,
            "settings_url": settings_tab_url(settings_tab),
        }

    if str(scenario.code or "").strip() == SCENARIO_CODE_FILL_BIRTHDAY_COUPON:
        request_scenario = (
            NotificationScenario.objects.select_related("template")
            .prefetch_related("bot_profiles")
            .filter(code=SCENARIO_CODE_FILL_BIRTHDAY_REQUEST)
            .first()
        )
        return [
            step_payload(
                1,
                "Просьба заполнить дату рождения",
                request_scenario,
                "Плановый сценарий просит гостя заполнить дату рождения в боте.",
                settings_tab="chain",
            ),
            step_payload(
                2,
                "Купон после заполнения даты рождения",
                scenario,
                "Купон выдаётся после появления события заполнения профиля.",
                settings_tab="coupons",
            ),
        ]

    return [
        step_payload(
            1,
            "Основной купонный автосценарий",
            scenario,
            config.effective_scenario_type_label,
            settings_tab="messages",
        )
    ]


def _build_coupon_autoscenario_launch_steps(
    config: CouponAutomationConfig,
    readiness: dict[str, object],
    urls: dict[str, str],
    *,
    check_requested: bool,
    control_plan: dict[str, object] | None,
    control_plan_error: str = "",
) -> list[dict[str, object]]:
    """
    Собирает пошаговый мастер запуска для пульта без изменения бизнес-логики.
    """
    blockers = list(readiness.get("blockers") or [])
    structurally_ready = bool(readiness.get("is_ready"))
    execution_mode = str(config.execution_mode or "")
    planner_enabled = bool(getattr(config.scenario, "is_active", False))

    steps: list[dict[str, object]] = []

    def add_step(
        number: int,
        title: str,
        status_label: str,
        status_class: str,
        detail: str,
        *,
        action_label: str = "",
        action_url: str = "",
        post_action: str = "",
        action_disabled: bool = False,
    ) -> None:
        steps.append(
            {
                "number": number,
                "title": title,
                "status_label": status_label,
                "status_class": status_class,
                "detail": detail,
                "action_label": action_label,
                "action_url": action_url,
                "post_action": post_action,
                "action_disabled": action_disabled,
            }
        )

    if structurally_ready:
        add_step(
            1,
            "Проверить основу",
            "Готово",
            "text-bg-success",
            "Шаблон, боты и источник купонов заданы.",
            action_label="Открыть настройки",
            action_url=urls["settings"],
        )
    else:
        add_step(
            1,
            "Проверить основу",
            "Требует настройки",
            "text-bg-warning text-dark",
            f"Структурные блокировки: {len(blockers)}.",
            action_label="Исправить настройки",
            action_url=urls["settings"],
        )

    if control_plan_error:
        add_step(
            2,
            "Проверить расчёт",
            "Ошибка проверки",
            "text-bg-danger",
            control_plan_error,
            action_label="Повторить проверку",
            post_action="check_readiness",
        )
    elif control_plan:
        planned_assignments = int(control_plan.get("planned_assignments") or 0)
        if control_plan.get("can_execute"):
            add_step(
                2,
                "Проверить расчёт",
                "Готово",
                "text-bg-success",
                f"План можно выполнить: к выдаче {planned_assignments}.",
                action_label="Повторить проверку",
                post_action="check_readiness",
            )
        else:
            plan_blockers = list(control_plan.get("blockers") or [])
            add_step(
                2,
                "Проверить расчёт",
                "Есть блокировки",
                "text-bg-warning text-dark",
                f"План построен, блокировок: {len(plan_blockers)}.",
                action_label="Повторить проверку",
                post_action="check_readiness",
            )
    elif check_requested:
        add_step(
            2,
            "Проверить расчёт",
            "Нет результата",
            "text-bg-secondary",
            "Расчёт не вернул данных. Проверьте настройки и повторите проверку.",
            action_label="Повторить проверку",
            post_action="check_readiness",
        )
    else:
        add_step(
            2,
            "Проверить расчёт",
            "Ожидает проверки",
            "text-bg-secondary",
            "Постройте план ближайшего запуска без резервирования купонов.",
            action_label="Проверить готовность",
            post_action="check_readiness",
        )

    if execution_mode == CouponAutomationConfig.ExecutionMode.PILOT:
        if structurally_ready:
            add_step(
                3,
                "Настроить пилот",
                "Готов к пилоту",
                "text-bg-info",
                "Пилотный режим включён. Запуск создаст резерв купонов и события vtelemax.",
                action_label="Создать пилот",
                post_action="run_pilot",
            )
        else:
            add_step(
                3,
                "Настроить пилот",
                "Ждёт исправлений",
                "text-bg-warning text-dark",
                "Сначала устраните структурные блокировки автосценария.",
                action_label="Открыть пилот",
                action_url=urls["settings_pilot"],
            )
    elif execution_mode == CouponAutomationConfig.ExecutionMode.AUTOMATIC:
        add_step(
            3,
            "Настроить пилот",
            "Пройдено",
            "text-bg-success",
            "Автосценарий уже переведён в автоматический режим.",
            action_label="Открыть отчёт",
            action_url=urls["report"],
        )
    elif execution_mode == CouponAutomationConfig.ExecutionMode.PAUSED:
        add_step(
            3,
            "Настроить пилот",
            "На паузе",
            "text-bg-secondary",
            "Автосценарий остановлен; перед пилотом выберите рабочий режим.",
            action_label="Открыть режим",
            action_url=urls["settings_state"],
        )
    else:
        add_step(
            3,
            "Настроить пилот",
            "Нужно включить пилот",
            "text-bg-secondary",
            "Переведите купонный режим в пилот и укажите контрольные телефоны.",
            action_label="Открыть пилот",
            action_url=urls["settings_pilot"],
        )

    if execution_mode == CouponAutomationConfig.ExecutionMode.AUTOMATIC and planner_enabled:
        add_step(
            4,
            "Боевой режим",
            "Активен",
            "text-bg-success",
            "Автоматический режим и планировщик включены.",
            action_label="Открыть отчёт",
            action_url=urls["report"],
        )
    elif execution_mode == CouponAutomationConfig.ExecutionMode.AUTOMATIC:
        add_step(
            4,
            "Боевой режим",
            "Включить планировщик",
            "text-bg-info",
            "Купонный режим автоматический, но планировщик уведомлений выключен.",
            action_label="Включить планировщик",
            post_action="enable_planner",
        )
    elif execution_mode == CouponAutomationConfig.ExecutionMode.PAUSED:
        add_step(
            4,
            "Боевой режим",
            "На паузе",
            "text-bg-secondary",
            "Для боевого запуска снимите паузу и верните автоматический режим через настройки.",
            action_label="Открыть режим",
            action_url=urls["settings_state"],
        )
    else:
        add_step(
            4,
            "Боевой режим",
            "После пилота",
            "text-bg-secondary",
            "После успешного пилота переведите режим в автоматический и включите планировщик.",
            action_label="Открыть режим",
            action_url=urls["settings_state"],
        )

    return steps


def _build_coupon_autoscenario_primary_step(launch_steps: list[dict[str, object]]) -> dict[str, object]:
    """
    Возвращает главный следующий шаг оператора из уже рассчитанного мастера запуска.
    """
    completed_statuses = {"Готово", "Пройдено", "Активен"}
    for step in launch_steps:
        if str(step.get("status_label") or "") not in completed_statuses:
            return {**step, "is_complete": False}
    if launch_steps:
        return {**launch_steps[-1], "is_complete": True}
    return {
        "number": "",
        "title": "Проверить настройки",
        "status_label": "Нет данных",
        "status_class": "text-bg-secondary",
        "detail": "Мастер запуска не смог определить следующий шаг.",
        "action_label": "Открыть настройки",
        "action_url": "",
        "post_action": "",
        "action_disabled": True,
        "is_complete": False,
    }


def _build_coupon_autoscenario_diagnostics(config: CouponAutomationConfig) -> list[dict[str, object]]:
    """
    Собирает краткую диагностику результата автосценария для операторского пульта.
    """
    run_stats = CouponAutoscenarioRun.objects.filter(config=config).aggregate(
        total=Count("id"),
        completed=Count("id", filter=Q(status=CouponAutoscenarioRun.Status.COMPLETED)),
        sync_pending=Count("id", filter=Q(status=CouponAutoscenarioRun.Status.SYNC_PENDING)),
        error=Count("id", filter=Q(status=CouponAutoscenarioRun.Status.ERROR)),
    )
    assignment_stats = CouponAutoscenarioAssignment.objects.filter(config=config).aggregate(
        total=Count("id"),
        reserved=Count("id", filter=Q(status=CouponAutoscenarioAssignment.Status.RESERVED)),
        sent=Count("id", filter=Q(status=CouponAutoscenarioAssignment.Status.SENT)),
        used=Count(
            "id",
            filter=Q(
                status__in=[
                    CouponAutoscenarioAssignment.Status.USED,
                    CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN,
                ]
            ),
        ),
        error=Count("id", filter=Q(status=CouponAutoscenarioAssignment.Status.ERROR)),
        vtelemax_pending=Count(
            "id",
            filter=Q(vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING),
        ),
        vtelemax_error=Count(
            "id",
            filter=Q(vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.ERROR),
        ),
        iiko_pending=Count(
            "id",
            filter=Q(iiko_category_add_status=CouponAutoscenarioAssignment.IikoCategorySyncStatus.PENDING),
        ),
        iiko_error=Count(
            "id",
            filter=Q(iiko_category_add_status=CouponAutoscenarioAssignment.IikoCategorySyncStatus.ERROR),
        ),
    )
    event_stats = NotificationEvent.objects.filter(scenario=config.scenario).aggregate(
        total=Count("id"),
        task_created=Count("id", filter=Q(status=NotificationEvent.Status.TASK_CREATED)),
        skipped=Count("id", filter=Q(status=NotificationEvent.Status.SKIPPED)),
        error=Count("id", filter=Q(status=NotificationEvent.Status.ERROR)),
    )
    task_stats = DispatchTask.objects.filter(notification_scenario=config.scenario).aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(status=DispatchTask.Status.PENDING)),
        queued=Count("id", filter=Q(status=DispatchTask.Status.QUEUED)),
        done=Count("id", filter=Q(status=DispatchTask.Status.DONE)),
        failed=Count("id", filter=Q(status=DispatchTask.Status.FAILED)),
    )

    def safe_int(stats: dict[str, object], key: str) -> int:
        return int(stats.get(key) or 0)

    def status_class(error_count: int, pending_count: int = 0) -> str:
        if error_count:
            return "text-bg-danger"
        if pending_count:
            return "text-bg-warning text-dark"
        return "text-bg-success"

    def status_label(error_count: int, pending_count: int = 0) -> str:
        if error_count:
            return "ошибки"
        if pending_count:
            return "ожидает"
        return "без ошибок"

    run_errors = safe_int(run_stats, "error")
    run_pending = safe_int(run_stats, "sync_pending")
    assignment_errors = (
        safe_int(assignment_stats, "error")
        + safe_int(assignment_stats, "vtelemax_error")
        + safe_int(assignment_stats, "iiko_error")
    )
    assignment_pending = safe_int(assignment_stats, "vtelemax_pending") + safe_int(
        assignment_stats,
        "iiko_pending",
    )
    event_errors = safe_int(event_stats, "error")
    task_errors = safe_int(task_stats, "failed")
    task_pending = safe_int(task_stats, "pending") + safe_int(task_stats, "queued")

    return [
        {
            "title": "Технические запуски",
            "value": safe_int(run_stats, "total"),
            "status_label": status_label(run_errors, run_pending),
            "status_class": status_class(run_errors, run_pending),
            "detail": (
                f"завершено: {safe_int(run_stats, 'completed')}, "
                f"ожидает vtelemax: {safe_int(run_stats, 'sync_pending')}, "
                f"ошибок: {safe_int(run_stats, 'error')}"
            ),
        },
        {
            "title": "Назначения купонов",
            "value": safe_int(assignment_stats, "total"),
            "status_label": status_label(assignment_errors, assignment_pending),
            "status_class": status_class(assignment_errors, assignment_pending),
            "detail": (
                f"отправлено: {safe_int(assignment_stats, 'sent')}, "
                f"использовано: {safe_int(assignment_stats, 'used')}, "
                f"резерв: {safe_int(assignment_stats, 'reserved')}"
            ),
        },
        {
            "title": "События уведомлений",
            "value": safe_int(event_stats, "total"),
            "status_label": status_label(event_errors),
            "status_class": status_class(event_errors),
            "detail": (
                f"задач создано: {safe_int(event_stats, 'task_created')}, "
                f"пропущено: {safe_int(event_stats, 'skipped')}, "
                f"ошибок: {safe_int(event_stats, 'error')}"
            ),
        },
        {
            "title": "Задачи доставки",
            "value": safe_int(task_stats, "total"),
            "status_label": status_label(task_errors, task_pending),
            "status_class": status_class(task_errors, task_pending),
            "detail": (
                f"ожидает: {safe_int(task_stats, 'pending')}, "
                f"в очереди: {safe_int(task_stats, 'queued')}, "
                f"доставлено: {safe_int(task_stats, 'done')}"
            ),
        },
    ]


def _build_coupon_autoscenario_olap_e2e_checklist(config: CouponAutomationConfig) -> dict[str, object]:
    """
    Собирает контрольный список проверки применения купона через OLAP.
    """

    def item(title: str, status_label: str, status_class: str, detail: str) -> dict[str, str]:
        return {
            "title": title,
            "status_label": status_label,
            "status_class": status_class,
            "detail": detail,
        }

    assignment = (
        CouponAutoscenarioAssignment.objects.select_related("run", "guest", "coupon")
        .filter(config=config)
        .order_by("-updated_at", "-created_at", "-id")
        .first()
    )
    if assignment is None:
        return {
            "assignment": None,
            "assignment_label": "",
            "coupon_label": "",
            "guest_label": "",
            "items": [
                item(
                    "Купон выдан в автосценарии",
                    "ожидает",
                    "text-bg-secondary",
                    "Сначала создайте пилотную волну или дождитесь боевого запуска.",
                ),
                item(
                    "Синхронизация выдачи во vtelemax",
                    "ожидает",
                    "text-bg-secondary",
                    "Событие появится после создания назначения купона.",
                ),
                item(
                    "Сообщение отправлено гостю",
                    "ожидает",
                    "text-bg-secondary",
                    "Отправка начнётся после подтверждения выдачи во vtelemax.",
                ),
                item(
                    "Применение найдено через OLAP",
                    "ожидает",
                    "text-bg-secondary",
                    "После применения на кассе дождитесь OLAP и синхронизации применений.",
                ),
                item(
                    "Статус применения отправлен во vtelemax",
                    "ожидает",
                    "text-bg-secondary",
                    "Событие обновления статуса применения (`status_update`) появится после фиксации применения купона.",
                ),
            ],
        }

    assignment_event = (
        CouponVtelemaxSyncQueue.objects.filter(
            autoscenario_assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
        )
        .order_by("-updated_at", "-id")
        .first()
    )
    status_update_event = (
        CouponVtelemaxSyncQueue.objects.filter(
            autoscenario_assignment=assignment,
            direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
        )
        .order_by("-updated_at", "-id")
        .first()
    )

    used_statuses = {
        CouponAutoscenarioAssignment.Status.USED,
        CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN,
    }
    delivered_statuses = used_statuses | {CouponAutoscenarioAssignment.Status.SENT}
    is_used = assignment.status in used_statuses
    is_delivered = assignment.status in delivered_statuses
    is_canceled = assignment.status == CouponAutoscenarioAssignment.Status.CANCELED
    is_error = assignment.status == CouponAutoscenarioAssignment.Status.ERROR

    coupon_label = f"{assignment.coupon_series}:{assignment.coupon_code}"
    if assignment.phone_e164:
        guest_label = assignment.phone_e164
    elif assignment.guest_id:
        guest_label = f"гость #{assignment.guest_id}"
    else:
        guest_label = "гость не определён"
    assignment_label = f"#{assignment.id}"

    checklist_items: list[dict[str, str]] = []
    if is_error:
        checklist_items.append(
            item(
                "Купон выдан в автосценарии",
                "ошибка",
                "text-bg-danger",
                assignment.status_details or assignment.status_reason or "Назначение купона завершилось ошибкой.",
            )
        )
    elif is_canceled:
        checklist_items.append(
            item(
                "Купон выдан в автосценарии",
                "отменено",
                "text-bg-secondary",
                f"Назначение {assignment_label} отменено, купон {coupon_label} не подходит для E2E-проверки.",
            )
        )
    else:
        checklist_items.append(
            item(
                "Купон выдан в автосценарии",
                "готово",
                "text-bg-success",
                f"Назначение {assignment_label}, купон {coupon_label}, получатель: {guest_label}.",
            )
        )

    if (
        assignment.vtelemax_sync_status == CouponAutoscenarioAssignment.VtelemaxSyncStatus.ERROR
        or getattr(assignment_event, "status", None) == CouponVtelemaxSyncQueue.Status.ERROR
    ):
        checklist_items.append(
            item(
                "Синхронизация выдачи во vtelemax",
                "ошибка",
                "text-bg-danger",
                assignment.vtelemax_sync_error
                or getattr(assignment_event, "last_error", "")
                or "vtelemax отклонил или не подтвердил выдачу купона.",
            )
        )
    elif (
        assignment.vtelemax_sync_status == CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK
        or getattr(assignment_event, "status", None) == CouponVtelemaxSyncQueue.Status.ACKED
    ):
        checklist_items.append(
            item(
                "Синхронизация выдачи во vtelemax",
                "подтверждено",
                "text-bg-success",
                "Выдача купона подтверждена vtelemax, можно контролировать доставку сообщения.",
            )
        )
    else:
        event_status = assignment_event.get_status_display() if assignment_event else "событие не создано"
        checklist_items.append(
            item(
                "Синхронизация выдачи во vtelemax",
                "ожидает",
                "text-bg-warning text-dark",
                f"Текущее состояние события выдачи: {event_status}.",
            )
        )

    if is_delivered:
        checklist_items.append(
            item(
                "Сообщение отправлено гостю",
                "отправлено",
                "text-bg-success",
                f"Текущий статус назначения: {assignment.get_status_display()}.",
            )
        )
    elif is_canceled:
        checklist_items.append(
            item(
                "Сообщение отправлено гостю",
                "отменено",
                "text-bg-secondary",
                "Назначение отменено, сообщение по этому купону не нужно ждать.",
            )
        )
    elif is_error:
        checklist_items.append(
            item(
                "Сообщение отправлено гостю",
                "ошибка",
                "text-bg-danger",
                assignment.status_details or "Назначение находится в ошибке.",
            )
        )
    else:
        checklist_items.append(
            item(
                "Сообщение отправлено гостю",
                "ожидает",
                "text-bg-warning text-dark",
                f"Текущий статус назначения: {assignment.get_status_display()}.",
            )
        )

    if is_used:
        used_details = []
        if assignment.used_order_id:
            used_details.append(f"заказ #{assignment.used_order_id}")
        if assignment.used_business_date:
            used_details.append(f"дата бизнеса {assignment.used_business_date:%Y-%m-%d}")
        if assignment.used_at:
            used_details.append(f"зафиксировано {timezone.localtime(assignment.used_at):%Y-%m-%d %H:%M}")
        checklist_items.append(
            item(
                "Применение найдено через OLAP",
                "найдено",
                "text-bg-success",
                ", ".join(used_details) or "Купон отмечен как применённый.",
            )
        )
    elif is_canceled:
        checklist_items.append(
            item(
                "Применение найдено через OLAP",
                "не требуется",
                "text-bg-secondary",
                "Назначение отменено до применения.",
            )
        )
    elif is_error:
        checklist_items.append(
            item(
                "Применение найдено через OLAP",
                "заблокировано",
                "text-bg-danger",
                "Сначала устраните ошибку назначения купона.",
            )
        )
    else:
        checklist_items.append(
            item(
                "Применение найдено через OLAP",
                "ожидает",
                "text-bg-warning text-dark",
                "Оставьте купон активным, примените его на кассе и дождитесь OLAP-синхронизации.",
            )
        )

    if not is_used:
        checklist_items.append(
            item(
                "Статус применения отправлен во vtelemax",
                "после применения",
                "text-bg-secondary",
                "Обновление статуса отправляется только после фиксации применения купона.",
            )
        )
    elif status_update_event is None:
        checklist_items.append(
            item(
                "Статус применения отправлен во vtelemax",
                "не создано",
                "text-bg-warning text-dark",
                "Применение найдено, но событие обновления статуса применения (`status_update`) для vtelemax ещё не создано.",
            )
        )
    elif status_update_event.status == CouponVtelemaxSyncQueue.Status.ACKED:
        checklist_items.append(
            item(
                "Статус применения отправлен во vtelemax",
                "подтверждено",
                "text-bg-success",
                "vtelemax подтвердил обновление статуса применённого купона.",
            )
        )
    elif status_update_event.status == CouponVtelemaxSyncQueue.Status.ERROR:
        checklist_items.append(
            item(
                "Статус применения отправлен во vtelemax",
                "ошибка",
                "text-bg-danger",
                status_update_event.last_error or "vtelemax не подтвердил обновление статуса применения.",
            )
        )
    else:
        checklist_items.append(
            item(
                "Статус применения отправлен во vtelemax",
                "ожидает",
                "text-bg-warning text-dark",
                f"Текущее состояние события обновления статуса применения (`status_update`): "
                f"{status_update_event.get_status_display()}.",
            )
        )

    return {
        "assignment": assignment,
        "assignment_label": assignment_label,
        "coupon_label": coupon_label,
        "guest_label": guest_label,
        "items": checklist_items,
    }


def _build_coupon_autoscenario_issue_rows(config: CouponAutomationConfig) -> list[dict[str, object]]:
    """
    Возвращает последние конкретные проблемы автосценария для операторского пульта.
    """

    def compact_text(value: object, *, default: str = "—", limit: int = 180) -> str:
        text = str(value or "").strip()
        if not text:
            return default
        if len(text) <= limit:
            return text
        return f"{text[: limit - 1].rstrip()}…"

    rows: list[dict[str, object]] = []

    for run in CouponAutoscenarioRun.objects.filter(
        config=config,
        status=CouponAutoscenarioRun.Status.ERROR,
    ).order_by("-updated_at", "-id")[:5]:
        run_messages = list(run.blockers or []) or list(run.warnings or [])
        rows.append(
            {
                "created_at": run.updated_at or run.created_at,
                "source": "Технический запуск",
                "object_label": f"#{run.id}",
                "status_label": run.get_status_display(),
                "detail": compact_text("; ".join(str(item) for item in run_messages), default="Ошибка запуска."),
            }
        )

    assignment_issue_filter = (
        Q(status=CouponAutoscenarioAssignment.Status.ERROR)
        | Q(vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.ERROR)
        | Q(iiko_category_add_status=CouponAutoscenarioAssignment.IikoCategorySyncStatus.ERROR)
    )
    for assignment in (
        CouponAutoscenarioAssignment.objects.filter(config=config)
        .filter(assignment_issue_filter)
        .order_by("-updated_at", "-id")[:5]
    ):
        status_parts = [assignment.get_status_display()]
        if assignment.vtelemax_sync_status == CouponAutoscenarioAssignment.VtelemaxSyncStatus.ERROR:
            status_parts.append(f"vtelemax: {assignment.get_vtelemax_sync_status_display()}")
        if assignment.iiko_category_add_status == CouponAutoscenarioAssignment.IikoCategorySyncStatus.ERROR:
            status_parts.append(f"iikoCard: {assignment.get_iiko_category_add_status_display()}")
        rows.append(
            {
                "created_at": assignment.updated_at or assignment.created_at,
                "source": "Назначение купона",
                "object_label": f"#{assignment.id} {assignment.coupon_series}:{assignment.coupon_code}",
                "status_label": "; ".join(status_parts),
                "detail": compact_text(
                    assignment.vtelemax_sync_error
                    or assignment.iiko_category_add_error
                    or assignment.status_details
                    or assignment.status_reason,
                    default="Ошибка назначения купона.",
                ),
            }
        )

    for event in (
        NotificationEvent.objects.filter(
            scenario=config.scenario,
            status=NotificationEvent.Status.ERROR,
        ).order_by("-updated_at", "-id")[:5]
    ):
        rows.append(
            {
                "created_at": event.updated_at or event.created_at,
                "source": "Событие уведомления",
                "object_label": f"#{event.id}",
                "status_label": event.get_status_display(),
                "detail": compact_text(event.error_text, default="Ошибка обработки события."),
            }
        )

    for task in (
        DispatchTask.objects.filter(
            notification_scenario=config.scenario,
            status=DispatchTask.Status.FAILED,
        )
        .select_related("bot_profile")
        .order_by("-updated_at", "-id")[:5]
    ):
        provider_label = task.get_provider_type_display()
        if task.bot_profile_id:
            provider_label = f"{provider_label} · {task.bot_profile.name}"
        rows.append(
            {
                "created_at": task.updated_at or task.created_at,
                "source": "Задача доставки",
                "object_label": f"#{task.id} {provider_label}",
                "status_label": task.get_status_display(),
                "detail": compact_text(task.last_error, default="Ошибка доставки сообщения."),
            }
        )

    return sorted(rows, key=lambda row: row["created_at"], reverse=True)[:10]


def _coupon_autoscenario_audience_venue_filter_summary(
    *,
    mode: str | None,
    venue_code: str | None,
    venue_name: str | None,
    inactive_days: int | None,
) -> str:
    """
    Кратко описывает дополнительный отбор аудитории по заведению.
    """
    safe_mode = str(mode or "").strip()
    if safe_mode != CouponAutomationConfig.AudienceVenueFilterMode.VISITED_ONCE_AND_INACTIVE:
        return ""

    venue = str(venue_name or venue_code or "заведение не выбрано").strip()
    try:
        days = int(inactive_days or 0)
    except (TypeError, ValueError):
        days = 0

    if days > 0:
        return f"Отбор гостей: был в заведении «{venue}» хотя бы 1 раз и не был там {days}+ дней."
    return f"Отбор гостей: был в заведении «{venue}» хотя бы 1 раз."


def _coupon_autoscenario_policy_label(*, venue_selection_mode: str = "") -> str:
    """
    Кратко описывает принятую стратегию выбора купона для маркетолога.
    """
    if venue_selection_mode == CouponAutomationConfig.VenueSelectionMode.ALL_VISITED:
        return (
            "Гость получает отдельный купон по каждому заведению из правил, где он уже был; "
            "«Вся сеть» используется только как запасное правило."
        )
    if venue_selection_mode == CouponAutomationConfig.VenueSelectionMode.FAVORITE:
        return (
            "Гость получает один купон по любимому заведению: больше заказов, при равенстве более свежий визит; "
            "если заведение не найдено, используется «Вся сеть»."
        )
    return (
        "Сначала используется правило по последнему заведению гостя из истории заказов; "
        "если подходящего правила или свободного купона нет, используется правило «Вся сеть»."
    )


def _coupon_autoscenario_policy_rows(
    *,
    cooldown_days: int | None,
    scenario_type: str = "",
    scenario_code: str = "",
    birthday_window_days: int | None = None,
    venue_selection_mode: str = "",
) -> list[tuple[str, str]]:
    """
    Возвращает человекочитаемые правила, которые должны быть видны на экранах автосценариев.
    """
    cooldown_label = f"не чаще 1 раза в {int(cooldown_days or 0)} дн."
    if venue_selection_mode == CouponAutomationConfig.VenueSelectionMode.ALL_VISITED:
        strategy_label = "все посещённые заведения из таблицы правил"
        venue_source_label = "история заказов по гостю и заведению"
        limit_label = "не больше лимита выдач за проход"
    elif venue_selection_mode == CouponAutomationConfig.VenueSelectionMode.FAVORITE:
        strategy_label = "любимое заведение, затем Вся сеть"
        venue_source_label = "число заказов по гостю и заведению; при равенстве свежий визит"
        venue_source_row_label = "Источник заведений"
        limit_label = "не больше 1 купона гостю за проход"
    else:
        strategy_label = "последнее заведение гостя, затем Вся сеть"
        venue_source_label = "история заказов"
        venue_source_row_label = "Источник последнего заведения"
        limit_label = "не больше 1 купона гостю за проход"
    if venue_selection_mode == CouponAutomationConfig.VenueSelectionMode.ALL_VISITED:
        venue_source_row_label = "Источник заведений"
    rows = [
        ("Стратегия", strategy_label),
        (venue_source_row_label, venue_source_label),
        ("Ограничение", limit_label),
        ("Повтор", cooldown_label),
        ("Если нет купонов", "гость пропускается и попадает в дефицит купонов"),
    ]
    if (
        str(scenario_type or "").strip() == CouponAutomationConfig.ScenarioType.BIRTHDAY_COUPON
        or str(scenario_code or "").strip() == SCENARIO_CODE_BIRTHDAY_COUPON
    ):
        rows.insert(
            2,
            (
                "Окно дня рождения",
                f"сегодня + {int(birthday_window_days or 0)} дн. включительно",
            ),
        )
        rows.insert(
            4,
            (
                "Повтор ко дню рождения",
                "не больше одного купона гостю за один год дня рождения",
            ),
        )
    return rows


class MailingsV2CampaignsHubView(TemplateView):
    """
    Главный экран mailings-v2.

    Пока выступает как маршрутизатор и operational overview:
    1. сводка по кампаниям;
    2. быстрые переходы в текущие рабочие формы;
    3. стартовая точка для поэтапного перевода UX из legacy.
    """

    template_name = "mailing_v2/campaigns_hub.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        all_campaigns_qs = (
            Mailing.objects.select_related("template")
            .annotate(
                recipients_total=Count("guests_rows", distinct=True),
                recipients_done=Count(
                    "guests_rows",
                    filter=Q(guests_rows__status=MailingGuest.Status.DONE),
                    distinct=True,
                ),
                recipients_error=Count(
                    "guests_rows",
                    filter=Q(guests_rows__status=MailingGuest.Status.ERROR),
                    distinct=True,
                ),
                recipients_planned=Count(
                    "guests_rows",
                    filter=Q(guests_rows__status=MailingGuest.Status.PLANNED),
                    distinct=True,
                ),
                recipients_in_progress=Count(
                    "guests_rows",
                    filter=Q(guests_rows__status=MailingGuest.Status.IN_PROGRESS),
                    distinct=True,
                ),
            )
            .order_by("-updated_at", "-id")
        )

        q = (self.request.GET.get("q") or "").strip()
        only_active = bool(self.request.GET.get("only_active"))
        with_errors = bool(self.request.GET.get("with_errors"))
        show_archived = bool(self.request.GET.get("show_archived"))
        created_from_raw = (self.request.GET.get("created_from") or "").strip()
        created_to_raw = (self.request.GET.get("created_to") or "").strip()

        created_from = parse_date(created_from_raw) if created_from_raw else None
        created_to = parse_date(created_to_raw) if created_to_raw else None

        campaigns_qs = all_campaigns_qs
        if not show_archived:
            campaigns_qs = campaigns_qs.filter(is_archived=False)
        if only_active:
            campaigns_qs = campaigns_qs.filter(is_active=True)
        if with_errors:
            campaigns_qs = campaigns_qs.filter(recipients_error__gt=0)
        if created_from:
            campaigns_qs = campaigns_qs.filter(created_at__date__gte=created_from)
        if created_to:
            campaigns_qs = campaigns_qs.filter(created_at__date__lte=created_to)
        if q:
            search_q = Q(name__icontains=q) | Q(template__name__icontains=q)
            if q.isdigit():
                search_q = search_q | Q(id=int(q))
            campaigns_qs = campaigns_qs.filter(search_q)

        dispatch_scope = DispatchTask.objects.filter(
            Q(mailing_guest__isnull=False) | Q(notification_scenario__isnull=False)
        )
        dispatch_stats = dispatch_scope.aggregate(
            total=Count("id"),
            pending=Count("id", filter=Q(status=DispatchTask.Status.PENDING)),
            queued=Count("id", filter=Q(status=DispatchTask.Status.QUEUED)),
            in_progress=Count("id", filter=Q(status=DispatchTask.Status.IN_PROGRESS)),
            done=Count("id", filter=Q(status=DispatchTask.Status.DONE)),
            failed=Count("id", filter=Q(status=DispatchTask.Status.FAILED)),
        )

        recently_updated_threshold = timezone.now() - timedelta(days=7)
        kpi_scope = all_campaigns_qs.filter(is_archived=False)
        context["kpi"] = {
            "campaigns_total": kpi_scope.count(),
            "campaigns_active": kpi_scope.filter(is_active=True).count(),
            "campaigns_recently_updated": kpi_scope.filter(updated_at__gte=recently_updated_threshold).count(),
            "campaigns_archived": all_campaigns_qs.filter(is_archived=True).count(),
            "templates_active": MessageTemplate.objects.filter(is_active=True).count(),
            "scenarios_active": NotificationScenario.objects.filter(is_active=True).count(),
            "dispatch_total": int(dispatch_stats.get("total") or 0),
            "dispatch_pending": int(dispatch_stats.get("pending") or 0)
            + int(dispatch_stats.get("queued") or 0)
            + int(dispatch_stats.get("in_progress") or 0),
            "dispatch_done": int(dispatch_stats.get("done") or 0),
            "dispatch_failed": int(dispatch_stats.get("failed") or 0),
        }

        campaigns = list(campaigns_qs[:100])
        for campaign in campaigns:
            template_obj = getattr(campaign, "template", None)
            display_name, technical_name = _resolve_template_title(template_obj)
            campaign.template_display_name = display_name
            campaign.template_technical_name = technical_name
            campaign.ui_status = _build_mailing_ui_status(
                campaign,
                row_stats={
                    "total": int(campaign.recipients_total or 0),
                    "planned": int(campaign.recipients_planned or 0),
                    "in_progress": int(campaign.recipients_in_progress or 0),
                    "done": int(campaign.recipients_done or 0),
                    "error": int(campaign.recipients_error or 0),
                },
            )

        context["campaigns_total_filtered"] = campaigns_qs.count()
        context["campaigns"] = campaigns
        context["filters"] = {
            "q": q,
            "only_active": only_active,
            "with_errors": with_errors,
            "show_archived": show_archived,
            "created_from": created_from_raw,
            "created_to": created_to_raw,
        }
        context["current_query_path"] = self.request.get_full_path()
        context["mailings_v2_flow"] = _build_mailings_v2_flow(active_area="campaigns")
        return context


class _MailingsV2CampaignFormMixin:
    """
    Общая логика формы кампании в новом UI.

    Используем текущую backend-модель и форму без смены контракта.
    """

    model = Mailing
    form_class = MailingForm
    template_name = "mailing_v2/campaign_form.html"

    @staticmethod
    def _active_templates_queryset():
        """
        Базовый queryset активных шаблонов для формы кампании.
        """
        return MessageTemplate.objects.filter(is_active=True).order_by("-created_at")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # В v2 по умолчанию показываем только активные шаблоны.
        if "template" in form.fields:
            active_templates = list(self._active_templates_queryset())
            user_templates = [template_obj for template_obj in active_templates if not _is_system_template(template_obj)]
            selected_templates = user_templates if user_templates else active_templates

            selected_template_ids = [template_obj.pk for template_obj in selected_templates]
            template_qs = self._active_templates_queryset().filter(pk__in=selected_template_ids)

            self._only_system_templates_available = bool(active_templates) and not bool(user_templates)
            self._active_templates_count = len(active_templates)

            form.fields["template"].queryset = template_qs
            form.fields["template"].label_from_instance = lambda template_obj: _resolve_template_title(template_obj)[0]

        if "bot_profiles" in form.fields:
            active_bot_profiles_qs = BotProfile.objects.filter(is_active=True).order_by("name", "id")
            form.fields["bot_profiles"].queryset = active_bot_profiles_qs
            self._has_active_bot_profiles = active_bot_profiles_qs.exists()

        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = getattr(self, "object", None)
        context["is_create"] = not bool(mailing and mailing.pk)
        context["legacy_list_url"] = reverse("mailings")
        context["v2_list_url"] = reverse("mailings_v2_campaigns")
        context["only_system_templates_available"] = bool(getattr(self, "_only_system_templates_available", False))
        context["active_templates_count"] = int(getattr(self, "_active_templates_count", 0))
        context["has_active_bot_profiles"] = bool(getattr(self, "_has_active_bot_profiles", False))
        context["bot_profiles_admin_url"] = "/admin/guests/botprofile/"
        form = context.get("form")
        template_queryset = None
        if form is not None and "template" in form.fields:
            template_queryset = form.fields["template"].queryset
        context["template_texts_by_id"] = {
            str(template_obj.id): str(template_obj.message_text or "")
            for template_obj in (template_queryset or MessageTemplate.objects.none())
        }

        if mailing and mailing.pk:
            row_stats = _build_mailing_row_stats(mailing)
            dispatch_stats = _build_mailing_dispatch_stats(mailing)
            context["guests_count"] = mailing.guests_rows.count()
            context["campaign_active_tab"] = "params"
            context["status_url"] = reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.pk})
            context["legacy_edit_url"] = reverse("mailing_edit", kwargs={"pk": mailing.pk})
            context["audience_url"] = reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.pk})
            context["runs_url"] = reverse("mailings_v2_campaigns_runs", kwargs={"pk": mailing.pk})
            context["ops_url"] = reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.pk})
            snapshot = _get_workbench_snapshot(self.request, mailing)
            context["workbench_snapshot"] = snapshot
            context["workbench_snapshot_url"] = _build_workbench_url_from_snapshot(snapshot) if snapshot else ""
            context["mailing_row_stats"] = row_stats
            context["dispatch_stats"] = dispatch_stats
            context["mailing_ui_status"] = _build_mailing_ui_status(
                mailing,
                row_stats=row_stats,
                dispatch_stats=dispatch_stats,
            )
        else:
            context["guests_count"] = 0
            context["campaign_active_tab"] = ""
            context["status_url"] = ""
            context["legacy_edit_url"] = ""
            context["audience_url"] = ""
            context["runs_url"] = ""
            context["ops_url"] = ""
            context["workbench_snapshot"] = None
            context["workbench_snapshot_url"] = ""
            context["mailing_row_stats"] = _empty_mailing_row_stats()
            context["dispatch_stats"] = _empty_dispatch_stats()
            context["mailing_ui_status"] = _build_mailing_ui_status(None)
        return context


class MailingsV2CampaignCreateView(_MailingsV2CampaignFormMixin, CreateView):
    """
    Создание кампании в новом UI.

    Логика сохранения соответствует текущей legacy-форме.
    """

    def get_initial(self):
        """
        Поддерживает prefill шаблона при переходе из раздела templates.
        """
        initial = super().get_initial()
        today = timezone.localdate()
        period_end = today + timedelta(days=14)
        initial.setdefault("scheduled_date", today.isoformat())
        initial.setdefault("scheduled_time_begin", f"{today.isoformat()}T00:00")
        initial.setdefault("scheduled_time_end", f"{period_end.isoformat()}T23:59")
        initial.setdefault("send_window_begin", "09:00")
        initial.setdefault("send_window_end", "21:00")

        template_id_raw = str(self.request.GET.get("template_id") or "").strip()
        if template_id_raw.isdigit():
            template = MessageTemplate.objects.filter(pk=int(template_id_raw), is_active=True).first()
            if template:
                initial["template"] = template.pk
                if not initial.get("name"):
                    initial["name"] = f"Кампания: {template.name}"
        return initial

    def form_valid(self, form):
        self.object = form.save(commit=False)
        self.object.is_active = False
        self.object.is_archived = False

        now = timezone.now()
        if hasattr(self.object, "created_at") and not self.object.created_at:
            self.object.created_at = now
        if hasattr(self.object, "updated_at"):
            self.object.updated_at = now

        self.object.save()
        form.save_m2m()
        messages.success(self.request, f"Кампания создана (ID {self.object.id}).")
        return redirect("mailings_v2_campaigns_edit", pk=self.object.pk)


class MailingsV2CampaignUpdateView(_MailingsV2CampaignFormMixin, UpdateView):
    """
    Редактирование кампании в новом UI.
    """

    def form_valid(self, form):
        self.object = form.save(commit=False)
        if hasattr(self.object, "updated_at"):
            self.object.updated_at = timezone.now()
        self.object.save()
        form.save_m2m()
        messages.success(self.request, "Изменения кампании сохранены.")
        return redirect("mailings_v2_campaigns_edit", pk=self.object.pk)


class MailingsV2CampaignStatusView(TemplateView):
    """
    Экран статуса и операционного управления кампанией.

    Сводит в одном месте:
    1. ключевые счётчики аудитории и доставки;
    2. переходы к операционным экранам;
    3. управляющие действия: запуск, пауза, повтор, проверка перед запуском, немедленный запуск.
    """

    template_name = "mailing_v2/campaign_status.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])
        context["mailing"] = mailing
        context["v2_list_url"] = reverse("mailings_v2_campaigns")
        context["campaign_active_tab"] = "status"
        context["audience_url"] = reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.pk})
        context["runs_url"] = reverse("mailings_v2_campaigns_runs", kwargs={"pk": mailing.pk})
        context["jobs_url"] = reverse("mailings_v2_campaigns_jobs", kwargs={"pk": mailing.pk})
        context["errors_url"] = reverse("mailings_v2_campaigns_errors", kwargs={"pk": mailing.pk})
        context["logs_url"] = reverse("mailings_v2_campaigns_logs", kwargs={"pk": mailing.pk})
        context["ops_url"] = reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.pk})
        context["legacy_logs_url"] = reverse("mailing_logs", kwargs={"pk": mailing.pk})
        context["legacy_logs_txt_url"] = reverse("mailing_logs_txt", kwargs={"pk": mailing.pk})
        context["coupon_report_url"] = (
            f"{reverse('reports_coupon_campaigns')}?{urlencode({'campaign_id': mailing.id})}"
        )
        context["guests_count"] = mailing.guests_rows.count()
        row_stats = _build_mailing_row_stats(mailing)
        dispatch_stats = _build_mailing_dispatch_stats(mailing)
        snapshot = _get_workbench_snapshot(self.request, mailing)
        context["mailing_row_stats"] = row_stats
        context["dispatch_stats"] = dispatch_stats
        context["mailing_ui_status"] = _build_mailing_ui_status(
            mailing,
            row_stats=row_stats,
            dispatch_stats=dispatch_stats,
        )
        context["workbench_snapshot"] = snapshot
        context["workbench_snapshot_url"] = _build_workbench_url_from_snapshot(snapshot) if snapshot else ""
        context["mailing_ops_dry_run_report"] = self.request.session.pop("mailing_ops_dry_run_report", None)
        context["mailing_ops_run_now_report"] = self.request.session.pop("mailing_ops_run_now_report", None)

        coupon_campaign_report = None
        coupon_campaign_report_error = ""
        if str(getattr(mailing, "coupon_series", "") or "").strip():
            try:
                coupon_campaign_report = build_coupon_campaign_performance_snapshot(
                    mailing=mailing
                ).to_dict()
            except Exception as err:  # noqa: BLE001
                logger.exception(
                    "Coupon campaign report build failed: campaign_id=%s error=%s",
                    mailing.id,
                    err,
                )
                coupon_campaign_report_error = (
                    "Не удалось построить купонный отчёт. Проверьте логи сервиса."
                )
        context["coupon_campaign_report"] = coupon_campaign_report
        context["coupon_campaign_report_error"] = coupon_campaign_report_error
        context["coupon_iiko_category_stats"] = (
            _build_coupon_iiko_category_stats(mailing)
            if str(getattr(mailing, "coupon_series", "") or "").strip()
            else None
        )
        return context


class MailingsV2CampaignOpsView(View):
    """
    Операционные POST-действия для кампании в mailings-v2.

    Поддерживает:
    1. безопасный старт/пауза кампании;
    2. возврат ошибочных и зависших строк в состояние «запланировано»;
    3. ручной повтор задач доставки со статусом «ошибка».
    4. безопасную отмену кампании с освобождением неотправленных купонов.
    """

    http_method_names = ["post"]

    @staticmethod
    def _resolve_next_url(request, default_url: str) -> str:
        next_url = (request.POST.get("next") or "").strip()
        if next_url and url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return next_url
        return default_url

    def post(self, request, *args, **kwargs):
        mailing = get_object_or_404(Mailing, pk=kwargs["pk"])
        action = (request.POST.get("action") or "").strip()
        list_url = reverse("mailings_v2_campaigns")
        status_url = reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.pk})

        if mailing.is_archived and action in {
            "toggle_active",
            "retry_failed_rows",
            "requeue_in_progress_rows",
            "retry_failed_dispatch",
            "dry_run_campaign",
            "run_now_campaign",
            "cancel_campaign",
        }:
            messages.error(request, "Архивная кампания недоступна для операционных действий.")
            return redirect(self._resolve_next_url(request, status_url))

        if action == "toggle_active":
            mailing.is_active = not bool(mailing.is_active)
            if hasattr(mailing, "updated_at"):
                mailing.updated_at = timezone.now()
            mailing.save(update_fields=["is_active"] + (["updated_at"] if hasattr(mailing, "updated_at") else []))
            if mailing.is_active:
                messages.success(request, f"Кампания #{mailing.id} запущена.")
            else:
                messages.success(request, f"Кампания #{mailing.id} поставлена на паузу.")
            return redirect(self._resolve_next_url(request, status_url))

        if action == "retry_failed_rows":
            updated = MailingGuest.objects.filter(
                mailing=mailing,
                status=MailingGuest.Status.ERROR,
            ).update(
                status=MailingGuest.Status.PLANNED,
                delivery_status="retry_requested",
                error_description=None,
                scheduled_datetime=timezone.now(),
            )
            if updated > 0:
                messages.success(request, f"Ошибочных строк возвращено в запланированные: {updated}.")
            else:
                messages.info(request, "Ошибочных строк не найдено.")
            return redirect(self._resolve_next_url(request, status_url))

        if action == "requeue_in_progress_rows":
            updated = MailingGuest.objects.filter(
                mailing=mailing,
                status=MailingGuest.Status.IN_PROGRESS,
            ).update(
                status=MailingGuest.Status.PLANNED,
                delivery_status="requeued_from_ui",
            )
            if updated > 0:
                messages.success(request, f"Зависших строк возвращено в запланированные: {updated}.")
            else:
                messages.info(request, "Зависших строк не найдено.")
            return redirect(self._resolve_next_url(request, status_url))

        if action == "retry_failed_dispatch":
            now = timezone.now()
            updated = DispatchTask.objects.filter(
                mailing_guest__mailing=mailing,
                status=DispatchTask.Status.FAILED,
            ).update(
                status=DispatchTask.Status.PENDING,
                enqueued_at=None,
                queue_name=None,
                started_at=None,
                finished_at=None,
                last_error=None,
                available_at=now,
                updated_at=now,
                attempt=0,
            )
            if updated > 0:
                messages.success(request, f"Задач доставки возвращено в ожидание: {updated}.")
            else:
                messages.info(request, "Задач доставки с ошибкой не найдено.")
            return redirect(self._resolve_next_url(request, status_url))

        if action == "dry_run_campaign":
            report = _build_mailing_dry_run_report(mailing=mailing, now=timezone.now())
            request.session["mailing_ops_dry_run_report"] = report
            request.session.modified = True
            messages.info(
                request,
                (
                    f"Проверка перед запуском: готово строк={report['ready_rows']}, "
                    f"доступно для отправки={report['ready_rows_with_targets']}, "
                    f"заблокировано={report['ready_rows_without_targets']}."
                ),
            )
            return redirect(self._resolve_next_url(request, status_url))

        if action == "run_now_campaign":
            report = _run_mailing_now(
                mailing=mailing,
                now=timezone.now(),
                max_batches=MAILINGS_V2_RUN_NOW_MAX_BATCHES,
            )
            request.session["mailing_ops_run_now_report"] = report
            request.session.modified = True
            processed_rows = int(report.get("processed_rows_total") or 0)
            if processed_rows > 0:
                messages.success(
                    request,
                    (
                        f"Немедленный запуск: обработано строк {processed_rows}, "
                        f"батчей {report['processed_batches']}, "
                        f"достигнут лимит батчей={report['reached_batch_limit']}."
                    ),
                )
            else:
                messages.info(
                    request,
                    (
                        f"Немедленный запуск: строки не обработаны "
                        f"(кампания в периоде={report['schedule_window_open']}, "
                        f"окно отправки открыто={report['send_window_open']}, "
                        f"готово строк={report['ready_rows_before']})."
                    ),
                )
            return redirect(self._resolve_next_url(request, status_url))

        if action == "cancel_campaign":
            if mailing.is_archived:
                messages.info(request, f"Кампания #{mailing.id} уже в архиве.")
                return redirect(self._resolve_next_url(request, status_url))

            lifecycle_service = CouponCampaignLifecycleService()
            stats = lifecycle_service.cancel_campaign(
                mailing=mailing,
                reason="campaign_canceled_by_operator",
                now=timezone.now(),
                dry_run=False,
            )
            payload = stats.to_dict()
            messages.success(
                request,
                (
                    f"Кампания #{mailing.id} остановлена. "
                    f"Строк отменено={payload['rows_canceled']}, "
                    f"задач доставки отменено={payload['dispatch_tasks_canceled']}, "
                    f"купонов подготовлено к освобождению={payload.get('assignments_release_pending', 0)}."
                ),
            )
            return redirect(self._resolve_next_url(request, status_url))

        if action == "archive_campaign":
            if mailing.is_archived:
                messages.info(request, f"Кампания #{mailing.id} уже в архиве.")
            else:
                now = timezone.now()
                mailing.is_archived = True
                mailing.is_active = False
                if hasattr(mailing, "updated_at"):
                    mailing.updated_at = now
                    mailing.save(update_fields=["is_archived", "is_active", "updated_at"])
                else:
                    mailing.save(update_fields=["is_archived", "is_active"])
                messages.success(request, f"Кампания #{mailing.id} перенесена в архив.")
            return redirect(self._resolve_next_url(request, list_url))

        if action == "duplicate_campaign":
            now = timezone.now()
            with transaction.atomic():
                duplicate = Mailing.objects.create(
                    name=f"{mailing.name} (копия)",
                    template=mailing.template,
                    scheduled_date=mailing.scheduled_date,
                    scheduled_time_begin=mailing.scheduled_time_begin,
                    scheduled_time_end=mailing.scheduled_time_end,
                    is_active=False,
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                    send_window_begin=mailing.send_window_begin,
                    send_window_end=mailing.send_window_end,
                    target_mode=mailing.target_mode,
                    queue_priority=mailing.queue_priority,
                    source_filter_snapshot=mailing.source_filter_snapshot or {},
                    coupon_series=mailing.coupon_series,
                    coupon_venue_code=mailing.coupon_venue_code,
                    coupon_venue_name=mailing.coupon_venue_name,
                    coupon_promo_text=mailing.coupon_promo_text,
                )
                duplicate.bot_profiles.set(mailing.bot_profiles.all())
                source_rows = mailing.guests_rows.values(
                    "guest_id",
                    "phone",
                    "email",
                    "text_mailing_list",
                    "scheduled_datetime",
                )
                duplicate_rows = [
                    MailingGuest(
                        mailing=duplicate,
                        guest_id=row["guest_id"],
                        phone=row["phone"],
                        email=row["email"],
                        text_mailing_list=row["text_mailing_list"],
                        scheduled_datetime=row["scheduled_datetime"],
                        status=MailingGuest.Status.PLANNED,
                        error_description=None,
                        external_id=None,
                        sent_at=None,
                        delivery_status="duplicated_from_campaign",
                        created_at=now,
                    )
                    for row in source_rows
                ]
                if duplicate_rows:
                    MailingGuest.objects.bulk_create(duplicate_rows, batch_size=1000)

            messages.success(
                request,
                f"Кампания #{mailing.id} продублирована: создана #{duplicate.id}, строк аудитории={len(duplicate_rows)}.",
            )
            return redirect("mailings_v2_campaigns_edit", pk=duplicate.pk)

        messages.error(request, "Неизвестное действие кампании.")
        return redirect(self._resolve_next_url(request, status_url))


class MailingsV2CampaignAudienceView(TemplateView):
    """
    Просмотр аудитории выбранной кампании.

    Экран нужен как промежуточная валидация состава перед отправкой.
    """

    template_name = "mailing_v2/campaign_audience.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])
        rows_qs = (
            MailingGuest.objects.filter(mailing=mailing)
            .select_related("guest")
            .order_by("-id")
        )
        rows = list(rows_qs[:300])
        _decorate_mailing_rows(rows)
        context["mailing"] = mailing
        context["rows"] = rows
        context["stats"] = rows_qs.aggregate(
            total=Count("id"),
            planned=Count("id", filter=Q(status=MailingGuest.Status.PLANNED)),
            in_progress=Count("id", filter=Q(status=MailingGuest.Status.IN_PROGRESS)),
            done=Count("id", filter=Q(status=MailingGuest.Status.DONE)),
            error=Count("id", filter=Q(status=MailingGuest.Status.ERROR)),
        )
        context["mailing_import_report"] = self.request.session.pop("mailing_import_report", None)
        context["mailing_import_error"] = self.request.session.pop("mailing_import_error", None)
        snapshot = _get_workbench_snapshot(self.request, mailing)
        context["workbench_snapshot"] = snapshot
        context["workbench_snapshot_url"] = _build_workbench_url_from_snapshot(snapshot) if snapshot else ""
        context["campaign_active_tab"] = "audience"
        return context


class MailingsV2CampaignRunsView(TemplateView):
    """
    Экран запусков/истории по конкретной кампании.

    Даёт операционный срез по двум связанным слоям:
    1. строки получателей (`MailingGuest`);
    2. задачи доставки (`DispatchTask`).
    """

    template_name = "mailing_v2/campaign_runs.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])

        query = (self.request.GET.get("q") or "").strip()
        selected_row_status = (self.request.GET.get("row_status") or "").strip()
        selected_task_status = (self.request.GET.get("task_status") or "").strip()
        selected_provider_type = (self.request.GET.get("provider_type") or "").strip()

        valid_row_statuses = {value for value, _ in MailingGuest.Status.choices}
        valid_task_statuses = {value for value, _ in DispatchTask.Status.choices}
        valid_providers = {value for value, _ in BotProfile.ProviderType.choices}

        rows_scope = MailingGuest.objects.filter(mailing=mailing).select_related("guest")
        if selected_row_status in valid_row_statuses:
            rows_scope = rows_scope.filter(status=selected_row_status)
        else:
            selected_row_status = ""

        tasks_scope = DispatchTask.objects.filter(mailing_guest__mailing=mailing).select_related(
            "mailing_guest",
            "guest",
            "bot_profile",
        )
        if selected_task_status in valid_task_statuses:
            tasks_scope = tasks_scope.filter(status=selected_task_status)
        else:
            selected_task_status = ""

        if selected_provider_type in valid_providers:
            tasks_scope = tasks_scope.filter(provider_type=selected_provider_type)
        else:
            selected_provider_type = ""

        if query:
            rows_scope = rows_scope.filter(
                Q(phone__icontains=query)
                | Q(delivery_status__icontains=query)
                | Q(error_description__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(guest__first_name__icontains=query)
                | Q(guest__last_name__icontains=query)
            )
            tasks_scope = tasks_scope.filter(
                Q(external_chat_id__icontains=query)
                | Q(last_error__icontains=query)
                | Q(message_text__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(mailing_guest__phone__icontains=query)
            )

        rows_filtered_total = rows_scope.count()
        tasks_filtered_total = tasks_scope.count()

        rows = list(rows_scope.order_by("-id")[:200])
        tasks = list(tasks_scope.order_by("-id")[:200])
        _decorate_mailing_rows(rows)
        _decorate_dispatch_tasks(tasks)

        timeline = _build_dispatch_timeline(tasks_scope.order_by("-updated_at")[:60])

        context["mailing"] = mailing
        context["rows"] = rows
        context["tasks"] = tasks
        context["timeline"] = timeline
        context["rows_filtered_total"] = rows_filtered_total
        context["tasks_filtered_total"] = tasks_filtered_total
        context["row_stats"] = _build_mailing_row_stats(mailing)
        context["task_stats"] = _build_mailing_dispatch_stats(mailing)
        context["row_status_choices"] = _localized_choices(
            MailingGuest.Status.choices,
            _localize_mailing_row_status,
        )
        context["task_status_choices"] = _localized_choices(
            DispatchTask.Status.choices,
            _localize_dispatch_status,
        )
        context["provider_choices"] = _localized_choices(
            BotProfile.ProviderType.choices,
            _localize_provider_type,
        )
        context["selected_row_status"] = selected_row_status
        context["selected_task_status"] = selected_task_status
        context["selected_provider_type"] = selected_provider_type
        context["query"] = query
        context["campaign_active_tab"] = "runs"
        return context


class MailingsV2CampaignJobsView(TemplateView):
    """
    Экран заданий отправки по конкретной кампании.

    Фокус:
    1. операционный срез по DispatchTask;
    2. фильтры по статусу/провайдеру/очереди;
    3. агрегаты для быстрой диагностики по результатам доставки.
    """

    template_name = "mailing_v2/campaign_jobs.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])

        query = (self.request.GET.get("q") or "").strip()
        selected_task_status = (self.request.GET.get("task_status") or "").strip()
        selected_provider_type = (self.request.GET.get("provider_type") or "").strip()
        selected_queue_name = (self.request.GET.get("queue_name") or "").strip()

        valid_task_statuses = {value for value, _ in DispatchTask.Status.choices}
        valid_providers = {value for value, _ in BotProfile.ProviderType.choices}

        tasks_scope = DispatchTask.objects.filter(mailing_guest__mailing=mailing).select_related(
            "mailing_guest",
            "guest",
            "bot_profile",
        )

        if selected_task_status in valid_task_statuses:
            tasks_scope = tasks_scope.filter(status=selected_task_status)
        else:
            selected_task_status = ""

        if selected_provider_type in valid_providers:
            tasks_scope = tasks_scope.filter(provider_type=selected_provider_type)
        else:
            selected_provider_type = ""

        if selected_queue_name:
            tasks_scope = tasks_scope.filter(queue_name=selected_queue_name)

        if query:
            tasks_scope = tasks_scope.filter(
                Q(external_chat_id__icontains=query)
                | Q(last_error__icontains=query)
                | Q(message_text__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(mailing_guest__phone__icontains=query)
            )

        tasks_filtered_total = int(tasks_scope.count())
        tasks = list(tasks_scope.order_by("-updated_at", "-id")[:250])
        _decorate_dispatch_tasks(tasks)

        provider_status_rows = list(
            tasks_scope.values("provider_type", "status").annotate(total=Count("id")).order_by("provider_type", "status")
        )
        for row in provider_status_rows:
            row["provider_type_label"] = _localize_provider_type(row.get("provider_type"))
            row["status_label"] = _localize_dispatch_status(row.get("status"))
        queue_rows = list(
            tasks_scope.values("queue_name").annotate(total=Count("id")).order_by("-total", "queue_name")[:30]
        )
        for row in queue_rows:
            row["queue_name_label"] = _localize_queue_name(row.get("queue_name"))
        top_errors = list(
            tasks_scope.filter(status=DispatchTask.Status.FAILED)
            .exclude(last_error__isnull=True)
            .exclude(last_error__exact="")
            .values("provider_type", "last_error")
            .annotate(total=Count("id"))
            .order_by("-total", "provider_type", "last_error")[:25]
        )
        for row in top_errors:
            row["provider_type_label"] = _localize_provider_type(row.get("provider_type"))
        delivery_feedback_rows = list(
            MailingGuest.objects.filter(mailing=mailing)
            .exclude(delivery_status__isnull=True)
            .exclude(delivery_status__exact="")
            .values("delivery_status")
            .annotate(total=Count("id"))
            .order_by("-total", "delivery_status")[:25]
        )
        for row in delivery_feedback_rows:
            row["delivery_status_label"] = _localize_delivery_status(row.get("delivery_status"))

        context["mailing"] = mailing
        context["query"] = query
        context["selected_task_status"] = selected_task_status
        context["selected_provider_type"] = selected_provider_type
        context["selected_queue_name"] = selected_queue_name
        context["task_status_choices"] = _localized_choices(
            DispatchTask.Status.choices,
            _localize_dispatch_status,
        )
        context["provider_choices"] = _localized_choices(
            BotProfile.ProviderType.choices,
            _localize_provider_type,
        )
        queue_name_values = list(
            DispatchTask.objects.filter(mailing_guest__mailing=mailing)
            .exclude(queue_name__isnull=True)
            .exclude(queue_name__exact="")
            .values_list("queue_name", flat=True)
            .distinct()
            .order_by("queue_name")
        )
        context["queue_name_choices"] = [
            (queue_name, _localize_queue_name(queue_name))
            for queue_name in queue_name_values
        ]
        context["tasks"] = tasks
        context["tasks_filtered_total"] = tasks_filtered_total
        context["task_stats"] = _build_mailing_dispatch_stats(mailing)
        context["provider_status_rows"] = provider_status_rows
        context["queue_rows"] = queue_rows
        context["top_errors"] = top_errors
        context["delivery_feedback_rows"] = delivery_feedback_rows
        context["campaign_active_tab"] = "jobs"
        return context


class MailingsV2CampaignErrorsView(TemplateView):
    """
    Экран ошибок кампании в mailings-v2.

    Показывает две проблемные зоны:
    1. error-строки аудитории (`MailingGuest`);
    2. failed-задачи доставки (`DispatchTask`).
    """

    template_name = "mailing_v2/campaign_errors.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])

        query = (self.request.GET.get("q") or "").strip()
        selected_delivery_status = (self.request.GET.get("delivery_status") or "").strip()
        selected_provider_type = (self.request.GET.get("provider_type") or "").strip()

        delivery_status_choices = list(
            MailingGuest.objects.filter(mailing=mailing)
            .exclude(delivery_status__isnull=True)
            .exclude(delivery_status__exact="")
            .values_list("delivery_status", flat=True)
            .distinct()
            .order_by("delivery_status")
        )
        if selected_delivery_status and selected_delivery_status not in set(delivery_status_choices):
            selected_delivery_status = ""

        valid_providers = {value for value, _ in BotProfile.ProviderType.choices}
        if selected_provider_type not in valid_providers:
            selected_provider_type = ""

        row_error_codes = {
            "dispatch_no_targets",
            "dispatch_no_bot_profiles",
            "dispatch_enqueue_error",
            "dispatch_enqueue_exception",
            "retry_requested",
            "requeued_from_ui",
        }
        row_errors_scope = MailingGuest.objects.filter(mailing=mailing).filter(
            Q(status=MailingGuest.Status.ERROR)
            | Q(error_description__isnull=False)
            | Q(delivery_status__in=row_error_codes)
        ).select_related("guest")
        row_errors_scope = row_errors_scope.exclude(
            Q(error_description__isnull=True)
            & (Q(delivery_status__isnull=True) | Q(delivery_status__exact=""))
            & ~Q(status=MailingGuest.Status.ERROR)
        )
        if selected_delivery_status:
            row_errors_scope = row_errors_scope.filter(delivery_status=selected_delivery_status)
        if query:
            row_errors_scope = row_errors_scope.filter(
                Q(phone__icontains=query)
                | Q(delivery_status__icontains=query)
                | Q(error_description__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(guest__first_name__icontains=query)
                | Q(guest__last_name__icontains=query)
            )

        failed_dispatch_scope = DispatchTask.objects.filter(
            mailing_guest__mailing=mailing,
            status=DispatchTask.Status.FAILED,
        ).select_related("mailing_guest", "guest", "bot_profile")
        if selected_provider_type:
            failed_dispatch_scope = failed_dispatch_scope.filter(provider_type=selected_provider_type)
        if query:
            failed_dispatch_scope = failed_dispatch_scope.filter(
                Q(external_chat_id__icontains=query)
                | Q(last_error__icontains=query)
                | Q(message_text__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(mailing_guest__phone__icontains=query)
            )

        row_errors_total = row_errors_scope.count()
        failed_dispatch_total = failed_dispatch_scope.count()
        row_error_groups = list(
            row_errors_scope.values("delivery_status")
            .annotate(total=Count("id"))
            .order_by("-total", "delivery_status")[:20]
        )
        for row in row_error_groups:
            row["delivery_status_label"] = _localize_delivery_status(row.get("delivery_status"))
        dispatch_error_groups = list(
            failed_dispatch_scope.values("provider_type", "last_error")
            .annotate(total=Count("id"))
            .order_by("-total", "provider_type", "last_error")[:20]
        )
        for row in dispatch_error_groups:
            row["provider_type_label"] = _localize_provider_type(row.get("provider_type"))
        row_errors = list(row_errors_scope.order_by("-id")[:200])
        failed_dispatch = list(failed_dispatch_scope.order_by("-updated_at", "-id")[:200])
        _decorate_mailing_rows(row_errors)
        _decorate_dispatch_tasks(failed_dispatch)

        context["mailing"] = mailing
        context["query"] = query
        context["selected_delivery_status"] = selected_delivery_status
        context["selected_provider_type"] = selected_provider_type
        context["delivery_status_choices"] = [
            (value, _localize_delivery_status(value))
            for value in delivery_status_choices
        ]
        context["provider_choices"] = _localized_choices(
            BotProfile.ProviderType.choices,
            _localize_provider_type,
        )
        context["row_errors_total"] = row_errors_total
        context["failed_dispatch_total"] = failed_dispatch_total
        context["row_error_groups"] = row_error_groups
        context["dispatch_error_groups"] = dispatch_error_groups
        context["row_errors"] = row_errors
        context["failed_dispatch"] = failed_dispatch
        context["current_query_path"] = self.request.get_full_path()
        context["row_stats"] = _build_mailing_row_stats(mailing)
        context["task_stats"] = _build_mailing_dispatch_stats(mailing)
        context["campaign_active_tab"] = "errors"
        return context


class MailingsV2CampaignLogsView(TemplateView):
    """
    Экран логов кампании в mailings-v2.

    Даёт комбинированный журнал:
    1. изменения строк аудитории (`MailingGuest`);
    2. события dispatch-задач (`DispatchTask`).
    """

    template_name = "mailing_v2/campaign_logs.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])

        query = (self.request.GET.get("q") or "").strip()
        selected_row_status = (self.request.GET.get("row_status") or "").strip()
        selected_task_status = (self.request.GET.get("task_status") or "").strip()

        valid_row_statuses = {value for value, _ in MailingGuest.Status.choices}
        valid_task_statuses = {value for value, _ in DispatchTask.Status.choices}

        rows_scope = MailingGuest.objects.filter(mailing=mailing).select_related("guest")
        if selected_row_status in valid_row_statuses:
            rows_scope = rows_scope.filter(status=selected_row_status)
        else:
            selected_row_status = ""

        tasks_scope = DispatchTask.objects.filter(mailing_guest__mailing=mailing).select_related(
            "mailing_guest",
            "guest",
            "bot_profile",
        )
        if selected_task_status in valid_task_statuses:
            tasks_scope = tasks_scope.filter(status=selected_task_status)
        else:
            selected_task_status = ""

        if query:
            rows_scope = rows_scope.filter(
                Q(phone__icontains=query)
                | Q(delivery_status__icontains=query)
                | Q(error_description__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(guest__first_name__icontains=query)
                | Q(guest__last_name__icontains=query)
            )
            tasks_scope = tasks_scope.filter(
                Q(external_chat_id__icontains=query)
                | Q(last_error__icontains=query)
                | Q(message_text__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(mailing_guest__phone__icontains=query)
            )

        rows_filtered_total = rows_scope.count()
        tasks_filtered_total = tasks_scope.count()

        rows = list(rows_scope.order_by("-id")[:200])
        tasks = list(tasks_scope.order_by("-updated_at", "-id")[:200])
        _decorate_mailing_rows(rows)
        _decorate_dispatch_tasks(tasks)
        timeline = _build_mailing_log_timeline(rows=rows[:120], tasks=tasks[:120])

        context["mailing"] = mailing
        context["query"] = query
        context["selected_row_status"] = selected_row_status
        context["selected_task_status"] = selected_task_status
        context["row_status_choices"] = _localized_choices(
            MailingGuest.Status.choices,
            _localize_mailing_row_status,
        )
        context["task_status_choices"] = _localized_choices(
            DispatchTask.Status.choices,
            _localize_dispatch_status,
        )
        context["rows"] = rows
        context["tasks"] = tasks
        context["timeline"] = timeline
        context["rows_filtered_total"] = rows_filtered_total
        context["tasks_filtered_total"] = tasks_filtered_total
        context["row_stats"] = _build_mailing_row_stats(mailing)
        context["task_stats"] = _build_mailing_dispatch_stats(mailing)
        context["campaign_active_tab"] = "logs"
        return context


class MailingsV2TemplatesView(TemplateView):
    """
    Каркас раздела шаблонов в новом контуре.

    На этапе bridge используется текущий backend CRUD шаблонов.
    """

    template_name = "mailing_v2/templates_hub.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        show_inactive = bool(self.request.GET.get("show_inactive"))
        query = (self.request.GET.get("q") or "").strip()
        template_kind = str(self.request.GET.get("template_kind") or "all").strip().lower()
        if template_kind not in {"all", "user", "system"}:
            template_kind = "all"

        template_rows = MessageTemplate.objects.annotate(
            mailings_total=Count("mailings", distinct=True),
            scenarios_total=Count("notification_scenarios", distinct=True),
            active_coupon_autoscenarios_total=Count(
                "notification_scenarios__coupon_automation_config",
                filter=Q(
                    notification_scenarios__coupon_automation_config__execution_mode__in=(
                        COUPON_TEMPLATE_LOCK_EXECUTION_MODES
                    )
                ),
                distinct=True,
            ),
        ).order_by("-updated_at")

        if not show_inactive:
            template_rows = template_rows.filter(is_active=True)

        if query:
            template_rows = template_rows.filter(
                Q(name__icontains=query) | Q(description__icontains=query) | Q(message_text__icontains=query)
            )

        system_filter_q = Q(created_by__iexact="system") | (
            Q(name__startswith="SYSTEM_") & Q(name__endswith="_TEMPLATE")
        )
        if template_kind == "system":
            template_rows = template_rows.filter(system_filter_q)
        elif template_kind == "user":
            template_rows = template_rows.exclude(system_filter_q)

        templates = list(template_rows[:100])
        for template_obj in templates:
            display_name, technical_name = _resolve_template_title(template_obj)
            template_obj.display_name = display_name
            template_obj.technical_name = technical_name
            template_obj.is_system_template = _is_system_template(template_obj)

        context["templates_total"] = MessageTemplate.objects.count()
        context["templates_active"] = MessageTemplate.objects.filter(is_active=True).count()
        context["templates"] = templates
        context["show_inactive"] = show_inactive
        context["query"] = query
        context["template_kind"] = template_kind
        return context


class MailingsV2TemplateCreateView(CreateView):
    """
    Создание шаблона в новом контуре.
    """

    model = MessageTemplate
    form_class = MessageTemplateForm
    template_name = "mailing_v2/template_form.html"

    def _get_source_template(self) -> MessageTemplate | None:
        source_template_id = str(self.request.GET.get("source_template_id") or "").strip()
        if not source_template_id:
            return None
        if not source_template_id.isdigit():
            return None
        return MessageTemplate.objects.filter(pk=source_template_id).first()

    def get_initial(self):
        initial = super().get_initial()
        source_template = self._get_source_template()
        if source_template is None:
            return initial

        display_name, _technical_name = _resolve_template_title(source_template)
        copied_name = f"{display_name or source_template.name} (копия)"
        initial.update(
            {
                "name": copied_name[:150],
                "description": source_template.description or f"На основе шаблона ID {source_template.pk}",
                "message_text": source_template.message_text,
                "is_active": source_template.is_active,
            }
        )
        return initial

    @staticmethod
    def _build_new_template_preview_source() -> MessageTemplate:
        """
        Возвращает временный объект шаблона для предпросмотра на форме создания.
        """
        return MessageTemplate()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_guest_id = str(self.request.GET.get("guest_id") or "").strip()
        form = context.get("form")
        message_text_override = ""
        if form is not None:
            message_text_override = str(form.data.get("message_text") or form.initial.get("message_text") or "")

        context.update(
            _build_template_preview_state(
                template_obj=self._build_new_template_preview_source(),
                selected_guest_id=selected_guest_id,
                message_text_override=message_text_override,
            )
        )
        source_template = self._get_source_template()
        context["source_template"] = source_template
        context["source_template_detail_url"] = (
            reverse("mailings_v2_templates_detail", kwargs={"pk": source_template.pk})
            if source_template is not None
            else ""
        )
        context["preview_requested"] = bool(selected_guest_id)
        return context

    def post(self, request, *args, **kwargs):
        if str(request.POST.get("action") or "").strip() == "preview":
            self.object = None
            form = self.get_form()
            selected_guest_id = str(request.POST.get("preview_guest_id") or "").strip()
            message_text_override = str(request.POST.get("message_text") or "")

            context = self.get_context_data(form=form)
            context["selected_guest_id"] = selected_guest_id
            context.update(
                _build_template_preview_state(
                    template_obj=self._build_new_template_preview_source(),
                    selected_guest_id=selected_guest_id,
                    message_text_override=message_text_override,
                )
            )
            context["preview_requested"] = True
            return self.render_to_response(context)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        obj = form.save(commit=False)
        # На этом этапе сохраняем совместимость с текущим backend-контрактом.
        obj.created_by = "mailings_v2_user"
        obj.save()
        messages.success(self.request, f"Шаблон создан (ID {obj.id}).")
        return redirect("mailings_v2_templates_edit", pk=obj.pk)


class MailingsV2TemplateDetailView(DetailView):
    """
    Детальная карточка шаблона с предпросмотром на госте.
    """

    model = MessageTemplate
    template_name = "mailing_v2/template_detail.html"
    context_object_name = "template_obj"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        display_name, technical_name = _resolve_template_title(self.object)
        context["template_display_name"] = display_name
        context["template_technical_name"] = technical_name
        context["template_is_system"] = _is_system_template(self.object)
        context["campaign_prefill_url"] = (
            f"{reverse('mailings_v2_campaigns_new')}?{urlencode({'template_id': self.object.id})}"
        )
        context["template_copy_url"] = (
            f"{reverse('mailings_v2_templates_new')}?{urlencode({'source_template_id': self.object.id})}"
        )
        coupon_usage_rows = _build_coupon_autoscenario_template_usage_rows(self.object)
        context["coupon_autoscenario_usages"] = coupon_usage_rows
        context["template_locked_by_coupon_autoscenarios"] = any(
            row["is_locking"] for row in coupon_usage_rows
        )

        preview_context = _build_template_preview_state(
            template_obj=self.object,
            selected_guest_id=str(self.request.GET.get("guest_id") or "").strip(),
            message_text_override=self.object.message_text,
        )
        context.update(preview_context)
        return context


class MailingsV2TemplateUpdateView(UpdateView):
    """
    Редактирование шаблона в новом контуре.
    """

    model = MessageTemplate
    form_class = MessageTemplateForm
    template_name = "mailing_v2/template_form.html"

    def _build_editor_context(self) -> dict[str, object]:
        display_name, technical_name = _resolve_template_title(self.object)
        coupon_usage_rows = _build_coupon_autoscenario_template_usage_rows(self.object)
        return {
            "template_display_name": display_name,
            "template_technical_name": technical_name,
            "template_is_system": _is_system_template(self.object),
            "campaign_prefill_url": f"{reverse('mailings_v2_campaigns_new')}?{urlencode({'template_id': self.object.id})}",
            "template_copy_url": (
                f"{reverse('mailings_v2_templates_new')}?"
                f"{urlencode({'source_template_id': self.object.id})}"
            ),
            "coupon_autoscenario_usages": coupon_usage_rows,
            "template_locked_by_coupon_autoscenarios": any(
                row["is_locking"] for row in coupon_usage_rows
            ),
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self._build_editor_context())
        if context.get("template_locked_by_coupon_autoscenarios"):
            _mark_template_form_readonly(context.get("form"))

        selected_guest_id = str(self.request.GET.get("guest_id") or "").strip()
        preview_context = _build_template_preview_state(
            template_obj=self.object,
            selected_guest_id=selected_guest_id,
            message_text_override=self.object.message_text,
        )
        context.update(preview_context)
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if (
            str(request.POST.get("action") or "").strip() != "preview"
            and _is_template_locked_by_coupon_autoscenarios(self.object)
        ):
            messages.error(
                self.request,
                "Шаблон не сохранён: он используется в купонном автосценарии в режиме «Пилот» или «Активен».",
            )
            return redirect("mailings_v2_templates_detail", pk=self.object.pk)

        if str(request.POST.get("action") or "").strip() == "preview":
            form = self.get_form()
            selected_guest_id = str(request.POST.get("preview_guest_id") or "").strip()
            message_text_override = str(request.POST.get("message_text") or "")

            context = self.get_context_data(form=form, object=self.object)
            context.update(self._build_editor_context())
            context["selected_guest_id"] = selected_guest_id
            context.update(
                _build_template_preview_state(
                    template_obj=self.object,
                    selected_guest_id=selected_guest_id,
                    message_text_override=message_text_override,
                )
            )
            context["preview_requested"] = True
            return self.render_to_response(context)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, "Шаблон сохранен.")
        return redirect("mailings_v2_templates_edit", pk=self.object.pk)


def _build_coupon_autoscenario_template_usage_rows(template_obj: MessageTemplate | None) -> list[dict[str, object]]:
    """
    Возвращает купонные автосценарии, где используется шаблон.
    """
    if template_obj is None or not template_obj.pk:
        return []

    configs = (
        CouponAutomationConfig.objects.select_related("scenario")
        .filter(scenario__template=template_obj)
        .order_by("scenario__name", "scenario__code", "id")
    )
    rows: list[dict[str, object]] = []
    for config in configs:
        scenario = config.scenario
        rows.append(
            {
                "config_id": config.pk,
                "scenario_name": scenario.name,
                "scenario_code": scenario.code,
                "execution_mode": config.execution_mode,
                "execution_mode_label": config.get_execution_mode_display(),
                "is_locking": config.execution_mode in COUPON_TEMPLATE_LOCK_EXECUTION_MODES,
                "settings_url": reverse(
                    "mailings_v2_coupon_autoscenario_settings",
                    kwargs={"pk": config.pk},
                ),
            }
        )
    return rows


def _is_template_locked_by_coupon_autoscenarios(template_obj: MessageTemplate | None) -> bool:
    """
    Запрещает правку шаблона, уже задействованного в работающем купонном сценарии.
    """
    if template_obj is None or not template_obj.pk:
        return False
    return CouponAutomationConfig.objects.filter(
        scenario__template=template_obj,
        execution_mode__in=COUPON_TEMPLATE_LOCK_EXECUTION_MODES,
    ).exists()


def _mark_template_form_readonly(form) -> None:
    """
    Оставляет форму видимой, но не даёт случайно принять её как редактируемую.
    """
    if form is None:
        return
    for field_name, field in form.fields.items():
        if field_name == "is_active":
            field.widget.attrs["disabled"] = "disabled"
        else:
            field.widget.attrs["readonly"] = "readonly"


class MailingsV2MonitorView(TemplateView):
    """
    Каркас мониторинга задач доставки.

    Показывает агрегаты из DispatchTask по текущему состоянию очереди.
    """

    template_name = "mailing_v2/monitor_hub.html"

    @staticmethod
    def _build_redirect_url(*, return_query: str) -> str:
        """
        Собирает URL возврата на monitor с сохранением активных фильтров.
        """
        base_url = reverse("mailings_v2_monitor")
        safe_query = str(return_query or "").strip()
        if not safe_query:
            return base_url
        return f"{base_url}?{safe_query}"

    @staticmethod
    def _normalize_filters(params) -> dict[str, str]:
        """
        Нормализует входные фильтры monitor из QueryDict/словаря.
        """
        selected_mailing_id = str(params.get("mailing_id") or "").strip()
        selected_status = str(params.get("status") or "").strip()
        selected_provider = str(params.get("provider_type") or "").strip()
        selected_scenario_id = str(params.get("scenario_id") or "").strip()

        if not selected_mailing_id.isdigit():
            selected_mailing_id = ""
        if not selected_scenario_id.isdigit():
            selected_scenario_id = ""

        valid_statuses = {value for value, _ in DispatchTask.Status.choices}
        if selected_status not in valid_statuses:
            selected_status = ""

        valid_providers = {value for value, _ in BotProfile.ProviderType.choices}
        if selected_provider not in valid_providers:
            selected_provider = ""

        return {
            "mailing_id": selected_mailing_id,
            "status": selected_status,
            "provider_type": selected_provider,
            "scenario_id": selected_scenario_id,
        }

    @classmethod
    def _build_filtered_scope(cls, params):
        """
        Возвращает queryset DispatchTask по фильтрам monitor.
        """
        scope = DispatchTask.objects.filter(
            Q(mailing_guest__isnull=False) | Q(notification_scenario__isnull=False)
        ).select_related(
            "mailing_guest__mailing",
            "notification_scenario",
            "bot_profile",
            "guest",
        )
        filters = cls._normalize_filters(params)

        if filters["mailing_id"]:
            scope = scope.filter(mailing_guest__mailing_id=int(filters["mailing_id"]))
        if filters["status"]:
            scope = scope.filter(status=filters["status"])
        if filters["provider_type"]:
            scope = scope.filter(provider_type=filters["provider_type"])
        if filters["scenario_id"]:
            scope = scope.filter(notification_scenario_id=int(filters["scenario_id"]))

        return scope, filters

    def post(self, request, *args, **kwargs):
        """
        Быстрые операционные действия по задачам доставки на экране мониторинга.

        Поддерживает:
        1. повтор ошибочных задач с обнулением попыток;
        2. возврат ожидающих задач в очередь с доступностью с текущего момента.
        """
        action = str(request.POST.get("action") or "").strip()
        return_query = str(request.POST.get("return_query") or "").strip()
        redirect_url = self._build_redirect_url(return_query=return_query)

        filter_params = QueryDict(return_query, mutable=False) if return_query else request.POST
        scope, filters = self._build_filtered_scope(filter_params)
        now = timezone.now()

        if action == "retry_failed_tasks":
            candidates = scope.filter(status=DispatchTask.Status.FAILED)
            updated = candidates.update(
                status=DispatchTask.Status.PENDING,
                enqueued_at=None,
                queue_name=None,
                started_at=None,
                finished_at=None,
                available_at=now,
                updated_at=now,
                attempt=0,
                last_error=None,
            )
            request.session["mailings_v2_monitor_ops_report"] = {
                "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
                "action": "retry_failed_tasks",
                "action_label": "Перезапуск ошибочных задач",
                "updated_tasks": int(updated),
                "filters": filters,
            }
            if updated > 0:
                messages.success(request, f"Ошибочных задач возвращено в ожидание: {updated}.")
            else:
                messages.info(request, "Под выбранный фильтр ошибочные задачи не найдены.")
            return redirect(redirect_url)

        if action == "requeue_waiting_tasks":
            candidates = scope.filter(status__in=[DispatchTask.Status.PENDING, DispatchTask.Status.QUEUED])
            updated = candidates.update(
                status=DispatchTask.Status.PENDING,
                enqueued_at=None,
                queue_name=None,
                started_at=None,
                finished_at=None,
                available_at=now,
                updated_at=now,
            )
            request.session["mailings_v2_monitor_ops_report"] = {
                "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
                "action": "requeue_waiting_tasks",
                "action_label": "Повторная постановка ожидающих задач",
                "updated_tasks": int(updated),
                "filters": filters,
            }
            if updated > 0:
                messages.success(request, f"Ожидающих задач возвращено в очередь: {updated}.")
            else:
                messages.info(request, "Под выбранный фильтр ожидающие задачи не найдены.")
            return redirect(redirect_url)

        messages.error(request, "Неизвестное действие мониторинга.")
        return redirect(redirect_url)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scope, filters = self._build_filtered_scope(self.request.GET)

        status_rows = list(scope.values("status").annotate(total=Count("id")).order_by("status"))
        for row in status_rows:
            row["status_label"] = _localize_dispatch_status(row.get("status"))
        provider_rows = list(scope.values("provider_type").annotate(total=Count("id")).order_by("-total"))
        for row in provider_rows:
            row["provider_type_label"] = _localize_provider_type(row.get("provider_type"))
        recent_rows = list(scope.order_by("-id")[:200])
        _decorate_dispatch_tasks(recent_rows)
        campaigns = Mailing.objects.order_by("-created_at")[:200]
        scenarios = NotificationScenario.objects.order_by("code")[:200]

        dispatch_pending = scope.filter(
            Q(status=DispatchTask.Status.PENDING)
            | Q(status=DispatchTask.Status.QUEUED)
            | Q(status=DispatchTask.Status.IN_PROGRESS)
        ).count()

        retry_candidates = scope.filter(
            status=DispatchTask.Status.FAILED,
            attempt__lt=F("max_attempts"),
        ).count()
        retry_exhausted = scope.filter(
            status=DispatchTask.Status.FAILED,
            attempt__gte=F("max_attempts"),
        ).count()
        retry_in_queue = scope.filter(
            status__in=[DispatchTask.Status.PENDING, DispatchTask.Status.QUEUED],
            attempt__gt=0,
        ).count()
        retry_attempted = scope.filter(attempt__gt=0).count()
        max_attempt_observed = int(scope.aggregate(max_attempt=Max("attempt")).get("max_attempt") or 0)

        context["campaigns"] = campaigns
        context["scenarios"] = scenarios
        context["status_choices"] = _localized_choices(
            DispatchTask.Status.choices,
            _localize_dispatch_status,
        )
        context["provider_choices"] = _localized_choices(
            BotProfile.ProviderType.choices,
            _localize_provider_type,
        )
        context["selected_mailing_id"] = filters["mailing_id"]
        context["selected_status"] = filters["status"]
        context["selected_provider_type"] = filters["provider_type"]
        context["selected_scenario_id"] = filters["scenario_id"]
        context["recent_rows"] = recent_rows
        context["dispatch_pending"] = dispatch_pending
        context["status_rows"] = status_rows
        context["provider_rows"] = provider_rows
        context["total_tasks"] = scope.count()
        context["failed_tasks"] = scope.filter(status=DispatchTask.Status.FAILED).count()
        context["retry_candidates"] = int(retry_candidates)
        context["retry_exhausted"] = int(retry_exhausted)
        context["retry_in_queue"] = int(retry_in_queue)
        context["retry_attempted"] = int(retry_attempted)
        context["max_attempt_observed"] = max_attempt_observed
        context["return_query"] = self.request.GET.urlencode()
        context["monitor_ops_report"] = self.request.session.pop("mailings_v2_monitor_ops_report", None)
        return context


class MailingsV2ScenariosView(TemplateView):
    """
    Каркас раздела автосценариев.

    Отображает текущий перечень сценариев и их базовые показатели.
    """

    template_name = "mailing_v2/scenarios_hub.html"

    @staticmethod
    def _parse_limit_per_scenario(raw_value: str, *, default: int = 500) -> int:
        """
        Нормализует лимит обработки одного сценария для ручного запуска.
        """
        try:
            parsed = int(str(raw_value or "").strip())
        except (TypeError, ValueError):
            return int(default)
        if parsed <= 0:
            return int(default)
        return min(parsed, 5000)

    @staticmethod
    def _parse_positive_int(raw_value: str, *, default: int, max_value: int) -> int:
        """
        Нормализует пользовательский лимит для безопасного предпросмотра.
        """
        try:
            parsed = int(str(raw_value or "").strip())
        except (TypeError, ValueError):
            return int(default)
        if parsed <= 0:
            return int(default)
        return min(parsed, max_value)

    @staticmethod
    def _build_redirect_url(*, return_query: str) -> str:
        """
        Собирает URL возврата на экран сценариев с сохранением фильтров.
        """
        base_url = reverse("mailings_v2_scenarios")
        safe_query = str(return_query or "").strip()
        if not safe_query:
            return base_url
        return f"{base_url}?{safe_query}"

    def post(self, request, *args, **kwargs):
        """
        Обрабатывает ручной one-shot запуск плановых сценариев.
        """
        action = str(request.POST.get("action") or "").strip()
        return_query = str(request.POST.get("return_query") or "").strip()
        redirect_url = self._build_redirect_url(return_query=return_query)

        if action == "run_coupon_pilot":
            scenario_code = str(request.POST.get("scenario_code") or "").strip()
            scan_limit = self._parse_positive_int(
                request.POST.get("coupon_scan_limit"),
                default=5000,
                max_value=100000,
            )
            try:
                result = execute_coupon_autoscenario_pilot(
                    scenario_code=scenario_code,
                    scan_limit=scan_limit,
                    confirm=True,
                )
            except CouponAutoscenarioPreviewError as exc:
                messages.error(request, str(exc))
                return redirect(redirect_url)

            request.session["mailings_v2_coupon_pilot_report"] = {
                "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
                "scenario_code": result.plan.scenario_code,
                "run_id": result.run_id,
                "created_assignments": result.created_assignments,
                "queue_events_created": result.queue_events_created,
                "planned_assignments": result.plan.planned_assignments,
                "coupon_series": result.plan.coupon_series,
                "venue_name": result.plan.venue_name,
            }
            messages.success(
                request,
                (
                    "Пробный запуск купонного автосценария создан: "
                    f"номер запуска={result.run_id}, назначений={result.created_assignments}, "
                    f"событий vtelemax={result.queue_events_created}."
                ),
            )
            return redirect(redirect_url)

        if action == "cleanup_coupon_pilot":
            try:
                result = cleanup_coupon_autoscenario_pilot_assignment(
                    assignment_id=int(str(request.POST.get("assignment_id") or "0")),
                    reason="pilot_cleanup_from_ui",
                )
            except (TypeError, ValueError, CouponAutoscenarioPreviewError) as exc:
                messages.error(request, str(exc))
                return redirect(redirect_url)

            request.session["mailings_v2_coupon_cleanup_report"] = {
                "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
                "assignment_id": result.assignment_id,
                "queue_event_id": result.queue_event_id,
                "queue_event_created": result.queue_event_created,
                "coupon_series": result.coupon_series,
                "coupon_code": result.coupon_code,
            }
            messages.success(
                request,
                (
                    "Пилотный купон поставлен в очередь на отмену vtelemax: "
                    f"назначение #{result.assignment_id}, событие #{result.queue_event_id}."
                ),
            )
            return redirect(redirect_url)

        if action != "run_schedule_once":
            messages.error(request, "Неизвестное действие для экрана сценариев.")
            return redirect(redirect_url)

        scenario_code = str(request.POST.get("scenario_code") or "").strip()
        limit_per_scenario = self._parse_limit_per_scenario(request.POST.get("limit_per_scenario"))
        scenario_codes = [scenario_code] if scenario_code else None
        stats = run_registered_schedule_scenarios(
            scenario_codes=scenario_codes,
            limit_per_scenario=limit_per_scenario,
        )

        report_rows: list[dict[str, int | str]] = []
        total_created_tasks = 0
        for code, stat in stats.items():
            created_tasks = int(getattr(stat, "created_tasks", 0) or 0)
            row = {
                "scenario_code": str(code),
                "scanned_guests": int(getattr(stat, "scanned_guests", 0) or 0),
                "matched_guests": int(getattr(stat, "matched_guests", 0) or 0),
                "created_tasks": created_tasks,
                "skipped_without_coupon": int(getattr(stat, "skipped_without_coupon", 0) or 0),
                "skipped_duplicate_or_no_targets": int(
                    getattr(stat, "skipped_duplicate_or_no_targets", 0) or 0
                ),
            }
            report_rows.append(row)
            total_created_tasks += created_tasks

        request.session["mailings_v2_scenarios_run_report"] = {
            "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
            "limit_per_scenario": int(limit_per_scenario),
            "selected_scenario_code": scenario_code,
            "rows": report_rows,
            "total_created_tasks": int(total_created_tasks),
        }

        if scenario_code:
            messages.success(
                request,
                (
                    f"Сценарий '{scenario_code}' обработан: "
                    f"создано задач={total_created_tasks}, лимит={limit_per_scenario}."
                ),
            )
        else:
            messages.success(
                request,
                (
                    "Плановые сценарии обработаны вручную: "
                    f"создано задач={total_created_tasks}, лимит={limit_per_scenario}."
                ),
            )
        return redirect(redirect_url)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        since_24h = now - timedelta(hours=24)

        query = str(self.request.GET.get("q") or "").strip()
        selected_trigger_type = str(self.request.GET.get("trigger_type") or "").strip()
        show_inactive = bool(self.request.GET.get("show_inactive"))
        only_system = bool(self.request.GET.get("only_system"))
        with_errors = bool(self.request.GET.get("with_errors"))
        selected_coupon_scenario_code = str(
            self.request.GET.get("coupon_scenario_code") or ""
        ).strip()
        coupon_check_requested = bool(self.request.GET.get("coupon_check"))
        coupon_scan_limit = self._parse_positive_int(
            self.request.GET.get("coupon_scan_limit"),
            default=5000,
            max_value=100000,
        )
        coupon_sample_limit = self._parse_positive_int(
            self.request.GET.get("coupon_sample_limit"),
            default=20,
            max_value=100,
        )

        scenarios_scope = NotificationScenario.objects.select_related("template").annotate(
            events_total=Count("events", distinct=True),
            events_24h=Count("events", filter=Q(events__created_at__gte=since_24h), distinct=True),
            events_error_24h=Count(
                "events",
                filter=Q(
                    events__created_at__gte=since_24h,
                    events__status=NotificationEvent.Status.ERROR,
                ),
                distinct=True,
            ),
            tasks_24h=Count("dispatch_tasks", filter=Q(dispatch_tasks__created_at__gte=since_24h), distinct=True),
            tasks_failed_24h=Count(
                "dispatch_tasks",
                filter=Q(
                    dispatch_tasks__created_at__gte=since_24h,
                    dispatch_tasks__status=DispatchTask.Status.FAILED,
                ),
                distinct=True,
            ),
            last_event_at=Max("events__created_at"),
        )

        if not show_inactive:
            scenarios_scope = scenarios_scope.filter(is_active=True)
        if only_system:
            scenarios_scope = scenarios_scope.filter(is_system=True)

        valid_trigger_types = {value for value, _ in NotificationScenario.TriggerType.choices}
        if selected_trigger_type in valid_trigger_types:
            scenarios_scope = scenarios_scope.filter(trigger_type=selected_trigger_type)
        else:
            selected_trigger_type = ""

        if with_errors:
            scenarios_scope = scenarios_scope.filter(
                Q(events_error_24h__gt=0) | Q(tasks_failed_24h__gt=0)
            )

        if query:
            scenarios_scope = scenarios_scope.filter(
                Q(code__icontains=query)
                | Q(name__icontains=query)
                | Q(description__icontains=query)
                | Q(template__name__icontains=query)
            )

        scenarios_scope = scenarios_scope.order_by("code")
        scenario_ids = scenarios_scope.values_list("id", flat=True)

        scenarios = list(scenarios_scope[:200])
        for scenario in scenarios:
            template_obj = getattr(scenario, "template", None)
            display_name, technical_name = _resolve_template_title(template_obj)
            scenario.template_display_name = display_name
            scenario.template_technical_name = technical_name

        coupon_configs = list(
            CouponAutomationConfig.objects.select_related("scenario", "scenario__template")
            .prefetch_related(
                Prefetch(
                    "coupon_rules",
                    queryset=CouponAutomationRule.objects.order_by("priority", "id"),
                ),
                "scenario__bot_profiles",
            )
            .annotate(
                runs_total=Count("runs", distinct=True),
                assignments_total=Count("assignments", distinct=True),
                assignments_sent=Count(
                    "assignments",
                    filter=Q(assignments__status=CouponAutoscenarioAssignment.Status.SENT),
                    distinct=True,
                ),
                assignments_used=Count(
                    "assignments",
                    filter=Q(
                        assignments__status__in=[
                            CouponAutoscenarioAssignment.Status.USED,
                            CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN,
                        ]
                    ),
                    distinct=True,
                ),
                assignments_error=Count(
                    "assignments",
                    filter=Q(assignments__status=CouponAutoscenarioAssignment.Status.ERROR),
                    distinct=True,
                ),
                last_run_at=Max("runs__created_at"),
            )
            .order_by("scenario__code")[:100]
        )
        for config in coupon_configs:
            _decorate_coupon_autoscenario_config(config)

        config_ids = [int(config.id) for config in coupon_configs]
        anonymous_coupon_keys_by_config: dict[int, set[tuple[str, str]]] = {
            int(config.id): set() for config in coupon_configs
        }
        if config_ids:
            used_assignment_rows = (
                CouponAutoscenarioAssignment.objects.filter(
                    config_id__in=config_ids,
                    status__in=[
                        CouponAutoscenarioAssignment.Status.USED,
                        CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN,
                    ],
                )
                .exclude(coupon_series="")
                .exclude(coupon_code="")
                .values("config_id", "coupon_series", "coupon_code")
            )
            all_coupon_keys: set[tuple[str, str]] = set()
            for row in used_assignment_rows:
                key = (
                    str(row.get("coupon_series") or "").strip(),
                    str(row.get("coupon_code") or "").strip(),
                )
                if not key[0] or not key[1]:
                    continue
                anonymous_coupon_keys_by_config.setdefault(int(row["config_id"]), set()).add(key)
                all_coupon_keys.add(key)

            anonymous_olap_coupon_keys: set[tuple[str, str]] = set()
            if all_coupon_keys:
                series_values = sorted({series for series, _ in all_coupon_keys})
                code_values = sorted({code for _, code in all_coupon_keys})
                anonymous_olap_coupon_keys = {
                    (
                        str(series or "").strip(),
                        str(number or "").strip(),
                    )
                    for series, number in OlapSalesRawLine.objects.filter(
                        sync_journal__source_webhook_id="control_pull_coupon_without_phone",
                        coupon_series__in=series_values,
                        coupon_number__in=code_values,
                    ).values_list("coupon_series", "coupon_number")
                }

            for config in coupon_configs:
                config.assignments_used_without_olap_guest = len(
                    anonymous_coupon_keys_by_config.get(int(config.id), set())
                    & anonymous_olap_coupon_keys
                )

        selected_coupon_config = next(
            (
                config
                for config in coupon_configs
                if config.scenario.code == selected_coupon_scenario_code
            ),
            None,
        )
        if selected_coupon_config is None and coupon_configs:
            selected_coupon_config = coupon_configs[0]
        if selected_coupon_config is not None:
            selected_coupon_scenario_code = selected_coupon_config.scenario.code

        coupon_recent_assignments = list(
            CouponAutoscenarioAssignment.objects.select_related("scenario", "config", "guest", "run")
            .order_by("-created_at", "-id")[:20]
        )
        for assignment in coupon_recent_assignments:
            assignment.can_cleanup_from_ui = (
                assignment.config.execution_mode == CouponAutomationConfig.ExecutionMode.PILOT
                and assignment.status
                in [
                    CouponAutoscenarioAssignment.Status.RESERVED,
                    CouponAutoscenarioAssignment.Status.SENT,
                ]
                and assignment.vtelemax_sync_status
                == CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK
            )

        coupon_plan = None
        coupon_plan_error = ""
        if coupon_check_requested and selected_coupon_scenario_code:
            try:
                plan = build_coupon_autoscenario_execution_plan(
                    scenario_code=selected_coupon_scenario_code,
                    scan_limit=coupon_scan_limit,
                )
                coupon_plan = plan.as_dict()
                coupon_plan.setdefault("message_target_guests", coupon_plan.get("sendable_guests", 0))
                coupon_plan.setdefault("blocked_without_message_target", 0)
                coupon_plan.setdefault(
                    "bot_bound_guests",
                    coupon_plan.get("message_target_guests", 0),
                )
                coupon_plan.setdefault("blocked_without_bot_binding", 0)
                coupon_plan.setdefault(
                    "blocked_without_message_permission",
                    max(
                        int(coupon_plan.get("bot_bound_guests") or 0)
                        - int(coupon_plan.get("message_target_guests") or 0),
                        0,
                    ),
                )
                coupon_plan["execution_state_label"] = _coupon_autoscenario_state_label(
                    coupon_plan.get("execution_mode", "")
                )
                coupon_plan["execution_state_hint"] = _coupon_autoscenario_state_hint(
                    coupon_plan.get("execution_mode", "")
                )
                coupon_plan["coupon_selection_policy_label"] = _coupon_autoscenario_policy_label(
                    venue_selection_mode=coupon_plan.get("venue_selection_mode", "")
                )
                coupon_plan["coupon_selection_policy_rows"] = _coupon_autoscenario_policy_rows(
                    cooldown_days=getattr(selected_coupon_config, "cooldown_days", None),
                    scenario_type=(
                        getattr(selected_coupon_config, "effective_scenario_type", "")
                        if selected_coupon_config is not None
                        else ""
                    ),
                    scenario_code=selected_coupon_scenario_code,
                    birthday_window_days=coupon_plan.get("birthday_preparation_window_days"),
                    venue_selection_mode=coupon_plan.get("venue_selection_mode", ""),
                )
                coupon_plan["audience_venue_filter_label"] = (
                    format_coupon_autoscenario_audience_venue_filter(
                        coupon_plan.get("audience_venue_filter_mode", "")
                    )
                )
                coupon_plan["audience_venue_filter_summary"] = (
                    _coupon_autoscenario_audience_venue_filter_summary(
                        mode=coupon_plan.get("audience_venue_filter_mode", ""),
                        venue_code=coupon_plan.get("audience_venue_code", ""),
                        venue_name=coupon_plan.get("audience_venue_name", ""),
                        inactive_days=coupon_plan.get("inactive_days_threshold"),
                    )
                )
                if (
                    selected_coupon_config is not None
                    and selected_coupon_config.effective_scenario_type
                    == CouponAutomationConfig.ScenarioType.BIRTHDAY_COUPON
                ):
                    coupon_plan["bot_bound_guests_label"] = "Именинников в новых ботах"
                    coupon_plan["bot_bound_guests_hint"] = "день рождения в периоде"
                    coupon_plan["message_target_guests_label"] = "С согласием на рассылку"
                    coupon_plan["message_target_guests_hint"] = "можно отправить сообщение"
                    coupon_plan["audience_source_label"] = (
                        "Показаны только гости из новых ботов Телеграм/ВК/Макс, у которых заполнена дата рождения "
                        "и день рождения попадает в период автосценария."
                    )
                    coupon_plan["sendable_channel_label"] = (
                        "«Именинников в новых ботах» — гости с активной привязкой к новым ботам и днём рождения "
                        "в периоде. «С согласием на рассылку» — те, кому дополнительно можно отправить сообщение."
                    )
                else:
                    coupon_plan["bot_bound_guests_label"] = "Гостей в новых ботах"
                    coupon_plan["bot_bound_guests_hint"] = "Телеграм, ВК, Макс"
                    coupon_plan["message_target_guests_label"] = "С согласием на рассылку"
                    coupon_plan["message_target_guests_hint"] = "можно отправить сообщение"
                    coupon_plan["audience_source_label"] = (
                        "Выборка строится по истории заказов: берутся гости, у которых последний заказ был "
                        f"не позднее даты отсечения «сегодня минус {coupon_plan.get('inactive_days_threshold', 0)} дн.»."
                    )
                    coupon_plan["sendable_channel_label"] = (
                        "«Гостей в новых ботах» — гости с активной привязкой к новым ботам Телеграм/ВК/Макс. "
                        "«С согласием на рассылку» — те, кому дополнительно можно отправить сообщение."
                    )
                coupon_plan["sample_plan_items"] = coupon_plan.get("plan_items", [])[
                    :coupon_sample_limit
                ]
                for item in coupon_plan["sample_plan_items"]:
                    item.setdefault("coupon_rule_label", "")
                    item.setdefault("coupon_selection_source_display", "")
                    item.setdefault("last_order_department_id", "")
                    item.setdefault("last_order_department_name", "")
            except CouponAutoscenarioPreviewError as exc:
                coupon_plan_error = str(exc)

        context["scenarios"] = scenarios
        context["coupon_autoscenario_configs"] = coupon_configs
        context["selected_coupon_config"] = selected_coupon_config
        context["coupon_execution_mode_choices"] = list(CouponAutomationConfig.ExecutionMode.choices)
        context["selected_coupon_scenario_code"] = selected_coupon_scenario_code
        context["coupon_check_requested"] = coupon_check_requested
        context["coupon_scan_limit"] = coupon_scan_limit
        context["coupon_sample_limit"] = coupon_sample_limit
        context["coupon_plan"] = coupon_plan
        context["coupon_plan_error"] = coupon_plan_error
        context["coupon_recent_assignments"] = coupon_recent_assignments
        context["coupon_pilot_report"] = self.request.session.pop(
            "mailings_v2_coupon_pilot_report",
            None,
        )
        context["coupon_cleanup_report"] = self.request.session.pop(
            "mailings_v2_coupon_cleanup_report",
            None,
        )
        context["scenarios_total"] = scenarios_scope.count()
        context["scenarios_active"] = scenarios_scope.filter(is_active=True).count()
        context["events_24h_total"] = NotificationEvent.objects.filter(
            scenario_id__in=scenario_ids,
            created_at__gte=since_24h,
        ).count()
        context["tasks_24h_total"] = DispatchTask.objects.filter(
            notification_scenario_id__in=scenario_ids,
            created_at__gte=since_24h,
        ).count()
        context["tasks_failed_24h_total"] = DispatchTask.objects.filter(
            notification_scenario_id__in=scenario_ids,
            created_at__gte=since_24h,
            status=DispatchTask.Status.FAILED,
        ).count()
        context["schedule_scenario_codes"] = list(get_registered_schedule_scenario_codes())
        context["trigger_type_choices"] = list(NotificationScenario.TriggerType.choices)
        context["selected_trigger_type"] = selected_trigger_type
        context["show_inactive"] = show_inactive
        context["only_system"] = only_system
        context["with_errors"] = with_errors
        context["query"] = query
        context["return_query"] = self.request.GET.urlencode()
        context["scenarios_run_report"] = self.request.session.pop("mailings_v2_scenarios_run_report", None)
        return context


class MailingsV2ScenariosV2View(MailingsV2ScenariosView):
    """
    Вторая версия рабочего экрана автосценариев.

    Переиспользует расчёты и действия базовой страницы, но показывает их
    по пользовательскому сценарию: выбор, состояние, следующий шаг, детали.
    """

    template_name = "mailing_v2/scenarios_hub_v2.html"

    @staticmethod
    def _build_redirect_url(*, return_query: str) -> str:
        """
        Возвращает оператора на вторую версию страницы с сохранением фильтров.
        """
        base_url = reverse("mailings_v2_scenarios_v2")
        safe_query = str(return_query or "").strip()
        if not safe_query:
            return base_url
        return f"{base_url}?{safe_query}"


class MailingsV2CouponAutoscenarioCreateView(FormView):
    """
    Создание пользовательского купонного автосценария из интерфейса рассылок.

    Форма создаёт выключенный `NotificationScenario`, создаёт или привязывает
    шаблон сообщения и черновой `CouponAutomationConfig`, затем переводит
    оператора в настройки правил купонов и пилота.
    """

    form_class = CouponAutomationScenarioCreateForm
    template_name = "mailing_v2/coupon_autoscenario_create.html"

    def _get_source_config(self) -> CouponAutomationConfig | None:
        if hasattr(self, "_source_config"):
            return self._source_config

        source_config_id = str(self.request.GET.get("source_config_id") or "").strip()
        if not source_config_id.isdigit():
            self._source_config = None
            return self._source_config

        self._source_config = (
            CouponAutomationConfig.objects.select_related("scenario", "scenario__template")
            .prefetch_related("scenario__bot_profiles", "coupon_rules")
            .filter(pk=int(source_config_id))
            .first()
        )
        return self._source_config

    @staticmethod
    def _build_existing_template_payload() -> dict[str, dict[str, object]]:
        preview_guest = SimpleNamespace(
            first_name="Анна",
            last_name="Иванова",
            phone="+79990000000",
            email="anna@example.test",
            birthdate=None,
        )
        payload: dict[str, dict[str, object]] = {}
        templates = MessageTemplate.objects.filter(is_active=True).order_by("name", "id")
        for template_obj in templates:
            display_name, technical_name = _resolve_template_title(template_obj)
            message_text = str(template_obj.message_text or "")
            coupon_code_error = ""
            try:
                validate_coupon_code_placeholder(message_text)
            except ValidationError as exc:
                coupon_code_error = "; ".join(exc.messages)
            payload[str(template_obj.pk)] = {
                "id": template_obj.pk,
                "name": template_obj.name,
                "display_name": display_name or template_obj.name,
                "technical_name": technical_name,
                "description": template_obj.description or "",
                "message_text": message_text,
                "preview_text": render_message_for_guest(
                    message_text,
                    preview_guest,
                    extra_context={
                        "coupon_code": "TEST123",
                        "days_without_visits": 30,
                        "days_until_birthday": 7,
                        "birthday_date": "01.07",
                    },
                ),
                "has_coupon_code": not bool(coupon_code_error),
                "coupon_code_status": (
                    f"Параметр {COUPON_CODE_PLACEHOLDER} найден."
                    if not coupon_code_error
                    else coupon_code_error
                ),
                "detail_url": reverse(
                    "mailings_v2_templates_detail",
                    kwargs={"pk": template_obj.pk},
                ),
                "edit_url": reverse(
                    "mailings_v2_templates_edit",
                    kwargs={"pk": template_obj.pk},
                ),
            }
        return payload

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["source_config"] = self._get_source_config()
        return kwargs

    def form_valid(self, form):
        config = form.save()
        self.object = config
        source_config = self._get_source_config()
        if source_config is not None:
            messages.success(
                self.request,
                "Купонный автосценарий создан как черновик на основе выбранного автосценария. "
                "Проверьте правила купонов и пилот перед запуском.",
            )
            return redirect(self.get_success_url())
        messages.success(
            self.request,
            "Купонный автосценарий создан как черновик. Настройте правила купонов и пилот перед запуском.",
        )
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse(
            "mailings_v2_coupon_autoscenario_settings",
            kwargs={"pk": self.object.pk},
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["scenarios_url"] = reverse("mailings_v2_scenarios")
        context["has_active_bot_profiles"] = BotProfile.objects.filter(is_active=True).exists()
        context["existing_template_payload"] = self._build_existing_template_payload()
        context["source_config"] = self._get_source_config()
        return context


class MailingsV2CouponAutoscenarioControlView(TemplateView):
    """
    Операторский пульт купонного автосценария.

    Экран не меняет правила подбора аудитории и купонов. Он собирает уже
    существующие сервисы в безопасный центр управления: проверка готовности,
    пилотный запуск, пауза и управление флагом планировщика.
    """

    template_name = "mailing_v2/coupon_autoscenario_control.html"

    @staticmethod
    def _parse_positive_int(raw_value: str, *, default: int, max_value: int) -> int:
        """
        Нормализует пользовательский лимит для безопасного расчёта.
        """
        try:
            parsed = int(str(raw_value or "").strip())
        except (TypeError, ValueError):
            return int(default)
        if parsed <= 0:
            return int(default)
        return min(parsed, max_value)

    @staticmethod
    def _control_url(config: CouponAutomationConfig, *, check: bool = False) -> str:
        url = reverse("mailings_v2_coupon_autoscenario_control", kwargs={"pk": config.pk})
        if check:
            return f"{url}?{urlencode({'check': '1'})}"
        return url

    def _load_config(self) -> CouponAutomationConfig:
        return get_object_or_404(
            CouponAutomationConfig.objects.select_related("scenario", "scenario__template").prefetch_related(
                "scenario__bot_profiles",
                Prefetch(
                    "coupon_rules",
                    queryset=CouponAutomationRule.objects.order_by("priority", "id"),
                ),
            ),
            pk=self.kwargs["pk"],
        )

    def post(self, request, *args, **kwargs):
        config = self._load_config()
        scenario = config.scenario
        action = str(request.POST.get("action") or "").strip()
        redirect_url = self._control_url(config, check=action in {"check_readiness"})

        if action == "enable_planner":
            readiness = _build_coupon_autoscenario_readiness(config)
            blockers = list(readiness.get("blockers") or [])
            if blockers:
                messages.error(
                    request,
                    "Планировщик не включён: устраните блокировки готовности автосценария.",
                )
                for blocker in blockers:
                    messages.warning(request, str(blocker))
                return redirect(self._control_url(config, check=True))

            scenario.is_active = True
            scenario.updated_at = timezone.now()
            scenario.save(update_fields=["is_active", "updated_at"])
            messages.success(request, "Планировщик уведомлений включён.")
            return redirect(self._control_url(config, check=True))

        if action == "disable_planner":
            scenario.is_active = False
            scenario.updated_at = timezone.now()
            scenario.save(update_fields=["is_active", "updated_at"])
            messages.success(request, "Планировщик уведомлений выключен.")
            return redirect(self._control_url(config, check=True))

        if action == "pause_autoscenario":
            config.execution_mode = CouponAutomationConfig.ExecutionMode.PAUSED
            config.save(update_fields=["execution_mode", "updated_at"])
            scenario.is_active = False
            scenario.updated_at = timezone.now()
            scenario.save(update_fields=["is_active", "updated_at"])
            messages.success(request, "Автосценарий поставлен на паузу, планировщик выключен.")
            return redirect(self._control_url(config, check=True))

        if action == "run_pilot":
            if config.execution_mode != CouponAutomationConfig.ExecutionMode.PILOT:
                messages.error(
                    request,
                    "Пилотная волна не создана: переведите купонный автосценарий в состояние «Пилот».",
                )
                return redirect(self._control_url(config, check=True))

            readiness = _build_coupon_autoscenario_readiness(config)
            blockers = list(readiness.get("blockers") or [])
            if blockers:
                messages.error(
                    request,
                    "Пилотная волна не создана: устраните блокировки готовности автосценария.",
                )
                for blocker in blockers:
                    messages.warning(request, str(blocker))
                return redirect(self._control_url(config, check=True))

            scan_limit = self._parse_positive_int(
                request.POST.get("coupon_scan_limit"),
                default=COUPON_AUTOSCENARIO_CONTROL_SCAN_LIMIT,
                max_value=100000,
            )
            try:
                result = execute_coupon_autoscenario_pilot(
                    scenario_code=scenario.code,
                    scan_limit=scan_limit,
                    confirm=True,
                )
            except CouponAutoscenarioPreviewError as exc:
                messages.error(request, str(exc))
                return redirect(self._control_url(config, check=True))

            request.session["mailings_v2_coupon_control_pilot_report"] = {
                "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
                "scenario_code": result.plan.scenario_code,
                "run_id": result.run_id,
                "created_assignments": result.created_assignments,
                "queue_events_created": result.queue_events_created,
                "planned_assignments": result.plan.planned_assignments,
            }
            request.session.modified = True
            messages.success(
                request,
                (
                    "Пилотная волна создана: "
                    f"запуск #{result.run_id}, назначений={result.created_assignments}, "
                    f"событий vtelemax={result.queue_events_created}."
                ),
            )
            return redirect(self._control_url(config, check=True))

        if action == "run_controlled_automatic":
            if config.execution_mode != CouponAutomationConfig.ExecutionMode.AUTOMATIC:
                messages.error(
                    request,
                    "Боевой запуск не создан: переведите купонный автосценарий в состояние «Активен».",
                )
                return redirect(self._control_url(config, check=True))

            configured_limit = max(1, int(config.max_recipients_per_run or 1))
            run_limit = self._parse_positive_int(
                request.POST.get("coupon_run_limit"),
                default=configured_limit,
                max_value=configured_limit,
            )
            scan_limit = self._parse_positive_int(
                request.POST.get("coupon_scan_limit"),
                default=COUPON_AUTOSCENARIO_CONTROL_SCAN_LIMIT,
                max_value=100000,
            )
            try:
                result = execute_coupon_autoscenario_automatic(
                    scenario_code=scenario.code,
                    limit=run_limit,
                    scan_limit=scan_limit,
                    confirm=True,
                )
            except CouponAutoscenarioPreviewError as exc:
                messages.error(request, str(exc))
                return redirect(self._control_url(config, check=True))

            request.session["mailings_v2_coupon_control_automatic_report"] = {
                "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
                "scenario_code": result.plan.scenario_code,
                "run_id": result.run_id,
                "created_assignments": result.created_assignments,
                "queue_events_created": result.queue_events_created,
                "planned_assignments": result.plan.planned_assignments,
                "run_limit": run_limit,
            }
            request.session.modified = True
            messages.success(
                request,
                (
                    "Контролируемый боевой запуск создан: "
                    f"запуск #{result.run_id}, лимит={run_limit}, "
                    f"назначений={result.created_assignments}, "
                    f"событий vtelemax={result.queue_events_created}."
                ),
            )
            return redirect(self._control_url(config, check=True))

        if action == "check_readiness":
            return redirect(redirect_url)

        messages.error(request, "Неизвестное действие пульта автосценария.")
        return redirect(self._control_url(config))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config = _decorate_coupon_autoscenario_config(self._load_config())
        scenario = config.scenario
        check_requested = bool(self.request.GET.get("check"))

        plan = None
        plan_error = ""
        if check_requested:
            try:
                plan_obj = build_coupon_autoscenario_execution_plan(
                    scenario_code=scenario.code,
                    scan_limit=COUPON_AUTOSCENARIO_CONTROL_SCAN_LIMIT,
                )
                plan = plan_obj.as_dict()
                plan["execution_state_label"] = _coupon_autoscenario_state_label(
                    plan.get("execution_mode", "")
                )
            except CouponAutoscenarioPreviewError as exc:
                plan_error = str(exc)

        autoscenario_urls = _build_coupon_autoscenario_urls(config)
        readiness = _build_coupon_autoscenario_readiness(config)

        context["config"] = config
        context["autoscenario_active_tab"] = "control"
        context["autoscenario_urls"] = autoscenario_urls
        context["readiness"] = readiness
        context["chain_steps"] = _build_coupon_autoscenario_chain_steps(config)
        launch_steps = _build_coupon_autoscenario_launch_steps(
            config,
            readiness,
            autoscenario_urls,
            check_requested=check_requested,
            control_plan=plan,
            control_plan_error=plan_error,
        )
        context["launch_steps"] = launch_steps
        context["primary_step"] = _build_coupon_autoscenario_primary_step(launch_steps)
        context["check_requested"] = check_requested
        context["control_plan"] = plan
        context["control_plan_error"] = plan_error
        context["diagnostic_rows"] = _build_coupon_autoscenario_diagnostics(config)
        context["olap_e2e_checklist"] = _build_coupon_autoscenario_olap_e2e_checklist(config)
        context["issue_rows"] = _build_coupon_autoscenario_issue_rows(config)
        context["pilot_report"] = self.request.session.pop(
            "mailings_v2_coupon_control_pilot_report",
            None,
        )
        context["automatic_report"] = self.request.session.pop(
            "mailings_v2_coupon_control_automatic_report",
            None,
        )
        context["recent_runs"] = list(
            CouponAutoscenarioRun.objects.filter(config=config)
            .order_by("-created_at", "-id")[:10]
        )
        context["recent_assignments"] = list(
            CouponAutoscenarioAssignment.objects.select_related("run", "guest")
            .filter(config=config)
            .order_by("-created_at", "-id")[:10]
        )
        return context


class MailingsV2CouponAutoscenarioControlV2View(MailingsV2CouponAutoscenarioControlView):
    """
    Вторая версия операторского пульта для сравнения UX-подходов.

    Бизнес-действия и проверки наследуются от первого пульта; отличается только
    порядок подачи информации: состояние, следующий шаг и раскрываемые детали.
    """

    template_name = "mailing_v2/coupon_autoscenario_control_v2.html"

    @staticmethod
    def _control_url(config: CouponAutomationConfig, *, check: bool = False) -> str:
        url = reverse("mailings_v2_coupon_autoscenario_control_v2", kwargs={"pk": config.pk})
        if check:
            return f"{url}?{urlencode({'check': '1'})}"
        return url

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["autoscenario_active_tab"] = "control_v2"
        return context


class MailingsV2CouponAutoscenarioSettingsView(UpdateView):
    """
    Пользовательская настройка купонного автосценария.

    Этот экран меняет только правила автосценария. Он не запускает пилот,
    не резервирует купоны и не создает события vtelemax.
    """

    model = CouponAutomationConfig
    form_class = CouponAutomationConfigForm
    template_name = "mailing_v2/coupon_autoscenario_settings.html"
    context_object_name = "config"

    def _build_rule_formset(self, *, data=None):
        return CouponAutomationRuleFormSet(
            data=data,
            instance=self.object,
            prefix="coupon_rules",
        )

    def _get_fill_birthday_request_scenario(self):
        scenario = getattr(self.object, "scenario", None)
        if scenario is None or str(scenario.code or "").strip() != SCENARIO_CODE_FILL_BIRTHDAY_COUPON:
            return None
        return (
            NotificationScenario.objects.select_related("template")
            .prefetch_related("bot_profiles")
            .filter(code=SCENARIO_CODE_FILL_BIRTHDAY_REQUEST)
            .first()
        )

    def _build_fill_birthday_request_form(self, *, data=None):
        request_scenario = self._get_fill_birthday_request_scenario()
        if request_scenario is None:
            return None
        return FillBirthdayRequestScenarioForm(
            data=data,
            instance=request_scenario,
            prefix="fill_birthday_request",
        )

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        rule_formset = self._build_rule_formset(data=request.POST)
        fill_birthday_request_form = self._build_fill_birthday_request_form(data=request.POST)
        request_form_is_valid = (
            fill_birthday_request_form is None or fill_birthday_request_form.is_valid()
        )
        if form.is_valid() and rule_formset.is_valid() and request_form_is_valid:
            if not self._validate_coupon_launch_readiness(form=form, rule_formset=rule_formset):
                return self.forms_invalid(form, rule_formset, fill_birthday_request_form)
            return self.forms_valid(form, rule_formset, fill_birthday_request_form)
        return self.forms_invalid(form, rule_formset, fill_birthday_request_form)

    @staticmethod
    def _has_active_coupon_rule(rule_formset) -> bool:
        for rule_form in rule_formset.forms:
            if not getattr(rule_form, "cleaned_data", None):
                continue
            if rule_form.cleaned_data.get("DELETE"):
                continue
            if not bool(rule_form.cleaned_data.get("is_active")):
                continue
            if str(rule_form.cleaned_data.get("coupon_series") or "").strip():
                return True
        return False

    def _validate_coupon_launch_readiness(self, *, form, rule_formset) -> bool:
        execution_mode = form.cleaned_data.get("execution_mode")
        if execution_mode not in {
            CouponAutomationConfig.ExecutionMode.PILOT,
            CouponAutomationConfig.ExecutionMode.AUTOMATIC,
        }:
            return True

        fallback_series = str(form.cleaned_data.get("coupon_series") or "").strip()
        if fallback_series or self._has_active_coupon_rule(rule_formset):
            return True

        form.add_error(
            None,
            "Нельзя перевести купонный автосценарий в «Пилот» или «Активен»: "
            "добавьте хотя бы одно активное правило с серией купонов или заполните резервную серию.",
        )
        return False

    def forms_valid(self, form, rule_formset, fill_birthday_request_form=None):
        with transaction.atomic():
            self.object = form.save()
            rule_formset.instance = self.object
            rule_formset.save()
            if fill_birthday_request_form is not None:
                fill_birthday_request_form.save()
        messages.success(self.request, "Настройки и купонные правила автосценария сохранены.")
        return redirect(self.get_success_url())

    def forms_invalid(self, form, rule_formset, fill_birthday_request_form=None):
        messages.error(
            self.request,
            "Настройки не сохранены. Исправьте ошибки в форме и повторите сохранение.",
        )
        return self.render_to_response(
            self.get_context_data(
                form=form,
                rule_formset=rule_formset,
                fill_birthday_request_form=fill_birthday_request_form,
            )
        )

    def get_success_url(self):
        return reverse(
            "mailings_v2_coupon_autoscenario_settings",
            kwargs={"pk": self.object.pk},
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["scenarios_url"] = reverse("mailings_v2_scenarios")
        context["autoscenario_active_tab"] = "settings"
        context["autoscenario_urls"] = _build_coupon_autoscenario_urls(self.object)
        scenario_code = self.object.scenario.code if self.object and self.object.scenario_id else ""
        context["preview_url"] = (
            f"{reverse('mailings_v2_scenarios')}?{urlencode({'coupon_scenario_code': scenario_code, 'coupon_check': '1'})}"
            if scenario_code
            else reverse("mailings_v2_scenarios")
        )
        context["coupon_rules"] = list(
            self.object.coupon_rules.order_by("priority", "id")
        )
        context.setdefault("rule_formset", self._build_rule_formset())
        fill_birthday_request_form = context.get("fill_birthday_request_form")
        if fill_birthday_request_form is None:
            fill_birthday_request_form = self._build_fill_birthday_request_form()
        context["fill_birthday_request_form"] = fill_birthday_request_form
        request_scenario = (
            fill_birthday_request_form.instance
            if fill_birthday_request_form is not None
            else None
        )
        context["fill_birthday_request_scenario"] = request_scenario
        request_template = getattr(request_scenario, "template", None)
        request_template_display_name, request_template_technical_name = _resolve_template_title(
            request_template
        )
        context["fill_birthday_request_template"] = request_template
        context["fill_birthday_request_template_display_name"] = request_template_display_name
        context["fill_birthday_request_template_technical_name"] = request_template_technical_name
        context["fill_birthday_request_template_detail_url"] = (
            reverse("mailings_v2_templates_detail", kwargs={"pk": request_template.pk})
            if request_template
            else ""
        )
        context["fill_birthday_request_template_edit_url"] = (
            reverse("mailings_v2_templates_edit", kwargs={"pk": request_template.pk})
            if request_template
            else ""
        )
        context["execution_state_label"] = _coupon_autoscenario_state_label(
            self.object.execution_mode
        )
        context["execution_state_hint"] = _coupon_autoscenario_state_hint(
            self.object.execution_mode
        )
        context["coupon_selection_policy_label"] = _coupon_autoscenario_policy_label(
            venue_selection_mode=self.object.venue_selection_mode
        )
        effective_scenario_type = resolve_coupon_autoscenario_type(self.object)
        context["coupon_selection_policy_rows"] = _coupon_autoscenario_policy_rows(
            cooldown_days=self.object.cooldown_days,
            scenario_type=effective_scenario_type,
            scenario_code=scenario_code,
            birthday_window_days=(self.object.settings or {}).get("birthday_preparation_window_days"),
            venue_selection_mode=self.object.venue_selection_mode,
        )
        template_obj = getattr(self.object.scenario, "template", None)
        template_display_name, template_technical_name = _resolve_template_title(template_obj)
        context["message_template"] = template_obj
        context["template_display_name"] = template_display_name
        context["template_technical_name"] = template_technical_name
        context["template_detail_url"] = (
            reverse("mailings_v2_templates_detail", kwargs={"pk": template_obj.pk})
            if template_obj
            else ""
        )
        context["template_edit_url"] = (
            reverse("mailings_v2_templates_edit", kwargs={"pk": template_obj.pk})
            if template_obj
            else ""
        )
        context["template_copy_url"] = (
            f"{reverse('mailings_v2_templates_new')}?{urlencode({'source_template_id': template_obj.pk})}"
            if template_obj
            else ""
        )
        context["message_template_preview_text"] = ""
        if template_obj is not None:
            preview_guest = SimpleNamespace(
                first_name="Анна",
                last_name="Иванова",
                phone="+79990000000",
                email="anna@example.test",
                birthdate=None,
            )
            context["message_template_preview_text"] = render_message_for_guest(
                template_obj.message_text,
                preview_guest,
                extra_context={
                    "coupon_code": "TEST123",
                    "days_without_visits": 30,
                    "days_until_birthday": 7,
                    "birthday_date": "01.07",
                },
            )
        active_bot_profiles_qs = BotProfile.objects.filter(is_active=True)
        context["has_active_bot_profiles"] = active_bot_profiles_qs.exists()
        return context


SYSTEM_TEMPLATE_NAME_MAP = {
    "SYSTEM_BALANCE_CHANGED_TEMPLATE": "Системный шаблон: изменение баланса",
    "SYSTEM_INACTIVE_7D_TEMPLATE": "Системный шаблон: неактивные 7 дней",
    "SYSTEM_INACTIVE_30D_COUPON_TEMPLATE": "Системный шаблон: неактивные 30 дней + купон",
    "SYSTEM_BIRTHDAY_COUPON_TEMPLATE": "Системный шаблон: день рождения + купон",
    "SYSTEM_MEAT_LOVER_30D_TEMPLATE": "Системный шаблон: любитель мяса 30 дней",
}


def _is_system_template(template_obj: MessageTemplate | None) -> bool:
    """
    Определяет, относится ли шаблон к системным.

    На текущем этапе используем совместимый эвристический признак:
    1. `created_by == "system"`;
    2. техническое имя формата `SYSTEM_*_TEMPLATE`.
    """
    if template_obj is None:
        return False

    created_by = str(getattr(template_obj, "created_by", "") or "").strip().lower()
    if created_by == "system":
        return True

    raw_name = str(getattr(template_obj, "name", "") or "").strip().upper()
    return raw_name.startswith("SYSTEM_") and raw_name.endswith("_TEMPLATE")


def _resolve_template_title(template_obj: MessageTemplate | None) -> tuple[str, str]:
    """
    Возвращает пару названий шаблона:
    1. display_name — человеко-понятный заголовок;
    2. technical_name — техническое имя (если отличается от display).
    """
    if template_obj is None:
        return "", ""

    raw_name = str(getattr(template_obj, "name", "") or "").strip()
    if not raw_name:
        return "Шаблон без названия", ""

    mapped_name = SYSTEM_TEMPLATE_NAME_MAP.get(raw_name)
    if mapped_name:
        return mapped_name, raw_name

    if raw_name.startswith("SYSTEM_") and raw_name.endswith("_TEMPLATE"):
        normalized = raw_name.removeprefix("SYSTEM_").removesuffix("_TEMPLATE").strip("_")
        words = [w for w in normalized.split("_") if w]
        pretty_name = " ".join(word.capitalize() if not word.isdigit() else word for word in words)
        if pretty_name:
            return f"Системный шаблон: {pretty_name}", raw_name

    return raw_name, ""


def _build_guest_display_name(guest: Guest) -> str:
    """
    Возвращает компактное человеко-понятное имя гостя для селекторов.
    """
    first_name = str(getattr(guest, "first_name", "") or "").strip()
    last_name = str(getattr(guest, "last_name", "") or "").strip()
    fio = " ".join(part for part in [first_name, last_name] if part)
    if fio:
        return fio

    phone = str(getattr(guest, "phone", "") or "").strip()
    if phone:
        return phone

    return f"Гость #{guest.id}"


def _build_template_preview_context(*, template_obj: MessageTemplate, guest: Guest) -> dict[str, object]:
    """
    Формирует расширенный контекст предпросмотра шаблона.

    Приоритет:
    1. payload/coupon последнего NotificationEvent для связки guest+template;
    2. fallback-расчёт days_without_visits по последнему визиту гостя.
    """
    context: dict[str, object] = {}
    latest_event = None
    if getattr(template_obj, "pk", None):
        latest_event = (
            NotificationEvent.objects.filter(
                guest=guest,
                scenario__template=template_obj,
            )
            .order_by("-event_at", "-id")
            .first()
        )

    if latest_event and isinstance(latest_event.payload, dict):
        for key, value in latest_event.payload.items():
            if isinstance(key, str):
                context[key] = value

    if latest_event and latest_event.coupon_code and not context.get("coupon_code"):
        context["coupon_code"] = str(latest_event.coupon_code)

    if "days_without_visits" not in context:
        last_visit_date = getattr(guest, "last_visit_date", None)
        if last_visit_date is not None:
            if hasattr(last_visit_date, "date"):
                last_visit_date = last_visit_date.date()
            try:
                context["days_without_visits"] = max((timezone.localdate() - last_visit_date).days, 0)
            except Exception:
                context["days_without_visits"] = ""
        else:
            context["days_without_visits"] = ""

    if "coupon_code" not in context:
        context["coupon_code"] = ""

    return context


def _build_template_preview_state(
    *,
    template_obj: MessageTemplate,
    selected_guest_id: str,
    message_text_override: str | None,
) -> dict[str, object]:
    """
    Собирает состояние предпросмотра шаблона для detail/edit экрана.

    Возвращает:
    1. список гостей для выбора;
    2. текущий выбранный guest_id;
    3. итоговый предпросмотр текста;
    4. подпись выбранного гостя.
    """
    safe_selected_guest_id = str(selected_guest_id or "").strip()

    guests = list(Guest.objects.order_by("-updated_at", "-id")[:300])
    for guest in guests:
        guest.display_name = _build_guest_display_name(guest)

    selected_guest: Guest | None = None
    if safe_selected_guest_id.isdigit():
        selected_guest = next(
            (guest for guest in guests if guest.id == int(safe_selected_guest_id)),
            None,
        )
        if selected_guest is None:
            selected_guest = Guest.objects.filter(id=int(safe_selected_guest_id)).first()
            if selected_guest is not None:
                selected_guest.display_name = _build_guest_display_name(selected_guest)
                guests.insert(0, selected_guest)

    preview_text = ""
    preview_guest_display_name = ""
    if selected_guest is not None:
        preview_guest_display_name = str(
            getattr(selected_guest, "display_name", "") or _build_guest_display_name(selected_guest)
        )
        preview_context = _build_template_preview_context(template_obj=template_obj, guest=selected_guest)
        message_text = (
            message_text_override
            if message_text_override is not None
            else str(getattr(template_obj, "message_text", "") or "")
        )
        preview_text = render_message_for_guest(
            message_text,
            selected_guest,
            extra_context=preview_context,
        )

    return {
        "guests": guests,
        "selected_guest_id": safe_selected_guest_id,
        "preview_text": preview_text,
        "preview_guest_display_name": preview_guest_display_name,
    }


def _build_mailings_v2_flow(*, active_area: str) -> dict[str, object]:
    """
    Формирует единый bridge-флоу для экранов mailings-v2.

    Шаги:
    1. гипотеза и отбор в workbench;
    2. подготовка шаблона;
    3. настройка и запуск кампании;
    4. мониторинг результата и разбор проблем.
    """
    monitor_url = reverse("mailings_v2_monitor")
    scenarios_url = reverse("mailings_v2_scenarios")
    step4_url = scenarios_url if active_area == "scenarios" else monitor_url

    steps = [
        {
            "number": 1,
            "title": "Гипотеза в рабочем экране гостей",
            "description": "Соберите сегмент и создайте черновик рассылки из отбора.",
            "help": "Вы формируете бизнес-гипотезу в экране «Гости»: выбираете фильтры и сохраняете отбор в черновик рассылки.",
            "url": reverse("guests_workbench"),
            "cta": "Открыть экран «Гости»",
        },
        {
            "number": 2,
            "title": "Шаблон сообщения",
            "description": "Подготовьте текст и проверьте предпросмотр на реальном госте.",
            "help": "На этом шаге создаётся или редактируется текст сообщения, а также проверяется итоговый вид сообщения для конкретного гостя.",
            "url": reverse("mailings_v2_templates"),
            "cta": "Открыть шаблоны",
            "secondary_url": reverse("mailings_v2_templates_new"),
            "secondary_cta": "Создать шаблон",
        },
        {
            "number": 3,
            "title": "Кампания и запуск",
            "description": "Проверьте аудиторию, выполните проверку перед запуском и запускайте кампанию.",
            "help": "Здесь настраиваются параметры запуска, состав аудитории и операционные действия: проверка перед запуском, немедленный запуск, запуск/пауза и повторы.",
            "url": reverse("mailings_v2_campaigns_new"),
            "cta": "Создать кампанию",
        },
        {
            "number": 4,
            "title": "Мониторинг и обратная связь",
            "description": "Контролируйте доставку, повторы, ошибки и корректируйте следующий запуск.",
            "help": "В мониторинге вы видите статусы доставки, ошибки и результаты отправок, чтобы улучшать следующий запуск.",
            "url": step4_url,
            "cta": "Открыть мониторинг",
        },
    ]

    return {
        "title": "Маршрут маркетолога",
        "steps": steps,
    }


def _is_time_in_window(current_time, window_begin, window_end) -> bool:
    """
    Проверяет вхождение времени в окно отправки.

    Повторяет текущую логику `mailing_worker.process_one_mailing`,
    чтобы dry-run и run-now давали одинаковый результат.
    """
    return window_begin <= current_time <= window_end


def _build_mailing_dry_run_report(mailing: Mailing, now) -> dict[str, object]:
    """
    Формирует dry-run отчёт по готовности кампании к немедленному запуску.
    """
    local_now = timezone.localtime(now)
    current_time = local_now.time()
    selected_bot_ids = list(
        mailing.bot_profiles.filter(is_active=True).values_list("id", flat=True).order_by("id")
    )

    rows_scope = MailingGuest.objects.filter(mailing=mailing)
    ready_scope = rows_scope.filter(status=MailingGuest.Status.PLANNED, scheduled_datetime__lte=now)
    ready_rows = int(ready_scope.count())

    ready_rows_with_targets = 0
    ready_rows_without_targets = 0
    ready_rows_with_file_telegram_id = 0
    if ready_rows > 0 and selected_bot_ids:
        delivery_plan = build_mailing_delivery_plan(
            ready_scope.values_list("guest_id", flat=True),
            selected_bot_ids=selected_bot_ids,
            target_mode=getattr(mailing, "target_mode", Mailing.TargetMode.PRIMARY_ONLY),
        )
        targetable_guest_ids = set(delivery_plan.deliverable_guest_ids)
        targetable_row_ids = set(
            ready_scope.filter(guest_id__in=targetable_guest_ids).values_list("id", flat=True)
        )

        has_selected_telegram_bot = mailing.bot_profiles.filter(
            id__in=selected_bot_ids,
            is_active=True,
            provider_type=BotProfile.ProviderType.TELEGRAM,
        ).exists()
        if has_selected_telegram_bot:
            file_telegram_row_ids = set(
                ready_scope.exclude(external_id__isnull=True)
                .exclude(external_id="")
                .values_list("id", flat=True)
            )
            ready_rows_with_file_telegram_id = len(file_telegram_row_ids)
            targetable_row_ids.update(file_telegram_row_ids)

        ready_rows_with_targets = len(targetable_row_ids)
        ready_rows_without_targets = max(ready_rows - ready_rows_with_targets, 0)

    report = {
        "generated_at": now.isoformat(),
        "mailing_id": int(mailing.id),
        "mailing_is_active": bool(mailing.is_active),
        "schedule_window_open": bool(mailing.scheduled_time_begin <= now <= mailing.scheduled_time_end),
        "send_window_open": bool(_is_time_in_window(current_time, mailing.send_window_begin, mailing.send_window_end)),
        "selected_bots_total": int(mailing.bot_profiles.count()),
        "selected_bots_active": int(len(selected_bot_ids)),
        "rows_total": int(rows_scope.count()),
        "planned_rows_total": int(rows_scope.filter(status=MailingGuest.Status.PLANNED).count()),
        "ready_rows": ready_rows,
        "future_rows": int(rows_scope.filter(status=MailingGuest.Status.PLANNED, scheduled_datetime__gt=now).count()),
        "in_progress_rows": int(rows_scope.filter(status=MailingGuest.Status.IN_PROGRESS).count()),
        "done_rows": int(rows_scope.filter(status=MailingGuest.Status.DONE).count()),
        "error_rows": int(rows_scope.filter(status=MailingGuest.Status.ERROR).count()),
        "ready_rows_with_targets": int(ready_rows_with_targets),
        "ready_rows_without_targets": int(ready_rows_without_targets),
        "ready_rows_with_file_telegram_id": int(ready_rows_with_file_telegram_id),
    }
    return report


def _is_coupon_sync_gate_ack_wait_report(gate_report: dict[str, object]) -> bool:
    """
    Проверяет, что ручной запуск остановился только из-за ожидания подтверждения купона от vtelemax.
    """
    if not bool(gate_report.get("coupon_mode")):
        return False
    if int(gate_report.get("rows_total") or 0) <= 0:
        return False
    if int(gate_report.get("rows_ready") or 0) > 0:
        return False
    if int(gate_report.get("rows_blocked") or 0) <= 0:
        return False

    global_blockers = gate_report.get("global_blockers") or []
    if global_blockers:
        return False

    issues_by_code = gate_report.get("issues_by_code") or {}
    if not isinstance(issues_by_code, dict) or not issues_by_code:
        return False

    soft_block_codes = {
        str(code)
        for code in getattr(mailing_worker_cmd, "COUPON_GATE_SOFT_BLOCK_CODES", set())
    }
    return all(str(code) in soft_block_codes for code in issues_by_code)


def _run_mailing_now(mailing: Mailing, now, max_batches: int) -> dict[str, object]:
    """
    Выполняет ограниченный one-shot запуск кампании через существующий producer-путь.
    """
    report_before = _build_mailing_dry_run_report(mailing=mailing, now=now)
    processed_rows_total = 0
    processed_batches = 0
    reached_batch_limit = False
    stopped_on_coupon_sync_gate_wait = False
    gate_reports: list[dict[str, object]] = []

    if report_before["send_window_open"] and report_before["ready_rows"] > 0:
        for _ in range(max_batches):
            gate_reports_before = len(gate_reports)
            processed = int(
                mailing_worker_cmd.process_one_mailing(
                    mailing=mailing,
                    now=now,
                    gate_reports_collector=gate_reports,
                )
                or 0
            )
            if processed <= 0:
                break
            processed_rows_total += processed
            processed_batches += 1
            new_gate_reports = gate_reports[gate_reports_before:]
            if new_gate_reports and all(
                _is_coupon_sync_gate_ack_wait_report(gate_report)
                for gate_report in new_gate_reports
            ):
                stopped_on_coupon_sync_gate_wait = True
                break
        if processed_batches >= max_batches and not stopped_on_coupon_sync_gate_wait:
            reached_batch_limit = True

    report_after = _build_mailing_dry_run_report(mailing=mailing, now=timezone.now())
    coupon_gate_blocked_reasons: dict[str, int] = {}
    coupon_gate_blocked_rows = 0
    for gate_report in gate_reports:
        coupon_gate_blocked_rows += int(gate_report.get("rows_blocked") or 0)
        issues = gate_report.get("issues") or []
        if isinstance(issues, list):
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                message = str(issue.get("message") or "").strip()
                reason = message or "Причина не указана"
                coupon_gate_blocked_reasons[reason] = int(coupon_gate_blocked_reasons.get(reason, 0) + 1)
        global_blockers = gate_report.get("global_blockers") or []
        if isinstance(global_blockers, list):
            for blocker in global_blockers:
                reason = str(blocker or "").strip()
                if not reason:
                    continue
                coupon_gate_blocked_reasons[reason] = int(coupon_gate_blocked_reasons.get(reason, 0) + 1)
    return {
        "generated_at": timezone.now().isoformat(),
        "mailing_id": int(mailing.id),
        "schedule_window_open": bool(report_before["schedule_window_open"]),
        "send_window_open": bool(report_before["send_window_open"]),
        "ready_rows_before": int(report_before["ready_rows"]),
        "ready_rows_after": int(report_after["ready_rows"]),
        "processed_batches": int(processed_batches),
        "processed_rows_total": int(processed_rows_total),
        "batch_size": int(mailing_worker_cmd.BATCH_SIZE),
        "max_batches": int(max_batches),
        "reached_batch_limit": bool(reached_batch_limit),
        "stopped_on_coupon_sync_gate_wait": bool(stopped_on_coupon_sync_gate_wait),
        "coupon_mode": bool(getattr(mailing, "coupon_series", None)),
        "coupon_series": str(getattr(mailing, "coupon_series", "") or "").strip(),
        "coupon_venue_code": str(getattr(mailing, "coupon_venue_code", "") or "").strip(),
        "coupon_venue_name": str(getattr(mailing, "coupon_venue_name", "") or "").strip(),
        "coupon_gate_blocked_rows": coupon_gate_blocked_rows,
        "coupon_gate_blocked_reasons": coupon_gate_blocked_reasons,
    }


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _get_workbench_snapshot(request, mailing: Mailing | int) -> dict | None:
    """
    Достаёт и нормализует снимок фильтров рабочего экрана гостей для кампании.
    """
    mailing_id = int(mailing.pk if isinstance(mailing, Mailing) else mailing)
    raw_snapshot = None
    if isinstance(mailing, Mailing):
        stored_snapshot = getattr(mailing, "source_filter_snapshot", None)
        if isinstance(stored_snapshot, dict) and stored_snapshot:
            raw_snapshot = stored_snapshot

    all_snapshots = request.session.get("mailings_v2_workbench_snapshots", {})
    if raw_snapshot is None and isinstance(all_snapshots, dict):
        raw_snapshot = all_snapshots.get(str(mailing_id))
    if not isinstance(raw_snapshot, dict):
        return None

    venue_selection_mode = str(raw_snapshot.get("venue_selection_mode") or "").strip()
    audience_channel_group = str(raw_snapshot.get("audience_channel_group") or "").strip()
    mailing_target_mode = str(raw_snapshot.get("mailing_target_mode") or "").strip()
    mailing_queue_priority = str(raw_snapshot.get("mailing_queue_priority") or "").strip()
    mailing_bot_profiles = raw_snapshot.get("mailing_bot_profiles") or []
    if not isinstance(mailing_bot_profiles, list):
        mailing_bot_profiles = []
    source_layer = str(raw_snapshot.get("source_layer") or "").strip()
    historical_audience_mode = source_layer == "historical_all_time"
    venue_selection_mode_label = _localize_workbench_venue_selection_mode(venue_selection_mode)
    if historical_audience_mode and venue_selection_mode == "visited_once":
        venue_selection_mode_label = "Был хотя бы 1 раз за всё доступное время"

    snapshot = {
        "as_of_date": str(raw_snapshot.get("as_of_date") or "").strip(),
        "window_days": str(raw_snapshot.get("window_days") or "").strip(),
        "department_id": str(raw_snapshot.get("department_id") or "").strip(),
        "venue_selection_mode": venue_selection_mode,
        "venue_selection_mode_label": venue_selection_mode_label,
        "segment_code": str(raw_snapshot.get("segment_code") or "").strip(),
        "focus_category_code": str(raw_snapshot.get("focus_category_code") or "").strip(),
        "audience_channel_group": audience_channel_group,
        "audience_channel_group_label": _localize_workbench_audience_group(audience_channel_group),
        "audience_limit_enabled": bool(raw_snapshot.get("audience_limit_enabled")),
        "audience_limit": _safe_int(raw_snapshot.get("audience_limit")),
        "selected_total": _safe_int(raw_snapshot.get("selected_total")),
        "selected_rows_count": _safe_int(raw_snapshot.get("selected_rows_count")),
        "delivery_total_guests": _safe_int(raw_snapshot.get("delivery_total_guests")),
        "delivery_available_guests": _safe_int(raw_snapshot.get("delivery_available_guests")),
        "delivery_blocked_without_bot_binding": _safe_int(raw_snapshot.get("delivery_blocked_without_bot_binding")),
        "delivery_blocked_without_message_permission": _safe_int(
            raw_snapshot.get("delivery_blocked_without_message_permission")
        ),
        "delivery_legacy_telegram_guests": _safe_int(raw_snapshot.get("delivery_legacy_telegram_guests")),
        "delivery_planned_tasks": _safe_int(raw_snapshot.get("delivery_planned_tasks")),
        "mailing_template_id": _safe_int(raw_snapshot.get("mailing_template_id")),
        "mailing_template_name": str(raw_snapshot.get("mailing_template_name") or "").strip(),
        "mailing_target_mode": mailing_target_mode,
        "mailing_target_mode_label": _localize_target_mode(mailing_target_mode),
        "mailing_queue_priority": mailing_queue_priority,
        "mailing_queue_priority_label": _localize_queue_priority(mailing_queue_priority),
        "mailing_send_window_begin": str(raw_snapshot.get("mailing_send_window_begin") or "").strip(),
        "mailing_send_window_end": str(raw_snapshot.get("mailing_send_window_end") or "").strip(),
        "mailing_bot_profiles": [str(item) for item in mailing_bot_profiles if str(item or "").strip()],
        "source_layer": source_layer,
        "source_layer_label": "Всё доступное время" if historical_audience_mode else "Оконная аналитика",
        "historical_audience_mode": historical_audience_mode,
        "saved_at": str(raw_snapshot.get("saved_at") or "").strip(),
        "complex_filters": [],
    }
    snapshot["mailing_bot_profiles_summary"] = ", ".join(snapshot["mailing_bot_profiles"])

    complex_filters_raw = raw_snapshot.get("complex_filters") or []
    if isinstance(complex_filters_raw, list):
        for item in complex_filters_raw:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            operator = str(item.get("operator") or "").strip()
            value = str(item.get("value") or "").strip()
            if not (field or operator or value):
                continue
            snapshot["complex_filters"].append(
                {
                    "field": field,
                    "operator": operator,
                    "value": value,
                }
            )
    return snapshot


def _build_workbench_url_from_snapshot(snapshot: dict) -> str:
    """
    Собирает URL перехода в Workbench по сохранённому snapshot фильтров.
    """
    params = {"active_tab": "overview"}
    for key in (
        "as_of_date",
        "window_days",
        "department_id",
        "venue_selection_mode",
        "segment_code",
        "focus_category_code",
        "audience_channel_group",
    ):
        value = str(snapshot.get(key) or "").strip()
        if value:
            params[key] = value

    complex_filters = snapshot.get("complex_filters") or []
    if isinstance(complex_filters, list) and complex_filters:
        params["cf_field"] = [str(item.get("field") or "").strip() for item in complex_filters]
        params["cf_op"] = [str(item.get("operator") or "").strip() for item in complex_filters]
        params["cf_value"] = [str(item.get("value") or "").strip() for item in complex_filters]

    base_url = reverse("guests_workbench")
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params, doseq=True)}"


def _build_campaign_wizard_state(*, mailing: Mailing | None, active_tab: str) -> dict[str, object]:
    """
    Формирует состояние мастер-флоу кампании (3 шага) для нового UI.

    Шаги:
    1. параметры кампании;
    2. аудитория;
    3. проверка и запуск.
    """
    if not mailing or not mailing.pk:
        return {
            "current_step": 1,
            "summary": "Шаг 1 из 3: заполните параметры кампании и сохраните черновик.",
            "cta_label": "Сохранить шаг 1",
            "cta_url": "",
            "steps": [
                {"number": 1, "title": "Параметры", "status": "current", "url": ""},
                {"number": 2, "title": "Аудитория", "status": "todo", "url": ""},
                {"number": 3, "title": "Проверка и запуск", "status": "todo", "url": ""},
            ],
        }

    rows_total = int(mailing.guests_rows.count())
    has_audience = rows_total > 0
    has_dispatch_activity = DispatchTask.objects.filter(mailing_guest__mailing=mailing).exists()
    has_send_results = mailing.guests_rows.filter(status__in=[MailingGuest.Status.DONE, MailingGuest.Status.ERROR]).exists()

    if not has_audience:
        recommended_step = 2
    elif has_dispatch_activity or has_send_results or mailing.is_active:
        recommended_step = 3
    else:
        recommended_step = 3

    tab_to_step = {
        "overview": recommended_step,
        "audience": 2,
        "runs": 3,
        "jobs": 3,
        "errors": 3,
        "logs": 3,
    }
    current_step = int(tab_to_step.get(active_tab or "overview", recommended_step))

    step1_url = reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.pk})
    step2_url = reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.pk})
    step3_url = reverse("mailings_v2_campaigns_runs", kwargs={"pk": mailing.pk})

    if current_step <= 1:
        summary = "Шаг 1 из 3: проверьте шаблон, окна отправки, режим и выбранных ботов."
        cta_label = "Перейти к аудитории"
        cta_url = step2_url
    elif current_step == 2:
        summary = (
            f"Шаг 2 из 3: соберите аудиторию. Сейчас строк в кампании: {rows_total}."
            " Когда состав готов, переходите к запуску."
        )
        cta_label = "Перейти к запуску"
        cta_url = step3_url
    else:
        summary = (
            "Шаг 3 из 3: выполните проверку перед запуском, проверьте задания/ошибки и запустите кампанию."
        )
        cta_label = "Открыть экран запусков"
        cta_url = step3_url

    steps = []
    for number, title, url in (
        (1, "Параметры", step1_url),
        (2, "Аудитория", step2_url),
        (3, "Проверка и запуск", step3_url),
    ):
        if number < current_step:
            status = "done"
        elif number == current_step:
            status = "current"
        else:
            status = "todo"
        steps.append(
            {
                "number": number,
                "title": title,
                "status": status,
                "url": url,
            }
        )

    return {
        "current_step": current_step,
        "summary": summary,
        "cta_label": cta_label,
        "cta_url": cta_url,
        "steps": steps,
    }


_MAILING_ROW_STATUS_LABELS_RU: dict[str, str] = {
    MailingGuest.Status.PLANNED: "запланировано",
    MailingGuest.Status.IN_PROGRESS: "в обработке",
    MailingGuest.Status.DONE: "успешно",
    MailingGuest.Status.ERROR: "ошибка",
}

_DELIVERY_STATUS_LABELS_RU: dict[str, str] = {
    "pending": "ожидает",
    "queued": "в очереди",
    "in_progress": "в обработке",
    "done": "доставлено",
    "success": "доставлено",
    "delivered": "доставлено",
    "failed": "ошибка",
    "error": "ошибка",
    "dispatch_no_targets": "нет целей отправки",
    "dispatch_no_bot_profiles": "нет активных ботов",
    "dispatch_enqueue_error": "ошибка постановки в очередь",
    "dispatch_enqueue_exception": "исключение при постановке в очередь",
    "retry_requested": "запрошен повтор",
    "requeued_from_ui": "повторно поставлено из UI",
    "duplicated_from_campaign": "скопировано из исходной кампании",
}


def _localize_mailing_row_status(value: str | None) -> str:
    status = (str(value or "")).strip()
    if not status:
        return "—"
    return _MAILING_ROW_STATUS_LABELS_RU.get(status, status)


def _localize_delivery_status(value: str | None) -> str:
    status = (str(value or "")).strip()
    if not status:
        return "—"
    return _DELIVERY_STATUS_LABELS_RU.get(status, status)


def _empty_mailing_row_stats() -> dict[str, int]:
    return {
        "total": 0,
        "planned": 0,
        "in_progress": 0,
        "done": 0,
        "error": 0,
    }


def _empty_dispatch_stats() -> dict[str, int]:
    return {
        "total": 0,
        "pending": 0,
        "queued": 0,
        "in_progress": 0,
        "done": 0,
        "failed": 0,
        "canceled": 0,
    }


def _build_mailing_row_stats(mailing: Mailing) -> dict[str, int]:
    """
    Сводка по статусам строк получателей для конкретной кампании.
    """
    stats = mailing.guests_rows.aggregate(
        total=Count("id"),
        planned=Count("id", filter=Q(status=MailingGuest.Status.PLANNED)),
        in_progress=Count("id", filter=Q(status=MailingGuest.Status.IN_PROGRESS)),
        done=Count("id", filter=Q(status=MailingGuest.Status.DONE)),
        error=Count("id", filter=Q(status=MailingGuest.Status.ERROR)),
    )
    result = _empty_mailing_row_stats()
    for key in result.keys():
        result[key] = int(stats.get(key) or 0)
    return result


def _build_mailing_dispatch_stats(mailing: Mailing) -> dict[str, int]:
    """
    Сводка по статусам dispatch-задач, связанных с кампанией.
    """
    scope = DispatchTask.objects.filter(mailing_guest__mailing=mailing)
    stats = scope.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(status=DispatchTask.Status.PENDING)),
        queued=Count("id", filter=Q(status=DispatchTask.Status.QUEUED)),
        in_progress=Count("id", filter=Q(status=DispatchTask.Status.IN_PROGRESS)),
        done=Count("id", filter=Q(status=DispatchTask.Status.DONE)),
        failed=Count("id", filter=Q(status=DispatchTask.Status.FAILED)),
        canceled=Count("id", filter=Q(status=DispatchTask.Status.CANCELED)),
    )
    result = _empty_dispatch_stats()
    for key in result.keys():
        result[key] = int(stats.get(key) or 0)
    return result


def _build_coupon_iiko_category_stats(mailing: Mailing) -> dict[str, int]:
    """
    Сводка pre-send gate iikoCard для купонной кампании.
    """
    assignments = CouponCampaignAssignment.objects.filter(campaign=mailing)
    assignment_stats = assignments.aggregate(
        total=Count("id"),
        disabled=Count(
            "id",
            filter=Q(
                iiko_category_add_status=CouponCampaignAssignment.IikoCategorySyncStatus.DISABLED
            ),
        ),
        pending=Count(
            "id",
            filter=Q(
                iiko_category_add_status=CouponCampaignAssignment.IikoCategorySyncStatus.PENDING
            ),
        ),
        ok=Count(
            "id",
            filter=Q(iiko_category_add_status=CouponCampaignAssignment.IikoCategorySyncStatus.OK),
        ),
        error=Count(
            "id",
            filter=Q(
                iiko_category_add_status=CouponCampaignAssignment.IikoCategorySyncStatus.ERROR
            ),
        ),
    )
    events = IikoCustomerCategorySyncEvent.objects.filter(campaign_assignment__campaign=mailing)
    event_stats = events.aggregate(
        events_total=Count("id"),
        events_pending=Count("id", filter=Q(status=IikoCustomerCategorySyncEvent.Status.PENDING)),
        events_sent=Count("id", filter=Q(status=IikoCustomerCategorySyncEvent.Status.SENT)),
        events_acked=Count("id", filter=Q(status=IikoCustomerCategorySyncEvent.Status.ACKED)),
        events_error=Count("id", filter=Q(status=IikoCustomerCategorySyncEvent.Status.ERROR)),
        events_skipped=Count("id", filter=Q(status=IikoCustomerCategorySyncEvent.Status.SKIPPED)),
        add_pending=Count(
            "id",
            filter=Q(
                action=IikoCustomerCategorySyncEvent.Action.ADD,
                status=IikoCustomerCategorySyncEvent.Status.PENDING,
            ),
        ),
        remove_pending=Count(
            "id",
            filter=Q(
                action=IikoCustomerCategorySyncEvent.Action.REMOVE,
                status=IikoCustomerCategorySyncEvent.Status.PENDING,
            ),
        ),
    )
    result: dict[str, int] = {}
    for key, value in {**assignment_stats, **event_stats}.items():
        result[str(key)] = int(value or 0)
    return result


def _build_dispatch_timeline(tasks: list[DispatchTask]) -> list[dict[str, object]]:
    """
    Формирует компактный таймлайн по последним задачам доставки.
    """
    timeline: list[dict[str, object]] = []
    for task in tasks:
        event_time = task.finished_at or task.started_at or task.enqueued_at or task.created_at
        timeline.append(
            {
                "task_id": int(task.id),
                "status": _localize_dispatch_status(task.status),
                "provider_type": _localize_provider_type(task.provider_type),
                "guest_phone": str(task.guest.phone) if task.guest and task.guest.phone else "",
                "event_time": event_time,
                "message": (task.last_error or "")[:200],
            }
        )
    timeline.sort(key=lambda item: item["event_time"] or timezone.now(), reverse=True)
    return timeline[:60]


def _build_mailing_log_timeline(
    *,
    rows: list[MailingGuest],
    tasks: list[DispatchTask],
) -> list[dict[str, object]]:
    """
    Собирает общий таймлайн изменений по строкам аудитории и задачам доставки.
    """
    timeline: list[dict[str, object]] = []

    for row in rows:
        # У модели MailingGuest нет updated_at, поэтому используем доступные временные поля.
        event_time = row.sent_at or row.created_at or row.scheduled_datetime
        timeline.append(
            {
                "kind": "row",
                "kind_label": "Строка аудитории",
                "event_time": event_time,
                "status": _localize_mailing_row_status(row.status),
                "phone": str(row.phone or (row.guest.phone if row.guest and row.guest.phone else "")),
                "title": f"Строка аудитории #{row.id}",
                "message": str(row.error_description or row.delivery_status or ""),
            }
        )

    for task in tasks:
        event_time = task.finished_at or task.started_at or task.enqueued_at or task.created_at
        timeline.append(
            {
                "kind": "dispatch",
                "kind_label": "Задача доставки",
                "event_time": event_time,
                "status": _localize_dispatch_status(task.status),
                "phone": str(task.guest.phone) if task.guest and task.guest.phone else "",
                "title": f"Задача доставки #{task.id}",
                "message": str(task.last_error or task.message_text or "")[:240],
            }
        )

    timeline.sort(key=lambda item: item["event_time"] or timezone.now(), reverse=True)
    return timeline[:120]


_DISPATCH_STATUS_LABELS_RU: dict[str, str] = {
    DispatchTask.Status.PENDING: "ожидает",
    DispatchTask.Status.QUEUED: "в очереди",
    DispatchTask.Status.IN_PROGRESS: "в обработке",
    DispatchTask.Status.DONE: "успешно",
    DispatchTask.Status.FAILED: "ошибка",
    DispatchTask.Status.CANCELED: "отменено",
}

_PROVIDER_TYPE_LABELS_RU: dict[str, str] = {
    "telegram": "Телеграм",
    "max": "Макс",
    "vk": "ВК",
}

_WORKBENCH_AUDIENCE_GROUP_LABELS_RU: dict[str, str] = {
    "all": "Все гости",
    "new_bots_sendable": "Доступна рассылка в новых ботах",
    "legacy_no_new_bot": "Историческая Telegram-аудитория",
    "new_bots_blocked": "В новых ботах, но рассылка запрещена",
}

_WORKBENCH_VENUE_SELECTION_LABELS_RU: dict[str, str] = {
    "": "Все заведения",
    "all": "Все заведения",
    "visited_once": "Был хотя бы 1 раз",
    "favorite": "Любимое заведение",
    "last_visit": "Последнее посещение",
}


def _localize_dispatch_status(value: str | None) -> str:
    status = str(value or "").strip()
    if not status:
        return "—"
    return _DISPATCH_STATUS_LABELS_RU.get(status, status)


def _localize_provider_type(value: str | None) -> str:
    provider_type = str(value or "").strip()
    if not provider_type:
        return "—"
    return _PROVIDER_TYPE_LABELS_RU.get(provider_type, provider_type)


def _localize_target_mode(value: str | None) -> str:
    target_mode = str(value or "").strip()
    if not target_mode:
        return "—"
    return dict(Mailing.TargetMode.choices).get(target_mode, target_mode)


def _localize_queue_priority(value: str | None) -> str:
    priority = str(value or "").strip()
    if not priority:
        return "—"
    return dict(Mailing.QueuePriority.choices).get(priority, priority)


def _localize_workbench_audience_group(value: str | None) -> str:
    audience_group = str(value or "").strip()
    if not audience_group:
        return "Все гости"
    return _WORKBENCH_AUDIENCE_GROUP_LABELS_RU.get(audience_group, audience_group)


def _localize_workbench_venue_selection_mode(value: str | None) -> str:
    mode = str(value or "").strip()
    return _WORKBENCH_VENUE_SELECTION_LABELS_RU.get(mode, mode or "Все заведения")


def _localize_queue_name(value: str | None) -> str:
    queue_name = str(value or "").strip()
    if not queue_name:
        return "—"
    parts = queue_name.split(":")
    if len(parts) >= 4 and parts[0] == "uq":
        return f"{_localize_provider_type(parts[2])} / {_localize_queue_priority(parts[3])}"
    return queue_name


def _localized_choices(choices, localizer) -> list[tuple[str, str]]:
    return [(value, localizer(value)) for value, _label in choices]


def _decorate_mailing_rows(rows: list[MailingGuest]) -> None:
    for row in rows:
        row.status_label = _localize_mailing_row_status(row.status)
        row.delivery_status_label = _localize_delivery_status(row.delivery_status)


def _decorate_dispatch_tasks(tasks: list[DispatchTask]) -> None:
    for task in tasks:
        task.status_label = _localize_dispatch_status(task.status)
        task.provider_type_label = _localize_provider_type(task.provider_type)
        task.queue_name_label = _localize_queue_name(task.queue_name)
        task.priority_label = _localize_queue_priority(task.priority)


def _build_mailing_ui_status(
    mailing: Mailing | None,
    *,
    row_stats: dict[str, int] | None = None,
    dispatch_stats: dict[str, int] | None = None,
) -> dict[str, str]:
    """
    Человекочитаемое состояние кампании для интерфейса.

    Это не новое поле в базе: состояние вычисляется из активности кампании,
    статусов строк аудитории и задач доставки.
    """
    if mailing is None:
        return {
            "code": "draft",
            "label": "черновик",
            "badge_class": "text-bg-light text-dark border",
            "description": "Кампания ещё не сохранена.",
        }

    row_stats = row_stats or _build_mailing_row_stats(mailing)
    dispatch_stats = dispatch_stats or _build_mailing_dispatch_stats(mailing)

    total_rows = int(row_stats.get("total") or 0)
    planned_rows = int(row_stats.get("planned") or 0)
    in_progress_rows = int(row_stats.get("in_progress") or 0)
    done_rows = int(row_stats.get("done") or 0)
    error_rows = int(row_stats.get("error") or 0)
    active_tasks = int(dispatch_stats.get("pending") or 0) + int(dispatch_stats.get("queued") or 0) + int(
        dispatch_stats.get("in_progress") or 0
    )
    failed_tasks = int(dispatch_stats.get("failed") or 0)
    final_rows = done_rows + error_rows
    has_only_future_planned_rows = False
    if planned_rows > 0 and in_progress_rows == 0 and active_tasks == 0 and final_rows == 0:
        planned_scope = mailing.guests_rows.filter(status=MailingGuest.Status.PLANNED)
        now = timezone.now()
        future_rows_exist = planned_scope.filter(scheduled_datetime__gt=now).exists()
        ready_rows_exist = planned_scope.filter(scheduled_datetime__lte=now).exists()
        has_only_future_planned_rows = future_rows_exist and not ready_rows_exist

    if mailing.is_archived:
        return {
            "code": "archived",
            "label": "архив",
            "badge_class": "text-bg-dark",
            "description": "Кампания перенесена в архив.",
        }

    if total_rows > 0 and planned_rows == 0 and in_progress_rows == 0 and active_tasks == 0 and final_rows >= total_rows:
        has_errors = error_rows > 0 or failed_tasks > 0
        return {
            "code": "completed",
            "label": "завершена",
            "badge_class": "text-bg-secondary",
            "description": "Обработка завершена, есть ошибки доставки." if has_errors else "Обработка завершена.",
        }

    if mailing.is_active and total_rows > 0 and has_only_future_planned_rows:
        return {
            "code": "scheduled",
            "label": "запланирована",
            "badge_class": "text-bg-primary",
            "description": "Кампания включена, отправка начнётся в запланированное время.",
        }

    if mailing.is_active:
        return {
            "code": "active",
            "label": "выполняется",
            "badge_class": "text-bg-success",
            "description": "Кампания включена и может обрабатывать подходящие строки.",
        }

    return {
        "code": "paused",
        "label": "пауза",
        "badge_class": "text-bg-secondary",
        "description": "Кампания остановлена или ожидает запуска.",
    }
