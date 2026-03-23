"""
Тесты контрольной дозагрузки OLAP (прямой срез -> journal).
"""

from __future__ import annotations

from datetime import date

from django.test import TestCase

from guests.models import OlapCheckSyncJournal, TerminalDepartmentMap
from guests.services.olap_control_pull import (
    OlapControlPullOptions,
    OlapControlPullService,
)


class _FakeOlapClient:
    def __init__(self, rows_by_department: dict[str, list[dict]]):
        self.rows_by_department = rows_by_department

    def build_sales_payload_for_department_window(self, **kwargs):
        return kwargs

    def query_olap(self, payload):
        department_ids = payload.get("department_ids") or []
        department_id = department_ids[0] if department_ids else ""
        return {"data": list(self.rows_by_department.get(department_id, [])), "summary": []}


class OlapControlPullServiceTests(TestCase):
    def setUp(self):
        super().setUp()
        TerminalDepartmentMap.objects.create(
            terminal_group_id="terminal-1",
            department_id="dept-1",
            department_code="D1",
            is_active=True,
        )

    def test_run_cycle_dry_run_counts_new_rows_without_writing(self):
        client = _FakeOlapClient(
            rows_by_department={
                "dept-1": [
                    {
                        "OpenDate.Typed": "2026-01-01",
                        "OrderNum": 1001,
                        "UniqOrderId.Id": "u-1001",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                    },
                    {
                        "OpenDate.Typed": "2026-01-01",
                        "OrderNum": 1002,
                        "UniqOrderId.Id": "u-1002",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                    },
                ]
            }
        )
        service = OlapControlPullService(client=client)

        stats = service.run_cycle(
            options=OlapControlPullOptions(
                business_date_from=date(2026, 1, 1),
                business_date_to=date(2026, 1, 1),
                dry_run=True,
            )
        )

        self.assertEqual(stats.departments_scanned, 1)
        self.assertEqual(stats.olap_rows_seen, 2)
        self.assertEqual(stats.distinct_order_keys_seen, 2)
        self.assertEqual(stats.would_create_journal_rows, 2)
        self.assertEqual(stats.created_journal_rows, 0)
        self.assertEqual(OlapCheckSyncJournal.objects.count(), 0)

    def test_run_cycle_write_is_idempotent_for_same_rows(self):
        client = _FakeOlapClient(
            rows_by_department={
                "dept-1": [
                    {
                        "OpenDate.Typed": "2026-01-02",
                        "OrderNum": 2001,
                        "UniqOrderId.Id": "u-2001",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                    },
                    {
                        "OpenDate.Typed": "2026-01-02",
                        "OrderNum": 2002,
                        "UniqOrderId.Id": "u-2002",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                    },
                ]
            }
        )
        service = OlapControlPullService(client=client)

        first_stats = service.run_cycle(
            options=OlapControlPullOptions(
                business_date_from=date(2026, 1, 2),
                business_date_to=date(2026, 1, 2),
                dry_run=False,
            )
        )
        second_stats = service.run_cycle(
            options=OlapControlPullOptions(
                business_date_from=date(2026, 1, 2),
                business_date_to=date(2026, 1, 2),
                dry_run=False,
            )
        )

        self.assertEqual(first_stats.created_journal_rows, 2)
        self.assertEqual(second_stats.created_journal_rows, 0)
        self.assertEqual(second_stats.duplicate_journal_rows, 2)
        self.assertEqual(OlapCheckSyncJournal.objects.count(), 2)
        self.assertEqual(
            OlapCheckSyncJournal.objects.filter(source_webhook_id="control_pull").count(),
            2,
        )
