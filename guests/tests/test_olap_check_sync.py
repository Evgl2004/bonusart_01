"""
Тесты сервиса дозагрузки чеков из журнала OLAP (S5).
"""

from __future__ import annotations

from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from guests.models import Guest, OlapCheckSyncJournal, OlapSalesRawLine
from guests.services.iiko_olap_client import IikoOlapRequestError, OlapPortionLoadStats
from guests.services.olap_check_sync import OlapCheckSyncWorkerService


class _FakeOlapClient:
    def __init__(
        self,
        *,
        rows=None,
        failed_order_number_portions=None,
        raise_error: Exception | None = None,
        resolver=None,
    ):
        self.rows = list(rows or [])
        self.failed_order_number_portions = list(failed_order_number_portions or [])
        self.raise_error = raise_error
        self.resolver = resolver
        self.calls = []

    def fetch_sales_in_portions(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_error is not None:
            raise self.raise_error
        if self.resolver is not None:
            rows, failed_portions = self.resolver(kwargs)
            stats = OlapPortionLoadStats(
                requested_portions=1,
                successful_portions=0 if failed_portions else 1,
                failed_portions=1 if failed_portions else 0,
                total_data_rows=len(rows),
                total_summary_rows=0,
                failed_order_number_portions=[list(part) for part in failed_portions],
            )
            return list(rows), [], stats
        stats = OlapPortionLoadStats(
            requested_portions=1,
            successful_portions=0 if self.failed_order_number_portions else 1,
            failed_portions=1 if self.failed_order_number_portions else 0,
            total_data_rows=len(self.rows),
            total_summary_rows=0,
            failed_order_number_portions=[list(part) for part in self.failed_order_number_portions],
        )
        return list(self.rows), [], stats


class OlapCheckSyncWorkerServiceTests(TestCase):
    """
    Проверки бизнес-логики воркера дозагрузки OLAP.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest = Guest.objects.create(
            first_name="Тест",
            phone="+79990001122",
            created_at=now,
            updated_at=now,
        )

    def _create_journal_row(
        self,
        *,
        key: str,
        order_number: int | None,
        business_day: date | None,
        department_id: str | None = "dept-1",
        status: str = OlapCheckSyncJournal.Status.NEW,
        attempt_count: int = 0,
        next_try_at=None,
    ) -> OlapCheckSyncJournal:
        return OlapCheckSyncJournal.objects.create(
            idempotency_key=key,
            status=status,
            guest=self.guest,
            order_number=order_number,
            business_date=business_day,
            department_id=department_id,
            attempt_count=attempt_count,
            next_try_at=next_try_at,
        )

    def test_run_iteration_marks_row_loaded_and_writes_raw_line(self):
        """
        При успешном ответе OLAP строка журнала должна перейти в loaded,
        а сырой слой должен получить запись.
        """
        row = self._create_journal_row(
            key="ok-1",
            order_number=698698,
            business_day=date(2026, 3, 18),
        )
        fake_client = _FakeOlapClient(
            rows=[
                {
                    "OpenDate.Typed": "2026-03-18",
                    "OrderNum": 698698,
                    "Department.Id": "dept-1",
                    "Department.Code": "D01",
                    "Department": "Грузин",
                    "UniqOrderId.Id": "order-uuid-1",
                    "ItemSaleEvent.Id": "event-1",
                    "DishCode": "54768",
                    "DishName": "Обед 490р",
                    "DishCategory.Id": "cat-1",
                    "DishCategory": "Бизнес-ланч",
                    "DishGroup.Id": "grp-1",
                    "DishGroup": "Комплексы",
                    "DishSumInt": 490,
                    "CouponInfo.Series": None,
                    "CouponInfo.Number": None,
                }
            ]
        )

        service = OlapCheckSyncWorkerService(
            client=fake_client,
            claim_limit=20,
            portion_size=10,
            max_attempts=3,
            retry_base_seconds=1,
        )
        stats = service.run_iteration()

        row.refresh_from_db()
        self.assertEqual(row.status, OlapCheckSyncJournal.Status.LOADED)
        self.assertIsNotNone(row.loaded_at)

        raw_line = OlapSalesRawLine.objects.get(sync_journal=row)
        self.assertEqual(raw_line.order_number, 698698)
        self.assertEqual(raw_line.dish_code, "54768")
        self.assertEqual(raw_line.dish_category_name, "Бизнес-ланч")

        self.assertEqual(stats.claimed_rows, 1)
        self.assertEqual(stats.loaded_rows, 1)
        self.assertEqual(stats.raw_rows_created, 1)

    def test_run_iteration_uses_strict_plus_minus_one_window_for_date_shifted_order(self):
        """
        Воркер должен сразу запрашивать OLAP в окне business_date±1 день
        и использовать обязательный фильтр Department.Id.
        """
        row = self._create_journal_row(
            key="fallback-1",
            order_number=94038,
            business_day=date(2025, 12, 13),
        )

        def _resolver(kwargs):
            date_from = kwargs["date_from"]
            date_to = kwargs["date_to"]
            self.assertEqual(str(date_from), "2025-12-12")
            self.assertEqual(str(date_to), "2025-12-14")
            self.assertEqual(kwargs["department_ids"], ["dept-1"])
            return [
                {
                    "OpenDate.Typed": "2025-12-12",
                    "OrderNum": 94038,
                    "Department.Id": "dept-1",
                    "Department.Code": "D01",
                    "Department": "Тестовый департамент",
                    "UniqOrderId.Id": "order-uuid-fallback",
                    "ItemSaleEvent.Id": "event-fallback-1",
                    "DishCode": "1001",
                    "DishName": "Шашлык",
                    "DishCategory.Id": "cat-meat",
                    "DishCategory": "Мясо",
                    "DishGroup.Id": "grp-meat",
                    "DishGroup": "Гриль",
                    "DishSumInt": 890,
                }
            ], []

        fake_client = _FakeOlapClient(resolver=_resolver)
        service = OlapCheckSyncWorkerService(
            client=fake_client,
            claim_limit=20,
            portion_size=10,
            max_attempts=3,
            retry_base_seconds=1,
        )

        stats = service.run_iteration()
        row.refresh_from_db()

        self.assertEqual(row.status, OlapCheckSyncJournal.Status.LOADED)
        self.assertEqual(stats.loaded_rows, 1)
        self.assertEqual(stats.retry_rows, 0)
        self.assertEqual(len(fake_client.calls), 1)

    def test_run_iteration_requires_department_id_filter(self):
        """
        Если в журнале отсутствует Department.Id, OLAP-запрос не выполняется,
        а задача уходит в retry с понятной ошибкой.
        """
        row = self._create_journal_row(
            key="no-dept-1",
            order_number=710001,
            business_day=date(2026, 3, 18),
            department_id=None,
        )
        fake_client = _FakeOlapClient(rows=[])
        service = OlapCheckSyncWorkerService(
            client=fake_client,
            claim_limit=20,
            portion_size=10,
            max_attempts=3,
            retry_base_seconds=1,
        )

        stats = service.run_iteration()
        row.refresh_from_db()

        self.assertEqual(row.status, OlapCheckSyncJournal.Status.RETRY)
        self.assertEqual(row.attempt_count, 1)
        self.assertIn("Department.Id", row.last_error or "")
        self.assertEqual(len(fake_client.calls), 0)
        self.assertEqual(stats.retry_rows, 1)

    def test_run_iteration_moves_not_found_to_retry_then_skipped(self):
        """
        Если OLAP не вернул строки по чеку:
        1. сначала ставим retry;
        2. при достижении max_attempts переводим в skipped.
        """
        row = self._create_journal_row(
            key="not-found-1",
            order_number=700001,
            business_day=date(2026, 3, 18),
        )
        fake_client = _FakeOlapClient(rows=[])
        service = OlapCheckSyncWorkerService(
            client=fake_client,
            claim_limit=20,
            portion_size=10,
            max_attempts=2,
            retry_base_seconds=1,
        )

        service.run_iteration()
        row.refresh_from_db()
        self.assertEqual(row.status, OlapCheckSyncJournal.Status.RETRY)
        self.assertEqual(row.attempt_count, 1)
        self.assertIsNotNone(row.next_try_at)

        row.next_try_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["next_try_at", "updated_at"])

        service.run_iteration()
        row.refresh_from_db()
        self.assertEqual(row.status, OlapCheckSyncJournal.Status.SKIPPED)
        self.assertEqual(row.attempt_count, 2)
        self.assertIsNone(row.next_try_at)

    def test_run_iteration_marks_invalid_rows_as_failed(self):
        """
        Невалидная запись журнала (без order_number) должна завершаться статусом failed.
        """
        row = self._create_journal_row(
            key="invalid-1",
            order_number=None,
            business_day=date(2026, 3, 18),
        )
        fake_client = _FakeOlapClient(rows=[])
        service = OlapCheckSyncWorkerService(client=fake_client, claim_limit=20, max_attempts=3)

        stats = service.run_iteration()
        row.refresh_from_db()

        self.assertEqual(row.status, OlapCheckSyncJournal.Status.FAILED)
        self.assertEqual(row.attempt_count, 1)
        self.assertEqual(stats.failed_rows, 1)
        self.assertEqual(len(fake_client.calls), 0)

    def test_run_iteration_marks_row_failed_after_olap_error_when_max_attempts_one(self):
        """
        При ошибке OLAP и max_attempts=1 запись должна сразу перейти в failed.
        """
        row = self._create_journal_row(
            key="olap-error-1",
            order_number=777001,
            business_day=date(2026, 3, 18),
        )
        fake_client = _FakeOlapClient(raise_error=IikoOlapRequestError("olap unavailable"))
        service = OlapCheckSyncWorkerService(
            client=fake_client,
            claim_limit=20,
            max_attempts=1,
            retry_base_seconds=1,
        )

        stats = service.run_iteration()
        row.refresh_from_db()

        self.assertEqual(row.status, OlapCheckSyncJournal.Status.FAILED)
        self.assertEqual(row.attempt_count, 1)
        self.assertEqual(stats.failed_rows, 1)
