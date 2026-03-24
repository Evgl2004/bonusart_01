"""
Тесты клиента iiko OLAP API (S4).
"""

from __future__ import annotations

from unittest.mock import Mock

from django.test import SimpleTestCase

from guests.services.iiko_olap_client import (
    IikoOlapClient,
    IikoOlapRequestError,
)


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("No JSON")
        return self._json_data


class IikoOlapClientTests(SimpleTestCase):
    """
    Проверки ключевых веток работы OLAP-клиента.
    """

    def _build_client(self, session: Mock) -> IikoOlapClient:
        return IikoOlapClient(
            base_url="https://example.iiko.it/resto/api",
            login="login",
            pass_hash="pass_hash",
            max_retries=2,
            retry_base_seconds=0.01,
            key_ttl_seconds=240,
            session=session,
        )

    def test_get_auth_key_accepts_plain_text_response(self):
        """
        Авторизация должна поддерживать plain-text ключ.
        """
        session = Mock()
        session.post.return_value = _FakeResponse(status_code=200, text="plain-key-token")
        client = self._build_client(session=session)

        key = client.get_auth_key()

        self.assertEqual(key, "plain-key-token")
        self.assertEqual(session.post.call_count, 1)

    def test_get_auth_key_accepts_json_response(self):
        """
        Авторизация должна поддерживать JSON с полем key/token.
        """
        session = Mock()
        session.post.return_value = _FakeResponse(status_code=200, text="{}", json_data={"key": "json-key-token"})
        client = self._build_client(session=session)

        key = client.get_auth_key()

        self.assertEqual(key, "json-key-token")
        self.assertEqual(session.post.call_count, 1)

    def test_query_olap_retries_after_transient_error(self):
        """
        При временной ошибке (503) клиент должен повторить запрос.
        """
        session = Mock()
        session.post.side_effect = [
            _FakeResponse(status_code=200, text="key-1"),
            _FakeResponse(status_code=503, text="service unavailable"),
            _FakeResponse(status_code=200, json_data={"data": [{"OrderNum": 1}], "summary": []}),
        ]
        client = self._build_client(session=session)

        response = client.query_olap({"reportType": "SALES"})

        self.assertEqual(response["data"][0]["OrderNum"], 1)
        # 1 раз auth + 2 попытки report
        self.assertEqual(session.post.call_count, 3)

    def test_query_olap_refreshes_key_after_401(self):
        """
        При 401 клиент должен один раз обновить ключ и повторить запрос.
        """
        session = Mock()
        session.post.side_effect = [
            _FakeResponse(status_code=200, text="key-1"),  # initial auth
            _FakeResponse(status_code=401, text="expired"),  # report with old key
            _FakeResponse(status_code=200, text="key-2"),  # refresh auth
            _FakeResponse(status_code=200, json_data={"data": [], "summary": []}),  # report with new key
        ]
        client = self._build_client(session=session)

        response = client.query_olap({"reportType": "SALES"})

        self.assertEqual(response["data"], [])
        self.assertEqual(session.post.call_count, 4)

    def test_fetch_sales_in_portions_splits_orders(self):
        """
        Порционная загрузка должна делить список чеков по размеру порции.
        """
        session = Mock()
        session.post.return_value = _FakeResponse(status_code=200, text="plain-key-token")
        client = self._build_client(session=session)

        query_calls: list[dict] = []

        def _fake_query(payload):
            query_calls.append(payload)
            return {"data": [{"rows": len(payload["filters"]["OrderNum"]["values"])}], "summary": []}

        client.query_olap = _fake_query  # type: ignore[assignment]

        rows, summary, stats = client.fetch_sales_in_portions(
            date_from="2026-03-18",
            date_to="2026-03-18",
            order_numbers=[1, 2, 3, 4, 5],
            portion_size=2,
            fail_fast=True,
        )

        self.assertEqual(len(query_calls), 3)
        self.assertEqual(stats.requested_portions, 3)
        self.assertEqual(stats.successful_portions, 3)
        self.assertEqual(stats.failed_portions, 0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(summary, [])

    def test_fetch_sales_in_portions_collects_failed_portion_when_not_fail_fast(self):
        """
        При fail_fast=False ошибка порции должна фиксироваться в stats, но загрузка продолжается.
        """
        session = Mock()
        session.post.return_value = _FakeResponse(status_code=200, text="plain-key-token")
        client = self._build_client(session=session)

        call_index = {"idx": 0}

        def _fake_query(payload):
            call_index["idx"] += 1
            order_values = payload["filters"]["OrderNum"]["values"]
            if call_index["idx"] == 2:
                raise IikoOlapRequestError("portion failed")
            return {"data": [{"order_values": order_values}], "summary": []}

        client.query_olap = _fake_query  # type: ignore[assignment]

        rows, _, stats = client.fetch_sales_in_portions(
            date_from="2026-03-18",
            date_to="2026-03-18",
            order_numbers=[10, 11, 12, 13],
            portion_size=2,
            fail_fast=False,
        )

        self.assertEqual(stats.requested_portions, 2)
        self.assertEqual(stats.successful_portions, 1)
        self.assertEqual(stats.failed_portions, 1)
        self.assertEqual(stats.failed_order_number_portions, [[12, 13]])
        self.assertEqual(len(rows), 1)

    def test_build_sales_payload_includes_discount_and_deleted_defaults(self):
        """
        Базовый payload для SALES должен запрашивать сумму после скидки и признак удаления строки.
        """
        session = Mock()
        client = self._build_client(session=session)

        payload = client.build_sales_payload(
            date_from="2026-03-18",
            date_to="2026-03-18",
            order_numbers=[1],
        )

        self.assertIn("DishDiscountSumInt", payload["aggregateFields"])
        self.assertIn("DeletedWithWriteoff", payload["groupByRowFields"])

    def test_build_sales_payload_for_department_window_has_no_order_filter(self):
        """
        Контрольный payload по Department.Id не должен содержать фильтр OrderNum.
        """
        session = Mock()
        client = self._build_client(session=session)

        payload = client.build_sales_payload_for_department_window(
            date_from="2026-03-01",
            date_to="2026-03-02",
            department_ids=["dept-1"],
        )

        self.assertEqual(payload["reportType"], "SALES")
        self.assertIn("Department.Id", payload["filters"])
        self.assertNotIn("OrderNum", payload["filters"])
        self.assertEqual(payload["filters"]["Department.Id"]["values"], ["dept-1"])
        self.assertIn("DeletedWithWriteoff", payload["groupByRowFields"])
