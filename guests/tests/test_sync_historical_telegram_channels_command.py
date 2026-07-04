from __future__ import annotations

from io import StringIO
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    HistoricalTelegramChannel,
    Mailing,
    MailingGuest,
    MessageTemplate,
)


class SyncHistoricalTelegramChannelsCommandTests(TestCase):
    """
    Проверяем безопасное наполнение исторических Telegram-каналов по рассылке.
    """

    def setUp(self):
        now = timezone.now()
        self.bot = BotProfile.objects.create(
            code="historical_sync_tg",
            name="Исторический Telegram",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.template = MessageTemplate.objects.create(
            name="Исторический шаблон",
            message_text="Привет",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Историческая кампания",
            template=self.template,
            scheduled_date=now.date(),
            scheduled_time_begin=now,
            scheduled_time_end=now + timedelta(hours=1),
            is_active=False,
            created_at=now,
            updated_at=now,
            send_window_begin=now.time().replace(second=0, microsecond=0),
            send_window_end=(now + timedelta(hours=1)).time().replace(second=0, microsecond=0),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.BULK,
        )
        self.mailing.bot_profiles.add(self.bot)

    def test_dry_run_does_not_write_channels(self):
        guest = Guest.objects.create(phone="+79991110001")
        self._mailing_row(guest=guest, external_id="hist-chat-1", delivery_status="done")
        out = StringIO()

        call_command(
            "sync_historical_telegram_channels",
            mailing_id=self.mailing.id,
            dry_run=True,
            stdout=out,
        )

        self.assertEqual(HistoricalTelegramChannel.objects.count(), 0)
        self.assertIn("Будет создано: 1", out.getvalue())

    def test_command_imports_only_successful_rows_and_is_idempotent(self):
        successful_guest = Guest.objects.create(phone="+79991110002")
        failed_guest = Guest.objects.create(phone="+79991110003")
        self._mailing_row(guest=successful_guest, external_id="hist-chat-2", delivery_status="done")
        self._mailing_row(guest=failed_guest, external_id="hist-chat-3", delivery_status="dispatch_failed")

        call_command("sync_historical_telegram_channels", mailing_id=self.mailing.id, stdout=StringIO())
        call_command("sync_historical_telegram_channels", mailing_id=self.mailing.id, stdout=StringIO())

        channels = list(HistoricalTelegramChannel.objects.order_by("guest_id"))
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].guest_id, successful_guest.id)
        self.assertEqual(channels[0].telegram_chat_id, "hist-chat-2")
        self.assertEqual(channels[0].delivery_state, HistoricalTelegramChannel.DeliveryState.SENDABLE)

    def test_command_does_not_reactivate_blocked_channel(self):
        guest = Guest.objects.create(phone="+79991110004")
        HistoricalTelegramChannel.objects.create(
            guest=guest,
            bot_profile=self.bot,
            telegram_chat_id="old-blocked-chat",
            delivery_state=HistoricalTelegramChannel.DeliveryState.BLOCKED,
            last_error_text="blocked",
        )
        self._mailing_row(guest=guest, external_id="new-chat-4", delivery_status="done")

        call_command("sync_historical_telegram_channels", mailing_id=self.mailing.id, stdout=StringIO())

        channel = HistoricalTelegramChannel.objects.get(guest=guest, bot_profile=self.bot)
        self.assertEqual(channel.delivery_state, HistoricalTelegramChannel.DeliveryState.BLOCKED)
        self.assertEqual(channel.telegram_chat_id, "old-blocked-chat")

    def test_command_skips_chat_id_conflict(self):
        owner = Guest.objects.create(phone="+79991110005")
        candidate = Guest.objects.create(phone="+79991110006")
        HistoricalTelegramChannel.objects.create(
            guest=owner,
            bot_profile=self.bot,
            telegram_chat_id="shared-chat",
            delivery_state=HistoricalTelegramChannel.DeliveryState.SENDABLE,
        )
        self._mailing_row(guest=candidate, external_id="shared-chat", delivery_status="done")
        out = StringIO()

        call_command("sync_historical_telegram_channels", mailing_id=self.mailing.id, stdout=out)

        self.assertFalse(
            HistoricalTelegramChannel.objects.filter(guest=candidate, bot_profile=self.bot).exists()
        )
        self.assertIn("Пропущено из-за конфликта chat_id: 1", out.getvalue())

    def test_successful_dispatch_task_is_enough_for_import(self):
        guest = Guest.objects.create(phone="+79991110007")
        row = self._mailing_row(
            guest=guest,
            external_id="hist-chat-7",
            delivery_status="queued_to_dispatch",
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            priority=DispatchTask.Priority.BULK,
            status=DispatchTask.Status.DONE,
            guest=guest,
            mailing_guest=row,
            bot_profile=self.bot,
            external_chat_id="hist-chat-7",
            message_text="Привет",
            payload={},
            available_at=timezone.now(),
            finished_at=timezone.now(),
        )

        call_command("sync_historical_telegram_channels", mailing_id=self.mailing.id, stdout=StringIO())

        self.assertTrue(
            HistoricalTelegramChannel.objects.filter(
                guest=guest,
                bot_profile=self.bot,
                telegram_chat_id="hist-chat-7",
            ).exists()
        )

    def _mailing_row(self, *, guest: Guest, external_id: str, delivery_status: str) -> MailingGuest:
        now = timezone.now()
        status = MailingGuest.Status.DONE if delivery_status == "done" else MailingGuest.Status.ERROR
        return MailingGuest.objects.create(
            mailing=self.mailing,
            guest=guest,
            phone=guest.phone,
            text_mailing_list="Текст",
            scheduled_datetime=now,
            status=status,
            external_id=external_id,
            sent_at=now if delivery_status == "done" else None,
            delivery_status=delivery_status,
            created_at=now,
        )
