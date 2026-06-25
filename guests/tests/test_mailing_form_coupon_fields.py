from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from guests.forms import MailingForm
from guests.models import BotProfile, CouponRegistryEntry, Mailing, MessageTemplate, TerminalDepartmentMap
from guests.services.coupon_constants import COUPON_VENUE_GLOBAL_CODE, COUPON_VENUE_GLOBAL_NAME


class MailingFormCouponFieldsTests(TestCase):
    """
    Проверки валидации купонных полей формы кампании.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="TEST_TEMPLATE",
            description="",
            message_text="Тест",
            created_by="test",
            is_active=True,
        )
        self.bot = BotProfile.objects.create(
            code="tg_coupon_form",
            name="TG coupon",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        TerminalDepartmentMap.objects.create(
            organization_id="org_1",
            terminal_group_id="term_1",
            department_id="DEP_1",
            department_name="Ассорти Франсуа",
            is_active=True,
        )
        CouponRegistryEntry.objects.create(
            series="TEST_SERIES",
            code="TST-FORM-1",
            venue_code="DEP_1",
            venue_name="Ассорти Франсуа",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )
        CouponRegistryEntry.objects.create(
            series="GLOBAL_SERIES",
            code="GLB-FORM-1",
            venue_code=COUPON_VENUE_GLOBAL_CODE,
            venue_name=COUPON_VENUE_GLOBAL_NAME,
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )

    def _base_form_data(self) -> dict[str, object]:
        return {
            "name": "Купонная кампания",
            "template": self.template.id,
            "scheduled_date": self.now.date().isoformat(),
            "scheduled_time_begin": self.now.strftime("%Y-%m-%dT%H:%M"),
            "scheduled_time_end": (self.now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "send_window_begin": self.now.strftime("%H:%M"),
            "send_window_end": (self.now + timedelta(hours=3)).strftime("%H:%M"),
            "target_mode": Mailing.TargetMode.PRIMARY_ONLY,
            "queue_priority": Mailing.QueuePriority.NORMAL,
            "bot_profiles": [str(self.bot.id)],
        }

    def test_requires_venue_and_promo_for_coupon_mode(self):
        data = self._base_form_data()
        data.update(
            {
                "coupon_series": "TEST_SERIES",
                "coupon_venue_code": "",
                "coupon_promo_text": "",
            }
        )

        form = MailingForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("coupon_venue_code", form.errors)
        self.assertIn("coupon_promo_text", form.errors)

    def test_coupon_series_is_selected_from_available_pool(self):
        form = MailingForm(data=self._base_form_data())

        series_choices = dict(form.fields["coupon_series"].choices)

        self.assertIn("", series_choices)
        self.assertIn("TEST_SERIES", series_choices)
        self.assertIn("GLOBAL_SERIES", series_choices)
        self.assertIn("доступно 1", series_choices["TEST_SERIES"])

    def test_rejects_unknown_coupon_series_for_new_campaign(self):
        data = self._base_form_data()
        data.update(
            {
                "coupon_series": "UNKNOWN_SERIES",
                "coupon_venue_code": "DEP_1",
                "coupon_promo_text": "Скидка 20% на сет",
            }
        )

        form = MailingForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("coupon_series", form.errors)

    def test_clears_coupon_fields_when_series_is_empty(self):
        data = self._base_form_data()
        data.update(
            {
                "coupon_series": "",
                "coupon_venue_code": "DEP_1",
                "coupon_title": "Сет в подарок",
                "coupon_promo_text": "Скидка 20%",
            }
        )

        form = MailingForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_json())
        instance = form.save(commit=False)
        self.assertIsNone(instance.coupon_series)
        self.assertIsNone(instance.coupon_venue_code)
        self.assertIsNone(instance.coupon_venue_name)
        self.assertIsNone(instance.coupon_title)
        self.assertIsNone(instance.coupon_promo_text)

    def test_resolves_venue_name_for_coupon_mode(self):
        data = self._base_form_data()
        data.update(
            {
                "coupon_series": "TEST_SERIES",
                "coupon_venue_code": "DEP_1",
                "coupon_title": "  Сет «Канпети» в подарок  ",
                "coupon_promo_text": "Скидка 20% на сет",
            }
        )

        form = MailingForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_json())
        instance = form.save(commit=False)
        self.assertEqual(instance.coupon_series, "TEST_SERIES")
        self.assertEqual(instance.coupon_venue_code, "DEP_1")
        self.assertEqual(instance.coupon_venue_name, "Ассорти Франсуа")
        self.assertEqual(instance.coupon_title, "Сет «Канпети» в подарок")
        self.assertEqual(instance.coupon_promo_text, "Скидка 20% на сет")

    def test_rejects_unknown_venue_code_for_coupon_mode(self):
        data = self._base_form_data()
        data.update(
            {
                "coupon_series": "TEST_SERIES",
                "coupon_venue_code": "UNKNOWN_DEP",
                "coupon_promo_text": "Скидка 20% на сет",
            }
        )

        form = MailingForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("coupon_venue_code", form.errors)

    def test_allows_global_venue_for_coupon_mode(self):
        data = self._base_form_data()
        data.update(
            {
                "coupon_series": "GLOBAL_SERIES",
                "coupon_venue_code": COUPON_VENUE_GLOBAL_CODE,
                "coupon_promo_text": "Общий купон на подарок",
            }
        )

        form = MailingForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_json())
        instance = form.save(commit=False)
        self.assertEqual(instance.coupon_series, "GLOBAL_SERIES")
        self.assertEqual(instance.coupon_venue_code, COUPON_VENUE_GLOBAL_CODE)
        self.assertEqual(instance.coupon_venue_name, COUPON_VENUE_GLOBAL_NAME)
