"""
Тесты проверки доступности доставки обычных рассылок.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from guests.models import BotProfile, Guest, GuestBotBinding, Mailing, VtelemaxRecipientChannel
from guests.services.mailing_delivery_targets import build_mailing_delivery_plan


class MailingDeliveryTargetsTests(TestCase):
    """
    Проверяем расчёт аудитории, которой реально можно поставить задачи доставки.
    """

    def setUp(self):
        self.bot_telegram = BotProfile.objects.create(
            code="delivery_tg",
            name="Телеграм",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.bot_vk = BotProfile.objects.create(
            code="delivery_vk",
            name="ВК",
            provider_type=BotProfile.ProviderType.VK,
            is_active=True,
        )

    def test_plan_separates_deliverable_missing_binding_and_missing_permission(self):
        """
        Проверка разделяет доставляемых, гостей без привязки и гостей без согласия.
        """
        deliverable = Guest.objects.create(phone="+79990000001")
        without_binding = Guest.objects.create(phone="+79990000002")
        without_permission = Guest.objects.create(phone="+79990000003")

        self._binding(deliverable, self.bot_telegram, external_chat_id="tg-1", is_primary=True)
        self._binding(
            without_permission,
            self.bot_telegram,
            external_chat_id="tg-3",
            is_primary=True,
            is_opt_in=False,
        )

        plan = build_mailing_delivery_plan(
            [deliverable.id, without_binding.id, without_permission.id],
            selected_bot_ids=[self.bot_telegram.id],
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
        )

        self.assertEqual(plan.total_guests, 3)
        self.assertEqual(plan.deliverable_guests, 1)
        self.assertEqual(plan.blocked_without_bot_binding, 1)
        self.assertEqual(plan.blocked_without_message_permission, 1)
        self.assertEqual(plan.planned_dispatch_tasks, 1)
        self.assertEqual(plan.deliverable_guest_ids, (deliverable.id,))

    def test_all_bots_counts_every_available_target(self):
        """
        В режиме «все активные боты» один гость может дать несколько задач доставки.
        """
        guest = Guest.objects.create(phone="+79990000004")
        self._binding(guest, self.bot_telegram, external_chat_id="tg-4", is_primary=True)
        self._binding(guest, self.bot_vk, external_chat_id="vk-4", is_primary=False)

        plan = build_mailing_delivery_plan(
            [guest.id],
            selected_bot_ids=[self.bot_telegram.id, self.bot_vk.id],
            target_mode=Mailing.TargetMode.ALL_BOTS,
        )

        self.assertEqual(plan.deliverable_guests, 1)
        self.assertEqual(plan.planned_dispatch_tasks, 2)
        self.assertCountEqual(plan.rows[0].providers, ("telegram", "vk"))
        self.assertCountEqual(plan.rows[0].bot_codes, ("delivery_tg", "delivery_vk"))

    def test_primary_only_uses_primary_binding_or_first_available_binding(self):
        """
        Режим «только основной бот» повторяет поведение реальной отправки.
        """
        with_primary = Guest.objects.create(phone="+79990000005")
        without_primary = Guest.objects.create(phone="+79990000006")

        self._binding(with_primary, self.bot_vk, external_chat_id="vk-5", is_primary=False)
        self._binding(with_primary, self.bot_telegram, external_chat_id="tg-5", is_primary=True)
        self._binding(without_primary, self.bot_vk, external_chat_id="vk-6", is_primary=False)
        self._binding(without_primary, self.bot_telegram, external_chat_id="tg-6", is_primary=False)

        plan = build_mailing_delivery_plan(
            [with_primary.id, without_primary.id],
            selected_bot_ids=[self.bot_telegram.id, self.bot_vk.id],
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
        )

        rows_by_guest = {row.guest_id: row for row in plan.rows}
        self.assertEqual(rows_by_guest[with_primary.id].providers, ("telegram",))
        self.assertEqual(rows_by_guest[without_primary.id].target_count, 1)

    def test_primary_only_uses_latest_vtelemax_channel_activity(self):
        """
        Основной канал обычной рассылки выбирается по последней активности vtelemax.
        """
        guest = Guest.objects.create(phone="+79990000011")
        old_primary = self._binding(guest, self.bot_telegram, external_chat_id="tg-11", is_primary=True)
        latest_binding = self._binding(guest, self.bot_vk, external_chat_id="vk-11", is_primary=False)
        now = timezone.now()
        self._channel_for_binding(
            old_primary,
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            effective_updated_at=now - timedelta(days=3),
        )
        self._channel_for_binding(
            latest_binding,
            platform=VtelemaxRecipientChannel.Platform.VK,
            effective_updated_at=now - timedelta(hours=1),
        )

        plan = build_mailing_delivery_plan(
            [guest.id],
            selected_bot_ids=[self.bot_telegram.id, self.bot_vk.id],
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
        )

        self.assertEqual(plan.deliverable_guests, 1)
        self.assertEqual(plan.rows[0].providers, ("vk",))
        self.assertEqual(plan.rows[0].bot_codes, ("delivery_vk",))

    def test_inactive_selected_bot_does_not_make_guest_deliverable(self):
        """
        Неактивный выбранный бот не считается доступным каналом отправки.
        """
        inactive_bot = BotProfile.objects.create(
            code="delivery_inactive",
            name="Неактивный бот",
            provider_type=BotProfile.ProviderType.MAX,
            is_active=False,
        )
        guest = Guest.objects.create(phone="+79990000007")
        self._binding(guest, inactive_bot, external_chat_id="max-7", is_primary=True)

        plan = build_mailing_delivery_plan(
            [guest.id],
            selected_bot_ids=[inactive_bot.id],
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
        )

        self.assertEqual(plan.active_selected_bots_total, 0)
        self.assertEqual(plan.deliverable_guests, 0)
        self.assertEqual(plan.blocked_without_bot_binding, 1)

    def test_legacy_telegram_channel_makes_guest_deliverable_without_new_binding(self):
        """
        Legacy-гость без GuestBotBinding может получить обычную рассылку через старый Telegram-канал.
        """
        guest = Guest.objects.create(phone="+79990000008")
        self._legacy_telegram_channel(guest, external_id="legacy-tg-8")

        plan = build_mailing_delivery_plan(
            [guest.id],
            selected_bot_ids=[self.bot_telegram.id, self.bot_vk.id],
            target_mode=Mailing.TargetMode.ALL_BOTS,
        )

        self.assertEqual(plan.deliverable_guests, 1)
        self.assertEqual(plan.legacy_telegram_guests, 1)
        self.assertEqual(plan.blocked_without_bot_binding, 0)
        self.assertEqual(plan.rows[0].providers, ("telegram",))
        self.assertEqual(plan.rows[0].bot_codes, ("delivery_tg",))
        self.assertEqual(plan.rows[0].channel_modes, ("legacy_vtelemax_channel",))

    def test_legacy_telegram_channel_requires_selected_telegram_bot(self):
        """
        Legacy fallback не используется, если в рассылке не выбран Telegram-бот.
        """
        guest = Guest.objects.create(phone="+79990000009")
        self._legacy_telegram_channel(guest, external_id="legacy-tg-9")

        plan = build_mailing_delivery_plan(
            [guest.id],
            selected_bot_ids=[self.bot_vk.id],
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
        )

        self.assertEqual(plan.deliverable_guests, 0)
        self.assertEqual(plan.legacy_telegram_guests, 0)
        self.assertEqual(plan.blocked_without_bot_binding, 1)

    def test_new_binding_without_permission_is_not_rerouted_to_legacy_channel(self):
        """
        Если у гостя уже есть новая привязка, запрет отправки не обходится legacy-каналом.
        """
        guest = Guest.objects.create(phone="+79990000010")
        self._binding(
            guest,
            self.bot_telegram,
            external_chat_id="tg-10",
            is_primary=True,
            is_opt_in=False,
        )
        self._legacy_telegram_channel(guest, external_id="legacy-tg-10")

        plan = build_mailing_delivery_plan(
            [guest.id],
            selected_bot_ids=[self.bot_telegram.id],
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
        )

        self.assertEqual(plan.deliverable_guests, 0)
        self.assertEqual(plan.legacy_telegram_guests, 0)
        self.assertEqual(plan.blocked_without_message_permission, 1)

    def _binding(
        self,
        guest: Guest,
        bot: BotProfile,
        *,
        external_chat_id: str,
        is_primary: bool,
        is_opt_in: bool = True,
        is_stop_sending: bool = False,
    ) -> GuestBotBinding:
        return GuestBotBinding.objects.create(
            guest=guest,
            bot=bot,
            external_chat_id=external_chat_id,
            is_primary=is_primary,
            is_active=True,
            is_opt_in=is_opt_in,
            is_stop_sending=is_stop_sending,
        )

    @staticmethod
    def _legacy_telegram_channel(
        guest: Guest,
        *,
        external_id: str,
        notifications_allowed: bool = True,
    ) -> VtelemaxRecipientChannel:
        return VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            phone_e164=guest.phone,
            external_id=external_id,
            is_registered=True,
            notifications_allowed=notifications_allowed,
            guest=guest,
        )

    @staticmethod
    def _channel_for_binding(
        binding: GuestBotBinding,
        *,
        platform: str,
        effective_updated_at,
    ) -> VtelemaxRecipientChannel:
        return VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform=platform,
            phone_e164=binding.guest.phone,
            external_id=binding.external_chat_id,
            is_registered=True,
            notifications_allowed=True,
            effective_updated_at=effective_updated_at,
            guest=binding.guest,
            guest_binding=binding,
        )
