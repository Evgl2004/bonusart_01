"""
Тесты сервиса дозагрузки чеков из журнала OLAP (S5).
"""

from __future__ import annotations

from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from guests.models import Guest, OlapCheckSyncJournal, OlapSalesRawLine
from guests.services.iiko_olap_client import IikoOlapRequestError, OlapPortionLoadStats
from guests.services.olap_check_sync import OlapCheckSyncWorkerService, OlapSyncIterationStats


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
                    "DishDiscountSumInt": 450,
                    "DeletedWithWriteoff": "NOT_DELETED",
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
        self.assertEqual(raw_line.dish_sum_before_discount, 490)
        self.assertEqual(raw_line.dish_sum_after_discount, 450)
        self.assertEqual(raw_line.discount_sum, 40)
        self.assertEqual(raw_line.dish_category_name, "Бизнес-ланч")

        self.assertEqual(stats.claimed_rows, 1)
        self.assertEqual(stats.loaded_rows, 1)
        self.assertEqual(stats.raw_rows_created, 1)

    def test_bulk_create_raw_lines_treats_existing_fingerprint_as_duplicate(self):
        """
        Параллельно вставленный fingerprint не должен ронять OLAP-проход.
        """
        row = self._create_journal_row(
            key="raw-race-1",
            order_number=698699,
            business_day=date(2026, 3, 18),
        )
        OlapSalesRawLine.objects.create(
            row_fingerprint="race-fingerprint",
            sync_journal=row,
            guest=self.guest,
            business_date=date(2026, 3, 18),
            department_id="dept-1",
            order_number=698699,
            uniq_order_id="race-order",
            item_sale_event_id="race-item",
            dish_code="race-dish",
            dish_name="Race dish",
            raw_payload={},
        )
        duplicate = OlapSalesRawLine(
            row_fingerprint="race-fingerprint",
            sync_journal=row,
            guest=self.guest,
            business_date=date(2026, 3, 18),
            department_id="dept-1",
            order_number=698699,
            uniq_order_id="race-order",
            item_sale_event_id="race-item",
            dish_code="race-dish",
            dish_name="Race dish",
            raw_payload={},
        )
        stats = OlapSyncIterationStats()
        service = OlapCheckSyncWorkerService(
            client=_FakeOlapClient(),
            claim_limit=20,
            portion_size=10,
            max_attempts=3,
            retry_base_seconds=1,
        )

        service._bulk_create_raw_lines(raw_lines_to_create=[duplicate], stats=stats)

        self.assertEqual(OlapSalesRawLine.objects.filter(row_fingerprint="race-fingerprint").count(), 1)
        self.assertEqual(stats.raw_rows_created, 0)
        self.assertEqual(stats.raw_rows_duplicates, 1)

    def test_run_iteration_skips_deleted_rows_and_sets_retry_when_no_active_lines(self):
        """
        Если по чеку пришли только удалённые строки OLAP (DeletedWithWriteoff!=NOT_DELETED),
        raw не пишется, а задача переводится в retry/skipped по общим правилам ретрая.
        """
        row = self._create_journal_row(
            key="deleted-only-1",
            order_number=798001,
            business_day=date(2026, 3, 18),
        )
        fake_client = _FakeOlapClient(
            rows=[
                {
                    "OpenDate.Typed": "2026-03-18",
                    "OrderNum": 798001,
                    "Department.Id": "dept-1",
                    "UniqOrderId.Id": "order-uuid-deleted-only",
                    "ItemSaleEvent.Id": "event-deleted-only",
                    "DishCode": "deleted-1",
                    "DishName": "Deleted line",
                    "DishSumInt": 100,
                    "DishDiscountSumInt": 0,
                    "DeletedWithWriteoff": "DELETED_WITHOUT_WRITEOFF",
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
        self.assertEqual(row.status, OlapCheckSyncJournal.Status.RETRY)
        self.assertEqual(row.attempt_count, 1)
        self.assertIn("DeletedWithWriteoff", row.last_error or "")
        self.assertEqual(OlapSalesRawLine.objects.filter(sync_journal=row).count(), 0)
        self.assertEqual(stats.loaded_rows, 0)
        self.assertEqual(stats.retry_rows, 1)

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

    def test_run_iteration_uses_only_exact_business_day_for_repeated_order_number(self):
        """
        If OLAP returns the same `OrderNum` for nearby days, worker must load only
        rows for the journal `business_date`.
        """
        row = self._create_journal_row(
            key="repeat-order-exact-day-1",
            order_number=500,
            business_day=date(2025, 12, 23),
        )
        fake_client = _FakeOlapClient(
            rows=[
                {
                    "OpenDate.Typed": "2025-12-22",
                    "OrderNum": 500,
                    "Department.Id": "dept-1",
                    "UniqOrderId.Id": "order-500-22",
                    "ItemSaleEvent.Id": "event-22",
                    "DishCode": "dish-22",
                    "DishName": "Dish 22",
                    "DishSumInt": 220,
                },
                {
                    "OpenDate.Typed": "2025-12-23",
                    "OrderNum": 500,
                    "Department.Id": "dept-1",
                    "UniqOrderId.Id": "order-500-23",
                    "ItemSaleEvent.Id": "event-23",
                    "DishCode": "dish-23",
                    "DishName": "Dish 23",
                    "DishSumInt": 230,
                },
                {
                    "OpenDate.Typed": "2025-12-24",
                    "OrderNum": 500,
                    "Department.Id": "dept-1",
                    "UniqOrderId.Id": "order-500-24",
                    "ItemSaleEvent.Id": "event-24",
                    "DishCode": "dish-24",
                    "DishName": "Dish 24",
                    "DishSumInt": 240,
                },
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
        self.assertEqual(stats.loaded_rows, 1)
        lines = list(OlapSalesRawLine.objects.filter(sync_journal=row).order_by("id"))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].business_date, date(2025, 12, 23))
        self.assertEqual(lines[0].dish_code, "dish-23")

    def test_run_iteration_marks_retry_when_repeated_order_dates_are_ambiguous(self):
        """
        If exact business day is absent and nearest OLAP dates are tied, row must
        be marked as retry with explicit ambiguous-dates reason.
        """
        row = self._create_journal_row(
            key="repeat-order-ambiguous-1",
            order_number=700,
            business_day=date(2025, 12, 23),
        )
        fake_client = _FakeOlapClient(
            rows=[
                {
                    "OpenDate.Typed": "2025-12-22",
                    "OrderNum": 700,
                    "Department.Id": "dept-1",
                    "UniqOrderId.Id": "order-700-22",
                    "ItemSaleEvent.Id": "event-700-22",
                    "DishCode": "dish-a",
                    "DishName": "Dish A",
                    "DishSumInt": 100,
                },
                {
                    "OpenDate.Typed": "2025-12-24",
                    "OrderNum": 700,
                    "Department.Id": "dept-1",
                    "UniqOrderId.Id": "order-700-24",
                    "ItemSaleEvent.Id": "event-700-24",
                    "DishCode": "dish-b",
                    "DishName": "Dish B",
                    "DishSumInt": 100,
                },
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

        self.assertEqual(row.status, OlapCheckSyncJournal.Status.RETRY)
        self.assertEqual(row.attempt_count, 1)
        self.assertIn("несколько дат", row.last_error or "")
        self.assertEqual(stats.retry_rows, 1)
        self.assertEqual(OlapSalesRawLine.objects.filter(sync_journal=row).count(), 0)

    def test_run_iteration_updates_journal_business_day_to_nearest_olap_day(self):
        """
        If exact business day is absent but nearest OLAP day is unique, worker
        should load that day and update journal business_date to OLAP day.
        """
        row = self._create_journal_row(
            key="repeat-order-nearest-day-1",
            order_number=900,
            business_day=date(2025, 12, 23),
        )
        fake_client = _FakeOlapClient(
            rows=[
                {
                    "OpenDate.Typed": "2025-12-24",
                    "OrderNum": 900,
                    "Department.Id": "dept-1",
                    "UniqOrderId.Id": "order-900-24",
                    "ItemSaleEvent.Id": "event-900-24",
                    "DishCode": "dish-nearest",
                    "DishName": "Dish nearest",
                    "DishSumInt": 300,
                },
                {
                    "OpenDate.Typed": "2025-12-25",
                    "OrderNum": 900,
                    "Department.Id": "dept-1",
                    "UniqOrderId.Id": "order-900-25",
                    "ItemSaleEvent.Id": "event-900-25",
                    "DishCode": "dish-far",
                    "DishName": "Dish far",
                    "DishSumInt": 500,
                },
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
        self.assertEqual(row.business_date, date(2025, 12, 24))
        self.assertEqual(stats.loaded_rows, 1)
        lines = list(OlapSalesRawLine.objects.filter(sync_journal=row).order_by("id"))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].business_date, date(2025, 12, 24))
        self.assertEqual(lines[0].dish_code, "dish-nearest")
