import uuid
from datetime import date

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    BotProfile,
    Guest,
    GuestBotBinding,
    GuestProfileCompletionEvent,
    VtelemaxRecipientChannel,
)
from guests.services.vtelemax_recipients_sync import VtelemaxRecipientsApplyService


class VtelemaxRecipientsApplyServiceTests(TestCase):
    def setUp(self):
        self.telegram_bot = BotProfile.objects.create(
            code="tg-main",
            name="Telegram Main",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.max_bot = BotProfile.objects.create(
            code="max-main",
            name="Max Main",
            provider_type=BotProfile.ProviderType.MAX,
            is_active=True,
        )
        self.vk_bot = BotProfile.objects.create(
            code="vk-main",
            name="VK Main",
            provider_type=BotProfile.ProviderType.VK,
            is_active=True,
        )
        self.guest = Guest.objects.create(
            phone="+79224800001",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        self.person_id = uuid.uuid4()

    def _build_item(self, **overrides):
        payload = {
            "person_id": str(self.person_id),
            "phone_e164": "+79224800001",
            "platform": "telegram",
            "external_id": "113703",
            "rules_accepted": True,
            "notifications_allowed": True,
            "is_registered": True,
            "registered_at": "2026-05-05T10:05:00Z",
            "state_updated_at": "2026-05-05T10:10:00Z",
            "account_created_at": "2026-05-05T10:00:00Z",
            "effective_updated_at": "2026-05-05T10:10:00Z",
        }
        payload.update(overrides)
        return payload

    def test_apply_items_creates_channel_and_binding(self):
        service = VtelemaxRecipientsApplyService(bot_code_telegram="tg-main")

        stats = service.apply_items(items=[self._build_item()], dry_run=False)

        self.assertEqual(stats.rows_total, 1)
        self.assertEqual(stats.rows_created, 1)
        self.assertEqual(stats.rows_binding_created, 1)
        self.assertEqual(VtelemaxRecipientChannel.objects.count(), 1)
        channel = VtelemaxRecipientChannel.objects.get()
        self.assertEqual(channel.person_id, self.person_id)
        self.assertEqual(channel.platform, "telegram")
        self.assertEqual(channel.guest_id, self.guest.id)
        self.assertTrue(channel.notifications_allowed)
        self.assertIsNotNone(channel.registered_at)
        self.assertIsNotNone(channel.guest_binding_id)

        binding = GuestBotBinding.objects.get()
        self.assertEqual(binding.guest_id, self.guest.id)
        self.assertEqual(binding.bot_id, self.telegram_bot.id)
        self.assertEqual(binding.external_chat_id, "113703")
        self.assertTrue(binding.is_active)
        self.assertTrue(binding.is_opt_in)
        self.assertFalse(binding.is_stop_sending)

    def test_apply_items_updates_existing_channel_and_disables_binding(self):
        service = VtelemaxRecipientsApplyService(bot_code_telegram="tg-main")
        service.apply_items(items=[self._build_item()], dry_run=False)

        stats = service.apply_items(
            items=[
                self._build_item(
                    notifications_allowed=False,
                    is_registered=True,
                    state_updated_at="2026-05-05T11:00:00Z",
                    effective_updated_at="2026-05-05T11:00:00Z",
                )
            ],
            dry_run=False,
        )

        self.assertEqual(stats.rows_total, 1)
        self.assertEqual(stats.rows_updated, 1)
        self.assertEqual(stats.rows_binding_updated, 1)
        channel = VtelemaxRecipientChannel.objects.get()
        self.assertFalse(channel.notifications_allowed)

        binding = GuestBotBinding.objects.get()
        self.assertFalse(binding.is_active)
        self.assertFalse(binding.is_opt_in)
        self.assertTrue(binding.is_stop_sending)

    def test_apply_items_dry_run_does_not_write(self):
        service = VtelemaxRecipientsApplyService(bot_code_telegram="tg-main")

        stats = service.apply_items(items=[self._build_item()], dry_run=True)

        self.assertEqual(stats.rows_total, 1)
        self.assertEqual(VtelemaxRecipientChannel.objects.count(), 0)
        self.assertEqual(GuestBotBinding.objects.count(), 0)

    def test_apply_items_is_idempotent_for_repeated_payload(self):
        service = VtelemaxRecipientsApplyService(bot_code_telegram="tg-main")

        first = service.apply_items(items=[self._build_item()], dry_run=False)
        second = service.apply_items(items=[self._build_item()], dry_run=False)

        self.assertEqual(first.rows_created, 1)
        self.assertEqual(second.rows_created, 0)
        self.assertEqual(second.rows_updated, 0)
        self.assertEqual(second.rows_binding_updated, 0)
        self.assertEqual(VtelemaxRecipientChannel.objects.count(), 1)
        self.assertEqual(GuestBotBinding.objects.count(), 1)

    def test_create_missing_guest_once_for_three_platforms(self):
        person_id = uuid.uuid4()
        service = VtelemaxRecipientsApplyService(
            bot_code_telegram="tg-main",
            bot_code_max="max-main",
            bot_code_vk="vk-main",
            create_missing_guests=True,
        )

        payloads = [
            self._build_item(
                person_id=str(person_id),
                phone_e164="+79993334455",
                platform="telegram",
                external_id="tg-1",
            ),
            self._build_item(
                person_id=str(person_id),
                phone_e164="+79993334455",
                platform="max",
                external_id="max-1",
            ),
            self._build_item(
                person_id=str(person_id),
                phone_e164="+79993334455",
                platform="vk",
                external_id="vk-1",
            ),
        ]

        stats = service.apply_items(items=payloads, dry_run=False)

        self.assertEqual(stats.rows_total, 3)
        self.assertEqual(VtelemaxRecipientChannel.objects.filter(phone_e164="+79993334455").count(), 3)
        self.assertEqual(Guest.objects.filter(phone="+79993334455").count(), 1)
        created_guest = Guest.objects.get(phone="+79993334455")
        self.assertEqual(GuestBotBinding.objects.filter(guest=created_guest).count(), 3)

    def test_apply_items_fills_guest_birthdate_if_empty(self):
        service = VtelemaxRecipientsApplyService(bot_code_telegram="tg-main")

        stats = service.apply_items(
            items=[self._build_item(birthdate="1991-05-17")],
            dry_run=False,
        )

        self.assertEqual(stats.rows_total, 1)
        self.assertEqual(stats.rows_birthdate_events_created, 1)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.birthdate, date(1991, 5, 17))
        event = GuestProfileCompletionEvent.objects.get(
            guest=self.guest,
            event_type=GuestProfileCompletionEvent.EventType.BIRTHDATE_FILLED,
        )
        self.assertEqual(event.profile_value, {"birthdate": "1991-05-17"})

        second = service.apply_items(
            items=[self._build_item(birthdate="1991-05-17")],
            dry_run=False,
        )
        self.assertEqual(second.rows_birthdate_events_created, 0)
        self.assertEqual(GuestProfileCompletionEvent.objects.filter(guest=self.guest).count(), 1)

    def test_create_missing_guest_uses_birthdate_without_completion_event(self):
        service = VtelemaxRecipientsApplyService(
            bot_code_telegram="tg-main",
            create_missing_guests=True,
        )

        stats = service.apply_items(
            items=[
                self._build_item(
                    person_id=str(uuid.uuid4()),
                    phone_e164="+79990000011",
                    external_id="tg-new-birth",
                    profile={"birthdate": "03.09.1988"},
                )
            ],
            dry_run=False,
        )

        self.assertEqual(stats.rows_total, 1)
        self.assertEqual(stats.rows_birthdate_events_created, 0)
        guest = Guest.objects.get(phone="+79990000011")
        self.assertEqual(guest.birthdate, date(1988, 9, 3))
        self.assertEqual(
            GuestProfileCompletionEvent.objects.filter(
                guest=guest,
                event_type=GuestProfileCompletionEvent.EventType.BIRTHDATE_FILLED,
            ).count(),
            0,
        )

    def test_create_missing_guest_requires_valid_channel_flags(self):
        service = VtelemaxRecipientsApplyService(
            bot_code_telegram="tg-main",
            create_missing_guests=True,
        )

        stats = service.apply_items(
            items=[
                self._build_item(
                    person_id=str(uuid.uuid4()),
                    phone_e164="+79990000022",
                    external_id="",
                    notifications_allowed=True,
                    is_registered=True,
                ),
                self._build_item(
                    person_id=str(uuid.uuid4()),
                    phone_e164="+79990000033",
                    external_id="tg-no-optin",
                    notifications_allowed=False,
                    is_registered=True,
                ),
            ],
            dry_run=False,
        )

        self.assertEqual(stats.rows_total, 2)
        self.assertEqual(stats.rows_not_eligible_for_guest_create, 2)
        self.assertEqual(stats.rows_guest_unresolved, 0)
        self.assertEqual(Guest.objects.filter(phone="+79990000022").count(), 0)
        self.assertEqual(Guest.objects.filter(phone="+79990000033").count(), 0)
        self.assertEqual(
            VtelemaxRecipientChannel.objects.filter(person_id__isnull=False).count(),
            2,
        )
        self.assertEqual(
            VtelemaxRecipientChannel.objects.filter(guest__isnull=False).count(),
            0,
        )
