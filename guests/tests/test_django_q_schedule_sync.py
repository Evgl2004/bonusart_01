"""
Тесты синхронизации расписания Django Q из settings.
"""

from __future__ import annotations

from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from django_q.models import Schedule

from guests.services.django_q_schedule_sync import sync_django_q_schedule_from_settings


class DjangoQScheduleSyncTests(TestCase):
    """
    Проверяет upsert/rename/prune/dry-run для django_q_schedule.
    """

    @override_settings(
        Q_CLUSTER={
            "schedule": {
                "sync_webhooks_recent": {
                    "func": "guests.tasks.fetch_pending_webhooks",
                    "minutes": 10,
                },
                "run_window_metrics_hourly": {
                    "func": "guests.tasks.run_window_metrics_scheduled_task",
                    "minutes": 60,
                },
            }
        },
        DJANGO_Q_SCHEDULE_MANAGED_NAMES=(
            "sync_webhooks_recent",
            "run_window_metrics_hourly",
        ),
        DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES=set(),
    )
    def test_sync_creates_rows_from_settings_map(self):
        stats = sync_django_q_schedule_from_settings(prune_stale=True, dry_run=False)

        self.assertEqual(stats.source_entries, 2)
        self.assertEqual(stats.created, 2)
        self.assertEqual(stats.updated, 0)
        self.assertEqual(stats.deleted, 0)
        self.assertEqual(Schedule.objects.filter(name="sync_webhooks_recent").count(), 1)
        self.assertEqual(Schedule.objects.filter(name="run_window_metrics_hourly").count(), 1)

    @override_settings(
        Q_CLUSTER={
            "schedule": {
                "run_coupon_autoscenarios": {
                    "func": "guests.tasks.run_coupon_autoscenarios_task",
                    "schedule_type": "C",
                    "cron": "0 10 * * *",
                }
            }
        },
        DJANGO_Q_SCHEDULE_MANAGED_NAMES=("run_coupon_autoscenarios",),
        DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES=set(),
    )
    def test_sync_creates_coupon_autoscenario_schedule_as_cron(self):
        stats = sync_django_q_schedule_from_settings(prune_stale=True, dry_run=False)

        self.assertEqual(stats.created, 1)
        schedule = Schedule.objects.get(name="run_coupon_autoscenarios")
        self.assertEqual(schedule.func, "guests.tasks.run_coupon_autoscenarios_task")
        self.assertEqual(schedule.schedule_type, Schedule.CRON)
        self.assertEqual(schedule.cron, "0 10 * * *")
        self.assertIsNone(schedule.minutes)

    @override_settings(
        Q_CLUSTER={
            "schedule": {
                "run_coupon_autoscenario_close": {
                    "func": "guests.tasks.run_coupon_autoscenario_close_task",
                    "schedule_type": "C",
                    "cron": "30 5,11 * * *",
                }
            }
        },
        DJANGO_Q_SCHEDULE_MANAGED_NAMES=("run_coupon_autoscenario_close",),
        DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES=set(),
    )
    def test_sync_creates_coupon_autoscenario_close_schedule_as_cron(self):
        stats = sync_django_q_schedule_from_settings(prune_stale=True, dry_run=False)

        self.assertEqual(stats.created, 1)
        schedule = Schedule.objects.get(name="run_coupon_autoscenario_close")
        self.assertEqual(schedule.func, "guests.tasks.run_coupon_autoscenario_close_task")
        self.assertEqual(schedule.schedule_type, Schedule.CRON)
        self.assertEqual(schedule.cron, "30 5,11 * * *")
        self.assertIsNone(schedule.minutes)

    @override_settings(
        Q_CLUSTER={
            "schedule": {
                "run_coupon_autoscenario_delivery_guard": {
                    "func": "guests.tasks.run_coupon_autoscenario_delivery_guard_task",
                    "schedule_type": "C",
                    "cron": "*/20 10-22 * * *",
                }
            }
        },
        DJANGO_Q_SCHEDULE_MANAGED_NAMES=("run_coupon_autoscenario_delivery_guard",),
        DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES=set(),
    )
    def test_sync_creates_coupon_autoscenario_delivery_guard_schedule_as_cron(self):
        stats = sync_django_q_schedule_from_settings(prune_stale=True, dry_run=False)

        self.assertEqual(stats.created, 1)
        schedule = Schedule.objects.get(name="run_coupon_autoscenario_delivery_guard")
        self.assertEqual(schedule.func, "guests.tasks.run_coupon_autoscenario_delivery_guard_task")
        self.assertEqual(schedule.schedule_type, Schedule.CRON)
        self.assertEqual(schedule.cron, "*/20 10-22 * * *")
        self.assertIsNone(schedule.minutes)

    @override_settings(
        Q_CLUSTER={
            "schedule": {
                "sync_webhooks_recent": {
                    "func": "guests.tasks.fetch_pending_webhooks",
                    "minutes": 10,
                }
            }
        },
        DJANGO_Q_SCHEDULE_MANAGED_NAMES=("sync_webhooks_recent",),
        DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES=set(),
    )
    def test_sync_renames_single_legacy_row_by_func(self):
        Schedule.objects.create(
            name="Fetch pending webhooks",
            func="guests.tasks.fetch_pending_webhooks",
            schedule_type=Schedule.MINUTES,
            minutes=10,
            repeats=-1,
            next_run=timezone.now(),
        )

        stats = sync_django_q_schedule_from_settings(prune_stale=True, dry_run=False)

        self.assertEqual(stats.created, 0)
        self.assertEqual(stats.updated, 1)
        self.assertEqual(stats.renamed_by_func, 1)
        self.assertEqual(Schedule.objects.filter(func="guests.tasks.fetch_pending_webhooks").count(), 1)
        self.assertTrue(Schedule.objects.filter(name="sync_webhooks_recent").exists())

    @override_settings(
        Q_CLUSTER={
            "schedule": {
                "sync_webhooks_recent": {
                    "func": "guests.tasks.fetch_pending_webhooks",
                    "minutes": 10,
                }
            }
        },
        DJANGO_Q_SCHEDULE_MANAGED_NAMES=(
            "sync_webhooks_recent",
            "run_order_fact_tail",
        ),
        DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES=set(),
    )
    def test_sync_prunes_stale_managed_rows(self):
        Schedule.objects.create(
            name="run_order_fact_tail",
            func="guests.tasks.run_order_fact_scheduled_task",
            schedule_type=Schedule.MINUTES,
            minutes=31,
            repeats=-1,
            next_run=timezone.now(),
        )

        stats = sync_django_q_schedule_from_settings(prune_stale=True, dry_run=False)

        self.assertEqual(stats.deleted, 1)
        self.assertFalse(Schedule.objects.filter(name="run_order_fact_tail").exists())
        self.assertTrue(Schedule.objects.filter(name="sync_webhooks_recent").exists())

    @override_settings(
        Q_CLUSTER={
            "schedule": {
                "sync_webhooks_recent": {
                    "func": "guests.tasks.fetch_pending_webhooks",
                    "minutes": 10,
                }
            }
        },
        DJANGO_Q_SCHEDULE_MANAGED_NAMES=("sync_webhooks_recent",),
        DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES=set(),
    )
    def test_sync_dry_run_does_not_modify_db(self):
        stats = sync_django_q_schedule_from_settings(prune_stale=True, dry_run=True)

        self.assertEqual(stats.created, 1)
        self.assertEqual(Schedule.objects.count(), 0)
