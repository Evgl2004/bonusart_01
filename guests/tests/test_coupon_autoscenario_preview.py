from __future__ import annotations

from datetime import date, datetime, timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from guests.models import (
    BotProfile,
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponAutomationConfig,
    CouponAutomationRule,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    GuestBotBinding,
    GuestRestaurantDailyOrderFact,
    Mailing,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
    OrderFact,
    Restaurant,
    VtelemaxRecipientChannel,
)
from guests.services.coupon_autoscenarios import (
    CouponAutoscenarioPreviewError,
    _autoscenario_assignments_for_update_queryset,
    build_coupon_autoscenario_execution_plan,
    cleanup_coupon_autoscenario_pilot_assignment,
    create_autoscenario_dispatch_after_vtelemax_ack,
    execute_coupon_autoscenario_automatic,
    execute_coupon_autoscenario_pilot,
    preview_coupon_autoscenario_audience,
)
from guests.services.notification_handler_registry import get_registered_schedule_scenario_codes
from guests.services.notification_registry import (
    SCENARIO_CODE_BIRTHDAY_COUPON,
    SCENARIO_CODE_INACTIVE_30D_COUPON,
)
from guests.tasks import run_coupon_autoscenarios_task


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
        self.bots_by_platform = {
            VtelemaxRecipientChannel.Platform.TELEGRAM: BotProfile.objects.create(
                code="test_coupon_autoscenario_telegram",
                name="Telegram test bot",
                provider_type=BotProfile.ProviderType.TELEGRAM,
                is_active=True,
            ),
            VtelemaxRecipientChannel.Platform.MAX: BotProfile.objects.create(
                code="test_coupon_autoscenario_max",
                name="MAX test bot",
                provider_type=BotProfile.ProviderType.MAX,
                is_active=True,
            ),
            VtelemaxRecipientChannel.Platform.VK: BotProfile.objects.create(
                code="test_coupon_autoscenario_vk",
                name="VK test bot",
                provider_type=BotProfile.ProviderType.VK,
                is_active=True,
            ),
        }
        self.scenario.bot_profiles.set(list(self.bots_by_platform.values()))

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

    def _prepare_birthday_autoscenario(self) -> tuple[NotificationScenario, CouponAutomationConfig]:
        scenario, _ = NotificationScenario.objects.get_or_create(
            code=SCENARIO_CODE_BIRTHDAY_COUPON,
            defaults={
                "name": "День рождения + купон",
                "description": "",
                "is_active": True,
                "is_system": True,
                "trigger_type": NotificationScenario.TriggerType.SCHEDULE,
                "template": self.template,
                "priority": NotificationScenario.Priority.BULK,
                "target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "distribution_mode": NotificationScenario.DistributionMode.IMMEDIATE,
                "timezone": "Asia/Yekaterinburg",
                "settings": {"coupon_required": True},
            },
        )
        scenario.is_active = True
        scenario.trigger_type = NotificationScenario.TriggerType.SCHEDULE
        scenario.template = self.template
        scenario.settings = {"coupon_required": True}
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
        config, _ = CouponAutomationConfig.objects.get_or_create(
            scenario=scenario,
            defaults={
                "execution_mode": CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
                "coupon_series": "AUTO_BIRTHDAY",
                "venue_code": "__global__",
                "venue_name": "Вся сеть",
                "coupon_validity_days": 14,
                "max_recipients_per_run": 10,
                "max_active_coupons_per_guest": 1,
                "cooldown_days": 365,
                "settings": {"birthday_preparation_window_days": 7},
            },
        )
        config.execution_mode = CouponAutomationConfig.ExecutionMode.REPORT_ONLY
        config.coupon_series = "AUTO_BIRTHDAY"
        config.venue_code = "__global__"
        config.venue_name = "Вся сеть"
        config.coupon_validity_days = 14
        config.max_recipients_per_run = 10
        config.max_active_coupons_per_guest = 1
        config.cooldown_days = 365
        config.settings = {"birthday_preparation_window_days": 7}
        config.save(
            update_fields=[
                "execution_mode",
                "coupon_series",
                "venue_code",
                "venue_name",
                "coupon_validity_days",
                "max_recipients_per_run",
                "max_active_coupons_per_guest",
                "cooldown_days",
                "settings",
                "updated_at",
            ]
        )
        scenario.bot_profiles.set(list(self.bots_by_platform.values()))
        return scenario, config

    def _guest(self, *, phone: str, first_name: str, birthdate: date | None = None) -> Guest:
        return Guest.objects.create(
            phone=phone,
            first_name=first_name,
            last_name="Тестовый",
            birthdate=birthdate,
            created_at=self.now,
            updated_at=self.now,
        )

    def _visit(self, *, guest: Guest, days_ago: int) -> None:
        sequence = OrderFact.objects.count() + 1
        OrderFact.objects.create(
            guest=guest,
            business_date=(self.now - timedelta(days=days_ago)).date(),
            department_id=self.restaurant.iiko_id,
            department_name=self.restaurant.name,
            order_number=100000 + sequence,
            uniq_order_id=f"autoscenario-visit-{guest.id}-{sequence}",
            first_seen_at=self.now - timedelta(days=days_ago),
        )

    def _sendable_channel(
        self,
        *,
        guest: Guest,
        platform: str = VtelemaxRecipientChannel.Platform.TELEGRAM,
        with_binding: bool = True,
        binding_opt_in: bool = True,
        binding_stop_sending: bool = False,
    ) -> None:
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
        if not with_binding:
            return

        bot = self.bots_by_platform[platform]
        GuestBotBinding.objects.get_or_create(
            guest=guest,
            bot=bot,
            defaults={
                "external_chat_id": f"chat-{platform}-{guest.id}",
                "is_primary": not GuestBotBinding.objects.filter(guest=guest, is_primary=True).exists(),
                "is_active": True,
                "is_opt_in": binding_opt_in,
                "is_stop_sending": binding_stop_sending,
            },
        )

    def _available_coupon(self, *, code: str) -> CouponRegistryEntry:
        return self._available_coupon_for_config(config=self.config, code=code)

    def _available_coupon_for_config(self, *, config: CouponAutomationConfig, code: str) -> CouponRegistryEntry:
        return CouponRegistryEntry.objects.create(
            series=config.coupon_series,
            code=code,
            venue_code=config.venue_code,
            venue_name=config.venue_name,
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

    def _daily_guest_venue_fact(
        self,
        *,
        guest: Guest,
        venue_code: str,
        days_ago: int,
        orders_count: int,
    ) -> None:
        GuestRestaurantDailyOrderFact.objects.create(
            guest=guest,
            business_date=(self.now - timedelta(days=days_ago)).date(),
            department_id=venue_code,
            orders_count=orders_count,
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
        self.assertEqual(plan.bot_bound_guests, 3)
        self.assertEqual(plan.blocked_without_bot_binding, 1)
        self.assertEqual(plan.sendable_guests, 3)
        self.assertEqual(plan.blocked_without_channel, 1)
        self.assertEqual(plan.message_target_guests, 3)
        self.assertEqual(plan.blocked_without_message_permission, 0)
        self.assertEqual(plan.blocked_existing_active_coupon, 1)
        self.assertEqual(plan.blocked_by_cooldown, 1)
        self.assertEqual(plan.eligible_guests, 1)
        self.assertEqual(plan.planned_assignments, 1)
        self.assertEqual(plan.plan_items[0].guest_id, eligible.id)
        self.assertEqual(plan.plan_items[0].coupon_id, available_coupon.id)
        self.assertEqual(self._side_effect_counts(), before_counts)

    def test_execution_plan_requires_message_target_before_coupon_assignment(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.settings = {"pilot_phones": ["+79990000109"]}
        self.config.save(update_fields=["execution_mode", "settings", "updated_at"])
        guest = self._guest(phone="+79990000109", first_name="NoBinding")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest, with_binding=False)
        self._available_coupon(code="AUTO-NO-BINDING")

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            scan_limit=20,
            now=self.now,
        )

        self.assertFalse(plan.can_execute)
        self.assertEqual(plan.bot_bound_guests, 0)
        self.assertEqual(plan.blocked_without_bot_binding, 1)
        self.assertEqual(plan.sendable_guests, 1)
        self.assertEqual(plan.message_target_guests, 0)
        self.assertEqual(plan.blocked_without_message_target, 1)
        self.assertEqual(plan.blocked_without_message_permission, 0)
        self.assertEqual(plan.eligible_guests, 0)
        self.assertEqual(plan.planned_assignments, 0)

    def test_execution_plan_requires_selected_notification_bot(self):
        self.scenario.bot_profiles.clear()
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.settings = {"pilot_phones": ["+79990000110"]}
        self.config.save(update_fields=["execution_mode", "settings", "updated_at"])
        guest = self._guest(phone="+79990000110", first_name="NoSelectedBot")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        self._available_coupon(code="AUTO-NO-SELECTED-BOT")

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            scan_limit=20,
            now=self.now,
        )

        self.assertFalse(plan.can_execute)
        self.assertEqual(plan.matched_guests, 1)
        self.assertEqual(plan.bot_bound_guests, 0)
        self.assertEqual(plan.blocked_without_bot_binding, 1)
        self.assertEqual(plan.sendable_guests, 1)
        self.assertEqual(plan.message_target_guests, 0)
        self.assertEqual(plan.blocked_without_message_target, 1)
        self.assertEqual(plan.blocked_without_message_permission, 0)
        self.assertEqual(plan.eligible_guests, 0)
        self.assertEqual(plan.planned_assignments, 0)

    def test_execution_plan_separates_bot_binding_from_message_permission(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.settings = {"pilot_phones": ["+79990000111", "+79990000112"]}
        self.config.max_recipients_per_run = 10
        self.config.save(
            update_fields=[
                "execution_mode",
                "settings",
                "max_recipients_per_run",
                "updated_at",
            ]
        )
        allowed = self._guest(phone="+79990000111", first_name="Allowed")
        blocked = self._guest(phone="+79990000112", first_name="Blocked")
        for guest in [allowed, blocked]:
            self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=allowed)
        self._sendable_channel(guest=blocked, binding_opt_in=False)
        coupon = self._available_coupon(code="AUTO-ALLOWED")

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            scan_limit=20,
            now=self.now,
        )

        self.assertTrue(plan.can_execute)
        self.assertEqual(plan.matched_guests, 2)
        self.assertEqual(plan.bot_bound_guests, 2)
        self.assertEqual(plan.blocked_without_bot_binding, 0)
        self.assertEqual(plan.sendable_guests, 2)
        self.assertEqual(plan.message_target_guests, 1)
        self.assertEqual(plan.blocked_without_message_target, 1)
        self.assertEqual(plan.blocked_without_message_permission, 1)
        self.assertEqual(plan.eligible_guests, 1)
        self.assertEqual(plan.planned_assignments, 1)
        self.assertEqual(plan.plan_items[0].guest_id, allowed.id)
        self.assertEqual(plan.plan_items[0].coupon_id, coupon.id)

    def test_execution_plan_selects_coupon_rule_by_latest_order_fact_with_global_fallback(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.AUTOMATIC
        self.config.coupon_series = ""
        self.config.venue_code = ""
        self.config.venue_name = ""
        self.config.max_recipients_per_run = 10
        self.config.save(
            update_fields=[
                "execution_mode",
                "coupon_series",
                "venue_code",
                "venue_name",
                "max_recipients_per_run",
                "updated_at",
            ]
        )
        CouponAutomationRule.objects.create(
            config=self.config,
            scope_type=CouponAutomationRule.ScopeType.VENUE,
            coupon_series="AUTO_DEP_1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            priority=10,
        )
        CouponAutomationRule.objects.create(
            config=self.config,
            scope_type=CouponAutomationRule.ScopeType.GLOBAL,
            coupon_series="AUTO_GLOBAL",
            venue_code="__global__",
            venue_name="Вся сеть",
            priority=100,
        )
        dep_guest = self._guest(phone="+79990000121", first_name="Dep")
        global_guest = self._guest(phone="+79990000122", first_name="Global")
        for guest in [dep_guest, global_guest]:
            self._visit(guest=guest, days_ago=45)
            self._sendable_channel(guest=guest)

        OrderFact.objects.create(
            guest=dep_guest,
            business_date=(self.now - timedelta(days=45)).date(),
            department_id="DEP_OTHER",
            department_name="Другое заведение",
            order_number=1,
            uniq_order_id="dep-old",
            first_seen_at=self.now - timedelta(days=45),
        )
        OrderFact.objects.create(
            guest=dep_guest,
            business_date=(self.now - timedelta(days=40)).date(),
            department_id="DEP_1",
            department_name="Тестовое заведение",
            order_number=2,
            uniq_order_id="dep-latest",
            first_seen_at=self.now - timedelta(days=40),
        )
        OrderFact.objects.create(
            guest=global_guest,
            business_date=(self.now - timedelta(days=40)).date(),
            department_id="DEP_OTHER",
            department_name="Другое заведение",
            order_number=3,
            uniq_order_id="global-latest",
            first_seen_at=self.now - timedelta(days=40),
        )
        venue_coupon = CouponRegistryEntry.objects.create(
            series="AUTO_DEP_1",
            code="DEP-COUPON",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )
        global_coupon = CouponRegistryEntry.objects.create(
            series="AUTO_GLOBAL",
            code="GLOBAL-COUPON",
            venue_code="__global__",
            venue_name="Вся сеть",
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )

        before_counts = self._side_effect_counts()

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            scan_limit=20,
            now=self.now,
        )

        items_by_guest = {item.guest_id: item for item in plan.plan_items}
        self.assertTrue(plan.can_execute)
        self.assertEqual(plan.planned_assignments, 2)
        self.assertEqual(items_by_guest[dep_guest.id].coupon_id, venue_coupon.id)
        self.assertEqual(items_by_guest[dep_guest.id].coupon_selection_source, "last_order_department")
        self.assertEqual(items_by_guest[dep_guest.id].last_order_department_id, "DEP_1")
        self.assertEqual(items_by_guest[global_guest.id].coupon_id, global_coupon.id)
        self.assertEqual(items_by_guest[global_guest.id].coupon_selection_source, "global_fallback")
        self.assertEqual(self._side_effect_counts(), before_counts)

    def test_execution_plan_selects_all_visited_venue_rules_for_one_guest(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.AUTOMATIC
        self.config.venue_selection_mode = CouponAutomationConfig.VenueSelectionMode.ALL_VISITED
        self.config.coupon_series = ""
        self.config.venue_code = ""
        self.config.venue_name = ""
        self.config.max_recipients_per_run = 10
        self.config.save(
            update_fields=[
                "execution_mode",
                "venue_selection_mode",
                "coupon_series",
                "venue_code",
                "venue_name",
                "max_recipients_per_run",
                "updated_at",
            ]
        )
        CouponAutomationRule.objects.create(
            config=self.config,
            scope_type=CouponAutomationRule.ScopeType.VENUE,
            coupon_series="AUTO_DEP_A",
            venue_code="DEP_A",
            venue_name="Заведение А",
            priority=10,
        )
        CouponAutomationRule.objects.create(
            config=self.config,
            scope_type=CouponAutomationRule.ScopeType.VENUE,
            coupon_series="AUTO_DEP_B",
            venue_code="DEP_B",
            venue_name="Заведение Б",
            priority=20,
        )
        guest = self._guest(phone="+79990000131", first_name="MultiVenue")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        self._daily_guest_venue_fact(guest=guest, venue_code="DEP_A", days_ago=90, orders_count=1)
        self._daily_guest_venue_fact(guest=guest, venue_code="DEP_B", days_ago=70, orders_count=2)
        coupon_a = CouponRegistryEntry.objects.create(
            series="AUTO_DEP_A",
            code="DEP-A-COUPON",
            venue_code="DEP_A",
            venue_name="Заведение А",
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )
        coupon_b = CouponRegistryEntry.objects.create(
            series="AUTO_DEP_B",
            code="DEP-B-COUPON",
            venue_code="DEP_B",
            venue_name="Заведение Б",
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            scan_limit=20,
            now=self.now,
        )

        self.assertTrue(plan.can_execute)
        self.assertEqual(plan.venue_selection_mode, CouponAutomationConfig.VenueSelectionMode.ALL_VISITED)
        self.assertEqual(plan.eligible_guests, 1)
        self.assertEqual(plan.planned_assignments, 2)
        self.assertEqual({item.coupon_id for item in plan.plan_items}, {coupon_a.id, coupon_b.id})
        self.assertEqual(
            {item.coupon_selection_source for item in plan.plan_items},
            {"visited_department"},
        )

    def test_execution_plan_selects_favorite_venue_rule_by_orders_count(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.AUTOMATIC
        self.config.venue_selection_mode = CouponAutomationConfig.VenueSelectionMode.FAVORITE
        self.config.coupon_series = ""
        self.config.venue_code = ""
        self.config.venue_name = ""
        self.config.max_recipients_per_run = 10
        self.config.save(
            update_fields=[
                "execution_mode",
                "venue_selection_mode",
                "coupon_series",
                "venue_code",
                "venue_name",
                "max_recipients_per_run",
                "updated_at",
            ]
        )
        CouponAutomationRule.objects.create(
            config=self.config,
            scope_type=CouponAutomationRule.ScopeType.VENUE,
            coupon_series="AUTO_FAV_A",
            venue_code="DEP_FAV_A",
            venue_name="Редкое заведение",
            priority=10,
        )
        CouponAutomationRule.objects.create(
            config=self.config,
            scope_type=CouponAutomationRule.ScopeType.VENUE,
            coupon_series="AUTO_FAV_B",
            venue_code="DEP_FAV_B",
            venue_name="Любимое заведение",
            priority=20,
        )
        guest = self._guest(phone="+79990000132", first_name="Favorite")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        self._daily_guest_venue_fact(guest=guest, venue_code="DEP_FAV_A", days_ago=80, orders_count=1)
        self._daily_guest_venue_fact(guest=guest, venue_code="DEP_FAV_B", days_ago=60, orders_count=4)
        CouponRegistryEntry.objects.create(
            series="AUTO_FAV_A",
            code="FAV-A-COUPON",
            venue_code="DEP_FAV_A",
            venue_name="Редкое заведение",
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )
        favorite_coupon = CouponRegistryEntry.objects.create(
            series="AUTO_FAV_B",
            code="FAV-B-COUPON",
            venue_code="DEP_FAV_B",
            venue_name="Любимое заведение",
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=self.scenario.code,
            scan_limit=20,
            now=self.now,
        )

        self.assertTrue(plan.can_execute)
        self.assertEqual(plan.venue_selection_mode, CouponAutomationConfig.VenueSelectionMode.FAVORITE)
        self.assertEqual(plan.planned_assignments, 1)
        self.assertEqual(plan.plan_items[0].coupon_id, favorite_coupon.id)
        self.assertEqual(plan.plan_items[0].coupon_selection_source, "favorite_department")
        self.assertEqual(plan.plan_items[0].venue_code, "DEP_FAV_B")

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
        self.assertTrue(any("Черновик" in blocker for blocker in plan.blockers))

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
            any("только через явный запуск" in warning for warning in plan.warnings)
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

    def test_inactive_30d_coupon_is_not_in_legacy_schedule_registry(self):
        self.assertNotIn(
            SCENARIO_CODE_INACTIVE_30D_COUPON,
            get_registered_schedule_scenario_codes(),
        )

    def test_execute_automatic_inactive_30d_reserves_coupon_and_queues_vtelemax(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.AUTOMATIC
        self.config.max_recipients_per_run = 1
        self.config.save(
            update_fields=["execution_mode", "max_recipients_per_run", "updated_at"]
        )
        guest = self._guest(phone="+79990000145", first_name="AutoInactive")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        coupon = self._available_coupon(code="AUTO-INACTIVE")

        result = execute_coupon_autoscenario_automatic(
            scenario_code=self.scenario.code,
            scan_limit=20,
            confirm=True,
            now=self.now,
        )

        self.assertFalse(result.dry_run)
        self.assertTrue(result.confirmed)
        self.assertEqual(result.created_assignments, 1)
        self.assertEqual(result.queue_events_created, 1)

        run = CouponAutoscenarioRun.objects.get(id=result.run_id)
        self.assertEqual(run.execution_mode, CouponAutomationConfig.ExecutionMode.AUTOMATIC)
        self.assertEqual(run.status, CouponAutoscenarioRun.Status.SYNC_PENDING)

        assignment = CouponAutoscenarioAssignment.objects.get(run=run)
        self.assertEqual(assignment.guest_id, guest.id)
        self.assertEqual(assignment.coupon_id, coupon.id)
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.RESERVED)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )

        coupon.refresh_from_db()
        self.assertFalse(coupon.is_active)
        self.assertEqual(coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)

        queue_event = CouponVtelemaxSyncQueue.objects.get(autoscenario_assignment=assignment)
        self.assertEqual(queue_event.direction, CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS)
        self.assertEqual(queue_event.status, CouponVtelemaxSyncQueue.Status.PENDING)
        self.assertEqual(NotificationEvent.objects.count(), 0)
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_execute_automatic_birthday_reserves_coupon_and_queues_vtelemax(self):
        birthday_scenario, birthday_config = self._prepare_birthday_autoscenario()
        birthday_config.execution_mode = CouponAutomationConfig.ExecutionMode.AUTOMATIC
        birthday_config.max_recipients_per_run = 1
        birthday_config.cooldown_days = 0
        birthday_config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "cooldown_days",
                "updated_at",
            ]
        )
        current_now = timezone.make_aware(datetime(2026, 6, 10, 10, 0))
        guest = self._guest(
            phone="+79990000146",
            first_name="BirthdayAuto",
            birthdate=date(1991, 6, 17),
        )
        self._sendable_channel(guest=guest)
        coupon = self._available_coupon_for_config(config=birthday_config, code="BDAY-AUTO")

        result = execute_coupon_autoscenario_automatic(
            scenario_code=birthday_scenario.code,
            scan_limit=20,
            confirm=True,
            now=current_now,
        )

        self.assertFalse(result.dry_run)
        self.assertTrue(result.confirmed)
        self.assertEqual(result.created_assignments, 1)
        self.assertEqual(result.queue_events_created, 1)
        self.assertEqual(result.plan.plan_items[0].days_until_birthday, 7)
        self.assertEqual(result.plan.plan_items[0].trigger_key, "birthday:2026")

        run = CouponAutoscenarioRun.objects.get(id=result.run_id)
        self.assertEqual(run.execution_mode, CouponAutomationConfig.ExecutionMode.AUTOMATIC)

        assignment = CouponAutoscenarioAssignment.objects.get(run=run)
        self.assertEqual(assignment.guest_id, guest.id)
        self.assertEqual(assignment.coupon_id, coupon.id)
        self.assertEqual(assignment.trigger_key, "birthday:2026")
        self.assertEqual(assignment.trigger_date, date(2026, 6, 17))
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.RESERVED)

        queue_event = CouponVtelemaxSyncQueue.objects.get(autoscenario_assignment=assignment)
        self.assertEqual(queue_event.payload_json["scenario_code"], birthday_scenario.code)
        self.assertEqual(queue_event.payload_json["days_until_birthday"], 7)
        self.assertEqual(NotificationEvent.objects.count(), 0)
        self.assertEqual(DispatchTask.objects.count(), 0)

    @override_settings(
        COUPON_AUTOSCENARIO_SCHEDULE_ENABLED=True,
        COUPON_AUTOSCENARIO_SCHEDULE_CODES={SCENARIO_CODE_BIRTHDAY_COUPON},
        COUPON_AUTOSCENARIO_SCHEDULE_LIMIT=1,
        COUPON_AUTOSCENARIO_SCHEDULE_SCAN_LIMIT=20,
    )
    def test_schedule_task_runs_automatic_birthday_autoscenario(self):
        birthday_scenario, birthday_config = self._prepare_birthday_autoscenario()
        birthday_config.execution_mode = CouponAutomationConfig.ExecutionMode.AUTOMATIC
        birthday_config.max_recipients_per_run = 10
        birthday_config.cooldown_days = 0
        birthday_config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "cooldown_days",
                "updated_at",
            ]
        )
        current_now = timezone.localdate()
        birthday_date = current_now + timedelta(days=1)
        guest = self._guest(
            phone="+79990000147",
            first_name="ScheduleBirthday",
            birthdate=date(1991, birthday_date.month, birthday_date.day),
        )
        self._sendable_channel(guest=guest)
        self._available_coupon_for_config(config=birthday_config, code="BDAY-SCHEDULE")

        created_assignments = run_coupon_autoscenarios_task()

        self.assertEqual(created_assignments, 1)
        run = CouponAutoscenarioRun.objects.get(scenario=birthday_scenario)
        self.assertEqual(run.execution_mode, CouponAutomationConfig.ExecutionMode.AUTOMATIC)
        assignment = CouponAutoscenarioAssignment.objects.get(run=run)
        self.assertEqual(assignment.guest_id, guest.id)
        self.assertEqual(assignment.trigger_key, f"birthday:{current_now.year}")
        self.assertEqual(CouponVtelemaxSyncQueue.objects.filter(autoscenario_assignment=assignment).count(), 1)
        self.assertEqual(NotificationEvent.objects.count(), 0)
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_execute_automatic_requires_active_execution_mode(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.save(update_fields=["execution_mode", "updated_at"])

        with self.assertRaisesMessage(CouponAutoscenarioPreviewError, "Активен"):
            execute_coupon_autoscenario_automatic(
                scenario_code=self.scenario.code,
                scan_limit=20,
                confirm=True,
                now=self.now,
            )

    def test_cleanup_pilot_assignment_creates_canceled_release_event(self):
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 1
        self.config.settings = {"pilot_phones": ["+79990000143"]}
        self.config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "settings",
                "updated_at",
            ]
        )
        guest = self._guest(phone="+79990000143", first_name="CleanupPilot")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        coupon = self._available_coupon(code="AUTO-CLEAN")
        result = execute_coupon_autoscenario_pilot(
            scenario_code=self.scenario.code,
            scan_limit=20,
            confirm=True,
            now=self.now,
        )
        assignment = CouponAutoscenarioAssignment.objects.get(run_id=result.run_id)
        assignment.vtelemax_sync_status = CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK
        assignment.vtelemax_synced_at = self.now
        assignment.save(
            update_fields=["vtelemax_sync_status", "vtelemax_synced_at", "updated_at"]
        )

        cleanup_result = cleanup_coupon_autoscenario_pilot_assignment(
            assignment_id=assignment.id,
            reason="test_cleanup",
            now=self.now + timedelta(minutes=5),
        )

        self.assertEqual(cleanup_result.assignment_id, assignment.id)
        self.assertTrue(cleanup_result.queue_event_created)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponAutoscenarioAssignment.Status.CANCELED)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )

        coupon.refresh_from_db()
        self.assertFalse(coupon.is_active)
        self.assertEqual(coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)

        event = CouponVtelemaxSyncQueue.objects.get(id=cleanup_result.queue_event_id)
        self.assertEqual(event.direction, CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE)
        self.assertEqual(event.status, CouponVtelemaxSyncQueue.Status.PENDING)
        self.assertEqual(event.autoscenario_assignment_id, assignment.id)
        self.assertEqual(event.payload_json["source"], "autoscenario")
        self.assertEqual(event.payload_json["autoscenario_assignment_id"], assignment.id)
        self.assertEqual(event.payload_json["status"], CouponAutoscenarioAssignment.Status.CANCELED)
        self.assertEqual(event.payload_json["meta"]["release_to_pool"], True)
        self.assertEqual(event.payload_json["meta"]["remove_from_guest"], True)
        self.assertEqual(event.payload_json["meta"]["cancel_reason"], "test_cleanup")

        repeated = cleanup_coupon_autoscenario_pilot_assignment(
            assignment_id=assignment.id,
            reason="test_cleanup",
            now=self.now + timedelta(minutes=6),
        )
        self.assertFalse(repeated.queue_event_created)
        self.assertEqual(repeated.queue_event_id, event.id)
        self.assertEqual(
            CouponVtelemaxSyncQueue.objects.filter(
                autoscenario_assignment=assignment,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            ).count(),
            1,
        )

    def test_autoscenario_assignment_lock_queryset_locks_only_assignment_table(self):
        queryset = (
            _autoscenario_assignments_for_update_queryset()
            .select_related("guest", "scenario", "scenario__template", "coupon", "run", "config")
            .filter(id=1)
        )

        self.assertEqual(queryset.query.select_for_update_of, ("self",))

    def test_pilot_dispatch_after_ack_is_available_immediately_even_for_uniform_scenario(self):
        self.template.message_text = "Мы давно не виделись ({{ days_without_visits }} дней). Купон {coupon_code}"
        self.template.save(update_fields=["message_text", "updated_at"])
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 1
        self.config.settings = {"pilot_phones": ["+79990000145"]}
        self.config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "settings",
                "updated_at",
            ]
        )
        now = self.now + timedelta(minutes=10)
        self.scenario.distribution_mode = NotificationScenario.DistributionMode.UNIFORM
        self.scenario.timezone = "UTC"
        self.scenario.send_window_begin = (now + timedelta(hours=1)).time().replace(microsecond=0)
        self.scenario.send_window_end = (now + timedelta(hours=2)).time().replace(microsecond=0)
        self.scenario.save(
            update_fields=[
                "distribution_mode",
                "timezone",
                "send_window_begin",
                "send_window_end",
                "updated_at",
            ]
        )
        guest = self._guest(phone="+79990000145", first_name="ImmediatePilot")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest, with_binding=False)
        bot = BotProfile.objects.create(
            code="tg_autoscenario_pilot_immediate",
            name="Telegram autoscenario pilot immediate",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.scenario.bot_profiles.add(bot)
        GuestBotBinding.objects.create(
            guest=guest,
            bot=bot,
            external_chat_id="pilot-chat-145",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        self._available_coupon(code="AUTO-PILOT-NOW")
        result = execute_coupon_autoscenario_pilot(
            scenario_code=self.scenario.code,
            scan_limit=20,
            confirm=True,
            now=self.now,
        )
        assignment = CouponAutoscenarioAssignment.objects.get(run_id=result.run_id)
        ack_time = self.now + timedelta(minutes=5)
        assignment.vtelemax_sync_status = CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK
        assignment.vtelemax_synced_at = ack_time
        assignment.save(update_fields=["vtelemax_sync_status", "vtelemax_synced_at", "updated_at"])

        created_tasks = create_autoscenario_dispatch_after_vtelemax_ack(
            assignment_id=assignment.id,
            now=ack_time,
        )

        self.assertEqual(created_tasks, 1)
        event = NotificationEvent.objects.get(source_ref=f"coupon_autoscenario_assignment:{assignment.id}")
        task = DispatchTask.objects.get(notification_event=event)
        self.assertEqual(event.planned_send_at, ack_time)
        self.assertEqual(task.available_at, ack_time)
        self.assertEqual(task.scheduled_at, ack_time)
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertIn("45 дней", task.message_text)
        self.assertNotIn("days_without_visits", task.message_text)

    def test_autoscenario_dispatch_after_ack_uses_guest_name_from_card(self):
        self.template.message_text = "{{ first_name }}: скоро день рождения. Купон {coupon_code}"
        self.template.save(update_fields=["message_text", "updated_at"])
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 1
        self.config.settings = {"pilot_phones": ["+79990000148"]}
        self.config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "settings",
                "updated_at",
            ]
        )
        guest = self._guest(phone="+79990000148", first_name="Андрей")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest)
        self._available_coupon(code="AUTO-NAME-CARD")

        result = execute_coupon_autoscenario_pilot(
            scenario_code=self.scenario.code,
            scan_limit=20,
            confirm=True,
            now=self.now,
        )
        assignment = CouponAutoscenarioAssignment.objects.get(run_id=result.run_id)
        ack_time = self.now + timedelta(minutes=5)
        assignment.vtelemax_sync_status = CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK
        assignment.vtelemax_synced_at = ack_time
        assignment.save(update_fields=["vtelemax_sync_status", "vtelemax_synced_at", "updated_at"])

        created_tasks = create_autoscenario_dispatch_after_vtelemax_ack(
            assignment_id=assignment.id,
            now=ack_time,
        )

        self.assertEqual(created_tasks, 1)
        event = NotificationEvent.objects.get(source_ref=f"coupon_autoscenario_assignment:{assignment.id}")
        task = DispatchTask.objects.get(notification_event=event)
        self.assertEqual(task.message_text, "Андрей: скоро день рождения. Купон AUTO-NAME-CARD")
        self.assertNotIn("first_name", task.message_text)

    def test_active_dispatch_after_ack_respects_send_window(self):
        self.template.message_text = "Мы давно не виделись ({{ days_without_visits }} дней). Купон {coupon_code}"
        self.template.save(update_fields=["message_text", "updated_at"])
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 1
        self.config.settings = {"pilot_phones": ["+79990000147"]}
        self.config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "settings",
                "updated_at",
            ]
        )
        current_now = timezone.now()
        self.scenario.distribution_mode = NotificationScenario.DistributionMode.UNIFORM
        self.scenario.timezone = "UTC"
        self.scenario.send_window_begin = (current_now + timedelta(hours=1)).time().replace(microsecond=0)
        self.scenario.send_window_end = (current_now + timedelta(hours=2)).time().replace(microsecond=0)
        self.scenario.save(
            update_fields=[
                "distribution_mode",
                "timezone",
                "send_window_begin",
                "send_window_end",
                "updated_at",
            ]
        )
        guest = self._guest(phone="+79990000147", first_name="WindowedActive")
        self._visit(guest=guest, days_ago=45)
        self._sendable_channel(guest=guest, with_binding=False)
        bot = BotProfile.objects.create(
            code="tg_autoscenario_windowed_active",
            name="Telegram autoscenario windowed active",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.scenario.bot_profiles.add(bot)
        GuestBotBinding.objects.create(
            guest=guest,
            bot=bot,
            external_chat_id="windowed-active-chat-147",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        self._available_coupon(code="AUTO-WINDOW")
        result = execute_coupon_autoscenario_pilot(
            scenario_code=self.scenario.code,
            scan_limit=20,
            confirm=True,
            now=current_now,
        )
        assignment = CouponAutoscenarioAssignment.objects.get(run_id=result.run_id)
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.AUTOMATIC
        self.config.save(update_fields=["execution_mode", "updated_at"])
        ack_time = current_now + timedelta(minutes=5)
        assignment.vtelemax_sync_status = CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK
        assignment.vtelemax_synced_at = ack_time
        assignment.save(update_fields=["vtelemax_sync_status", "vtelemax_synced_at", "updated_at"])

        created_tasks = create_autoscenario_dispatch_after_vtelemax_ack(
            assignment_id=assignment.id,
            now=ack_time,
        )

        self.assertEqual(created_tasks, 1)
        event = NotificationEvent.objects.get(source_ref=f"coupon_autoscenario_assignment:{assignment.id}")
        task = DispatchTask.objects.get(notification_event=event)
        self.assertGreater(event.planned_send_at, ack_time)
        self.assertEqual(task.available_at, event.planned_send_at)
        self.assertEqual(task.scheduled_at, event.planned_send_at)
        self.assertEqual(task.status, DispatchTask.Status.PENDING)

    def test_forced_pilot_without_visit_uses_safe_days_value_in_message(self):
        self.template.message_text = "Long time no see ({{ days_without_visits }} days). Coupon {coupon_code}"
        self.template.save(update_fields=["message_text", "updated_at"])
        self.config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        self.config.max_recipients_per_run = 1
        self.config.settings = {
            "pilot_phones": ["+79990000146"],
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
        guest = self._guest(phone="+79990000146", first_name="ForcedPilot")
        self._sendable_channel(guest=guest, with_binding=False)
        bot = BotProfile.objects.create(
            code="tg_autoscenario_forced_pilot",
            name="Telegram autoscenario forced pilot",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.scenario.bot_profiles.add(bot)
        GuestBotBinding.objects.create(
            guest=guest,
            bot=bot,
            external_chat_id="pilot-chat-146",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        self._available_coupon(code="AUTO-FORCED-NOW")

        result = execute_coupon_autoscenario_pilot(
            scenario_code=self.scenario.code,
            scan_limit=20,
            confirm=True,
            now=self.now,
        )

        assignment = CouponAutoscenarioAssignment.objects.get(run_id=result.run_id)
        self.assertIn("30 days", assignment.promo_text)
        queue_event = CouponVtelemaxSyncQueue.objects.get(autoscenario_assignment=assignment)
        self.assertEqual(queue_event.payload_json["days_without_visits"], 30)

        ack_time = self.now + timedelta(minutes=5)
        assignment.vtelemax_sync_status = CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK
        assignment.vtelemax_synced_at = ack_time
        assignment.save(update_fields=["vtelemax_sync_status", "vtelemax_synced_at", "updated_at"])

        created_tasks = create_autoscenario_dispatch_after_vtelemax_ack(
            assignment_id=assignment.id,
            now=ack_time,
            days_without_visits=queue_event.payload_json["days_without_visits"],
        )

        self.assertEqual(created_tasks, 1)
        event = NotificationEvent.objects.get(source_ref=f"coupon_autoscenario_assignment:{assignment.id}")
        task = DispatchTask.objects.get(notification_event=event)
        self.assertIn("30 days", task.message_text)
        self.assertNotIn("days_without_visits", task.message_text)

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

    def test_birthday_preview_selects_rolling_window_and_calculates_trigger(self):
        birthday_scenario, birthday_config = self._prepare_birthday_autoscenario()
        current_now = timezone.make_aware(datetime(2026, 6, 10, 10, 0))
        today_guest = self._guest(
            phone="+79990000201",
            first_name="Today",
            birthdate=date(1991, 6, 10),
        )
        soon_guest = self._guest(
            phone="+79990000202",
            first_name="Soon",
            birthdate=date(1992, 6, 17),
        )
        outside_guest = self._guest(
            phone="+79990000203",
            first_name="Outside",
            birthdate=date(1993, 6, 18),
        )
        for guest in [today_guest, soon_guest, outside_guest]:
            self._sendable_channel(guest=guest)
        self._available_coupon_for_config(config=birthday_config, code="BDAY-1")
        self._available_coupon_for_config(config=birthday_config, code="BDAY-2")

        preview = preview_coupon_autoscenario_audience(
            scenario_code=birthday_scenario.code,
            scan_limit=20,
            now=current_now,
        )

        self.assertEqual(preview.inactive_days_threshold, 0)
        self.assertEqual(preview.birthday_preparation_window_days, 7)
        self.assertEqual(preview.scanned_guests, 2)
        self.assertEqual(preview.matched_guests, 2)
        self.assertEqual(preview.sendable_guests, 2)
        rows_by_guest = {row.guest_id: row for row in preview.sample_rows}
        self.assertEqual(rows_by_guest[today_guest.id].days_until_birthday, 0)
        self.assertEqual(rows_by_guest[today_guest.id].birthday_date, date(2026, 6, 10))
        self.assertEqual(rows_by_guest[today_guest.id].trigger_key, "birthday:2026")
        self.assertEqual(rows_by_guest[soon_guest.id].days_until_birthday, 7)
        self.assertEqual(rows_by_guest[soon_guest.id].birthday_date, date(2026, 6, 17))
        self.assertNotIn(outside_guest.id, rows_by_guest)

    def test_birthday_plan_blocks_existing_assignment_for_same_birthday_year(self):
        birthday_scenario, birthday_config = self._prepare_birthday_autoscenario()
        birthday_config.execution_mode = CouponAutomationConfig.ExecutionMode.AUTOMATIC
        birthday_config.cooldown_days = 0
        birthday_config.save(update_fields=["execution_mode", "cooldown_days", "updated_at"])
        current_now = timezone.make_aware(datetime(2026, 6, 10, 10, 0))
        guest = self._guest(
            phone="+79990000211",
            first_name="Birthday",
            birthdate=date(1991, 6, 17),
        )
        self._sendable_channel(guest=guest)
        coupon = self._available_coupon_for_config(config=birthday_config, code="BDAY-PLAN")

        plan = build_coupon_autoscenario_execution_plan(
            scenario_code=birthday_scenario.code,
            scan_limit=20,
            now=current_now,
        )

        self.assertTrue(plan.can_execute)
        self.assertEqual(plan.planned_assignments, 1)
        self.assertEqual(plan.plan_items[0].guest_id, guest.id)
        self.assertEqual(plan.plan_items[0].coupon_id, coupon.id)
        self.assertEqual(plan.plan_items[0].days_until_birthday, 7)
        self.assertEqual(plan.plan_items[0].trigger_key, "birthday:2026")
        self.assertEqual(plan.plan_items[0].trigger_date, date(2026, 6, 17))

        existing_run = CouponAutoscenarioRun.objects.create(
            scenario=birthday_scenario,
            config=birthday_config,
            status=CouponAutoscenarioRun.Status.COMPLETED,
            execution_mode=CouponAutomationConfig.ExecutionMode.AUTOMATIC,
        )
        existing_coupon = self._available_coupon_for_config(config=birthday_config, code="BDAY-OLD")
        CouponAutoscenarioAssignment.objects.create(
            run=existing_run,
            scenario=birthday_scenario,
            config=birthday_config,
            guest=guest,
            coupon=existing_coupon,
            phone_e164=guest.phone,
            coupon_series=existing_coupon.series,
            coupon_code=existing_coupon.code,
            venue_code=existing_coupon.venue_code,
            venue_name=existing_coupon.venue_name,
            trigger_key="birthday:2026",
            trigger_date=date(2026, 6, 17),
            assigned_at=current_now - timedelta(days=1),
            lifetime_expires_at=current_now + timedelta(days=14),
            status=CouponAutoscenarioAssignment.Status.USED,
            vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.OK,
        )

        repeated_plan = build_coupon_autoscenario_execution_plan(
            scenario_code=birthday_scenario.code,
            scan_limit=20,
            now=current_now,
        )

        self.assertFalse(repeated_plan.can_execute)
        self.assertEqual(repeated_plan.blocked_existing_trigger, 1)
        self.assertEqual(repeated_plan.eligible_guests, 0)
        self.assertEqual(repeated_plan.planned_assignments, 0)

    def test_birthday_forced_pilot_uses_explicit_days_until_birthday(self):
        birthday_scenario, birthday_config = self._prepare_birthday_autoscenario()
        birthday_config.execution_mode = CouponAutomationConfig.ExecutionMode.PILOT
        birthday_config.max_recipients_per_run = 1
        birthday_config.settings = {
            "birthday_preparation_window_days": 7,
            "pilot_phones": ["+79990000221"],
            "pilot_include_unmatched": True,
            "pilot_days_until_birthday": 5,
        }
        birthday_config.save(
            update_fields=[
                "execution_mode",
                "max_recipients_per_run",
                "settings",
                "updated_at",
            ]
        )
        self.template.message_text = "До дня рождения {{ days_until_birthday }} дн. Купон {coupon_code}"
        self.template.save(update_fields=["message_text", "updated_at"])
        current_now = timezone.make_aware(datetime(2026, 6, 10, 10, 0))
        pilot_guest = self._guest(phone="+79990000221", first_name="PilotBirthday")
        self._sendable_channel(guest=pilot_guest)
        self._available_coupon_for_config(config=birthday_config, code="BDAY-PILOT")

        result = execute_coupon_autoscenario_pilot(
            scenario_code=birthday_scenario.code,
            scan_limit=20,
            confirm=True,
            now=current_now,
        )

        self.assertTrue(result.confirmed)
        self.assertEqual(result.created_assignments, 1)
        self.assertEqual(result.plan.pilot_forced_guests, 1)
        self.assertEqual(result.plan.plan_items[0].days_until_birthday, 5)
        self.assertTrue(result.plan.plan_items[0].is_pilot_forced)

        assignment = CouponAutoscenarioAssignment.objects.get(run_id=result.run_id)
        self.assertEqual(assignment.trigger_key, "birthday:2026")
        self.assertEqual(assignment.trigger_date, date(2026, 6, 15))
        self.assertIn("5 дн.", assignment.promo_text)
        queue_event = CouponVtelemaxSyncQueue.objects.get(autoscenario_assignment=assignment)
        self.assertEqual(queue_event.payload_json["days_until_birthday"], 5)
        self.assertEqual(queue_event.payload_json["trigger_key"], "birthday:2026")
        self.assertEqual(queue_event.payload_json["trigger_date"], "2026-06-15")

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
