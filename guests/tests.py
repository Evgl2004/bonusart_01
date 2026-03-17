"""
Интеграционные тесты цепочки:
NotificationScenario -> NotificationEvent -> DispatchTask.
"""

from datetime import timedelta

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
    Restaurant,
    VisitHistory,
)
from guests.services.notification_events import (
    SCENARIO_CODE_INACTIVE_7D,
    enqueue_notification_event_from_scenario,
)
from guests.services.notification_scenarios import run_scheduled_inactive_scenarios


class NotificationScenarioIntegrationTests(TestCase):
    """
    Проверка ключевых интеграционных сценариев новой модели уведомлений.
    """

    def setUp(self):
        self.guest = Guest.objects.create(
            phone="+79990001122",
            first_name="Иван",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        self.template = MessageTemplate.objects.create(
            name="TEST_TEMPLATE",
            description="Тестовый шаблон",
            message_text="Здравствуйте, {first_name}. {message_text}",
            created_by="test",
            is_active=True,
        )
        self.bot_profile = BotProfile.objects.create(
            code="tg_test_bot",
            name="Telegram Test Bot",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_profile,
            external_chat_id="123456",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

    def _create_scenario(
        self,
        *,
        code: str,
        trigger_type: str = NotificationScenario.TriggerType.WEBHOOK,
        distribution_mode: str = NotificationScenario.DistributionMode.IMMEDIATE,
        is_active: bool = True,
        settings: dict | None = None,
        cooldown_minutes: int = 0,
        max_per_day_per_guest: int | None = None,
    ) -> NotificationScenario:
        """
        Создаёт NotificationScenario для тестового кейса.
        """
        return NotificationScenario.objects.create(
            code=code,
            name=f"Scenario {code}",
            description="Тестовый сценарий",
            is_active=is_active,
            is_system=False,
            trigger_type=trigger_type,
            template=self.template,
            priority=NotificationScenario.Priority.HIGH,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=distribution_mode,
            timezone="Asia/Yekaterinburg",
            cooldown_minutes=cooldown_minutes,
            max_per_day_per_guest=max_per_day_per_guest,
            settings=settings or {},
        )

    def test_enqueue_notification_event_creates_event_and_task(self):
        """
        Проверяет базовую цепочку создания NotificationEvent и DispatchTask.
        """
        scenario = self._create_scenario(code="test_webhook_chain")

        created_tasks = enqueue_notification_event_from_scenario(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="webhook:1001",
            source_ref="1001",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "balance_changed"},
            template_context={"first_name": "Иван", "message_text": "Ваш баланс изменился"},
            fallback_message_text="Ваш баланс изменился",
        )

        self.assertEqual(created_tasks, 1)
        self.assertEqual(NotificationEvent.objects.count(), 1)
        self.assertEqual(DispatchTask.objects.count(), 1)

        event = NotificationEvent.objects.get()
        task = DispatchTask.objects.get()

        self.assertEqual(event.scenario_id, scenario.id)
        self.assertEqual(event.status, NotificationEvent.Status.TASK_CREATED)
        self.assertEqual(task.notification_event_id, event.id)
        self.assertEqual(task.notification_scenario_id, scenario.id)
        self.assertEqual(task.source_type, DispatchTask.SourceType.WEBHOOK)
        self.assertEqual(task.provider_type, BotProfile.ProviderType.TELEGRAM)
        self.assertEqual(task.external_chat_id, self.binding.external_chat_id)

    def test_enqueue_notification_event_deduplicates_by_scenario_and_key(self):
        """
        Проверяет дедупликацию: повтор не создаёт новую задачу доставки.
        """
        scenario = self._create_scenario(code="test_webhook_dedupe")

        first_created = enqueue_notification_event_from_scenario(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="webhook:2002",
            source_ref="2002",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "balance_changed"},
            template_context={"first_name": "Иван", "message_text": "Первый вызов"},
            fallback_message_text="Первый вызов",
        )
        second_created = enqueue_notification_event_from_scenario(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="webhook:2002",
            source_ref="2002",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "balance_changed"},
            template_context={"first_name": "Иван", "message_text": "Повтор"},
            fallback_message_text="Повтор",
        )

        self.assertEqual(first_created, 1)
        self.assertEqual(second_created, 0)
        self.assertEqual(NotificationEvent.objects.count(), 1)
        self.assertEqual(DispatchTask.objects.count(), 1)

        event = NotificationEvent.objects.get()
        self.assertEqual(event.duplicate_hits, 1)
        self.assertIsNotNone(event.last_duplicate_at)

    def test_enqueue_notification_event_respects_cooldown_limit(self):
        """
        Проверяет, что при cooldown второе событие по сценарию пропускается.
        """
        scenario = self._create_scenario(
            code="test_webhook_cooldown",
            cooldown_minutes=10,
        )
        base_time = timezone.now()

        first_created = enqueue_notification_event_from_scenario(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="cooldown:1",
            source_ref="cooldown-1",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "balance_changed"},
            template_context={"first_name": "Иван", "message_text": "Первое событие"},
            fallback_message_text="Первое событие",
            event_at=base_time,
        )
        second_created = enqueue_notification_event_from_scenario(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="cooldown:2",
            source_ref="cooldown-2",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "balance_changed"},
            template_context={"first_name": "Иван", "message_text": "Второе событие"},
            fallback_message_text="Второе событие",
            event_at=base_time + timedelta(minutes=1),
        )

        self.assertEqual(first_created, 1)
        self.assertEqual(second_created, 0)
        self.assertEqual(DispatchTask.objects.count(), 1)

        skipped_event = NotificationEvent.objects.get(
            scenario=scenario,
            dedupe_key="cooldown:2",
        )
        self.assertEqual(skipped_event.status, NotificationEvent.Status.SKIPPED)
        self.assertIn("cooldown", (skipped_event.error_text or "").lower())

    def test_enqueue_notification_event_respects_daily_limit(self):
        """
        Проверяет дневной лимит: в тот же день событие пропускается, на следующий день проходит.
        """
        scenario = self._create_scenario(
            code="test_webhook_daily_limit",
            max_per_day_per_guest=1,
        )
        base_time = timezone.now()

        first_created = enqueue_notification_event_from_scenario(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="daily:1",
            source_ref="daily-1",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "balance_changed"},
            template_context={"first_name": "Иван", "message_text": "Первый в день"},
            fallback_message_text="Первый в день",
            event_at=base_time,
        )
        second_created = enqueue_notification_event_from_scenario(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="daily:2",
            source_ref="daily-2",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "balance_changed"},
            template_context={"first_name": "Иван", "message_text": "Второй в день"},
            fallback_message_text="Второй в день",
            event_at=base_time + timedelta(hours=1),
        )
        third_created = enqueue_notification_event_from_scenario(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="daily:3",
            source_ref="daily-3",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "balance_changed"},
            template_context={"first_name": "Иван", "message_text": "Следующий день"},
            fallback_message_text="Следующий день",
            event_at=base_time + timedelta(days=1, minutes=1),
        )

        self.assertEqual(first_created, 1)
        self.assertEqual(second_created, 0)
        self.assertEqual(third_created, 1)
        self.assertEqual(DispatchTask.objects.count(), 2)

        skipped_event = NotificationEvent.objects.get(
            scenario=scenario,
            dedupe_key="daily:2",
        )
        self.assertEqual(skipped_event.status, NotificationEvent.Status.SKIPPED)
        self.assertIn("лимит", (skipped_event.error_text or "").lower())

    def test_scheduled_inactive_runner_creates_event_and_task(self):
        """
        Проверяет цепочку для планового сценария неактивности (inactive_7d).
        """
        restaurant = Restaurant.objects.create(
            iiko_id="rest_001",
            name="Тестовый ресторан",
        )
        VisitHistory.objects.create(
            guest=self.guest,
            restaurant=restaurant,
            visit_date=timezone.now() - timedelta(days=10),
            visit_count=1,
        )
        scenario = NotificationScenario.objects.get(code=SCENARIO_CODE_INACTIVE_7D)
        scenario.template = self.template
        scenario.trigger_type = NotificationScenario.TriggerType.SCHEDULE
        scenario.distribution_mode = NotificationScenario.DistributionMode.IMMEDIATE
        scenario.is_active = True
        scenario.settings = {"inactive_days": 7, "coupon_required": False}
        scenario.save(
            update_fields=[
                "template",
                "trigger_type",
                "distribution_mode",
                "is_active",
                "settings",
                "updated_at",
            ]
        )

        first_stats = run_scheduled_inactive_scenarios(
            scenario_codes=[scenario.code],
            limit_per_scenario=100,
        )
        second_stats = run_scheduled_inactive_scenarios(
            scenario_codes=[scenario.code],
            limit_per_scenario=100,
        )

        first_stat = first_stats[scenario.code]
        second_stat = second_stats[scenario.code]

        self.assertEqual(first_stat.created_tasks, 1)
        self.assertGreaterEqual(first_stat.matched_guests, 1)
        self.assertEqual(second_stat.created_tasks, 0)
        self.assertGreaterEqual(second_stat.skipped_duplicate_or_no_targets, 1)

        self.assertEqual(NotificationEvent.objects.filter(scenario=scenario).count(), 1)
        self.assertEqual(
            DispatchTask.objects.filter(notification_scenario=scenario).count(),
            1,
        )
