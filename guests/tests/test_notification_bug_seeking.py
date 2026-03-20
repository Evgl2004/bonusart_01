"""
Bug-seeking тесты для notification-сервисов.

Фокус:
1. Некорректные входные данные (payload/списки bot_id);
2. Безопасная деградация вместо падения;
3. Контроль маршрутизации, чтобы не допустить «рассылку всем» при плохом фильтре.
"""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    GuestBotBinding,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
    NotificationScenarioBotProfileLink,
)
from guests.services.notification_events import create_notification_event
from guests.services.universal_queue.notification_producer import enqueue_guest_notification_tasks


class NotificationBugSeekingTests(TestCase):
    """
    Набор негативных сценариев для producer и create_notification_event.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.guest = Guest.objects.create(
            phone="+79998887766",
            first_name="Тест",
            created_at=now,
            updated_at=now,
        )
        self.template = MessageTemplate.objects.create(
            name="BUG_SEEK_TEMPLATE",
            description="Шаблон для bug-seeking тестов",
            message_text="Здравствуйте, {first_name}! {message_text}",
            created_by="tests",
            is_active=True,
        )
        self.tg_primary = BotProfile.objects.create(
            code="tg_bug_seek_primary",
            name="TG Bug Seek Primary",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.tg_secondary = BotProfile.objects.create(
            code="tg_bug_seek_secondary",
            name="TG Bug Seek Secondary",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.tg_primary,
            external_chat_id="chat-primary",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.tg_secondary,
            external_chat_id="chat-secondary",
            is_primary=False,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

    def _create_scenario(self, code: str) -> NotificationScenario:
        """
        Создаёт активный webhook-сценарий для теста.
        """
        return NotificationScenario.objects.create(
            code=code,
            name=f"Scenario {code}",
            description="Bug-seeking scenario",
            is_active=True,
            is_system=False,
            trigger_type=NotificationScenario.TriggerType.WEBHOOK,
            template=self.template,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.ALL_BOTS,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
        )

    def test_enqueue_with_invalid_allowed_bot_ids_does_not_fanout(self):
        """
        При полностью невалидном allowed_bot_profile_ids producer не должен рассылать «всем».
        """
        created = enqueue_guest_notification_tasks(
            guest=self.guest,
            message_text="Тест",
            primary_only=False,
            allowed_bot_profile_ids=["bad", None, -5, "0"],
        )

        self.assertEqual(created, 0)
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_enqueue_with_mixed_allowed_bot_ids_filters_only_valid(self):
        """
        При смешанном списке bot_id должны использоваться только валидные и уникальные.
        """
        created = enqueue_guest_notification_tasks(
            guest=self.guest,
            message_text="Тест",
            primary_only=False,
            allowed_bot_profile_ids=["bad", self.tg_secondary.id, str(self.tg_secondary.id), -1],
        )

        self.assertEqual(created, 1)
        task = DispatchTask.objects.get()
        self.assertEqual(task.bot_profile_id, self.tg_secondary.id)
        self.assertEqual(task.external_chat_id, "chat-secondary")

    def test_enqueue_with_invalid_payload_type_uses_safe_fallback(self):
        """
        Некорректный тип payload не должен падать: сохраняем безопасную диагностическую структуру.
        """
        created = enqueue_guest_notification_tasks(
            guest=self.guest,
            message_text="Тест",
            primary_only=True,
            payload=["bad", "payload"],
        )

        self.assertEqual(created, 1)
        task = DispatchTask.objects.get()
        self.assertEqual(task.payload.get("payload_error"), "invalid_payload_type")
        self.assertEqual(task.payload.get("payload_type"), "list")
        self.assertIn("bad", task.payload.get("payload_preview", ""))

    def test_create_notification_event_accepts_scalar_route_allowed_bot_id(self):
        """
        Скалярный route_allowed_bot_profile_ids (int) должен корректно маршрутизировать задачу.
        """
        scenario = self._create_scenario("bug_seek_scalar_route_bot_id")

        created = create_notification_event(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="bug-scalar-route-1",
            source_ref="bug-scalar-route-1",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "scalar-route"},
            template_context={"first_name": "Тест", "message_text": "Маршрут"},
            fallback_message_text="Маршрут",
            route_target_mode=NotificationScenario.TargetMode.ALL_BOTS,
            route_allowed_bot_profile_ids=self.tg_secondary.id,
        )

        self.assertEqual(created, 1)
        task = DispatchTask.objects.get(notification_scenario=scenario)
        self.assertEqual(task.bot_profile_id, self.tg_secondary.id)

    def test_create_notification_event_accepts_scalar_string_route_allowed_bot_id(self):
        """
        Скалярный route_allowed_bot_profile_ids в виде строки не должен разбираться «по символам».
        """
        scenario = self._create_scenario("bug_seek_scalar_string_route_bot_id")

        created = create_notification_event(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="bug-scalar-string-route-1",
            source_ref="bug-scalar-string-route-1",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "scalar-string-route"},
            template_context={"first_name": "Тест", "message_text": "Маршрут строкой"},
            fallback_message_text="Маршрут строкой",
            route_target_mode=NotificationScenario.TargetMode.ALL_BOTS,
            route_allowed_bot_profile_ids=str(self.tg_primary.id),
        )

        self.assertEqual(created, 1)
        task = DispatchTask.objects.get(notification_scenario=scenario)
        self.assertEqual(task.bot_profile_id, self.tg_primary.id)

    def test_create_notification_event_with_invalid_payload_type_uses_safe_fallback(self):
        """
        Некорректный payload в create_notification_event не должен ронять событие/задачу.
        """
        scenario = self._create_scenario("bug_seek_invalid_event_payload")
        NotificationScenarioBotProfileLink.objects.create(
            scenario=scenario,
            bot_profile=self.tg_primary,
        )

        created = create_notification_event(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="bug-invalid-payload-1",
            source_ref="bug-invalid-payload-1",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload=["bad", "event-payload"],
            template_context={"first_name": "Тест", "message_text": "Payload fallback"},
            fallback_message_text="Payload fallback",
        )

        self.assertEqual(created, 1)
        event = NotificationEvent.objects.get(scenario=scenario)
        task = DispatchTask.objects.get(notification_scenario=scenario)

        self.assertEqual(event.payload.get("payload_error"), "invalid_payload_type")
        self.assertEqual(event.payload.get("payload_type"), "list")
        self.assertEqual(task.payload.get("payload_error"), "invalid_payload_type")
