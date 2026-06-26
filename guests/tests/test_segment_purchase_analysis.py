from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from guests.models import (
    Guest,
    GuestRestaurantDailyOrderFact,
    GuestRestaurantWindowMetrics,
    OlapCategoryDict,
    OlapCheckSyncJournal,
    OlapNomenclatureDict,
    OlapSalesRawLine,
    OrderFact,
)
from guests.services.guest_workbench import (
    BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE,
    NEW_IN_VENUE_SEGMENT_CODE,
)
from guests.services.segment_purchase_analysis import DEFAULT_SEGMENT_CODE


class SegmentPurchaseAnalysisTests(TestCase):
    def setUp(self):
        self.as_of_date = date(2026, 3, 23)
        self.department_id = "dep-1"
        self.other_department_id = "dep-2"
        self.journal = OlapCheckSyncJournal.objects.create(
            idempotency_key="segment-purchase-analysis",
            status=OlapCheckSyncJournal.Status.LOADED,
            business_date=self.as_of_date,
            department_id=self.department_id,
        )
        self.category = OlapCategoryDict.objects.create(
            iiko_category_external_id="cat-food",
            category_name="Еда",
            is_active=True,
        )
        self._create_nomenclature("dish-burger", "Бургер из справочника", "Основное")
        self._create_nomenclature("dish-tea", "Чай из справочника", "Напитки")
        self._create_department_option(self.department_id, "Заведение 1")
        self._create_department_option(self.other_department_id, "Заведение 2")

    def test_page_defaults_require_department_and_exclude_bot_segment(self):
        response = self.client.get(reverse("segment_purchase_analysis"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выберите заведение")

        payload = response.context["payload"]
        self.assertEqual(payload["filters"]["department_id"], "")
        self.assertEqual(payload["filters"]["segment_code"], DEFAULT_SEGMENT_CODE)
        self.assertEqual(payload["filters"]["period_days"], 60)
        self.assertEqual(payload["filters"]["top_limit"], 15)
        self.assertTrue(payload["filters"]["hide_zero_revenue"])
        self.assertEqual(payload["rows"], [])

        segment_codes = {item["code"] for item in payload["filters"]["segment_options"]}
        self.assertNotIn(BOT_ACTIVE_NO_VISITS_180D_SEGMENT_CODE, segment_codes)

    def test_cooling_segment_aggregates_raw_lines_for_selected_department(self):
        cooling_guest = self._create_guest("001")
        active_guest = self._create_guest("002")
        self._create_window_metrics(cooling_guest, visits_30=0, visits_60=1, visits_180=1)
        self._create_window_metrics(active_guest, visits_30=2, visits_60=2, visits_180=2)

        self._create_raw_line(
            fingerprint="cooling-burger-1",
            guest=cooling_guest,
            business_date=date(2026, 2, 20),
            department_id=self.department_id,
            order_number=101,
            dish_code="dish-burger",
            dish_amount="2",
            net_sum="500.00",
        )
        self._create_raw_line(
            fingerprint="cooling-burger-2",
            guest=cooling_guest,
            business_date=date(2026, 2, 21),
            department_id=self.department_id,
            order_number=102,
            dish_code="dish-burger",
            dish_amount="1",
            net_sum="250.00",
        )
        self._create_raw_line(
            fingerprint="cooling-tea-1",
            guest=cooling_guest,
            business_date=date(2026, 2, 19),
            department_id=self.department_id,
            order_number=103,
            dish_code="dish-tea",
            dish_amount="1",
            net_sum="100.00",
        )
        self._create_raw_line(
            fingerprint="cooling-free-modifier",
            guest=cooling_guest,
            business_date=date(2026, 2, 18),
            department_id=self.department_id,
            order_number=107,
            dish_code="dish-free",
            dish_amount="99",
            net_sum="0.00",
        )
        self._create_raw_line(
            fingerprint="active-burger",
            guest=active_guest,
            business_date=date(2026, 3, 20),
            department_id=self.department_id,
            order_number=104,
            dish_code="dish-burger",
            dish_amount="9",
            net_sum="900.00",
        )
        self._create_raw_line(
            fingerprint="cooling-other-dep",
            guest=cooling_guest,
            business_date=date(2026, 2, 20),
            department_id=self.other_department_id,
            order_number=105,
            dish_code="dish-burger",
            dish_amount="7",
            net_sum="700.00",
        )
        self._create_raw_line(
            fingerprint="cooling-too-old",
            guest=cooling_guest,
            business_date=date(2026, 1, 1),
            department_id=self.department_id,
            order_number=106,
            dish_code="dish-burger",
            dish_amount="5",
            net_sum="500.00",
        )

        response = self.client.get(
            reverse("segment_purchase_analysis"),
            {
                "department_id": self.department_id,
                "segment_code": DEFAULT_SEGMENT_CODE,
                "period_days": 60,
                "top_limit": 10,
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.context["payload"]
        self.assertEqual(payload["stats"]["segment_size"], 1)
        self.assertEqual(payload["stats"]["guests_with_purchases"], 1)
        self.assertEqual(payload["stats"]["orders_count"], 3)
        self.assertEqual(payload["stats"]["raw_lines_count"], 3)
        self.assertTrue(payload["filters"]["hide_zero_revenue"])

        self.assertEqual(len(payload["rows"]), 2)
        first_row = payload["rows"][0]
        self.assertEqual(first_row["dish_code"], "dish-burger")
        self.assertEqual(first_row["dish_name"], "Бургер из справочника")
        self.assertEqual(first_row["category_name"], "Еда")
        self.assertEqual(first_row["group_name"], "Основное")
        self.assertEqual(Decimal(first_row["quantity_total_value"]), Decimal("3"))
        self.assertEqual(first_row["guests_count"], 1)
        self.assertEqual(first_row["orders_count"], 2)
        self.assertEqual(Decimal(first_row["sales_sum_value"]), Decimal("750.00"))

        second_row = payload["rows"][1]
        self.assertEqual(second_row["dish_code"], "dish-tea")
        self.assertEqual(Decimal(second_row["quantity_total_value"]), Decimal("1"))

        response_with_free_items = self.client.get(
            reverse("segment_purchase_analysis"),
            {
                "department_id": self.department_id,
                "segment_code": DEFAULT_SEGMENT_CODE,
                "period_days": 60,
                "top_limit": 10,
                "hide_zero_revenue": 0,
            },
            secure=True,
        )

        self.assertEqual(response_with_free_items.status_code, 200)
        payload_with_free_items = response_with_free_items.context["payload"]
        self.assertFalse(payload_with_free_items["filters"]["hide_zero_revenue"])
        self.assertEqual(payload_with_free_items["stats"]["orders_count"], 4)
        self.assertEqual(payload_with_free_items["stats"]["raw_lines_count"], 4)
        self.assertEqual(payload_with_free_items["rows"][0]["dish_code"], "dish-free")
        self.assertEqual(Decimal(payload_with_free_items["rows"][0]["quantity_total_value"]), Decimal("99"))
        self.assertEqual(Decimal(payload_with_free_items["rows"][0]["sales_sum_value"]), Decimal("0.00"))

    def test_new_segment_uses_only_first_purchase_date_rows(self):
        guest = self._create_guest("003")
        self._create_window_metrics(guest, visits_30=1, visits_60=1, visits_180=1)
        self._create_daily_order(guest, date(2026, 3, 10))
        self._create_daily_order(guest, date(2026, 3, 12))

        self._create_raw_line(
            fingerprint="new-first-day",
            guest=guest,
            business_date=date(2026, 3, 10),
            department_id=self.department_id,
            order_number=201,
            dish_code="dish-tea",
            dish_amount="2",
            net_sum="200.00",
        )
        self._create_raw_line(
            fingerprint="new-second-day",
            guest=guest,
            business_date=date(2026, 3, 12),
            department_id=self.department_id,
            order_number=202,
            dish_code="dish-burger",
            dish_amount="5",
            net_sum="500.00",
        )

        response = self.client.get(
            reverse("segment_purchase_analysis"),
            {
                "department_id": self.department_id,
                "segment_code": NEW_IN_VENUE_SEGMENT_CODE,
                "period_days": 60,
                "top_limit": 10,
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.context["payload"]
        self.assertEqual(payload["stats"]["segment_size"], 1)
        self.assertEqual(payload["stats"]["raw_lines_count"], 1)
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0]["dish_code"], "dish-tea")
        self.assertEqual(Decimal(payload["rows"][0]["quantity_total_value"]), Decimal("2"))

    def _create_guest(self, suffix: str) -> Guest:
        return Guest.objects.create(phone=f"+79990000{suffix}")

    def _create_department_option(self, department_id: str, department_name: str) -> None:
        OrderFact.objects.create(
            business_date=date(2026, 3, 20),
            department_id=department_id,
            department_name=department_name,
            order_number=900000 + OrderFact.objects.count(),
            uniq_order_id=f"dep-option-{department_id}",
            gross_sum=Decimal("0.00"),
            net_sum=Decimal("0.00"),
        )

    def _create_nomenclature(self, code: str, name: str, group_name: str) -> None:
        OlapNomenclatureDict.objects.create(
            iiko_nomenclature_external_id=code,
            nomenclature_name=name,
            olap_category=self.category,
            dish_group_name=group_name,
            is_active=True,
        )

    def _create_window_metrics(
        self,
        guest: Guest,
        *,
        visits_30: int,
        visits_60: int,
        visits_180: int,
    ) -> None:
        for window_days, visits_count in (
            (30, visits_30),
            (60, visits_60),
            (180, visits_180),
        ):
            GuestRestaurantWindowMetrics.objects.create(
                as_of_date=self.as_of_date,
                guest=guest,
                department_id=self.department_id,
                window_days=window_days,
                orders_count=visits_count,
                visits_count=visits_count,
                avg_check_net=Decimal("100.00"),
                sum_net=Decimal("100.00") * visits_count,
                rating_score=Decimal("1.00"),
            )

    def _create_daily_order(self, guest: Guest, business_date: date) -> None:
        GuestRestaurantDailyOrderFact.objects.create(
            business_date=business_date,
            guest=guest,
            department_id=self.department_id,
            orders_count=1,
            sum_net=Decimal("100.00"),
        )

    def _create_raw_line(
        self,
        *,
        fingerprint: str,
        guest: Guest,
        business_date: date,
        department_id: str,
        order_number: int,
        dish_code: str,
        dish_amount: str,
        net_sum: str,
    ) -> None:
        OlapSalesRawLine.objects.create(
            row_fingerprint=fingerprint,
            sync_journal=self.journal,
            guest=guest,
            business_date=business_date,
            department_id=department_id,
            department_name=department_id,
            order_number=order_number,
            uniq_order_id=f"uniq-{order_number}",
            item_sale_event_id=f"event-{fingerprint}",
            dish_code=dish_code,
            dish_name=f"Raw {dish_code}",
            dish_category_id="cat-food",
            dish_category_name="Raw category",
            dish_group_id="group-raw",
            dish_group_name="Raw group",
            dish_amount=Decimal(dish_amount),
            dish_sum_before_discount=Decimal(net_sum),
            dish_sum_after_discount=Decimal(net_sum),
            bonus_sum=Decimal("0.00"),
            raw_payload={},
        )
