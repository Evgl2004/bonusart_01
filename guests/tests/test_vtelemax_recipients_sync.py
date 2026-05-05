import uuid

from django.test import TestCase
from django.utils import timezone

from guests.models import BotProfile, Guest, GuestBotBinding, VtelemaxRecipientChannel
from guests.services.vtelemax_recipients_sync import VtelemaxRecipientsApplyService


class VtelemaxRecipientsApplyServiceTests(TestCase):
    def setUp(self):
        self.telegram_bot = BotProfile.objects.create(
            code="tg-main",
            name="Telegram Main",
            provider_type=BotProfile.ProviderType.TELEGRAM,
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

