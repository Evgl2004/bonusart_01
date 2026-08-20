"""Положительные и отрицательные тесты исходящей интерактивности."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx
from asgiref.sync import async_to_sync
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    GuestBotBinding,
    InteractionButtonSet,
    Mailing,
    MailingGuest,
    MessageInteraction,
    MessageTemplate,
    NotificationScenario,
)
from guests.services.message_interaction_outgoing import (
    MAX_SIGNED_BIGINT,
    TELEGRAM_CALLBACK_DATA_LIMIT_BYTES,
    DispatchTaskAlreadyExists,
    MessageInteractionConfigurationError,
    build_max_attachments,
    build_normalized_button_rows,
    build_service_data,
    build_telegram_reply_markup,
    build_vk_keyboard,
    create_dispatch_task_with_optional_interaction,
    interactions_enabled_for_new_task,
)
from guests.services.universal_queue.mailing_producer import enqueue_mailing_rows_as_dispatch_tasks
from guests.services.universal_queue.notification_producer import enqueue_guest_notification_tasks
from guests.services.universal_queue.provider_clients import (
    MaxAsyncSender,
    ProviderPermanentError,
    TelegramAsyncSender,
    VkAsyncSender,
)
from guests.services.universal_queue.provider_worker import AsyncProviderWorker


class MessageInteractionPayloadTests(SimpleTestCase):
    """Проверяет закрытый контракт служебного JSON и раскладки кнопок."""

    def test_version_two_contains_clicked_action_first_for_every_button(self):
        """Каждая кнопка передаёт утверждённый составной код с нажатием первым."""

        expected = {
            (InteractionButtonSet.RATING_MENU, "l"): "ldm",
            (InteractionButtonSet.RATING_MENU, "d"): "dlm",
            (InteractionButtonSet.RATING_MENU, "m"): "mld",
            (InteractionButtonSet.RATING_COUPONS, "l"): "ldc",
            (InteractionButtonSet.RATING_COUPONS, "d"): "dlc",
            (InteractionButtonSet.RATING_COUPONS, "c"): "cld",
        }

        for (button_set, action), composite_action in expected.items():
            with self.subTest(button_set=button_set, action=action):
                raw_value = build_service_data(
                    interaction_id=123456,
                    button_set=button_set,
                    action=action,
                )
                self.assertEqual(
                    json.loads(raw_value),
                    {"t": "si", "v": 2, "i": 123456, "a": composite_action},
                )

    def test_maximum_identifier_stays_within_telegram_limit(self):
        """Максимальный bigint оставляет подтверждённый запас по пределу Telegram."""

        raw_value = build_service_data(
            interaction_id=MAX_SIGNED_BIGINT,
            button_set=InteractionButtonSet.RATING_MENU,
            action="l",
        )

        self.assertEqual(len(raw_value.encode("utf-8")), 50)
        self.assertLessEqual(
            len(raw_value.encode("utf-8")),
            TELEGRAM_CALLBACK_DATA_LIMIT_BYTES,
        )

    def test_normalized_sets_keep_two_rows_and_expected_labels(self):
        """Оценки находятся в первом ряду, навигация — во втором."""

        menu_rows = build_normalized_button_rows(
            interaction_id=1,
            button_set=InteractionButtonSet.RATING_MENU,
        )
        coupon_rows = build_normalized_button_rows(
            interaction_id=2,
            button_set=InteractionButtonSet.RATING_COUPONS,
        )

        self.assertEqual([[button.action for button in row] for row in menu_rows], [["l", "d"], ["m"]])
        self.assertEqual([[button.action for button in row] for row in coupon_rows], [["l", "d"], ["c"]])
        self.assertEqual(menu_rows[0][0].text, "👍 Нравится")
        self.assertEqual(menu_rows[0][1].text, "👎 Не нравится")
        self.assertEqual(menu_rows[1][0].text, "☰ Меню")
        self.assertEqual(coupon_rows[1][0].text, "🎟 В купоны")

    def test_platform_builders_use_verified_structures_and_styles(self):
        """Платформенные структуры совпадают с живыми исходящими проверками."""

        telegram = build_telegram_reply_markup(
            interaction_id=10,
            button_set=InteractionButtonSet.RATING_MENU,
        )
        vk = build_vk_keyboard(
            interaction_id=10,
            button_set=InteractionButtonSet.RATING_MENU,
        )
        max_attachments = build_max_attachments(
            interaction_id=10,
            button_set=InteractionButtonSet.RATING_MENU,
        )

        self.assertEqual(
            [[button["style"] for button in row] for row in telegram["inline_keyboard"]],
            [["success", "danger"], ["primary"]],
        )
        self.assertEqual(
            [[button["color"] for button in row] for row in vk["buttons"]],
            [["positive", "negative"], ["primary"]],
        )
        self.assertEqual(max_attachments[0]["type"], "inline_keyboard")
        self.assertEqual(
            [[button["type"] for button in row] for row in max_attachments[0]["payload"]["buttons"]],
            [["callback", "callback"], ["callback"]],
        )

    def test_invalid_identifiers_sets_and_actions_are_rejected(self):
        """Некорректные значения не должны превращаться в частично рабочие кнопки."""

        invalid_identifiers = (True, 0, -1, MAX_SIGNED_BIGINT + 1, "1")
        for interaction_id in invalid_identifiers:
            with self.subTest(interaction_id=interaction_id):
                with self.assertRaises(MessageInteractionConfigurationError):
                    build_service_data(
                        interaction_id=interaction_id,
                        button_set=InteractionButtonSet.RATING_MENU,
                        action="l",
                    )

        for button_set, action in (
            ("unknown", "l"),
            (InteractionButtonSet.NONE, "l"),
            (InteractionButtonSet.RATING_MENU, "c"),
            (InteractionButtonSet.RATING_COUPONS, "m"),
        ):
            with self.subTest(button_set=button_set, action=action):
                with self.assertRaises(MessageInteractionConfigurationError):
                    build_service_data(
                        interaction_id=1,
                        button_set=button_set,
                        action=action,
                    )

    @override_settings(
        MESSAGE_INTERACTIONS_ENABLED=True,
        MESSAGE_INTERACTIONS_ALLOWED_PROVIDERS="telegram, vk",
    )
    def test_string_provider_allowlist_is_split_by_commas(self):
        self.assertTrue(interactions_enabled_for_new_task("telegram"))
        self.assertTrue(interactions_enabled_for_new_task("VK"))
        self.assertFalse(interactions_enabled_for_new_task("max"))


class DispatchTaskInteractionAtomicityTests(TestCase):
    """Проверяет транзакционную границу задачи и интерактивности."""

    @staticmethod
    def _task_fields(idempotency_key: str) -> dict[str, object]:
        return {
            "source_type": DispatchTask.SourceType.SYSTEM,
            "provider_type": BotProfile.ProviderType.TELEGRAM,
            "priority": DispatchTask.Priority.NORMAL,
            "status": DispatchTask.Status.PENDING,
            "external_chat_id": "123456",
            "message_text": "Проверка атомарности",
            "payload": {},
            "idempotency_key": idempotency_key,
        }

    def test_interactive_task_and_interaction_are_created_together(self):
        task = create_dispatch_task_with_optional_interaction(
            button_set=InteractionButtonSet.RATING_MENU,
            interaction_enabled=True,
            **self._task_fields("interaction-atomic-success"),
        )

        self.assertEqual(task.message_interaction.button_set, InteractionButtonSet.RATING_MENU)
        self.assertEqual(DispatchTask.objects.count(), 1)
        self.assertEqual(MessageInteraction.objects.count(), 1)

    def test_none_or_disabled_interaction_does_not_create_relation(self):
        task_without_buttons = create_dispatch_task_with_optional_interaction(
            button_set=InteractionButtonSet.NONE,
            interaction_enabled=True,
            **self._task_fields("interaction-none"),
        )
        historical_task = create_dispatch_task_with_optional_interaction(
            button_set=InteractionButtonSet.RATING_MENU,
            interaction_enabled=False,
            **self._task_fields("interaction-disabled"),
        )

        self.assertFalse(MessageInteraction.objects.filter(dispatch_task=task_without_buttons).exists())
        self.assertFalse(MessageInteraction.objects.filter(dispatch_task=historical_task).exists())

    def test_idempotency_duplicate_has_separate_explicit_error(self):
        fields = self._task_fields("interaction-duplicate")
        create_dispatch_task_with_optional_interaction(
            button_set=InteractionButtonSet.RATING_MENU,
            interaction_enabled=True,
            **fields,
        )

        with self.assertRaises(DispatchTaskAlreadyExists):
            create_dispatch_task_with_optional_interaction(
                button_set=InteractionButtonSet.RATING_MENU,
                interaction_enabled=True,
                **fields,
            )

        self.assertEqual(DispatchTask.objects.count(), 1)
        self.assertEqual(MessageInteraction.objects.count(), 1)

    def test_interaction_integrity_error_rolls_back_new_task(self):
        with patch.object(MessageInteraction.objects, "create", side_effect=IntegrityError("forced")):
            with self.assertRaises(IntegrityError):
                create_dispatch_task_with_optional_interaction(
                    button_set=InteractionButtonSet.RATING_MENU,
                    interaction_enabled=True,
                    **self._task_fields("interaction-rollback"),
                )

        self.assertFalse(DispatchTask.objects.filter(idempotency_key="interaction-rollback").exists())


@override_settings(
    MESSAGE_INTERACTIONS_ENABLED=True,
    MESSAGE_INTERACTIONS_ALLOWED_PROVIDERS={"telegram", "vk", "max"},
)
class OutgoingInteractionProducerTests(TestCase):
    """Проверяет применение настройки к рассылкам и автосценариям."""

    def setUp(self):
        now = timezone.now()
        self.now = now
        self.guest = Guest.objects.create(
            phone="+79990001234",
            first_name="Интерактивность",
            created_at=now,
            updated_at=now,
        )
        self.bot = BotProfile.objects.create(
            code="tg_interaction_outgoing",
            name="TG Interaction Outgoing",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot,
            external_chat_id="interaction-chat",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        self.template = MessageTemplate.objects.create(
            name="Шаблон интерактивности",
            message_text="Тест интерактивности",
            is_active=True,
        )

    def _create_mailing(self, *, button_set: str) -> Mailing:
        mailing = Mailing.objects.create(
            name=f"Рассылка {button_set}",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now,
            scheduled_time_end=self.now + timedelta(hours=1),
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=self.now.time().replace(second=0, microsecond=0),
            send_window_end=(self.now + timedelta(hours=1)).time().replace(second=0, microsecond=0),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.BULK,
            button_set=button_set,
        )
        mailing.bot_profiles.add(self.bot)
        return mailing

    def _create_mailing_row(self, mailing: Mailing, *, external_id: str | None = None) -> MailingGuest:
        return MailingGuest.objects.create(
            mailing=mailing,
            guest=self.guest,
            phone=self.guest.phone,
            external_id=external_id,
            text_mailing_list="Исходящее сообщение",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )

    def test_modern_mailing_creates_selected_interaction(self):
        mailing = self._create_mailing(button_set=InteractionButtonSet.RATING_COUPONS)
        row = self._create_mailing_row(mailing)

        summary = enqueue_mailing_rows_as_dispatch_tasks(mailing, [row], now=self.now)

        self.assertEqual(summary.tasks_created, 1)
        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertEqual(task.message_interaction.button_set, InteractionButtonSet.RATING_COUPONS)

    def test_historical_external_id_route_ignores_selected_buttons(self):
        self.binding.delete()
        mailing = self._create_mailing(button_set=InteractionButtonSet.RATING_MENU)
        row = self._create_mailing_row(mailing, external_id="historical-chat")

        summary = enqueue_mailing_rows_as_dispatch_tasks(mailing, [row], now=self.now)

        self.assertEqual(summary.tasks_created, 1)
        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertEqual(task.payload["channel_mode"], "mailing_row_external_id")
        self.assertFalse(MessageInteraction.objects.filter(dispatch_task=task).exists())

    def test_notification_scenario_creates_selected_interaction(self):
        scenario = NotificationScenario.objects.create(
            code="interaction_outgoing_scenario",
            name="Интерактивный сценарий",
            template=self.template,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            button_set=InteractionButtonSet.RATING_MENU,
        )

        created = enqueue_guest_notification_tasks(
            guest=self.guest,
            message_text="Автоматическое сообщение",
            notification_scenario=scenario,
            source_key="interaction-notification",
        )

        self.assertEqual(created, 1)
        task = DispatchTask.objects.get(notification_scenario=scenario)
        self.assertEqual(task.message_interaction.button_set, InteractionButtonSet.RATING_MENU)

    def test_notification_without_scenario_remains_plain(self):
        created = enqueue_guest_notification_tasks(
            guest=self.guest,
            message_text="Системное сообщение",
            source_key="plain-notification",
        )

        self.assertEqual(created, 1)
        task = DispatchTask.objects.get()
        self.assertFalse(MessageInteraction.objects.filter(dispatch_task=task).exists())

    @override_settings(MESSAGE_INTERACTIONS_ENABLED=False)
    def test_global_switch_disables_only_new_interaction(self):
        mailing = self._create_mailing(button_set=InteractionButtonSet.RATING_MENU)
        row = self._create_mailing_row(mailing)

        summary = enqueue_mailing_rows_as_dispatch_tasks(mailing, [row], now=self.now)

        self.assertEqual(summary.tasks_created, 1)
        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertFalse(MessageInteraction.objects.filter(dispatch_task=task).exists())

    @override_settings(MESSAGE_INTERACTIONS_ALLOWED_PROVIDERS={"vk", "max"})
    def test_provider_allowlist_keeps_plain_delivery_for_disabled_platform(self):
        mailing = self._create_mailing(button_set=InteractionButtonSet.RATING_MENU)
        row = self._create_mailing_row(mailing)

        summary = enqueue_mailing_rows_as_dispatch_tasks(mailing, [row], now=self.now)

        self.assertEqual(summary.tasks_created, 1)
        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertEqual(task.provider_type, BotProfile.ProviderType.TELEGRAM)
        self.assertFalse(MessageInteraction.objects.filter(dispatch_task=task).exists())


class ProviderInteractionRequestTests(SimpleTestCase):
    """Проверяет фактические тела запросов без внешней сети."""

    class _BotProfileStub:
        @staticmethod
        def resolve_token() -> str:
            return "test-token"

    class _InteractionStub:
        def __init__(self, interaction_id: int, button_set: str):
            self.id = interaction_id
            self.button_set = button_set

    class _TaskStub:
        def __init__(self, *, interaction=None, payload=None):
            self.bot_profile = ProviderInteractionRequestTests._BotProfileStub()
            self.payload = payload or {}
            if interaction is not None:
                self.message_interaction = interaction

    @staticmethod
    def _response(payload: dict[str, object]) -> httpx.Response:
        request = httpx.Request("POST", "https://provider.example/send")
        return httpx.Response(200, json=payload, request=request)

    def _interactive_task(self) -> _TaskStub:
        return self._TaskStub(
            interaction=self._InteractionStub(321, InteractionButtonSet.RATING_MENU),
            payload={"max_user_id": "max-user"},
        )

    def test_telegram_sender_includes_two_row_keyboard(self):
        sender = TelegramAsyncSender()
        sender.client = Mock()
        sender.client.post = AsyncMock(
            return_value=self._response({"ok": True, "result": {"message_id": 1}})
        )

        async_to_sync(sender.send)(self._interactive_task(), "telegram-chat", "Текст")

        body = sender.client.post.call_args.kwargs["json"]
        keyboard = body["reply_markup"]["inline_keyboard"]
        self.assertEqual(
            [[button["style"] for button in row] for row in keyboard],
            [["success", "danger"], ["primary"]],
        )
        self.assertEqual(json.loads(keyboard[0][0]["callback_data"])["a"], "ldm")

    def test_vk_sender_includes_compact_two_row_keyboard(self):
        sender = VkAsyncSender()
        sender.client = Mock()
        sender.client.post = AsyncMock(
            return_value=self._response({"response": {"message_id": 2}})
        )

        with patch("guests.services.universal_queue.provider_clients.random.randint", return_value=1):
            async_to_sync(sender.send)(self._interactive_task(), "vk-peer", "Текст")

        request_data = sender.client.post.call_args.kwargs["data"]
        keyboard = json.loads(request_data["keyboard"])
        self.assertEqual(
            [[button["color"] for button in row] for row in keyboard["buttons"]],
            [["positive", "negative"], ["primary"]],
        )
        self.assertNotIn("\": ", request_data["keyboard"])
        self.assertNotIn(", ", request_data["keyboard"])

    @override_settings(MAX_API_BASE_URL="https://platform-api.max.ru")
    def test_max_sender_includes_two_row_keyboard_attachment(self):
        sender = MaxAsyncSender()
        sender.client = Mock()
        sender.client.post = AsyncMock(return_value=self._response({"id": "max-message"}))

        async_to_sync(sender.send)(self._interactive_task(), "max-user", "Текст")

        body = sender.client.post.call_args.kwargs["json"]
        rows = body["attachments"][0]["payload"]["buttons"]
        self.assertEqual(
            [[button["text"] for button in row] for row in rows],
            [["👍 Нравится", "👎 Не нравится"], ["☰ Меню"]],
        )

    def test_plain_task_does_not_receive_keyboard(self):
        sender = TelegramAsyncSender()
        sender.client = Mock()
        sender.client.post = AsyncMock(
            return_value=self._response({"ok": True, "result": {"message_id": 3}})
        )

        async_to_sync(sender.send)(self._TaskStub(), "telegram-chat", "Обычный текст")

        body = sender.client.post.call_args.kwargs["json"]
        self.assertNotIn("reply_markup", body)

    def test_keyboard_error_prevents_external_request(self):
        sender = TelegramAsyncSender()
        sender.client = Mock()
        sender.client.post = AsyncMock()

        with patch(
            "guests.services.universal_queue.provider_clients.build_provider_interaction_parameters",
            side_effect=MessageInteractionConfigurationError("forced"),
        ):
            with self.assertRaises(ProviderPermanentError):
                async_to_sync(sender.send)(self._interactive_task(), "telegram-chat", "Текст")

        sender.client.post.assert_not_awaited()


class ProviderWorkerInteractionLoadingTests(TestCase):
    """Проверяет отсутствие отдельного запроса за интерактивностью в работнике."""

    def test_claim_loads_interaction_with_task(self):
        task = DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            priority=DispatchTask.Priority.NORMAL,
            status=DispatchTask.Status.QUEUED,
            external_chat_id="worker-chat",
            message_text="Текст",
            payload={},
        )
        MessageInteraction.objects.create(
            dispatch_task=task,
            button_set=InteractionButtonSet.RATING_MENU,
        )

        with self.assertNumQueries(2):
            claimed_task = AsyncProviderWorker._claim_task_sync(task.id)
            interaction_id = claimed_task.message_interaction.id

        self.assertGreater(interaction_id, 0)
