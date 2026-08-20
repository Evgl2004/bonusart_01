from __future__ import annotations

import uuid
from datetime import time, timedelta

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    BotProfile,
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponAutomationConfig,
    CouponRegistryEntry,
    DispatchTask,
    Guest,
    InteractionButtonSet,
    Mailing,
    MailingGuest,
    MessageInteraction,
    MessageInteractionEvent,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
)
from guests.services.coupon_campaign_reporting import (
    build_coupon_campaign_performance_snapshot,
)
from guests.services.simple_mailing_reporting import (
    build_simple_mailing_report_snapshot,
)
from guests.views_reports import CouponAutoscenarioReportsView


class MessageInteractionReportsIntegrationTests(TestCase):
    """Проверки областей задач в трёх существующих отчётах."""

    def setUp(self):
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Шаблон проверки взаимодействий",
            description="",
            message_text="Тест",
            created_by="tests",
            is_active=True,
        )

    def _create_mailing(self, *, coupon_series: str | None) -> Mailing:
        return Mailing.objects.create(
            name="Проверка отчёта",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now,
            scheduled_time_end=self.now + timedelta(hours=1),
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=time(9, 0),
            send_window_end=time(21, 0),
            coupon_series=coupon_series,
        )

    def _create_mailing_task(
        self,
        *,
        mailing: Mailing,
        guest: Guest,
        action: str,
    ) -> DispatchTask:
        mailing_guest = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Тест",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.DONE,
            created_at=self.now,
        )
        task = DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.DONE,
            mailing_guest=mailing_guest,
            guest=guest,
        )
        interaction = MessageInteraction.objects.create(
            dispatch_task=task,
            button_set=(
                InteractionButtonSet.RATING_COUPONS
                if action == MessageInteractionEvent.Action.COUPONS
                else InteractionButtonSet.RATING_MENU
            ),
        )
        MessageInteractionEvent.objects.create(
            event_id=uuid.uuid4(),
            interaction=interaction,
            action=action,
            occurred_at=self.now,
            result=MessageInteractionEvent.Result.ACCEPTED,
        )
        return task

    def test_simple_mailing_report_uses_only_selected_mailing_tasks(self):
        guest = Guest.objects.create(phone="+70000000101")
        selected = self._create_mailing(coupon_series=None)
        other = self._create_mailing(coupon_series=None)
        self._create_mailing_task(
            mailing=selected,
            guest=guest,
            action=MessageInteractionEvent.Action.MENU,
        )
        self._create_mailing_task(
            mailing=other,
            guest=guest,
            action=MessageInteractionEvent.Action.LIKE,
        )

        interactions = build_simple_mailing_report_snapshot(
            mailing=selected
        ).to_dict()["interactions"]

        self.assertEqual(interactions["messages_with_buttons_total"], 1)
        self.assertEqual(interactions["menu_clicks_total"], 1)
        self.assertEqual(interactions["likes_total"], 0)

    def test_coupon_campaign_report_keeps_opening_separate_from_coupon_use(self):
        guest = Guest.objects.create(phone="+70000000102")
        mailing = self._create_mailing(coupon_series="REPORT_TEST")
        self._create_mailing_task(
            mailing=mailing,
            guest=guest,
            action=MessageInteractionEvent.Action.COUPONS,
        )

        report = build_coupon_campaign_performance_snapshot(mailing=mailing).to_dict()

        self.assertEqual(report["interactions"]["coupon_clicks_total"], 1)
        self.assertEqual(report["assignments_used"], 0)

    def test_autoscenario_report_uses_tasks_of_selected_assignments(self):
        guest = Guest.objects.create(phone="+70000000103")
        scenario = NotificationScenario.objects.create(
            code="interaction_report_scenario",
            name="Сценарий проверки отчёта",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            is_active=True,
            button_set=InteractionButtonSet.RATING_COUPONS,
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.AUTOMATIC,
            coupon_series="AUTO_REPORT_TEST",
            max_recipients_per_run=1,
        )
        run = CouponAutoscenarioRun.objects.create(
            scenario=scenario,
            config=config,
            status=CouponAutoscenarioRun.Status.COMPLETED,
            execution_mode=CouponAutomationConfig.ExecutionMode.AUTOMATIC,
            planned_assignments=1,
            created_assignments=1,
            queue_events_created=1,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="AUTO_REPORT_TEST",
            code="AUTO-REPORT-1",
            source=CouponRegistryEntry.SourceType.GENERATED,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        assignment = CouponAutoscenarioAssignment.objects.create(
            run=run,
            scenario=scenario,
            config=config,
            guest=guest,
            coupon=coupon,
            phone_e164=guest.phone,
            coupon_series=coupon.series,
            coupon_code=coupon.code,
            assigned_at=self.now,
            sent_at=self.now,
            status=CouponAutoscenarioAssignment.Status.SENT,
        )
        notification_event = NotificationEvent.objects.create(
            scenario=scenario,
            guest=guest,
            source_type=NotificationEvent.SourceType.SCHEDULE,
            source_ref=f"coupon_autoscenario_assignment:{assignment.id}",
            dedupe_key=f"coupon_autoscenario_assignment:{assignment.id}",
            status=NotificationEvent.Status.TASK_CREATED,
            planned_send_at=self.now,
        )
        task = DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.DONE,
            guest=guest,
            notification_scenario=scenario,
            notification_event=notification_event,
        )
        interaction = MessageInteraction.objects.create(
            dispatch_task=task,
            button_set=InteractionButtonSet.RATING_COUPONS,
        )
        MessageInteractionEvent.objects.create(
            event_id=uuid.uuid4(),
            interaction=interaction,
            action=MessageInteractionEvent.Action.LIKE,
            occurred_at=self.now,
            result=MessageInteractionEvent.Result.ACCEPTED,
        )

        report = CouponAutoscenarioReportsView()._build_report(
            scenario=scenario,
            date_from=None,
            date_to=None,
        )

        self.assertEqual(report["interactions"]["messages_with_buttons_total"], 1)
        self.assertEqual(report["interactions"]["likes_total"], 1)
        self.assertEqual(report["interactions"]["coupon_clicks_total"], 0)
