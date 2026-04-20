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
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    Guest,
    GuestRestaurantDailyCategoryFact,
    OlapCategoryDict,
    OlapCheckSyncJournal,
    OlapNomenclatureDict,
    OlapSalesRawLine,
    TerminalDepartmentMap,
)


class AuditFocusDailyFactCommandTests(TestCase):
    def test_command_detects_missing_stale_and_mismatch_rows(self):
        dept_a = "a90230ee-9035-4916-8b93-e69ef29e4f48"
        dept_b = "c9a0df27-11dc-4bee-83a3-f0a5aa16c185"

        TerminalDepartmentMap.objects.create(
            terminal_group_id="tg-a",
            department_id=dept_a,
            department_name="Чина",
            is_active=True,
        )
        TerminalDepartmentMap.objects.create(
            terminal_group_id="tg-b",
            department_id=dept_b,
            department_name="Сами Сусами",
            is_active=True,
        )

        guest_1 = Guest.objects.create(phone="+79000000001")
        guest_2 = Guest.objects.create(phone="+79000000002")
        guest_3 = Guest.objects.create(phone="+79000000003")

        soup_category = OlapCategoryDict.objects.create(
            iiko_category_external_id="soup-cat-ext-id",
            category_name="Супы",
            is_active=True,
        )
        soup_nomenclature = OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id="dish-soup-001",
            nomenclature_name="Суп Далянь",
            olap_category=soup_category,
            is_active=True,
        )

        focus = FocusCategory.objects.create(
            code="soups_china",
            name="Супы Чина",
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=soup_category,
            is_enabled=True,
        )
        FocusCategoryNomenclatureResolved.objects.create(
            focus_category=focus,
            nomenclature=soup_nomenclature,
            source_reason=FocusCategoryNomenclatureResolved.SourceReason.DIRECT_OLAP,
        )

        journal_1 = OlapCheckSyncJournal.objects.create(
            idempotency_key="audit-daily-k1",
            status=OlapCheckSyncJournal.Status.LOADED,
            business_date=date(2026, 4, 19),
            department_id=dept_a,
            loaded_at=timezone.now(),
        )
        journal_2 = OlapCheckSyncJournal.objects.create(
            idempotency_key="audit-daily-k2",
            status=OlapCheckSyncJournal.Status.LOADED,
            business_date=date(2026, 4, 20),
            department_id=dept_b,
            loaded_at=timezone.now(),
        )

        # Ожидаемый ключ №1 (будет в daily, но с расхождением по orders_count).
        OlapSalesRawLine.objects.create(
            row_fingerprint="audit-daily-fp-1",
            sync_journal=journal_1,
            guest=guest_1,
            business_date=date(2026, 4, 19),
            department_id=dept_a,
            department_name="Чина",
            order_number=10,
            dish_code="dish-soup-001",
            dish_name="Суп Далянь",
            dish_sum_before_discount=Decimal("100.00"),
            dish_sum_after_discount=Decimal("100.00"),
            bonus_sum=Decimal("0"),
        )
        OlapSalesRawLine.objects.create(
            row_fingerprint="audit-daily-fp-2",
            sync_journal=journal_1,
            guest=guest_1,
            business_date=date(2026, 4, 19),
            department_id=dept_a,
            department_name="Чина",
            order_number=10,
            dish_code="dish-soup-001",
            dish_name="Суп Далянь",
            dish_sum_before_discount=Decimal("50.00"),
            dish_sum_after_discount=Decimal("50.00"),
            bonus_sum=Decimal("0"),
        )

        # Ожидаемый ключ №2 (в daily отсутствует => missing).
        # Для net проверяем fallback: dish_sum_after_discount=0 => берётся gross.
        OlapSalesRawLine.objects.create(
            row_fingerprint="audit-daily-fp-3",
            sync_journal=journal_2,
            guest=guest_3,
            business_date=date(2026, 4, 20),
            department_id=dept_b,
            department_name="Сами Сусами",
            order_number=77,
            dish_code="dish-soup-001",
            dish_name="Суп Далянь",
            dish_sum_before_discount=Decimal("200.00"),
            dish_sum_after_discount=Decimal("0.00"),
            bonus_sum=Decimal("10.00"),
        )

        # Ключ совпадает с expected, но метрика orders_count отличается => mismatch.
        GuestRestaurantDailyCategoryFact.objects.create(
            business_date=date(2026, 4, 19),
            guest=guest_1,
            department_id=dept_a,
            focus_category=focus,
            orders_count=2,
            items_count=2,
            sum_gross=Decimal("150.00"),
            sum_net=Decimal("150.00"),
            bonus_sum=Decimal("0.00"),
        )
        # Лишний ключ, которого нет в expected => stale.
        GuestRestaurantDailyCategoryFact.objects.create(
            business_date=date(2026, 4, 18),
            guest=guest_2,
            department_id=dept_b,
            focus_category=focus,
            orders_count=1,
            items_count=1,
            sum_gross=Decimal("100.00"),
            sum_net=Decimal("100.00"),
            bonus_sum=Decimal("0.00"),
        )

        output = io.StringIO()
        call_command(
            "audit_focus_daily_fact",
            "--focus-code=soups_china",
            "--as-of-date=2026-04-20",
            "--window-days=3",
            "--output-format=json",
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(payload["counts"]["expected_daily_rows"], 2)
        self.assertEqual(payload["counts"]["actual_daily_rows"], 2)
        self.assertEqual(payload["counts"]["missing_daily_rows"], 1)
        self.assertEqual(payload["counts"]["stale_daily_rows"], 1)
        self.assertEqual(payload["counts"]["mismatch_daily_rows"], 1)

        self.assertEqual(payload["totals"]["expected_sum_net"], "350.00")
        self.assertEqual(payload["totals"]["actual_sum_net"], "250.00")

    def test_command_requires_focus_identifier(self):
        with self.assertRaises(CommandError):
            call_command(
                "audit_focus_daily_fact",
                "--as-of-date=2026-04-20",
                "--window-days=180",
                stdout=io.StringIO(),
            )

