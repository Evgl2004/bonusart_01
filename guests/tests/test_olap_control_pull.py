"""
Тесты контрольной дозагрузки OLAP (прямой срез -> journal).
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    CouponCampaignAssignment,
    CouponRegistryEntry,
    Guest,
    Mailing,
    MessageTemplate,
    OlapCheckSyncJournal,
    TerminalDepartmentMap,
)
from guests.services.olap_control_pull import (
    OlapControlPullOptions,
    OlapControlPullService,
)


class _FakeOlapClient:
    def __init__(self, rows_by_department: dict[str, list[dict]]):
        self.rows_by_department = rows_by_department
        self.payloads: list[dict] = []

    def build_sales_payload_for_department_window(self, **kwargs):
        self.payloads.append(dict(kwargs))
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
        self.guest_one = Guest.objects.create(phone="+79990000001")
        self.guest_two = Guest.objects.create(phone="+79990000002")

    def _create_coupon_assignment(self, *, code: str) -> CouponCampaignAssignment:
        now = timezone.now()
        template = MessageTemplate.objects.create(
            name=f"Control pull coupon template {code}",
            description="",
            message_text="Тестовый купон",
            created_by="test",
            is_active=True,
        )
        mailing = Mailing.objects.create(
            name=f"Control pull coupon campaign {code}",
            template=template,
            scheduled_date=now.date(),
            scheduled_time_begin=now - timedelta(hours=1),
            scheduled_time_end=now + timedelta(days=7),
            is_active=True,
            created_at=now,
            updated_at=now,
            send_window_begin=(now - timedelta(hours=1)).time(),
            send_window_end=(now + timedelta(hours=1)).time(),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.NORMAL,
            coupon_series="TEST",
            coupon_venue_code="dept-1",
            coupon_venue_name="Тестовое заведение",
            coupon_promo_text="Тестовая акция",
        )
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code=code,
            venue_code="dept-1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        return CouponCampaignAssignment.objects.create(
            campaign=mailing,
            guest=self.guest_one,
            coupon=coupon,
            phone_e164=self.guest_one.phone,
            coupon_series="TEST",
            coupon_code=code,
            venue_code="dept-1",
            venue_name="Тестовое заведение",
            assigned_at=now,
            status=CouponCampaignAssignment.Status.SENT,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=now,
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
                        "Delivery.CustomerPhone": "+79990000001",
                    },
                    {
                        "OpenDate.Typed": "2026-01-01",
                        "OrderNum": 1002,
                        "UniqOrderId.Id": "u-1002",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "+79990000002",
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
        self.assertEqual(stats.olap_rows_with_phone, 2)
        self.assertEqual(stats.olap_rows_without_phone, 0)
        self.assertEqual(stats.olap_rows_phone_without_guest, 0)
        self.assertEqual(stats.distinct_order_keys_seen, 2)
        self.assertEqual(stats.would_create_journal_rows, 2)
        self.assertEqual(stats.created_journal_rows, 0)
        self.assertEqual(OlapCheckSyncJournal.objects.count(), 0)

    def test_run_cycle_requests_phone_and_card_fields(self):
        client = _FakeOlapClient(
            rows_by_department={
                "dept-1": [
                    {
                        "OpenDate.Typed": "2026-01-01",
                        "OrderNum": 1001,
                        "UniqOrderId.Id": "u-1001",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "+79990000001",
                        "Delivery.CustomerCardNumber": "CARD-001",
                    },
                ]
            }
        )
        service = OlapControlPullService(client=client)

        service.run_cycle(
            options=OlapControlPullOptions(
                business_date_from=date(2026, 1, 1),
                business_date_to=date(2026, 1, 1),
                dry_run=True,
            )
        )

        self.assertEqual(len(client.payloads), 1)
        group_fields = client.payloads[0]["group_by_row_fields"]
        self.assertIn("Delivery.CustomerPhone", group_fields)
        self.assertIn("Delivery.CustomerCardNumber", group_fields)
        self.assertIn("CouponInfo.Series", group_fields)
        self.assertIn("CouponInfo.Number", group_fields)
        self.assertIn("DeletedWithWriteoff", group_fields)

    def test_run_cycle_creates_coupon_task_without_phone_by_assigned_coupon(self):
        self._create_coupon_assignment(code="NO-PHONE-1")
        client = _FakeOlapClient(
            rows_by_department={
                "dept-1": [
                    {
                        "OpenDate.Typed": "2026-01-01",
                        "OrderNum": 1101,
                        "UniqOrderId.Id": "u-1101",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "",
                        "CouponInfo.Series": "TEST",
                        "CouponInfo.Number": "NO-PHONE-1",
                    },
                ]
            }
        )
        service = OlapControlPullService(client=client)

        stats = service.run_cycle(
            options=OlapControlPullOptions(
                business_date_from=date(2026, 1, 1),
                business_date_to=date(2026, 1, 1),
                dry_run=False,
            )
        )

        self.assertEqual(stats.olap_rows_without_phone, 1)
        self.assertEqual(stats.olap_rows_without_phone_with_coupon, 1)
        self.assertEqual(stats.olap_rows_without_phone_coupon_matched, 1)
        self.assertEqual(stats.created_journal_rows, 1)
        row = OlapCheckSyncJournal.objects.get(order_number=1101)
        self.assertEqual(row.guest_id, self.guest_one.id)
        self.assertEqual(
            row.source_webhook_id,
            OlapControlPullService.COUPON_WITHOUT_PHONE_SOURCE,
        )

    def test_run_cycle_skips_coupon_without_phone_when_assignment_missing(self):
        client = _FakeOlapClient(
            rows_by_department={
                "dept-1": [
                    {
                        "OpenDate.Typed": "2026-01-01",
                        "OrderNum": 1102,
                        "UniqOrderId.Id": "u-1102",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "",
                        "CouponInfo.Series": "TEST",
                        "CouponInfo.Number": "UNKNOWN-COUPON",
                    },
                ]
            }
        )
        service = OlapControlPullService(client=client)

        stats = service.run_cycle(
            options=OlapControlPullOptions(
                business_date_from=date(2026, 1, 1),
                business_date_to=date(2026, 1, 1),
                dry_run=False,
            )
        )

        self.assertEqual(stats.olap_rows_without_phone, 1)
        self.assertEqual(stats.olap_rows_without_phone_with_coupon, 1)
        self.assertEqual(stats.olap_rows_without_phone_coupon_matched, 0)
        self.assertEqual(stats.olap_rows_without_phone_coupon_missing_assignment, 1)
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
                        "Delivery.CustomerPhone": "+79990000001",
                    },
                    {
                        "OpenDate.Typed": "2026-01-02",
                        "OrderNum": 2002,
                        "UniqOrderId.Id": "u-2002",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "+79990000002",
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
            OlapCheckSyncJournal.objects.filter(guest__isnull=False).count(),
            2,
        )
        self.assertEqual(
            OlapCheckSyncJournal.objects.filter(source_webhook_id="control_pull").count(),
            2,
        )

    def test_run_cycle_skips_rows_without_phone_or_unknown_guest(self):
        client = _FakeOlapClient(
            rows_by_department={
                "dept-1": [
                    {
                        "OpenDate.Typed": "2026-01-03",
                        "OrderNum": 3001,
                        "UniqOrderId.Id": "u-3001",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "",
                    },
                    {
                        "OpenDate.Typed": "2026-01-03",
                        "OrderNum": 3002,
                        "UniqOrderId.Id": "u-3002",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "+79990009999",
                    },
                    {
                        "OpenDate.Typed": "2026-01-03",
                        "OrderNum": 3003,
                        "UniqOrderId.Id": "u-3003",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "+79990000001",
                    },
                ]
            }
        )
        service = OlapControlPullService(client=client)

        with patch.object(service, "_get_or_create_guest_id_by_phone", return_value=None):
            stats = service.run_cycle(
                options=OlapControlPullOptions(
                    business_date_from=date(2026, 1, 3),
                    business_date_to=date(2026, 1, 3),
                    dry_run=True,
                )
            )

        self.assertEqual(stats.olap_rows_seen, 3)
        self.assertEqual(stats.olap_rows_with_phone, 2)
        self.assertEqual(stats.olap_rows_without_phone, 1)
        self.assertEqual(stats.olap_rows_phone_without_guest, 1)
        self.assertEqual(stats.would_create_journal_rows, 1)

    def test_run_cycle_skips_blacklisted_phone(self):
        client = _FakeOlapClient(
            rows_by_department={
                "dept-1": [
                    {
                        "OpenDate.Typed": "2026-01-04",
                        "OrderNum": 4001,
                        "UniqOrderId.Id": "u-4001",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "+79990001111",
                    },
                    {
                        "OpenDate.Typed": "2026-01-04",
                        "OrderNum": 4002,
                        "UniqOrderId.Id": "u-4002",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "+79990000001",
                    },
                ]
            }
        )
        service = OlapControlPullService(
            client=client,
            phone_denylist={"+79990001111"},
        )

        with patch.object(service, "_get_or_create_guest_id_by_phone") as mocked_restore:
            stats = service.run_cycle(
                options=OlapControlPullOptions(
                    business_date_from=date(2026, 1, 4),
                    business_date_to=date(2026, 1, 4),
                    dry_run=True,
                )
            )

        mocked_restore.assert_not_called()
        self.assertEqual(stats.olap_rows_seen, 2)
        self.assertEqual(stats.olap_rows_with_phone, 2)
        self.assertEqual(stats.olap_rows_blacklisted_phone, 1)
        self.assertEqual(stats.olap_rows_phone_without_guest, 0)
        self.assertEqual(stats.would_create_journal_rows, 1)

    def test_run_cycle_skips_deleted_with_writeoff_rows(self):
        client = _FakeOlapClient(
            rows_by_department={
                "dept-1": [
                    {
                        "OpenDate.Typed": "2026-01-04",
                        "OrderNum": 4101,
                        "UniqOrderId.Id": "u-4101",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "DeletedWithWriteoff": "DELETED_WITHOUT_WRITEOFF",
                        "Delivery.CustomerPhone": "+79990000001",
                    },
                    {
                        "OpenDate.Typed": "2026-01-04",
                        "OrderNum": 4102,
                        "UniqOrderId.Id": "u-4102",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "DeletedWithWriteoff": "NOT_DELETED",
                        "Delivery.CustomerPhone": "+79990000001",
                    },
                ]
            }
        )
        service = OlapControlPullService(client=client)

        stats = service.run_cycle(
            options=OlapControlPullOptions(
                business_date_from=date(2026, 1, 4),
                business_date_to=date(2026, 1, 4),
                dry_run=True,
            )
        )

        self.assertEqual(stats.olap_rows_seen, 2)
        self.assertEqual(stats.olap_rows_deleted_with_writeoff, 1)
        self.assertEqual(stats.olap_rows_with_phone, 1)
        self.assertEqual(stats.would_create_journal_rows, 1)

    def test_run_cycle_recovers_unknown_guest_by_phone(self):
        client = _FakeOlapClient(
            rows_by_department={
                "dept-1": [
                    {
                        "OpenDate.Typed": "2026-01-04",
                        "OrderNum": 4001,
                        "UniqOrderId.Id": "u-4001",
                        "Department.Id": "dept-1",
                        "Department.Code": "D1",
                        "Delivery.CustomerPhone": "+79990004444",
                    },
                ]
            }
        )
        service = OlapControlPullService(client=client)
        restored_guest = Guest.objects.create(phone="+79990005555")

        with patch.object(
            service,
            "_get_or_create_guest_id_by_phone",
            return_value=restored_guest.id,
        ) as mocked_restore:
            stats = service.run_cycle(
                options=OlapControlPullOptions(
                    business_date_from=date(2026, 1, 4),
                    business_date_to=date(2026, 1, 4),
                    dry_run=True,
                )
            )

        mocked_restore.assert_called_once_with("+79990004444")
        self.assertEqual(stats.olap_rows_seen, 1)
        self.assertEqual(stats.olap_rows_with_phone, 1)
        self.assertEqual(stats.olap_rows_phone_without_guest, 0)
        self.assertEqual(stats.would_create_journal_rows, 1)
