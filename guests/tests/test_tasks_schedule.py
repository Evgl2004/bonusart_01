"""
Тесты плановых OLAP-задач в guests.tasks.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from guests import tasks


class OlapScheduleTasksTests(SimpleTestCase):
    """
    Проверяет запуск one-shot задач OLAP через Django Q.
    """

    @override_settings(OLAP_SYNC_SCHEDULE_ENABLED=False)
    @patch("guests.tasks.build_iiko_olap_client_from_settings")
    def test_sync_task_returns_zero_when_disabled(self, mocked_builder):
        """
        Если флаг выключен, задача не должна создавать клиент и выполнять sync.
        """
        result = tasks.run_olap_sync_scheduled_task()
        self.assertEqual(result, 0)
        mocked_builder.assert_not_called()

    @override_settings(
        OLAP_SYNC_SCHEDULE_ENABLED=True,
        OLAP_SYNC_WINDOW_START_LOCAL="12:00",
        OLAP_SYNC_WINDOW_END_LOCAL="01:00",
    )
    @patch("guests.tasks.build_iiko_olap_client_from_settings")
    @patch("guests.tasks.timezone.localtime")
    def test_sync_task_skips_outside_working_window(self, mocked_localtime, mocked_builder):
        """
        Вне рабочего окна sync-задача должна завершаться без запросов в OLAP.
        """
        mocked_localtime.return_value = timezone.make_aware(datetime(2026, 3, 19, 9, 0, 0))
        result = tasks.run_olap_sync_scheduled_task()
        self.assertEqual(result, 0)
        mocked_builder.assert_not_called()

    @override_settings(
        OLAP_SYNC_SCHEDULE_ENABLED=True,
        OLAP_SYNC_WINDOW_START_LOCAL="12:00",
        OLAP_SYNC_WINDOW_END_LOCAL="01:00",
        OLAP_SYNC_SCHEDULE_CLAIM_LIMIT=77,
        OLAP_SYNC_SCHEDULE_PORTION_SIZE=33,
        OLAP_SYNC_SCHEDULE_MAX_ATTEMPTS=4,
        OLAP_SYNC_SCHEDULE_RETRY_BASE_SECONDS=90,
        OLAP_SYNC_SCHEDULE_LOCK_TIMEOUT_SECONDS=600,
    )
    @patch("guests.tasks.OlapCheckSyncWorkerService")
    @patch("guests.tasks.build_iiko_olap_client_from_settings")
    @patch("guests.tasks.timezone.localtime")
    def test_sync_task_runs_iteration_inside_window(
        self,
        mocked_localtime,
        mocked_builder,
        mocked_service_cls,
    ):
        """
        В рабочем окне задача должна выполнить ровно один проход и закрыть клиент.
        """
        mocked_localtime.return_value = timezone.make_aware(datetime(2026, 3, 19, 12, 30, 0))

        mocked_client = MagicMock()
        mocked_builder.return_value = mocked_client
        mocked_service = MagicMock()
        mocked_service.run_iteration.return_value = SimpleNamespace(
            claimed_rows=5,
            loaded_rows=4,
            retry_rows=1,
            failed_rows=0,
            skipped_rows=0,
            raw_rows_created=4,
            raw_rows_duplicates=0,
            successful_portions=1,
            failed_portions=0,
        )
        mocked_service_cls.return_value = mocked_service

        result = tasks.run_olap_sync_scheduled_task()

        self.assertEqual(result, 5)
        mocked_service.run_iteration.assert_called_once()
        mocked_service_cls.assert_called_once_with(
            client=mocked_client,
            claim_limit=77,
            portion_size=33,
            max_attempts=4,
            retry_base_seconds=90,
            lock_timeout_seconds=600,
        )
        mocked_client.close.assert_called_once()

    @override_settings(OLAP_REBUILD_SCHEDULE_ENABLED=False)
    @patch("guests.tasks.call_command")
    def test_rebuild_task_returns_zero_when_disabled(self, mocked_call_command):
        """
        При выключенном флаге rebuild-задача не должна вызывать pipeline.
        """
        result = tasks.run_olap_rebuild_scheduled_task()
        self.assertEqual(result, 0)
        mocked_call_command.assert_not_called()

    @override_settings(
        OLAP_REBUILD_SCHEDULE_ENABLED=True,
        OLAP_REBUILD_SCHEDULE_CONTINUE_ON_STEP_ERROR=True,
        OLAP_REBUILD_SCHEDULE_BATCH_SIZE=1500,
        OLAP_REBUILD_SCHEDULE_WINDOW_DAYS="7,30,180",
        OLAP_REBUILD_SCHEDULE_USE_TODAY_AS_OF_DATE=True,
        OLAP_REBUILD_SCHEDULE_DEPARTMENT_ID="dept-42",
    )
    @patch("guests.tasks.timezone.localdate")
    @patch("guests.tasks.call_command")
    def test_rebuild_task_calls_pipeline_once(self, mocked_call_command, mocked_localdate):
        """
        Плановый rebuild должен запускать pipeline в one-shot режиме.
        """
        mocked_localdate.return_value = date(2026, 3, 19)

        result = tasks.run_olap_rebuild_scheduled_task()

        self.assertEqual(result, 1)
        mocked_call_command.assert_called_once_with(
            "run_olap_pipeline",
            once=True,
            skip_olap_sync=True,
            continue_on_step_error=True,
            batch_size=1500,
            department_id="dept-42",
            window_days=["7", "30", "180"],
            as_of_date="2026-03-19",
        )


class OlapDerivedScheduleTasksTests(SimpleTestCase):
    """
    Проверяет расписание инкрементальных витрин (order/daily/window).
    """

    @override_settings(OLAP_ORDER_FACT_SCHEDULE_ENABLED=False)
    @patch("guests.tasks.call_command")
    def test_order_fact_task_returns_zero_when_disabled(self, mocked_call_command):
        result = tasks.run_order_fact_scheduled_task()
        self.assertEqual(result, 0)
        mocked_call_command.assert_not_called()

    @override_settings(
        OLAP_ORDER_FACT_SCHEDULE_ENABLED=True,
        OLAP_ORDER_FACT_SCHEDULE_TAIL_DAYS=3,
        OLAP_ORDER_FACT_SCHEDULE_END_LAG_DAYS=1,
        OLAP_ORDER_FACT_SCHEDULE_BATCH_SIZE=1500,
    )
    @patch("guests.tasks.timezone.localdate")
    @patch("guests.tasks.call_command")
    def test_order_fact_task_calls_sync_once(self, mocked_call_command, mocked_localdate):
        mocked_localdate.return_value = date(2026, 3, 23)

        result = tasks.run_order_fact_scheduled_task()

        self.assertEqual(result, 1)
        mocked_call_command.assert_called_once_with(
            "sync_order_fact",
            once=True,
            business_date_from="2026-03-20",
            business_date_to="2026-03-22",
            batch_size=1500,
        )

    @override_settings(OLAP_DAILY_FACT_SCHEDULE_ENABLED=False)
    @patch("guests.tasks.call_command")
    def test_daily_fact_task_returns_zero_when_disabled(self, mocked_call_command):
        result = tasks.run_daily_fact_scheduled_task()
        self.assertEqual(result, 0)
        mocked_call_command.assert_not_called()

    @override_settings(
        OLAP_DAILY_FACT_SCHEDULE_ENABLED=True,
        OLAP_DAILY_FACT_SCHEDULE_TAIL_DAYS=2,
        OLAP_DAILY_FACT_SCHEDULE_END_LAG_DAYS=0,
        OLAP_DAILY_FACT_SCHEDULE_BATCH_SIZE=1200,
    )
    @patch("guests.tasks.timezone.localdate")
    @patch("guests.tasks.call_command")
    def test_daily_fact_task_calls_sync_once(self, mocked_call_command, mocked_localdate):
        mocked_localdate.return_value = date(2026, 3, 23)

        result = tasks.run_daily_fact_scheduled_task()

        self.assertEqual(result, 1)
        mocked_call_command.assert_called_once_with(
            "sync_daily_category_fact",
            once=True,
            business_date_from="2026-03-22",
            business_date_to="2026-03-23",
            batch_size=1200,
        )

    @override_settings(OLAP_WINDOW_METRICS_SCHEDULE_ENABLED=False)
    @patch("guests.tasks.call_command")
    def test_window_metrics_task_returns_zero_when_disabled(self, mocked_call_command):
        result = tasks.run_window_metrics_scheduled_task()
        self.assertEqual(result, 0)
        mocked_call_command.assert_not_called()

    @override_settings(
        OLAP_WINDOW_METRICS_SCHEDULE_ENABLED=True,
        OLAP_WINDOW_METRICS_SCHEDULE_AS_OF_LAG_DAYS=1,
        OLAP_WINDOW_METRICS_SCHEDULE_BATCH_SIZE=1300,
        OLAP_WINDOW_METRICS_SCHEDULE_WINDOW_DAYS="7,14,30",
        OLAP_WINDOW_METRICS_SCHEDULE_DEPARTMENT_ID="dept-77",
    )
    @patch("guests.tasks.timezone.localdate")
    @patch("guests.tasks.call_command")
    def test_window_metrics_task_calls_sync_once(self, mocked_call_command, mocked_localdate):
        mocked_localdate.return_value = date(2026, 3, 23)

        result = tasks.run_window_metrics_scheduled_task()

        self.assertEqual(result, 1)
        mocked_call_command.assert_called_once_with(
            "sync_window_metrics",
            once=True,
            as_of_date="2026-03-22",
            window_days=["7", "14", "30"],
            batch_size=1300,
            department_id="dept-77",
        )
