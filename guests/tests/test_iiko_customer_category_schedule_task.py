from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from guests import tasks


class IikoCustomerCategoryScheduleTaskTests(SimpleTestCase):
    """
    Проверяет плановую задачу доставки событий категорий гостей SAGUR -> iikoCard.
    """

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=False,
        IIKO_CUSTOMER_CATEGORY_SYNC_SCHEDULE_ENABLED=True,
    )
    @patch("guests.tasks.IikoCustomerCategorySyncService")
    def test_category_sync_task_returns_zero_when_globally_disabled(self, mocked_service_cls):
        result = tasks.run_iiko_customer_category_sync_queue_task()
        self.assertEqual(result, 0)
        mocked_service_cls.from_settings.assert_not_called()

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_CUSTOMER_CATEGORY_SYNC_SCHEDULE_ENABLED=False,
    )
    @patch("guests.tasks.IikoCustomerCategorySyncService")
    def test_category_sync_task_returns_zero_when_schedule_disabled(self, mocked_service_cls):
        result = tasks.run_iiko_customer_category_sync_queue_task()
        self.assertEqual(result, 0)
        mocked_service_cls.from_settings.assert_not_called()

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_CUSTOMER_CATEGORY_SYNC_SCHEDULE_ENABLED=True,
        IIKO_CUSTOMER_CATEGORY_SYNC_BATCH_SIZE=77,
    )
    @patch("guests.tasks.IikoCustomerCategorySyncService")
    def test_category_sync_task_processes_batch(self, mocked_service_cls):
        mocked_stats = MagicMock()
        mocked_stats.to_dict.return_value = {
            "scanned": 5,
            "processed": 4,
            "acked": 3,
            "failed": 1,
            "skipped": 1,
            "skipped_max_attempts": 0,
            "add_acked": 2,
            "remove_acked": 1,
        }
        mocked_service = MagicMock()
        mocked_service.process_batch.return_value = mocked_stats
        mocked_service_cls.from_settings.return_value = mocked_service

        result = tasks.run_iiko_customer_category_sync_queue_task()

        self.assertEqual(result, 4)
        mocked_service_cls.from_settings.assert_called_once_with()
        mocked_service.process_batch.assert_called_once_with(limit=77)

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_CUSTOMER_CATEGORY_SYNC_SCHEDULE_ENABLED=True,
    )
    @patch("guests.tasks.IikoCustomerCategorySyncService")
    def test_category_sync_task_returns_zero_on_error(self, mocked_service_cls):
        mocked_service_cls.from_settings.side_effect = RuntimeError("sync failed")

        result = tasks.run_iiko_customer_category_sync_queue_task()

        self.assertEqual(result, 0)
