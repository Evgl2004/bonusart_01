from __future__ import annotations

from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from guests.models import (
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponAutomationConfig,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    Mailing,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
    Restaurant,
    VisitHistory,
    VtelemaxRecipientChannel,
)
from guests.services.coupon_autoscenarios import (
    CouponAutoscenarioPreviewError,
    _autoscenario_assignments_for_update_queryset,
    build_coupon_autoscenario_execution_plan,
    execute_coupon_autoscenario_pilot,
    preview_coupon_autoscenario_audience,
)
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON


class CouponAutoscenarioPreviewTests(TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Autoscenario preview template",
            description="",
            message_text="Ваш купон: {coupon_code}",
            created_by="tests",
            is_active=True,
        )
        self.scenario = self._prepare_scenario()
        self.config = CouponAutomationConfig.objects.create(
            scenario=self.scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="AUTO_INACTIVE_30",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            max_active_coupons_per_guest=1,
            cooldown_days=30,
        )
        self.restaurant = Restaurant.objects.create(iiko_id="DEP_1", name="Тестовое заведение")

    def _prepare_scenario(self) -> NotificationScenario:
        scenario, _ = NotificationScenario.objects.get_or_create(
            code=SCENARIO_CODE_INACTIVE_30D_COUPON,
            defaults={
                "name": "Не был 30 дней + купон",
                "description": "",
                "is_active": True,
                "is_system": True,
                "trigger_type": NotificationScenario.TriggerType.SCHEDULE,
                "template": self.template,
                "priority": NotificationScenario.Priority.BULK,
                "target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "distribution_mode": NotificationScenario.DistributionMode.IMMEDIATE,
                "timezone": "Asia/Yekaterinburg",
                "settings": {"inactive_days": 30},
            },
        )
        scenario.is_active = True
        scenario.trigger_type = NotificationScenario.TriggerType.SCHEDULE
        scenario.template = self.template
        scenario.settings = {"inactive_days": 30}
        scenario.timezone = "Asia/Yekaterinburg"
        scenario.save(
            update_fields=[
                "is_active",
                "trigger_type",
                "template",
                "settings",
                "timezone",
                "updated_at",
            ]
        )
        return scenario

    def _guest(self, *, phone: str, first_name: str) -> Guest:
        return Guest.objects.create(
            phone=phone,
            first_name=first_name,
            last_name="Тестовый",
            created_at=self.now,
            updated_at=self.now,
        )

    def _visit(self, *, guest: Guest, days_ago: int) -> None:
        VisitHistory.objects.create(
            guest=guest,
            restaurant=self.restaurant,
            visit_date=self.now - timedelta(days=days_ago),
            visit_count=1,
        )

    def _sendable_channel(self, *, guest: Guest, platform: str = VtelemaxRecipientChannel.Platform.TELEGRAM) -> None:
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid4(),
            platform=platform,
            phone_e164=guest.phone,
            external_id=f"external-{guest.id}",
            rules_accepted=True,
            notifications_allowed=True,
            is_registered=True,
            guest=guest,
        )

    def _available_coupon(self, *, code: str) -> CouponRegistryEntry:
        return CouponRegistryEntry.objects.create(
            series=self.config.coupon_series,
            code=code,
            venue_code=self.config.venue_code,
            venue_name=self.config.venue_name,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )

    def _mailing(self) -> Mailing:
        return Mailing.objects.create(
            name="Autoscenario legacy assignment holder",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now,
            scheduled_time_end=self.now + timedelta(days=14),
            is_active=False,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=self.now.time(),
            send_window_end=(self.now + timedelta(hours=2)).time(),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.BULK,
            coupon_series=self.config.coupon_series,
            coupon_venue_code=self.config.venue_code,
            coupon_venue_name=self.config.venue_name,
            coupon_promo_text="Test promo",
        )

    def _assignment(
        self,
        *,
        mailing: Mailing,
        guest: Guest,
        coupon: CouponRegistryEntry,
        status: str,
        assigned_at=None,
    ) -> CouponCampaignAssignment:
        coupon.is_active = False
        coupon.pool_status = CouponRegistryEntry.PoolStatus.ASSIGNED
        coupon.assigned_at = assigned_at or self.now
        coupon.save(update_fields=["is_active", "pool_status", "assigned_at", "updated_at"])
        return CouponCampaignAssignment.objects.create(
            campaign=mailing,
            guest=guest,
            coupon=coupon,
            phone_e164=guest.phone,
            coupon_series=coupon.series,
            coupon_code=coupon.code,
            venue_code=coupon.venue_code,
            venue_name=coupon.venue_name,
            assigned_at=assigned_at or self.now,
            lifetime_expires_at=self.now + timedelta(days=14),
            status=status,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
        )

    def test_preview_counts_audience_channels_and_coupons_without_side_effects(self):
        old_sendable = self._guest(phone="+79990000001", first_name="Можно")
        old_blocked = self._guest(phone="+79990000002", first_name="БезКанала")
        recent = self._guest(phone="+79990000003", first_name="Свежий")
        self._visit(guest=old_sendable, days_ago=45)
        self._visit(guest=old_blocked, days_ago=60)
        self._visit(guest=recent, days_ago=5)
        self._sendable_channel(guest=old_sendable)
        self._available_coupon(code="AUTO-1")

        before_counts = self._side_effect_counts()

        preview = preview_coupon_autoscenario_audience(
            scenario_code=self.scenario.code,
            now=self.now,
        )

        self.assertEqual(preview.scanned_guests, 2)
        self.assertEqual(preview.scan_limit, 5000)
        self.assertEqual(preview.matched_guests, 2)
        self.assertEqual(preview.sendable_guests, 1)
        self.assertEqual(preview.blocked_without_channel, 1)
        self.assertEqual(preview.planned_recipients_for_run, 1)
        self.assertEqual(preview.available_coupons, 1)
        self.assertEqual(preview.coupon_shortage, 0)
        self.assertEqual([row.guest_id for row in preview.sample_rows], [old_sendable.id, old_blocked.id])
        self.assertEqual([row.guest_id for row in preview.sample_sendable_rows], [old_sendable.id])
        self.assertEqual([row.guest_id for row in preview.sample_blocked_rows], [old_blocked.id])
        self.assertEqual(preview.sample_rows[0].sendable_channels, ("telegram",))
        self.assertEqual(self._side_effect_counts(), before_counts)

    def test_preview_scans_beyond_run_limit_to_find_sendable_guests(self):
        self.config.max_recipients_per_run = 1
        self.config.save(update_fields=["max_recipients_per_run", "updated_at"])
        old_blocked = self._guest(phone="+79990000004", first_name="БезКанала")
        old_sendable = self._guest(phone="+79990000005", first_name="Достижимый")
        self._visit(guest=old_blocked, days_ago=45)
        self._visit(guest=old_sendable, days_ago=45)
        self._sendable_channel(guest=old_sendable)

        preview = preview_coupon_autoscenario_audience(
            scenario_code=self.scenario.code,
            scan_limit=10,
            now=self.now,
        )

        self.assertEqual(preview.max_recipients_per_run, 1)
        self.assertEqual(preview.scan_limit, 10)
        self.assertEqual(preview.scanned_guests, 2)
        self.assertEqual(preview.matched_guests, 2)
        self.assertEqual(preview.sendable_guests, 1)
        self.assertEqual(preview.blocked_without_channel, 1)
        self.assertEqual(preview.planned_recipients_for_run, 1)
        self.assertEqual([row.guest_id for row in preview.sample_sendable_rows], [old_sendable.id])

    def test_preview_reports_coupon_shortage(self):
        old_sendable = self._guest(phone="+79990000011", first_name="Можно")
        self._visit(guest=old_sendable, days_ago=45)
        self._sendable_channel(guest=old_sendable)

        preview = preview_coupon_autoscenario_audience(
            scenario_code=self.scenario.code,
            now=self.now,
        )

        self.assertEqual(preview.available_coupons, 0)
        self.assertEqual(preview.coupon_shortage, 1)
        self.assertTrue(any("1" in warning for warning in preview.warnings))

    def test_preview_command_prints_summary(self):
        old_sendable = self._guest(phone="+79990000021", first_name="Команда")
        self._visit(guest=old_sendable, days_ago=45)
        self._sendable_channel(guest=old_sendable)
        self._available_coupon(code="AUTO-CMD")
        stdout = StringIO()

        call_command(
            "preview_coupon_autoscenario",
            "--scenario-code",
            self.scenario.code,
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn("scenario_code=inactive_30d_coupon", output)
        self.assertIn("scan_limit=5000", output)
        self.assertIn("matched_guests=1", output)
        self.assertIn("sendable_guests=1", output)
        self.assertIn("planned_recipients_for_run=1", output)
        self.assertIn("available_coupons=1", output)

    def test_preview_requires_coupon_config(self):
        self.config.delete()

        with self.assertRaises(CouponAutoscenarioPreviewError):
            preview_coupon_autoscenario_audience(scenario_code=self.scenario.code, now=self.now)

    def test_execution_plan_pairs_eligible_guests_with_coupons_without_side_effects(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 5
        self.config.cooldown_days = 30
        self.config.settings = {"pilot_phones": ["+79990000101"]}
        self.config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "cooldown_days",
                "settings",
                "updated_at",
            ]
        )
        eligible = self._guest(phone="+79990000101", first_name="Eligible")
        active = self._guest(phone="+79990000102", first_name="Active")
        cooldown = self._guest(phone="+79990000103", first_name="Cooldown")
        blocked = self._guest(phone="+79990000104", first_name="Blocked")
        for guest in [eligible, active, cooldown, blocked]:
            self._visit(guest=guest, days_ago=45)
        for guest in [eligible, active, cooldown]:
            self._sendable_channel(guest=guest)

        mailing = self._mailing()
        active_coupon = self._available_coupon(code="AUTO-ACTIVE")
        cooldown_coupon = self._available_coupon(code="AUTO-COOLDOWN")
        self._assignment(
            mailing=mailing,
            guest=active,
            coupon=active_coupon,
            status=CouponCampaignAssignment.Status.RESERVED,
        )
        self._assignment(
            mailing=mailing,
            guest=cooldown,
            coupon=cooldown_coupon,
            status=CouponCampaignAssignment.Status.USED,
            assigned_at=self.now - timedelta(days=10),
        )
        available_coupon = self._available_coupon(code="AUTO-PLAN")
        before_counts = self._side_effect_counts()

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            scan_limit=20,
            now=self.now,
        )

        self.assertTrue(plan.can_execute)
        self.assertEqual(plan.matched_guests, 4)
        self.assertEqual(plan.sendable_guests, 3)
        self.assertEqual(plan.blocked_without_channel, 1)
        self.assertEqual(plan.blocked_existing_active_coupon, 1)
        self.assertEqual(plan.blocked_by_cooldown, 1)
        self.assertEqual(plan.eligible_guests, 1)
        self.assertEqual(plan.planned_assignments, 1)
        self.assertEqual(plan.plan_items[0].guest_id, eligible.id)
        self.assertEqual(plan.plan_items[0].coupon_id, available_coupon.id)
        self.assertEqual(self._side_effect_counts(), before_counts)

    def test_execution_plan_blocks_report_only_mode(self):
        guest = self._guest(phone="+79990000111", first_name="ReportOnly")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        self._available_coupon(code="AUTO-REPORT")

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            now=self.now,
        )

        self.assertFalse(plan.can_execute)
        self.assertEqual(plan.planned_assignments, 1)
        self.assertTrue(any("Только отч" in blocker for blocker in plan.blockers))

    def test_execution_plan_uses_default_pilot_phone_when_allowlist_is_empty(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 5
        self.config.settings = {}
        self.config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "settings",
                "updated_at",
            ]
        )
        pilot_guest = self._guest(phone="+79129923438", first_name="DefaultPilot")
        other_guest = self._guest(phone="+79990000112", first_name="Other")
        for guest in [pilot_guest, other_guest]:
            self._visit(guest=guest, days_ago=45)
            self._sendable_channel(guest=guest)
        pilot_coupon = self._available_coupon(code="AUTO-DEFAULT-PILOT")
        self._available_coupon(code="AUTO-OTHER")

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            scan_limit=20,
            now=self.now,
        )

        self.assertTrue(plan.can_execute)
        self.assertTrue(plan.used_default_pilot_phone)
        self.assertEqual(plan.pilot_phone_filters, ("+79129923438",))
        self.assertEqual(plan.blocked_by_pilot_filter, 1)
        self.assertEqual(plan.eligible_guests, 1)
        self.assertEqual(plan.plan_items[0].guest_id, pilot_guest.id)
        self.assertEqual(plan.plan_items[0].coupon_id, pilot_coupon.id)

    def test_execution_plan_can_force_include_pilot_phone_outside_business_segment(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 5
        self.config.settings = {
            "pilot_phones": ["+79990000122"],
            "pilot_include_unmatched": True,
        }
        self.config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "settings",
                "updated_at",
            ]
        )
        pilot_guest = self._guest(phone="+79990000122", first_name="FreshPilot")
        old_guest = self._guest(phone="+79990000123", first_name="OldGuest")
        self._visit(guest=pilot_guest, days_ago=5)
        self._visit(guest=old_guest, days_ago=45)
        self._sendable_channel(guest=pilot_guest)
        self._sendable_channel(guest=old_guest)
        pilot_coupon = self._available_coupon(code="AUTO-FORCED-PILOT")
        self._available_coupon(code="AUTO-OLD")

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            scan_limit=20,
            now=self.now,
        )

        self.assertTrue(plan.can_execute)
        self.assertEqual(plan.pilot_phone_filters, ("+79990000122",))
        self.assertEqual(plan.pilot_forced_guests, 1)
        self.assertEqual(plan.blocked_by_pilot_filter, 1)
        self.assertEqual(plan.eligible_guests, 1)
        self.assertEqual(plan.plan_items[0].guest_id, pilot_guest.id)
        self.assertEqual(plan.plan_items[0].coupon_id, pilot_coupon.id)
        self.assertTrue(any("дополнительно включено" in warning for warning in plan.warnings))

    def test_execution_plan_allows_pilot_when_notification_scenario_is_inactive(self):
        self.scenario.is_active = False
        self.scenario.save(update_fields=["is_active", "updated_at"])
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 1
        self.config.settings = {
            "pilot_phones": ["+79990000124"],
            "pilot_include_unmatched": True,
        }
        self.config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "settings",
                "updated_at",
            ]
        )
        pilot_guest = self._guest(phone="+79990000124", first_name="InactiveScenarioPilot")
        self._visit(guest=pilot_guest, days_ago=5)
        self._sendable_channel(guest=pilot_guest)
        pilot_coupon = self._available_coupon(code="AUTO-INACTIVE-SCENARIO")

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            scan_limit=20,
            now=self.now,
        )

        self.assertTrue(plan.can_execute)
        self.assertEqual(plan.planned_assignments, 1)
        self.assertEqual(plan.plan_items[0].guest_id, pilot_guest.id)
        self.assertEqual(plan.plan_items[0].coupon_id, pilot_coupon.id)
        self.assertTrue(
            any("старого планировщика" in warning for warning in plan.warnings)
        )

    def test_plan_command_prints_safe_summary(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.settings = {"pilot_phones": ["+79990000121"]}
        self.config.save(update_fields=["execution_mode", "settings", "updated_at"])
        guest = self._guest(phone="+79990000121", first_name="CommandPlan")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        self._available_coupon(code="AUTO-CMD-PLAN")
        stdout = StringIO()

        call_command(
            "plan_coupon_autoscenario",
            "--scenario-code",
            self.scenario.code,
            "--scan-limit",
            "20",
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn("scenario_code=inactive_30d_coupon", output)
        self.assertIn("can_execute=True", output)
        self.assertIn("planned_assignments=1", output)
        self.assertIn("AUTO-CMD-PLAN", output)

    def test_execute_pilot_dry_run_has_no_side_effects(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 1
        self.config.settings = {"pilot_phones": ["+79990000131"]}
        self.config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "settings",
                "updated_at",
            ]
        )
        guest = self._guest(phone="+79990000131", first_name="DryRun")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        self._available_coupon(code="AUTO-DRY")
        before_counts = self._side_effect_counts()

        result = execute_coupon_autoscenario_pilot(
            scenario_code=self.scenario.code,
            scan_limit=20,
            confirm=False,
            now=self.now,
        )

        self.assertTrue(result.dry_run)
        self.assertFalse(result.confirmed)
        self.assertIsNone(result.run_id)
        self.assertEqual(result.plan.planned_assignments, 1)
        self.assertEqual(result.created_assignments, 0)
        self.assertEqual(result.queue_events_created, 0)
        self.assertEqual(self._side_effect_counts(), before_counts)

    def test_execute_pilot_confirm_reserves_coupon_and_queues_vtelemax_without_messages(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 1
        self.config.settings = {"pilot_phones": ["+79990000141"]}
        self.config.coupon_promo_text_template = (
            "Купон {coupon_code} для {first_name}. Действует до {coupon_expires_at}."
        )
        self.config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "coupon_promo_text_template",
                "settings",
                "updated_at",
            ]
        )
        guest = self._guest(phone="+79990000141", first_name="Pilot")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest, platform=VtelemaxRecipientChannel.Platform.MAX)
        coupon = self._available_coupon(code="AUTO-PILOT")

        result = execute_coupon_autoscenario_pilot(
            scenario_code=self.scenario.code,
            scan_limit=20,
            confirm=True,
            now=self.now,
        )

        self.assertFalse(result.dry_run)
        self.assertTrue(result.confirmed)
        self.assertIsNotNone(result.run_id)
        self.assertEqual(result.created_assignments, 1)
        self.assertEqual(result.queue_events_created, 1)

        run = CouponAutoscenarioRun.objects.get(id=result.run_id)
        self.assertEqual(run.status, CouponAutoscenarioRun.Status.SYNC_PENDING)
        self.assertEqual(run.scenario_id, self.scenario.id)
        self.assertEqual(run.created_assignments, 1)
        self.assertEqual(run.queue_events_created, 1)

        assignment = CouponAutoscenarioAssignment.objects.get(run=run)
        self.assertEqual(assignment.guest_id, guest.id)
        self.assertEqual(assignment.coupon_id, coupon.id)
        self.assertEqual(assignment.coupon_code, "AUTO-PILOT")
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.RESERVED)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )
        self.assertIn("AUTO-PILOT", assignment.promo_text)
        self.assertIn("Pilot", assignment.promo_text)

        coupon.refresh_from_db()
        self.assertFalse(coupon.is_active)
        self.assertEqual(coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)
        self.assertIsNotNone(coupon.assigned_at)

        queue_event = CouponVtelemaxSyncQueue.objects.get(autoscenario_assignment=assignment)
        self.assertIsNone(queue_event.assignment_id)
        self.assertEqual(queue_event.direction, CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS)
        self.assertEqual(queue_event.status, CouponVtelemaxSyncQueue.Status.PENDING)
        self.assertEqual(queue_event.payload_json["source"], "autoscenario")
        self.assertEqual(queue_event.payload_json["scenario_code"], self.scenario.code)
        self.assertEqual(queue_event.payload_json["assignment_id"], assignment.id)
        self.assertEqual(queue_event.payload_json["coupon_code"], "AUTO-PILOT")
        self.assertIn("valid_until", queue_event.payload_json)

        self.assertEqual(CouponCampaignAssignment.objects.count(), 0)
        self.assertEqual(NotificationEvent.objects.count(), 0)
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_autoscenario_assignment_lock_queryset_locks_only_assignment_table(self):
        queryset = (
            _autoscenario_assignments_for_update_queryset()
            .select_related("guest", "scenario", "scenario__template", "coupon", "run", "config")
            .filter(id=1)
        )

        self.assertEqual(queryset.query.select_for_update_of, ("self",))

    def test_execute_pilot_confirm_requires_pilot_mode(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.REPORT_ONLY
        self.config.save(update_fields=["execution_mode", "updated_at"])
        guest = self._guest(phone="+79990000151", first_name="BlockedMode")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        self._available_coupon(code="AUTO-BLOCKED-MODE")

        with self.assertRaises(CouponAutoscenarioPreviewError):
            execute_coupon_autoscenario_pilot(
                scenario_code=self.scenario.code,
                scan_limit=20,
                confirm=True,
                now=self.now,
            )

        self.assertEqual(CouponAutoscenarioRun.objects.count(), 0)
        self.assertEqual(CouponAutoscenarioAssignment.objects.count(), 0)
        self.assertEqual(CouponVtelemaxSyncQueue.objects.count(), 0)

    def test_execute_pilot_command_dry_run_prints_no_side_effects(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.settings = {"pilot_phones": ["+79990000161"]}
        self.config.save(update_fields=["execution_mode", "settings", "updated_at"])
        guest = self._guest(phone="+79990000161", first_name="CommandDryRun")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        self._available_coupon(code="AUTO-CMD-DRY")
        before_counts = self._side_effect_counts()
        stdout = StringIO()

        call_command(
            "execute_coupon_autoscenario_pilot",
            "--scenario-code",
            self.scenario.code,
            "--scan-limit",
            "20",
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn("dry_run=True", output)
        self.assertIn("created_assignments=0", output)
        self.assertIn("queue_events_created=0", output)
        self.assertIn("guest_messages_created=0", output)
        self.assertEqual(self._side_effect_counts(), before_counts)

    @staticmethod
    def _side_effect_counts() -> dict[str, int]:
        return {
            "autoscenario_runs": CouponAutoscenarioRun.objects.count(),
            "autoscenario_assignments": CouponAutoscenarioAssignment.objects.count(),
            "notification_events": NotificationEvent.objects.count(),
            "dispatch_tasks": DispatchTask.objects.count(),
            "coupon_assignments": CouponCampaignAssignment.objects.count(),
            "coupon_queue": CouponVtelemaxSyncQueue.objects.count(),
        }
