"""Исходящий контракт и атомарность отслеживаемых ссылок."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError, connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    GuestBotBinding,
    InteractionButtonSet,
    InteractionLinkLabelCode,
    Mailing,
    MailingGuest,
    MessageInteraction,
    MessageInteractionLinkDestination,
    MessageInteractionTrackedLink,
    MessageTemplate,
    NotificationScenario,
)
from guests.services.message_interaction_outgoing import (
    DispatchTaskCreationSpec,
    MessageInteractionConfigurationError,
    build_max_attachments,
    build_provider_interaction_parameters,
    build_service_data,
    build_telegram_reply_markup,
    build_vk_keyboard,
    create_dispatch_task_with_optional_interaction,
    create_dispatch_tasks_with_optional_interactions,
)
from guests.services.universal_queue.mailing_producer import (
    enqueue_mailing_rows_as_dispatch_tasks,
)
from guests.services.universal_queue.notification_producer import (
    enqueue_guest_notification_tasks,
)
from guests.services.universal_queue.provider_worker import AsyncProviderWorker


TRACKED_LINK_SETTINGS = {
    "MESSAGE_TRACKED_LINKS_ENABLED": True,
    "MESSAGE_TRACKED_LINK_PUBLIC_BASE_URL": "https://sagur.example/r/v1/",
    "MESSAGE_TRACKED_LINK_ALLOWED_HOSTS": {"rest.market"},
}
PUBLIC_TOKEN = "A" * 32
PUBLIC_URL = f"https://sagur.example/r/v1/{PUBLIC_TOKEN}"


def _unsaved_tracked_link(
    *,
    label_code: str = InteractionLinkLabelCode.DELIVERY,
    disabled: bool = False,
) -> MessageInteractionTrackedLink:
    """Возвращает снимок, достаточный для проверки чистых построителей."""

    return MessageInteractionTrackedLink(
        public_token=PUBLIC_TOKEN,
        label_code=label_code,
        target_url="https://rest.market/",
        disabled_at=timezone.now() if disabled else None,
    )


@override_settings(**TRACKED_LINK_SETTINGS)
class TrackedLinkPlatformContractTests(SimpleTestCase):
    """Проверяет неизменный трёхрядный контракт каждой платформы."""

    def test_service_data_keeps_version_two_actions_without_link_action(self):
        expected_actions = {"l": "ldm", "d": "dlm", "m": "mld"}

        for action, composite_action in expected_actions.items():
            with self.subTest(action=action):
                payload = json.loads(
                    build_service_data(
                        interaction_id=42,
                        button_set=InteractionButtonSet.RATING_MENU_LINK,
                        action=action,
                    )
                )
                self.assertEqual(payload, {"t": "si", "v": 2, "i": 42, "a": composite_action})

        with self.assertRaises(MessageInteractionConfigurationError):
            build_service_data(
                interaction_id=42,
                button_set=InteractionButtonSet.RATING_MENU_LINK,
                action="link",
            )

    def test_telegram_uses_url_only_in_the_second_row(self):
        markup = build_telegram_reply_markup(
            interaction_id=42,
            button_set=InteractionButtonSet.RATING_MENU_LINK,
            tracked_link=_unsaved_tracked_link(),
        )

        rows = markup["inline_keyboard"]
        self.assertEqual([len(row) for row in rows], [2, 1, 1])
        self.assertEqual(rows[1][0], {
            "text": "Заказать доставку",
            "url": PUBLIC_URL,
        })
        self.assertNotIn("style", rows[1][0])
        self.assertNotIn("callback_data", rows[1][0])
        self.assertEqual(json.loads(rows[0][0]["callback_data"])["a"], "ldm")
        self.assertEqual(json.loads(rows[2][0]["callback_data"])["a"], "mld")

    def test_vk_uses_open_link_only_in_the_second_row(self):
        keyboard = build_vk_keyboard(
            interaction_id=42,
            button_set=InteractionButtonSet.RATING_MENU_LINK,
            tracked_link=_unsaved_tracked_link(
                label_code=InteractionLinkLabelCode.BOOKING,
            ),
        )

        rows = keyboard["buttons"]
        self.assertEqual([len(row) for row in rows], [2, 1, 1])
        self.assertEqual(rows[1][0], {
            "action": {
                "type": "open_link",
                "label": "Забронировать столик",
                "link": PUBLIC_URL,
            },
        })
        self.assertNotIn("color", rows[1][0])
        self.assertNotIn("payload", rows[1][0]["action"])
        self.assertEqual(rows[2][0]["action"]["type"], "callback")

    def test_max_uses_link_only_in_the_second_row(self):
        attachments = build_max_attachments(
            interaction_id=42,
            button_set=InteractionButtonSet.RATING_MENU_LINK,
            tracked_link=_unsaved_tracked_link(),
        )

        rows = attachments[0]["payload"]["buttons"]
        self.assertEqual([len(row) for row in rows], [2, 1, 1])
        self.assertEqual(rows[1][0], {
            "type": "link",
            "text": "Заказать доставку",
            "url": PUBLIC_URL,
        })
        self.assertNotIn("payload", rows[1][0])
        self.assertEqual(rows[2][0]["type"], "callback")

    def test_missing_disabled_or_malformed_link_stops_building(self):
        invalid_links = (
            None,
            _unsaved_tracked_link(disabled=True),
            MessageInteractionTrackedLink(
                public_token="short",
                label_code=InteractionLinkLabelCode.DELIVERY,
                target_url="https://rest.market/",
            ),
        )
        for tracked_link in invalid_links:
            with self.subTest(tracked_link=tracked_link):
                with self.assertRaises(MessageInteractionConfigurationError):
                    build_telegram_reply_markup(
                        interaction_id=42,
                        button_set=InteractionButtonSet.RATING_MENU_LINK,
                        tracked_link=tracked_link,
                    )

    @override_settings(MESSAGE_TRACKED_LINK_PUBLIC_BASE_URL="https://sagur.example/wrong/")
    def test_inexact_public_path_is_rejected(self):
        with self.assertRaises(MessageInteractionConfigurationError):
            build_vk_keyboard(
                interaction_id=42,
                button_set=InteractionButtonSet.RATING_MENU_LINK,
                tracked_link=_unsaved_tracked_link(),
            )


@override_settings(**TRACKED_LINK_SETTINGS)
class TrackedLinkAtomicCreationTests(TestCase):
    """Проверяет единую транзакцию задачи, интерактивности и ссылки."""

    def setUp(self):
        self.destination = MessageInteractionLinkDestination.objects.create(
            code="delivery_test",
            name="Тестовая доставка",
            label_code=InteractionLinkLabelCode.DELIVERY,
            target_url="https://rest.market/",
            is_active=True,
        )

    @staticmethod
    def _task_fields(idempotency_key: str) -> dict[str, object]:
        return {
            "source_type": DispatchTask.SourceType.SYSTEM,
            "provider_type": BotProfile.ProviderType.TELEGRAM,
            "priority": DispatchTask.Priority.NORMAL,
            "status": DispatchTask.Status.PENDING,
            "external_chat_id": "tracked-link-chat",
            "message_text": "Проверка ссылки",
            "payload": {},
            "idempotency_key": idempotency_key,
        }

    def _create_link_task(self, idempotency_key: str) -> DispatchTask:
        return create_dispatch_task_with_optional_interaction(
            button_set=InteractionButtonSet.RATING_MENU_LINK,
            interaction_enabled=True,
            tracked_link_destination=self.destination,
            **self._task_fields(idempotency_key),
        )

    def test_single_creation_copies_snapshot_and_generates_192_bit_token(self):
        task = self._create_link_task("tracked-link-single")

        tracked_link = task.message_interaction.tracked_link
        self.assertEqual(tracked_link.label_code, InteractionLinkLabelCode.DELIVERY)
        self.assertEqual(tracked_link.target_url, "https://rest.market/")
        self.assertRegex(tracked_link.public_token, r"^[A-Za-z0-9_-]{32}$")
        self.assertEqual(MessageInteractionTrackedLink.objects.count(), 1)

    def test_missing_inactive_unsaved_or_http_destination_creates_nothing(self):
        inactive = MessageInteractionLinkDestination.objects.create(
            code="inactive_test",
            name="Неактивное назначение",
            label_code=InteractionLinkLabelCode.DETAILS,
            target_url="https://rest.market/details",
            is_active=False,
        )
        insecure = MessageInteractionLinkDestination(
            code="insecure_test",
            name="Незащищённый адрес",
            label_code=InteractionLinkLabelCode.WEBSITE,
            target_url="http://rest.market/",
            is_active=True,
        )
        unsaved = MessageInteractionLinkDestination(
            code="unsaved_test",
            name="Несохранённое назначение",
            label_code=InteractionLinkLabelCode.WEBSITE,
            target_url="https://rest.market/",
            is_active=True,
        )

        for index, destination in enumerate(
            (None, inactive, insecure, unsaved)
        ):
            with self.subTest(destination=destination):
                with self.assertRaises(MessageInteractionConfigurationError):
                    create_dispatch_task_with_optional_interaction(
                        button_set=InteractionButtonSet.RATING_MENU_LINK,
                        interaction_enabled=True,
                        tracked_link_destination=destination,
                        **self._task_fields(f"tracked-link-invalid-{index}"),
                    )

        self.assertEqual(DispatchTask.objects.count(), 0)
        self.assertEqual(MessageInteraction.objects.count(), 0)
        self.assertEqual(MessageInteractionTrackedLink.objects.count(), 0)

    @override_settings(MESSAGE_TRACKED_LINK_ALLOWED_HOSTS=set())
    def test_task_uses_saved_snapshot_without_rechecking_allowed_hosts(self):
        task = self._create_link_task("tracked-link-saved-snapshot")

        self.assertEqual(
            task.message_interaction.tracked_link.target_url,
            self.destination.target_url,
        )

    @override_settings(MESSAGE_TRACKED_LINKS_ENABLED=False)
    def test_link_switch_blocks_only_new_modern_link(self):
        with self.assertRaises(MessageInteractionConfigurationError):
            self._create_link_task("tracked-link-disabled")
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_historical_route_remains_plain_without_validating_link(self):
        self.destination.is_active = False
        self.destination.save(update_fields=["is_active"])

        task = create_dispatch_task_with_optional_interaction(
            button_set=InteractionButtonSet.RATING_MENU_LINK,
            interaction_enabled=False,
            tracked_link_destination=self.destination,
            **self._task_fields("tracked-link-historical"),
        )

        self.assertFalse(MessageInteraction.objects.filter(dispatch_task=task).exists())
        self.assertEqual(MessageInteractionTrackedLink.objects.count(), 0)

    def test_destination_for_non_link_set_is_rejected_before_task_creation(self):
        with self.assertRaises(MessageInteractionConfigurationError):
            create_dispatch_task_with_optional_interaction(
                button_set=InteractionButtonSet.RATING_MENU,
                interaction_enabled=True,
                tracked_link_destination=self.destination,
                **self._task_fields("tracked-link-wrong-set"),
            )
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_link_integrity_error_rolls_back_task_and_interaction(self):
        with patch.object(
            MessageInteractionTrackedLink.objects,
            "create",
            side_effect=IntegrityError("forced"),
        ) as create_link:
            with self.assertRaises(IntegrityError):
                self._create_link_task("tracked-link-rollback")

        self.assertEqual(create_link.call_count, 1)
        self.assertEqual(DispatchTask.objects.count(), 0)
        self.assertEqual(MessageInteraction.objects.count(), 0)

    def test_token_conflict_is_retried_without_duplicate_snapshot(self):
        first_task = self._create_link_task("tracked-link-first")
        existing_token = first_task.message_interaction.tracked_link.public_token
        replacement_token = "B" * 32

        with patch(
            "guests.services.message_interaction_outgoing._generate_public_token",
            side_effect=[existing_token, replacement_token],
        ):
            second_task = self._create_link_task("tracked-link-second")

        self.assertEqual(
            second_task.message_interaction.tracked_link.public_token,
            replacement_token,
        )
        self.assertEqual(MessageInteractionTrackedLink.objects.count(), 2)

    def test_bulk_path_uses_one_insert_for_each_affected_table(self):
        specifications = [
            DispatchTaskCreationSpec(
                button_set=InteractionButtonSet.RATING_MENU_LINK,
                interaction_enabled=True,
                tracked_link_destination=self.destination,
                dispatch_task_fields=self._task_fields(f"tracked-link-bulk-{index}"),
            )
            for index in range(5)
        ]

        with CaptureQueriesContext(connection) as captured:
            result = create_dispatch_tasks_with_optional_interactions(specifications)

        insert_queries = [
            query["sql"]
            for query in captured.captured_queries
            if "INSERT INTO" in query["sql"]
        ]
        self.assertEqual(len(result.created_tasks), 5)
        self.assertFalse(result.errors)
        self.assertEqual(
            sum('"dispatch_tasks"' in sql for sql in insert_queries),
            1,
        )
        self.assertEqual(
            sum('"message_interactions"' in sql for sql in insert_queries),
            1,
        )
        self.assertEqual(
            sum('"message_interaction_tracked_links"' in sql for sql in insert_queries),
            1,
        )

    def test_provider_parameters_use_stored_snapshot_not_destination(self):
        task = self._create_link_task("tracked-link-provider")
        self.destination.is_active = False
        self.destination.save(update_fields=["is_active"])

        parameters = build_provider_interaction_parameters(
            task=DispatchTask.objects.select_related(
                "message_interaction__tracked_link"
            ).get(pk=task.pk),
            provider_type=BotProfile.ProviderType.TELEGRAM,
        )

        self.assertEqual(
            parameters["reply_markup"]["inline_keyboard"][1][0]["url"],
            f"https://sagur.example/r/v1/{task.message_interaction.tracked_link.public_token}",
        )


@override_settings(
    MESSAGE_INTERACTIONS_ENABLED=True,
    MESSAGE_INTERACTIONS_ALLOWED_PROVIDERS={"telegram"},
    **TRACKED_LINK_SETTINGS,
)
class TrackedLinkProducerTests(TestCase):
    """Проверяет передачу назначения рассылкой и автосценарием."""

    def setUp(self):
        self.now = timezone.now()
        self.guest = Guest.objects.create(
            phone="+79990004321",
            first_name="Ссылка",
            created_at=self.now,
            updated_at=self.now,
        )
        self.bot = BotProfile.objects.create(
            code="tg_tracked_link",
            name="TG Tracked Link",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot,
            external_chat_id="tracked-link-producer",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        self.template = MessageTemplate.objects.create(
            name="Шаблон ссылки",
            message_text="Сообщение со ссылкой",
            is_active=True,
        )
        self.destination = MessageInteractionLinkDestination.objects.create(
            code="delivery_producer",
            name="Доставка для производителя",
            label_code=InteractionLinkLabelCode.DELIVERY,
            target_url="https://rest.market/",
            is_active=True,
        )

    def _create_mailing(self) -> tuple[Mailing, MailingGuest]:
        mailing = Mailing.objects.create(
            name="Рассылка со ссылкой",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now,
            scheduled_time_end=self.now + timedelta(hours=1),
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=self.now.time().replace(second=0, microsecond=0),
            send_window_end=(self.now + timedelta(hours=1)).time().replace(
                second=0,
                microsecond=0,
            ),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.BULK,
            button_set=InteractionButtonSet.RATING_MENU_LINK,
            tracked_link_destination=self.destination,
        )
        mailing.bot_profiles.add(self.bot)
        row = MailingGuest.objects.create(
            mailing=mailing,
            guest=self.guest,
            phone=self.guest.phone,
            text_mailing_list="Рассылка со ссылкой",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )
        return mailing, row

    def test_mailing_creates_tracked_link_snapshot(self):
        mailing, row = self._create_mailing()

        summary = enqueue_mailing_rows_as_dispatch_tasks(mailing, [row], now=self.now)

        self.assertEqual(summary.tasks_created, 1)
        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertEqual(task.message_interaction.tracked_link.target_url, "https://rest.market/")

    def test_historical_mailing_does_not_create_interaction_or_link(self):
        mailing, row = self._create_mailing()
        self.binding.delete()
        row.external_id = "historical-link-chat"
        row.save(update_fields=["external_id"])

        summary = enqueue_mailing_rows_as_dispatch_tasks(mailing, [row], now=self.now)

        self.assertEqual(summary.tasks_created, 1)
        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertFalse(MessageInteraction.objects.filter(dispatch_task=task).exists())
        self.assertEqual(MessageInteractionTrackedLink.objects.count(), 0)

    def test_notification_scenario_creates_tracked_link_snapshot(self):
        scenario = NotificationScenario.objects.create(
            code="tracked_link_scenario",
            name="Сценарий со ссылкой",
            template=self.template,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            button_set=InteractionButtonSet.RATING_MENU_LINK,
            tracked_link_destination=self.destination,
        )

        created = enqueue_guest_notification_tasks(
            guest=self.guest,
            message_text="Автоматическое сообщение со ссылкой",
            notification_scenario=scenario,
            source_key="tracked-link-notification",
        )

        self.assertEqual(created, 1)
        task = DispatchTask.objects.get(notification_scenario=scenario)
        self.assertEqual(task.message_interaction.tracked_link.label_code, "delivery")


@override_settings(**TRACKED_LINK_SETTINGS)
class TrackedLinkProviderWorkerLoadingTests(TestCase):
    """Проверяет загрузку ссылки без дополнительного запроса отправителя."""

    def test_claim_loads_interaction_and_tracked_link_with_task(self):
        destination = MessageInteractionLinkDestination.objects.create(
            code="delivery_worker",
            name="Доставка для работника",
            label_code=InteractionLinkLabelCode.DELIVERY,
            target_url="https://rest.market/",
            is_active=True,
        )
        task = create_dispatch_task_with_optional_interaction(
            button_set=InteractionButtonSet.RATING_MENU_LINK,
            interaction_enabled=True,
            tracked_link_destination=destination,
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            priority=DispatchTask.Priority.NORMAL,
            status=DispatchTask.Status.QUEUED,
            external_chat_id="tracked-link-worker",
            message_text="Текст",
            payload={},
        )

        with self.assertNumQueries(2):
            claimed_task = AsyncProviderWorker._claim_task_sync(task.id)
            public_token = claimed_task.message_interaction.tracked_link.public_token

        self.assertRegex(public_token, r"^[A-Za-z0-9_-]{32}$")
