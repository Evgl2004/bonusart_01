from __future__ import annotations

import io
import json
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from guests.models import (
    OlapCheckSyncJournal,
    OlapSalesRawLine,
    OrderFact,
    TerminalDepartmentMap,
)


class DiagnoseOlapDataFlowCommandTests(TestCase):
    def test_command_builds_end_to_end_report_and_detects_journal_date_mismatch(self):
        dept_id = "c9a0df27-11dc-4bee-83a3-f0a5aa16c185"
        terminal_id = "8809b3f7-445e-41a0-b80c-c36dc07ad5fb"

        TerminalDepartmentMap.objects.create(
            terminal_group_id=terminal_id,
            department_id=dept_id,
            department_name="Сами Сусами",
            is_active=True,
        )

        j1 = OlapCheckSyncJournal.objects.create(
            idempotency_key="diag-k1",
            status=OlapCheckSyncJournal.Status.LOADED,
            source_webhook_id="16094",
            order_number=39,
            business_date=date(2026, 1, 1),
            event_at=timezone.now(),
            department_id=dept_id,
            terminal_group_id=terminal_id,
            loaded_at=timezone.now(),
        )
        j2 = OlapCheckSyncJournal.objects.create(
            idempotency_key="diag-k2",
            status=OlapCheckSyncJournal.Status.LOADED,
            source_webhook_id="16601",
            order_number=44,
            business_date=date(2026, 1, 2),
            event_at=timezone.now(),
            department_id=dept_id,
            terminal_group_id=terminal_id,
            loaded_at=timezone.now(),
        )

        OlapSalesRawLine.objects.create(
            row_fingerprint="diag-fp-1",
            sync_journal=j1,
            business_date=date(2026, 1, 1),
            department_id=dept_id,
            department_name="Сами Сусами",
            order_number=39,
            uniq_order_id="u39",
            dish_sum_before_discount=Decimal("3705.00"),
            dish_sum_after_discount=Decimal("3705.00"),
        )
        # Важный кейс: raw на 01.01, но journal.business_date = 02.01
        OlapSalesRawLine.objects.create(
            row_fingerprint="diag-fp-2",
            sync_journal=j2,
            business_date=date(2026, 1, 1),
            department_id=dept_id,
            department_name="Сами Сусами",
            order_number=44,
            uniq_order_id="u44",
            dish_sum_before_discount=Decimal("1485.00"),
            dish_sum_after_discount=Decimal("1485.00"),
        )

        OrderFact.objects.create(
            business_date=date(2026, 1, 1),
            department_id=dept_id,
            department_name="Сами Сусами",
            order_number=39,
            uniq_order_id="u39",
            gross_sum=Decimal("3705.00"),
            net_sum=Decimal("3705.00"),
            discount_sum=Decimal("0.00"),
            bonus_sum=Decimal("0.00"),
            items_count=15,
            categories_count=7,
        )
        OrderFact.objects.create(
            business_date=date(2026, 1, 1),
            department_id=dept_id,
            department_name="Сами Сусами",
            order_number=44,
            uniq_order_id="u44",
            gross_sum=Decimal("1485.00"),
            net_sum=Decimal("1485.00"),
            discount_sum=Decimal("0.00"),
            bonus_sum=Decimal("0.00"),
            items_count=2,
            categories_count=1,
        )

        output = io.StringIO()
        call_command(
            "diagnose_olap_data_flow",
            "--date-from=2026-01-01",
            "--date-to=2026-01-01",
            "--department-name=Сами Сусами",
            "--output-format=json",
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(payload["counts"]["journal_rows"], 1)
        self.assertEqual(payload["counts"]["raw_rows"], 2)
        self.assertEqual(payload["counts"]["order_fact_rows"], 2)
        self.assertEqual(payload["counts"]["journal_rows_linked_from_raw"], 2)
        self.assertEqual(payload["counts"]["journal_rows_without_raw"], 0)
        self.assertEqual(payload["counts"]["journal_rows_linked_from_raw_outside_scope"], 1)
        self.assertEqual(payload["counts"]["raw_rows_linked_to_journal_outside_scope"], 1)

        self.assertEqual(Decimal(payload["sums"]["raw_net"]), Decimal("5190.00"))
        self.assertEqual(Decimal(payload["sums"]["order_fact_net"]), Decimal("5190.00"))

        self.assertTrue(payload["quality_checks"]["raw_vs_order_fact_keys_equal"])
        self.assertEqual(payload["quality_checks"]["raw_rows_with_journal_business_date_mismatch_count"], 1)

    def test_command_requires_restaurant_filter(self):
        with self.assertRaises(CommandError):
            call_command(
                "diagnose_olap_data_flow",
                "--date-from=2026-01-01",
                "--date-to=2026-01-01",
                stdout=io.StringIO(),
            )
