"""
Синхронизация расписания Django Q из settings в таблицу django_q_schedule.

Проблема:
1. В проекте расписание описывается в settings.Q_CLUSTER["schedule"].
2. django-q2 исполняет только строки из модели django_q.models.Schedule.
3. Без явного upsert в БД задачи из settings могут не запускаться.

Этот модуль реализует idempotent sync:
1. upsert по name;
2. fallback-переименование legacy-строки по func (если совпадение единственное);
3. опциональное удаление stale-строк для управляемых ключей.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any
from typing import Iterable
from typing import Mapping

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule

logger = logging.getLogger(__name__)


# Управляемые ключи расписания проекта.
# Нужны, чтобы корректно удалять stale-строки при отключении env-флагов.
DEFAULT_MANAGED_SCHEDULE_NAMES: tuple[str, ...] = (
    "sync_webhooks_recent",
    "run_notification_scenarios",
    "run_vtelemax_recipients_delta",
    "run_olap_sync_windowed",
    "run_olap_rebuild_nightly",
    "run_order_fact_tail",
    "run_daily_fact_tail",
    "run_daily_order_fact_tail",
    "run_order_focus_fact_tail",
    "run_window_metrics_hourly",
    "run_window_category_metrics_hourly",
    "run_olap_control_pull_daily",
)


@dataclass(slots=True)
class DjangoQScheduleSyncStats:
    """
    Статистика синхронизации расписания Django Q.
    """

    source_entries: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    renamed_by_func: int = 0
    skipped_invalid: int = 0


@dataclass(slots=True, frozen=True)
class _NormalizedScheduleEntry:
    name: str
    func: str
    schedule_type: str
    minutes: int | None
    cron: str | None
    hook: str | None
    args: str | None
    kwargs: str | None
    repeats: int
    cluster: str | None
    intended_date_kwarg: str | None


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_args_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, tuple):
        return repr(value)
    if isinstance(value, list):
        return repr(tuple(value))
    return _clean_optional_text(value)


def _normalize_kwargs_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return repr(value)
    return _clean_optional_text(value)


def _normalize_schedule_entry(
    schedule_name: str,
    raw_entry: Mapping[str, Any],
) -> _NormalizedScheduleEntry | None:
    if not isinstance(raw_entry, Mapping):
        return None

    func = _clean_optional_text(raw_entry.get("func"))
    if not func:
        return None

    raw_schedule_type = _clean_optional_text(raw_entry.get("schedule_type"))
    schedule_type = (raw_schedule_type or "").upper()

    raw_minutes = raw_entry.get("minutes")
    minutes: int | None = None
    if raw_minutes not in (None, ""):
        try:
            minutes = max(1, int(raw_minutes))
        except (TypeError, ValueError):
            minutes = None

    cron = _clean_optional_text(raw_entry.get("cron"))

    if not schedule_type:
        if minutes is not None:
            schedule_type = Schedule.MINUTES
        elif cron:
            schedule_type = Schedule.CRON
        else:
            schedule_type = Schedule.MINUTES

    if schedule_type == Schedule.MINUTES and minutes is None:
        minutes = 1

    if schedule_type == Schedule.CRON and not cron:
        return None

    try:
        repeats = int(raw_entry.get("repeats", -1))
    except (TypeError, ValueError):
        repeats = -1

    return _NormalizedScheduleEntry(
        name=schedule_name,
        func=func,
        schedule_type=schedule_type,
        minutes=minutes,
        cron=cron,
        hook=_clean_optional_text(raw_entry.get("hook")),
        args=_normalize_args_value(raw_entry.get("args")),
        kwargs=_normalize_kwargs_value(raw_entry.get("kwargs")),
        repeats=repeats,
        cluster=_clean_optional_text(raw_entry.get("cluster")),
        intended_date_kwarg=_clean_optional_text(raw_entry.get("intended_date_kwarg")),
    )


def _get_project_schedule_map() -> dict[str, Mapping[str, Any]]:
    q_cluster = getattr(settings, "Q_CLUSTER", {}) or {}
    schedule_map = q_cluster.get("schedule", {}) or {}
    if not isinstance(schedule_map, Mapping):
        return {}
    return {
        str(name): entry
        for name, entry in schedule_map.items()
        if isinstance(name, str)
    }


def _load_managed_schedule_names(extra_names: Iterable[str] | None = None) -> set[str]:
    managed_names: set[str] = set(DEFAULT_MANAGED_SCHEDULE_NAMES)

    settings_managed = getattr(settings, "DJANGO_Q_SCHEDULE_MANAGED_NAMES", ())
    if settings_managed:
        managed_names.update(str(name).strip() for name in settings_managed if str(name).strip())

    settings_extra = getattr(settings, "DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES", set())
    if settings_extra:
        managed_names.update(str(name).strip() for name in settings_extra if str(name).strip())

    if extra_names:
        managed_names.update(str(name).strip() for name in extra_names if str(name).strip())

    return managed_names


def _calc_next_run_for_entry(entry: _NormalizedScheduleEntry):
    if entry.schedule_type == Schedule.CRON and entry.cron:
        pseudo_schedule = Schedule(
            schedule_type=Schedule.CRON,
            cron=entry.cron,
            next_run=timezone.now(),
        )
        return pseudo_schedule.calculate_next_run(timezone.now())
    return timezone.now()


def _build_field_values(entry: _NormalizedScheduleEntry) -> dict[str, Any]:
    return {
        "func": entry.func,
        "hook": entry.hook,
        "args": entry.args,
        "kwargs": entry.kwargs,
        "schedule_type": entry.schedule_type,
        "minutes": entry.minutes,
        "repeats": entry.repeats,
        "cron": entry.cron,
        "cluster": entry.cluster,
        "intended_date_kwarg": entry.intended_date_kwarg,
    }


def sync_django_q_schedule_from_settings(
    *,
    schedule_map: Mapping[str, Mapping[str, Any]] | None = None,
    managed_names: Iterable[str] | None = None,
    prune_stale: bool = True,
    dry_run: bool = False,
) -> DjangoQScheduleSyncStats:
    """
    Синхронизирует settings.Q_CLUSTER["schedule"] -> django_q_schedule.

    Поведение:
    1. idempotent upsert по `name`;
    2. fallback legacy-rename по `func`, если найдено ровно одно совпадение;
    3. при `prune_stale=True` удаляет stale-строки для managed key set.
    """

    source_map = dict(schedule_map or _get_project_schedule_map())
    stats = DjangoQScheduleSyncStats(source_entries=len(source_map))

    desired_entries: dict[str, _NormalizedScheduleEntry] = {}
    for schedule_name, raw_entry in source_map.items():
        normalized_entry = _normalize_schedule_entry(schedule_name, raw_entry)
        if normalized_entry is None:
            stats.skipped_invalid += 1
            continue
        desired_entries[schedule_name] = normalized_entry

    desired_names = set(desired_entries.keys())

    for schedule_name in sorted(desired_entries):
        entry = desired_entries[schedule_name]
        field_values = _build_field_values(entry)

        existing = Schedule.objects.filter(name=entry.name).order_by("id").first()
        legacy_renamed = False

        if existing is None:
            # Legacy fallback: если имя поменялось, но функция совпадает,
            # переиспользуем единственную строку вместо дублирования.
            func_candidates = list(
                Schedule.objects.filter(func=entry.func)
                .exclude(name__in=desired_names)
                .order_by("id")[:2]
            )
            if len(func_candidates) == 1:
                existing = func_candidates[0]
                legacy_renamed = True

        if existing is None:
            stats.created += 1
            if dry_run:
                continue

            Schedule.objects.create(
                name=entry.name,
                next_run=_calc_next_run_for_entry(entry),
                **field_values,
            )
            continue

        changes: dict[str, Any] = {}

        if existing.name != entry.name:
            changes["name"] = entry.name

        for field_name, desired_value in field_values.items():
            if getattr(existing, field_name) != desired_value:
                changes[field_name] = desired_value

        if not changes:
            stats.unchanged += 1
            continue

        if legacy_renamed:
            stats.renamed_by_func += 1

        stats.updated += 1
        if dry_run:
            continue

        for field_name, desired_value in changes.items():
            setattr(existing, field_name, desired_value)

        if {"schedule_type", "minutes", "cron"} & set(changes.keys()):
            existing.next_run = _calc_next_run_for_entry(entry)

        existing.save()

    if prune_stale:
        managed_set = _load_managed_schedule_names(managed_names)
        managed_set.update(desired_names)
        stale_names = managed_set - desired_names
        if stale_names:
            stale_qs = Schedule.objects.filter(name__in=stale_names)
            stale_count = stale_qs.count()
            if stale_count:
                stats.deleted += stale_count
                if not dry_run:
                    stale_qs.delete()

    logger.info(
        "Django Q schedule sync: source=%s created=%s updated=%s unchanged=%s deleted=%s renamed=%s skipped_invalid=%s dry_run=%s prune_stale=%s",
        stats.source_entries,
        stats.created,
        stats.updated,
        stats.unchanged,
        stats.deleted,
        stats.renamed_by_func,
        stats.skipped_invalid,
        dry_run,
        prune_stale,
    )

    return stats
