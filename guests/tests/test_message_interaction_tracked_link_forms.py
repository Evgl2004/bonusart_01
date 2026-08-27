"""Проверки пользовательской настройки отслеживаемых ссылок."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.test import TestCase, override_settings
from django.utils import timezone

from guests.admin import (
    MailingAdminForm,
    MessageInteractionLinkDestinationAdmin,
    MessageInteractionLinkDestinationAdminForm,
)
from guests.forms import (
    CouponAutomationConfigForm,
    CouponAutomationScenarioCreateForm,
    MailingForm,
)
from guests.models import (
    BotProfile,
    CouponAutomationConfig,
    InteractionButtonSet,
    InteractionLinkLabelCode,
    Mailing,
    MessageInteractionLinkDestination,
    MessageTemplate,
    NotificationScenario,
)


@override_settings(MESSAGE_TRACKED_LINK_ALLOWED_HOSTS={"rest.market"})
class TrackedLinkFormTests(TestCase):
    """Проверяет согласованность набора кнопок и назначения во всех формах."""

    def setUp(self) -> None:
        self.now = timezone.now().replace(second=0, microsecond=0)
        self.template = MessageTemplate.objects.create(
            name="Шаблон формы ссылки",
            description="",
            message_text="Ваш купон: {coupon_code}",
            created_by="test",
            is_active=True,
        )
        self.bot = BotProfile.objects.create(
            code="tracked_link_form_bot",
            name="Бот формы ссылки",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            token="test-token",
            is_active=True,
        )
        self.destination = MessageInteractionLinkDestination.objects.create(
            code="delivery_form_test",
            name="Доставка для формы",
            label_code=InteractionLinkLabelCode.DELIVERY,
            target_url="https://rest.market/",
            is_active=True,
        )
        self.inactive_destination = MessageInteractionLinkDestination.objects.create(
            code="inactive_form_test",
            name="Отключённое назначение",
            label_code=InteractionLinkLabelCode.DETAILS,
            target_url="https://rest.market/old/",
            is_active=False,
        )

    def _mailing_data(self, **overrides) -> dict[str, object]:
        data: dict[str, object] = {
            "name": "Рассылка со ссылкой",
            "template": str(self.template.id),
            "scheduled_date": self.now.date().isoformat(),
            "scheduled_time_begin": self.now.strftime("%Y-%m-%dT%H:%M"),
            "scheduled_time_end": (self.now + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "send_window_begin": self.now.strftime("%H:%M"),
            "send_window_end": (self.now + timedelta(hours=1)).strftime("%H:%M"),
            "target_mode": Mailing.TargetMode.PRIMARY_ONLY,
            "queue_priority": Mailing.QueuePriority.NORMAL,
            "button_set": InteractionButtonSet.RATING_MENU_LINK,
            "tracked_link_destination": str(self.destination.id),
            "coupon_series": "",
            "bot_profiles": [str(self.bot.id)],
        }
        data.update(overrides)
        return data

    def _create_scenario_data(self, **overrides) -> dict[str, object]:
        data: dict[str, object] = {
            "code": "tracked_link_created_scenario",
            "name": "Созданный сценарий со ссылкой",
            "scenario_type": CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
            "inactive_days": "30",
            "template_mode": "create",
            "template_name": "Новый шаблон ссылки",
            "template_description": "",
            "template_text": "Ваш купон: {coupon_code}",
            "notification_bot_profiles": [str(self.bot.id)],
            "notification_button_set": InteractionButtonSet.RATING_MENU_LINK,
            "notification_tracked_link_destination": str(self.destination.id),
        }
        data.update(overrides)
        return data

    def test_mailing_requires_active_destination_only_for_link_set(self):
        missing = MailingForm(
            data=self._mailing_data(tracked_link_destination="")
        )
        inactive = MailingForm(
            data=self._mailing_data(
                tracked_link_destination=str(self.inactive_destination.id)
            )
        )
        valid = MailingForm(data=self._mailing_data())

        self.assertFalse(missing.is_valid())
        self.assertIn("tracked_link_destination", missing.errors)
        self.assertFalse(inactive.is_valid())
        self.assertIn("tracked_link_destination", inactive.errors)
        self.assertTrue(valid.is_valid(), valid.errors.as_json())
        self.assertEqual(valid.save(commit=False).tracked_link_destination, self.destination)

    def test_mailing_clears_destination_for_non_link_set(self):
        form = MailingForm(
            data=self._mailing_data(button_set=InteractionButtonSet.RATING_MENU)
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        mailing = form.save(commit=False)
        self.assertEqual(mailing.button_set, InteractionButtonSet.RATING_MENU)
        self.assertIsNone(mailing.tracked_link_destination)

    def test_historical_mailing_forces_plain_message_and_clears_destination(self):
        mailing = Mailing.objects.create(
            name="Историческая рассылка со старой настройкой",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now,
            scheduled_time_end=self.now + timedelta(hours=1),
            is_active=False,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=self.now.time(),
            send_window_end=(self.now + timedelta(hours=1)).time(),
            button_set=InteractionButtonSet.RATING_MENU_LINK,
            tracked_link_destination=self.destination,
            source_filter_snapshot={"source_layer": "historical_all_time"},
        )
        form = MailingForm(data=self._mailing_data(), instance=mailing)

        self.assertTrue(form.is_valid(), form.errors.as_json())
        saved = form.save(commit=False)
        self.assertTrue(form.fields["button_set"].disabled)
        self.assertTrue(form.fields["tracked_link_destination"].disabled)
        self.assertEqual(saved.button_set, InteractionButtonSet.NONE)
        self.assertIsNone(saved.tracked_link_destination)

    def test_scenario_creation_requires_and_saves_active_destination(self):
        missing = CouponAutomationScenarioCreateForm(
            data=self._create_scenario_data(
                notification_tracked_link_destination=""
            )
        )
        valid = CouponAutomationScenarioCreateForm(data=self._create_scenario_data())

        self.assertFalse(missing.is_valid())
        self.assertIn("notification_tracked_link_destination", missing.errors)
        self.assertTrue(valid.is_valid(), valid.errors.as_json())
        config = valid.save()
        self.assertEqual(config.scenario.button_set, InteractionButtonSet.RATING_MENU_LINK)
        self.assertEqual(config.scenario.tracked_link_destination, self.destination)

    def test_scenario_settings_save_destination_and_clear_it_for_another_set(self):
        scenario = NotificationScenario.objects.create(
            code="tracked_link_settings_scenario",
            name="Настройка сценария со ссылкой",
            is_active=False,
            is_system=False,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            template=self.template,
            button_set=InteractionButtonSet.NONE,
        )
        scenario.bot_profiles.add(self.bot)
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
        )
        data = {
            "execution_mode": CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            "audience_venue_filter_mode": CouponAutomationConfig.AudienceVenueFilterMode.DISABLED,
            "audience_venue_code": "",
            "venue_selection_mode": CouponAutomationConfig.VenueSelectionMode.LAST_ORDER,
            "coupon_series": "",
            "venue_code": "",
            "coupon_validity_days": "14",
            "max_recipients_per_run": "100",
            "cooldown_days": "30",
            "coupon_title_template": "",
            "coupon_promo_text_template": "",
            "min_order_amount": "",
            "iikocard_action_note": "",
            "pilot_phones": "",
            "notification_template": str(self.template.id),
            "notification_distribution_mode": NotificationScenario.DistributionMode.IMMEDIATE,
            "notification_button_set": InteractionButtonSet.RATING_MENU_LINK,
            "notification_tracked_link_destination": str(self.destination.id),
            "notification_target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
            "notification_bot_profiles": [str(self.bot.id)],
            "notification_timezone": "Asia/Yekaterinburg",
            "notification_send_window_begin": "",
            "notification_send_window_end": "",
        }
        form = CouponAutomationConfigForm(data=data, instance=config)

        self.assertTrue(form.is_valid(), form.errors.as_json())
        form.save()
        scenario.refresh_from_db()
        self.assertEqual(scenario.button_set, InteractionButtonSet.RATING_MENU_LINK)
        self.assertEqual(scenario.tracked_link_destination, self.destination)

        data["notification_button_set"] = InteractionButtonSet.RATING_MENU
        form = CouponAutomationConfigForm(data=data, instance=config)
        self.assertTrue(form.is_valid(), form.errors.as_json())
        form.save()
        scenario.refresh_from_db()
        self.assertEqual(scenario.button_set, InteractionButtonSet.RATING_MENU)
        self.assertIsNone(scenario.tracked_link_destination)

    def test_destination_admin_protects_historical_technical_fields(self):
        model_admin = MessageInteractionLinkDestinationAdmin(
            MessageInteractionLinkDestination,
            AdminSite(),
        )

        self.assertEqual(
            set(model_admin.get_readonly_fields(None, self.destination)),
            {"code", "label_code", "target_url", "created_at", "updated_at"},
        )
        self.assertFalse(model_admin.has_delete_permission(None, self.destination))

    def test_destination_admin_form_validates_creation_and_reactivation(self):
        allowed_creation = MessageInteractionLinkDestinationAdminForm(
            data={
                "code": "allowed_creation",
                "name": "Разрешённое назначение",
                "label_code": InteractionLinkLabelCode.WEBSITE,
                "target_url": "https://rest.market/page",
                "is_active": "on",
            }
        )
        forbidden_creation = MessageInteractionLinkDestinationAdminForm(
            data={
                "code": "forbidden_creation",
                "name": "Запрещённое назначение",
                "label_code": InteractionLinkLabelCode.WEBSITE,
                "target_url": "https://example.org/",
                "is_active": "on",
            }
        )
        forbidden_existing = MessageInteractionLinkDestination.objects.create(
            code="forbidden_existing",
            name="Ранее созданное назначение",
            label_code=InteractionLinkLabelCode.DETAILS,
            target_url="https://example.org/",
            is_active=False,
        )
        forbidden_reactivation = MessageInteractionLinkDestinationAdminForm(
            data={
                "code": forbidden_existing.code,
                "name": forbidden_existing.name,
                "label_code": forbidden_existing.label_code,
                "target_url": forbidden_existing.target_url,
                "is_active": "on",
            },
            instance=forbidden_existing,
        )

        self.assertTrue(allowed_creation.is_valid(), allowed_creation.errors.as_json())
        self.assertFalse(forbidden_creation.is_valid())
        self.assertIn("target_url", forbidden_creation.errors)
        self.assertFalse(forbidden_reactivation.is_valid())
        self.assertIn("target_url", forbidden_reactivation.errors)

    def test_mailing_admin_form_checks_button_set_and_active_destination(self):
        base_data = {
            "name": "Рассылка через административную панель",
            "template": str(self.template.pk),
            "scheduled_date": self.now.date().isoformat(),
            "scheduled_time_begin": self.now.strftime("%Y-%m-%d %H:%M:%S"),
            "scheduled_time_end": (self.now + timedelta(hours=1)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "created_at": self.now.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": self.now.strftime("%Y-%m-%d %H:%M:%S"),
            "is_active": "on",
            "send_window_begin": self.now.strftime("%H:%M:%S"),
            "send_window_end": (self.now + timedelta(hours=1)).strftime("%H:%M:%S"),
            "target_mode": Mailing.TargetMode.PRIMARY_ONLY,
            "queue_priority": Mailing.QueuePriority.NORMAL,
            "button_set": InteractionButtonSet.RATING_MENU_LINK,
            "tracked_link_destination": str(self.destination.pk),
            "coupon_series": "",
            "coupon_venue_code": "",
            "coupon_venue_name": "",
            "coupon_title": "",
            "coupon_promo_text": "",
            "source_filter_snapshot": "{}",
            "bot_profiles": [str(self.bot.pk)],
        }
        valid_form = MailingAdminForm(data=base_data)
        inactive_form = MailingAdminForm(
            data={
                **base_data,
                "tracked_link_destination": str(self.inactive_destination.pk),
            }
        )
        without_link_set = MailingAdminForm(
            data={
                **base_data,
                "button_set": InteractionButtonSet.RATING_MENU,
            }
        )

        self.assertTrue(valid_form.is_valid(), valid_form.errors.as_json())
        self.assertFalse(inactive_form.is_valid())
        self.assertIn("tracked_link_destination", inactive_form.errors)
        self.assertTrue(without_link_set.is_valid(), without_link_set.errors.as_json())
        self.assertIsNone(
            without_link_set.save(commit=False).tracked_link_destination
        )

    def test_destination_bulk_activation_is_all_or_nothing(self):
        allowed = MessageInteractionLinkDestination.objects.create(
            code="allowed_bulk_activation",
            name="Разрешённое массовое включение",
            label_code=InteractionLinkLabelCode.WEBSITE,
            target_url="https://rest.market/bulk",
            is_active=False,
        )
        forbidden = MessageInteractionLinkDestination.objects.create(
            code="forbidden_bulk_activation",
            name="Запрещённое массовое включение",
            label_code=InteractionLinkLabelCode.WEBSITE,
            target_url="https://example.org/bulk",
            is_active=False,
        )
        model_admin = MessageInteractionLinkDestinationAdmin(
            MessageInteractionLinkDestination,
            AdminSite(),
        )

        with patch.object(model_admin, "message_user") as message_user:
            model_admin.action_activate(
                None,
                MessageInteractionLinkDestination.objects.filter(
                    pk__in=[allowed.pk, forbidden.pk]
                ),
            )

        allowed.refresh_from_db()
        forbidden.refresh_from_db()
        self.assertFalse(allowed.is_active)
        self.assertFalse(forbidden.is_active)
        message_user.assert_called_once()
