from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from guests.models import (
    CouponAutomationConfig,
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponCampaignAssignment,
    CouponPoolBatch,
    CouponRegistryEntry,
    DispatchTask,
    Guest,
    Mailing,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
    OrderFact,
    TerminalDepartmentMap,
)
from guests.views_reports import CouponAutoscenarioReportsView


class CouponReportsViewsTests(TestCase):
    """
    Проверки новых экранов этапа C:
    1. хаб «Отчёты»;
    2. реестр купонов;
    3. отчёт по купонной кампании.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Coupon reports template",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Coupon campaign A",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(hours=1),
            scheduled_time_end=self.now + timedelta(hours=6),
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=(self.now - timedelta(hours=1)).time(),
            send_window_end=(self.now + timedelta(hours=2)).time(),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.NORMAL,
            coupon_series="TEST",
            coupon_venue_code="DEP_1",
            coupon_venue_name="Тестовое заведение",
            coupon_promo_text="Скидка 20%",
        )

    def test_reports_hub_opens_and_shows_coupon_links(self):
        response = self.client.get(reverse("reports"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reports/coupon-autoscenarios")
        self.assertContains(response, "Купонные кампании")
        self.assertContains(response, "Реестр купонов")

    def test_coupon_registry_filters_by_series_and_campaign(self):
        guest = Guest.objects.create(
            phone="+79991112233",
            first_name="Иван",
            created_at=self.now,
            updated_at=self.now,
        )
        batch = CouponPoolBatch.objects.create(
            batch_code="TEST_20260514_001",
            series="TEST",
            prefix="TST-",
            random_length=12,
            count_requested=2,
            count_generated=2,
            generated_by="tester",
        )
        coupon_match = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-AAAA1111",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            batch=batch,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
            iiko_checked_at=self.now,
        )
        CouponRegistryEntry.objects.create(
            series="OTHER",
            code="OTH-BBBB2222",
            venue_code="DEP_2",
            venue_name="Другое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            batch=batch,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
            iiko_checked_at=self.now,
        )
        CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=guest,
            coupon=coupon_match,
            person_id=uuid4(),
            phone_e164=guest.phone,
            coupon_series=coupon_match.series,
            coupon_code=coupon_match.code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Скидка 20%",
            assigned_at=self.now,
            status=CouponCampaignAssignment.Status.USED,
            used_at=self.now,
            used_order_id=123456789,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )

        response = self.client.get(
            reverse("coupon_registry"),
            {"series": "TEST", "campaign_id": str(self.mailing.id)},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TST-AAAA1111")
        self.assertNotContains(response, "OTH-BBBB2222")
        self.assertContains(response, str(self.mailing.id))
        self.assertContains(response, guest.phone)
        self.assertContains(response, "Использован")
        self.assertContains(response, "123456789")
        self.assertContains(response, "Синхронизирован")
        self.assertContains(response, "Генерация купонов")
        self.assertNotContains(response, "Операции реестра")

    def test_coupon_registry_shows_latest_autoscenario_assignment(self):
        campaign_guest = Guest.objects.create(
            phone="+79991112233",
            first_name="Иван",
            created_at=self.now,
            updated_at=self.now,
        )
        autoscenario_guest = Guest.objects.create(
            phone="+79994445566",
            first_name="Анна",
            created_at=self.now,
            updated_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="AUTO_REGISTRY",
            code="AUTO-0001",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
            iiko_checked_at=self.now,
        )
        CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=campaign_guest,
            coupon=coupon,
            person_id=uuid4(),
            phone_e164=campaign_guest.phone,
            coupon_series=coupon.series,
            coupon_code=coupon.code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            assigned_at=self.now,
            status=CouponCampaignAssignment.Status.CANCELED,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon",
            name="Остывшие 30 дней",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            is_active=False,
            is_system=True,
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            coupon_series=coupon.series,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            max_recipients_per_run=1,
        )
        run = CouponAutoscenarioRun.objects.create(
            scenario=scenario,
            config=config,
            status=CouponAutoscenarioRun.Status.COMPLETED,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            planned_assignments=1,
            created_assignments=1,
            queue_events_created=1,
        )
        CouponAutoscenarioAssignment.objects.create(
            run=run,
            scenario=scenario,
            config=config,
            guest=autoscenario_guest,
            coupon=coupon,
            person_id=uuid4(),
            phone_e164=autoscenario_guest.phone,
            coupon_series=coupon.series,
            coupon_code=coupon.code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            assigned_at=self.now + timedelta(minutes=10),
            status=CouponAutoscenarioAssignment.Status.CANCELED,
            vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now + timedelta(minutes=11),
        )

        response = self.client.get(
            reverse("coupon_registry"),
            {"series": coupon.series},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AUTO-0001")
        self.assertContains(response, "Автосценарий")
        self.assertContains(response, f"inactive_30d_coupon, run #{run.id}")
        self.assertContains(response, autoscenario_guest.phone)
        self.assertNotContains(response, campaign_guest.phone)

    def test_coupon_autoscenario_report_shows_delivery_and_order_fact_revenue(self):
        guest = Guest.objects.create(
            phone="+79994445566",
            first_name="Auto",
            created_at=self.now,
            updated_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="AUTO_REPORT",
            code="AUTO-0001",
            venue_code="DEP_1",
            venue_name="Test venue",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.USED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
            iiko_checked_at=self.now,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon_report",
            name="Inactive 30 report",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            is_active=False,
            is_system=True,
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.AUTOMATIC,
            coupon_series=coupon.series,
            venue_code="DEP_1",
            venue_name="Test venue",
            max_recipients_per_run=1,
            cooldown_days=30,
        )
        run = CouponAutoscenarioRun.objects.create(
            scenario=scenario,
            config=config,
            status=CouponAutoscenarioRun.Status.COMPLETED,
            execution_mode=CouponAutomationConfig.ExecutionMode.AUTOMATIC,
            scanned_guests=10,
            matched_guests=8,
            sendable_guests=5,
            eligible_guests=1,
            planned_assignments=1,
            created_assignments=1,
            queue_events_created=1,
            coupon_shortage=0,
        )
        assignment = CouponAutoscenarioAssignment.objects.create(
            run=run,
            scenario=scenario,
            config=config,
            guest=guest,
            coupon=coupon,
            person_id=uuid4(),
            phone_e164=guest.phone,
            coupon_series=coupon.series,
            coupon_code=coupon.code,
            venue_code="DEP_1",
            venue_name="Test venue",
            assigned_at=self.now,
            sent_at=self.now,
            status=CouponAutoscenarioAssignment.Status.USED,
            used_at=self.now,
            used_order_id=7001,
            used_business_date=self.now.date(),
            vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )
        event = NotificationEvent.objects.create(
            scenario=scenario,
            guest=guest,
            source_type=NotificationEvent.SourceType.SCHEDULE,
            source_ref=f"coupon_autoscenario_assignment:{assignment.id}",
            dedupe_key=f"coupon_autoscenario_assignment:{assignment.id}",
            status=NotificationEvent.Status.TASK_CREATED,
            planned_send_at=self.now,
            coupon_code=coupon.code,
            coupon_external_id=f"{coupon.series}:{coupon.code}",
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type="telegram",
            priority=DispatchTask.Priority.NORMAL,
            status=DispatchTask.Status.DONE,
            guest=guest,
            notification_scenario=scenario,
            notification_event=event,
            external_chat_id="12345",
            message_text="test",
            idempotency_key=f"coupon-autoscenario-test-{assignment.id}",
            available_at=self.now,
            scheduled_at=self.now,
            enqueued_at=self.now,
            started_at=self.now,
            finished_at=self.now,
            attempt=1,
        )
        OrderFact.objects.create(
            guest=guest,
            business_date=self.now.date(),
            department_id="DEP_1",
            department_name="Test venue",
            order_number=7001,
            uniq_order_id="auto-report-7001",
            gross_sum="540.00",
            net_sum="350.00",
            discount_sum="190.00",
            coupon_used=True,
            coupon_series=coupon.series,
            coupon_number=coupon.code,
            first_seen_at=self.now,
        )

        response = self.client.get(
            reverse("reports_coupon_autoscenarios"),
            {"scenario_code": scenario.code},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        daily_rows = response.context["autoscenario_report"]["daily_rows"]
        report_day = next(row for row in daily_rows if row["date"] == self.now.date().isoformat())
        self.assertEqual(report_day["weekday_short"], CouponAutoscenarioReportsView.weekday_short_labels[self.now.date().weekday()])
        self.assertEqual(report_day["is_weekend"], self.now.date().weekday() >= 5)
        self.assertIn("\n", report_day["axis_label"])
        self.assertContains(response, scenario.code)
        self.assertContains(response, coupon.code)
        self.assertContains(response, guest.phone)
        self.assertContains(response, "350.00")
        self.assertContains(response, "7001")
        self.assertContains(response, "Воронка боевых запусков")
        self.assertContains(response, "Динамика по дням")
        self.assertContains(response, "Попали под сценарий")
        self.assertContains(response, "Есть канал")
        self.assertContains(response, "Получили купон")
        self.assertContains(response, "Применили купон")
        self.assertContains(response, "Выходные дни подсвечены")
        self.assertContains(response, "Применения считаются по дате заказа из OLAP")
        self.assertContains(response, "Журнал пилотов")

    def test_coupon_autoscenario_report_keeps_pilot_runs_out_of_marketing_kpi(self):
        guest = Guest.objects.create(
            phone="+79990001122",
            first_name="Pilot",
            created_at=self.now,
            updated_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="AUTO_PILOT_REPORT",
            code="PILOT-0001",
            venue_code="DEP_1",
            venue_name="Test venue",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
            iiko_checked_at=self.now,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon_pilot_report",
            name="Inactive 30 pilot report",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            is_active=False,
            is_system=True,
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            coupon_series=coupon.series,
            venue_code="DEP_1",
            venue_name="Test venue",
            max_recipients_per_run=1,
            cooldown_days=30,
        )
        run = CouponAutoscenarioRun.objects.create(
            scenario=scenario,
            config=config,
            status=CouponAutoscenarioRun.Status.COMPLETED,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            scanned_guests=10,
            matched_guests=8,
            sendable_guests=5,
            eligible_guests=1,
            planned_assignments=1,
            created_assignments=1,
            queue_events_created=1,
            coupon_shortage=0,
        )
        assignment = CouponAutoscenarioAssignment.objects.create(
            run=run,
            scenario=scenario,
            config=config,
            guest=guest,
            coupon=coupon,
            person_id=uuid4(),
            phone_e164=guest.phone,
            coupon_series=coupon.series,
            coupon_code=coupon.code,
            venue_code="DEP_1",
            venue_name="Test venue",
            assigned_at=self.now,
            sent_at=self.now,
            status=CouponAutoscenarioAssignment.Status.SENT,
            vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )
        event = NotificationEvent.objects.create(
            scenario=scenario,
            guest=guest,
            source_type=NotificationEvent.SourceType.SCHEDULE,
            source_ref=f"coupon_autoscenario_assignment:{assignment.id}",
            dedupe_key=f"coupon_autoscenario_assignment:{assignment.id}",
            status=NotificationEvent.Status.TASK_CREATED,
            planned_send_at=self.now,
            coupon_code=coupon.code,
            coupon_external_id=f"{coupon.series}:{coupon.code}",
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type="telegram",
            priority=DispatchTask.Priority.NORMAL,
            status=DispatchTask.Status.DONE,
            guest=guest,
            notification_scenario=scenario,
            notification_event=event,
            external_chat_id="12345",
            message_text="pilot",
            idempotency_key=f"coupon-autoscenario-pilot-test-{assignment.id}",
            available_at=self.now,
            scheduled_at=self.now,
            enqueued_at=self.now,
            started_at=self.now,
            finished_at=self.now,
            attempt=1,
        )

        response = self.client.get(
            reverse("reports_coupon_autoscenarios"),
            {"scenario_code": scenario.code},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "За выбранный период боевых запусков не было")
        self.assertContains(response, "Маркетинговую эффективность оценивать нельзя")
        self.assertContains(response, "Боевых запусков")
        self.assertContains(response, "Журнал пилотов")
        self.assertContains(response, "Пилоты нужны для проверки текста")
        self.assertContains(response, "не входят в маркетинговые KPI")
        self.assertContains(response, coupon.code)
        self.assertContains(response, guest.phone)

    def test_coupon_generation_form_uses_venue_catalog(self):
        TerminalDepartmentMap.objects.create(
            terminal_group_id="terminal-gruzinka",
            department_id="DEP_GRUZINKA",
            department_name="Грузинка",
            is_active=True,
        )

        response = self.client.get(reverse("coupon_generation"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Грузинка (DEP_GRUZINKA)")
        self.assertContains(response, '<select name="venue_code"', html=False)
        self.assertNotContains(response, 'name="venue_name"', html=False)

    def test_coupon_generation_shows_selected_batch_actions(self):
        batch = CouponPoolBatch.objects.create(
            batch_code="TEST_BATCH_ACTIONS",
            series="TEST_ACTIONS",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            prefix="TST-",
            random_length=8,
            count_requested=2,
            count_generated=2,
            generated_by="tester",
            export_file_path="tools/test_actions.csv",
        )
        CouponRegistryEntry.objects.create(
            series=batch.series,
            code="TST-ACTION1",
            venue_code=batch.venue_code,
            venue_name=batch.venue_name,
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            batch=batch,
            pool_status=CouponRegistryEntry.PoolStatus.GENERATED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.NOT_CHECKED,
        )

        response = self.client.get(
            reverse("coupon_generation"),
            {"batch_code": batch.batch_code},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Текущая партия")
        self.assertContains(response, batch.batch_code)
        self.assertContains(response, "Скачать CSV")
        self.assertContains(response, "Проверить iikoCard")
        self.assertContains(response, "Открыть в реестре")

    def test_coupon_generation_shows_recent_batches_filtered_by_series(self):
        matched_batch = CouponPoolBatch.objects.create(
            batch_code="TEST_RECENT_MATCH_001",
            series="TEST_RECENT",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            prefix="TST-",
            random_length=8,
            count_requested=2,
            count_generated=2,
            generated_by="tester",
            export_file_path="tools/test_recent_match.csv",
        )
        CouponPoolBatch.objects.create(
            batch_code="TEST_RECENT_OTHER_001",
            series="OTHER_RECENT",
            venue_code="DEP_2",
            venue_name="Другое заведение",
            prefix="OTH-",
            random_length=8,
            count_requested=1,
            count_generated=1,
            generated_by="tester",
            export_file_path="tools/test_recent_other.csv",
        )

        response = self.client.get(
            reverse("coupon_generation"),
            {"series_hint": matched_batch.series},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Последние партии")
        self.assertContains(response, matched_batch.batch_code)
        self.assertNotContains(response, "TEST_RECENT_OTHER_001")
        self.assertContains(response, "Скачать CSV")

    def test_coupon_campaign_reports_builds_selected_campaign_report(self):
        snapshot_mock = Mock()
        snapshot_mock.to_dict.return_value = {
            "coupon_series": "TEST",
            "recipients_total": 120,
            "assignments_total": 120,
            "assignments_reserved": 10,
            "assignments_sent": 90,
            "assignments_used": 40,
            "assignments_used_after_campaign": 5,
            "assignments_error": 2,
            "coupons_sent_total": 130,
            "used_within_campaign": 35,
            "used_late_total": 5,
            "returned_guest_coupon": 12,
            "returned_window_days": 30,
            "revenue_net_used": "57000.00",
            "coupon_orders_avg_check": "1425.00",
            "unique_used_guests": 38,
            "coupon_orders_total": 40,
            "daily_usage_rows": [
                {
                    "business_date": (self.now.date() + timedelta(days=offset)).isoformat(),
                    "orders_count": 4 if offset == 0 else 0,
                    "used_coupons_count": 4 if offset == 0 else 0,
                    "revenue_net": "57000.00" if offset == 0 else "0",
                    "gross_sum": "60000.00" if offset == 0 else "0",
                    "discount_sum": "3000.00" if offset == 0 else "0",
                    "avg_check": "14250.00" if offset == 0 else "0",
                }
                for offset in range(15)
            ],
            "product_rank_rows": [
                {
                    "dish_code": "COFFEE-AM",
                    "dish_name": "Американо",
                    "orders_count": 4,
                    "quantity_total": "4",
                    "gross_sum": "760.00",
                    "revenue_net": "0.00",
                    "discount_sum": "760.00",
                }
            ],
            "order_detail_rows": [
                {
                    "business_date": "2026-05-20",
                    "order_number": 5001,
                    "guest_id": 100,
                    "coupon_code": "TST-001",
                    "department_name": "Тестовое заведение",
                    "gross_sum": "1500.00",
                    "revenue_net": "1200.00",
                    "discount_sum": "300.00",
                    "items_count": 1,
                    "items": [],
                }
            ],
            "usage_rate_percent": 30.77,
            "returned_guests_rate_percent": 34.29,
            "late_usage_rows": [],
        }

        with patch(
            "guests.views_reports.build_coupon_campaign_performance_snapshot",
            return_value=snapshot_mock,
        ) as build_mock:
            response = self.client.get(
                reverse("reports_coupon_campaigns"),
                {"campaign_id": str(self.mailing.id)},
                secure=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Кампания #")
        self.assertContains(response, "57000.00")
        self.assertContains(response, "30,77%")
        self.assertContains(response, "Заказы с купоном по дням")
        self.assertContains(response, "Средний чек")
        self.assertContains(response, "Первые 14 дней")
        self.assertContains(response, "Последние 14 дней")
        self.assertContains(response, "Весь период")
        self.assertContains(response, "Рейтинг позиций в заказах с купоном")
        self.assertContains(response, "Американо")
        self.assertNotContains(response, ">Доля<", html=False)
        self.assertNotContains(response, 'name: "Купонов"', html=False)
        self.assertNotContains(response, "Возвращаемость")
        self.assertNotContains(response, "Вернувшиеся гости")
        self.assertContains(response, "Карточка кампании")
        build_mock.assert_called_once_with(mailing=self.mailing)
