"""
Интеграционные тесты цепочки:
NotificationScenario -> NotificationEvent -> DispatchTask.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from guests.admin import NotificationScenarioAdminForm
from guests.models import (
    BotProfile,
    Category,
    DispatchTask,
    Guest,
    GuestBotBinding,
    GuestCategory,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
    Restaurant,
    VisitHistory,
)
from guests.services.notification_registry import (
    SCENARIO_CODE_BALANCE_CHANGED,
    get_registered_notification_scenario_code_choices,
)
from guests.services.notification_handler_registry import (
    run_registered_schedule_scenarios,
    run_webhook_scenario_by_code,
)
from guests.services.notification_events import (
    SCENARIO_CODE_INACTIVE_7D,
    create_notification_event,
    enqueue_notification_event_from_scenario,
)
from guests.services.notification_scenarios import run_scheduled_inactive_scenarios
from guests.services.webhooks import handle_api_webhook


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
        priority: str = NotificationScenario.Priority.HIGH,
        target_mode: str = NotificationScenario.TargetMode.PRIMARY_ONLY,
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
            priority=priority,
            target_mode=target_mode,
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

    def test_webhook_registry_runner_executes_balance_handler(self):
        """
        Проверяет запуск balance webhook-сценария через реестр code -> handler.
        """
        webhook = {
            "id": "wh_balance_1001",
            "category_id_ext": "BSamfrT83o4Cw5ZG1m4RU7N4CtW6WR2M",
            "parsed_body": {
                "phone": self.guest.phone,
                "notificationType": 9,
                "changeSum": "150",
                "text": "Баланс изменён на 150",
            },
        }

        created_tasks = run_webhook_scenario_by_code(
            scenario_code=SCENARIO_CODE_BALANCE_CHANGED,
            webhook=webhook,
            is_enabled=True,
            priority=DispatchTask.Priority.HIGH,
            primary_only=True,
        )

        self.assertEqual(created_tasks, 1)
        self.assertEqual(NotificationEvent.objects.count(), 1)
        self.assertEqual(DispatchTask.objects.count(), 1)

        event = NotificationEvent.objects.get()
        task = DispatchTask.objects.get()
        self.assertEqual(event.scenario.code, SCENARIO_CODE_BALANCE_CHANGED)
        self.assertEqual(task.notification_event_id, event.id)

    def test_webhook_registry_runner_returns_zero_for_unknown_code(self):
        """
        Для неизвестного webhook-кода реестр не падает и возвращает 0.
        """
        webhook = {
            "id": "wh_unknown_2002",
            "parsed_body": {"phone": self.guest.phone},
        }
        created_tasks = run_webhook_scenario_by_code(
            scenario_code="unknown_webhook_code",
            webhook=webhook,
            is_enabled=True,
            priority=DispatchTask.Priority.HIGH,
            primary_only=True,
        )

        self.assertEqual(created_tasks, 0)

    def test_webhook_registry_balance_call_overrides_scenario_routing(self):
        """
        Для balance-сценария параметры вызова должны иметь приоритет
        над routing-настройками сценария из БД.
        """
        second_bot = BotProfile.objects.create(
            code="tg_balance_override_bot",
            name="Telegram Balance Override Bot",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=second_bot,
            external_chat_id="654321",
            is_primary=False,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        scenario = NotificationScenario.objects.get(code=SCENARIO_CODE_BALANCE_CHANGED)
        scenario.priority = NotificationScenario.Priority.BULK
        scenario.target_mode = NotificationScenario.TargetMode.ALL_BOTS
        scenario.save(update_fields=["priority", "target_mode", "updated_at"])

        webhook = {
            "id": "wh_balance_override_9001",
            "category_id_ext": "BSamfrT83o4Cw5ZG1m4RU7N4CtW6WR2M",
            "parsed_body": {
                "phone": self.guest.phone,
                "notificationType": 9,
                "changeSum": "300",
                "text": "Баланс изменён на 300",
            },
        }

        created_tasks = run_webhook_scenario_by_code(
            scenario_code=SCENARIO_CODE_BALANCE_CHANGED,
            webhook=webhook,
            is_enabled=True,
            priority=DispatchTask.Priority.HIGH,
            primary_only=True,
        )

        self.assertEqual(created_tasks, 1)
        task = DispatchTask.objects.get()
        self.assertEqual(task.priority, DispatchTask.Priority.HIGH)
        self.assertEqual(task.external_chat_id, "123456")

    def test_handle_api_webhook_routes_balance_to_registry(self):
        """
        Центральный обработчик webhook должен вести balance-событие
        через реестр `run_webhook_scenario_by_code`.
        """
        webhook = {
            "id": "wh_balance_3003",
            "category_id_ext": "BSamfrT83o4Cw5ZG1m4RU7N4CtW6WR2M",
            "parsed_body": {
                "phone": self.guest.phone,
                "notificationType": 9,
                "changeSum": "200",
                "text": "Баланс изменён на 200",
            },
        }

        assigned, reason = handle_api_webhook(
            webhook,
            send_balance_notification=True,
        )

        self.assertTrue(assigned)
        self.assertIn("balance webhook processed", reason)
        self.assertEqual(NotificationEvent.objects.count(), 1)
        self.assertEqual(DispatchTask.objects.count(), 1)

    def test_handle_api_webhook_balance_respects_send_toggle(self):
        """
        При `send_balance_notification=False` вебхук считается обработанным,
        но задачи доставки не создаются.
        """
        webhook = {
            "id": "wh_balance_3004",
            "category_id_ext": "BSamfrT83o4Cw5ZG1m4RU7N4CtW6WR2M",
            "parsed_body": {
                "phone": self.guest.phone,
                "notificationType": 9,
                "changeSum": "250",
                "text": "Баланс изменён на 250",
            },
        }

        assigned, reason = handle_api_webhook(
            webhook,
            send_balance_notification=False,
        )

        self.assertTrue(assigned)
        self.assertIn("enqueued=0", reason)
        self.assertEqual(NotificationEvent.objects.count(), 0)
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_create_notification_event_uses_route_overrides(self):
        """
        Явные route-override должны иметь приоритет над настройками сценария из БД.
        """
        second_bot = BotProfile.objects.create(
            code="tg_test_bot_second",
            name="Telegram Test Bot 2",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=second_bot,
            external_chat_id="777777",
            is_primary=False,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        scenario = self._create_scenario(
            code="test_route_overrides",
            priority=NotificationScenario.Priority.BULK,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
        )

        created_tasks = create_notification_event(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="route:override:1",
            source_ref="route-override-1",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "routing_override"},
            template_context={"first_name": "Иван", "message_text": "Проверка override"},
            fallback_message_text="Проверка override",
            route_priority=DispatchTask.Priority.HIGH,
            route_target_mode=NotificationScenario.TargetMode.ALL_BOTS,
        )

        self.assertEqual(created_tasks, 2)
        tasks = list(DispatchTask.objects.filter(notification_scenario=scenario).order_by("id"))
        self.assertEqual(len(tasks), 2)
        self.assertEqual({task.priority for task in tasks}, {DispatchTask.Priority.HIGH})
        self.assertEqual({task.external_chat_id for task in tasks}, {"123456", "777777"})

    def test_create_notification_event_without_override_uses_scenario_defaults(self):
        """
        Без route-override применяются значения priority/target_mode из сценария.
        """
        second_bot = BotProfile.objects.create(
            code="tg_test_bot_third",
            name="Telegram Test Bot 3",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=second_bot,
            external_chat_id="888888",
            is_primary=False,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        scenario = self._create_scenario(
            code="test_route_defaults",
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
        )

        created_tasks = create_notification_event(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="route:default:1",
            source_ref="route-default-1",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "routing_default"},
            template_context={"first_name": "Иван", "message_text": "Проверка default"},
            fallback_message_text="Проверка default",
        )

        self.assertEqual(created_tasks, 1)
        task = DispatchTask.objects.get(notification_scenario=scenario)
        self.assertEqual(task.priority, DispatchTask.Priority.NORMAL)
        self.assertEqual(task.external_chat_id, "123456")

    def test_create_notification_event_route_allowed_bot_profiles(self):
        """
        route_allowed_bot_profile_ids должен ограничивать отправку выбранными ботами.
        """
        second_bot = BotProfile.objects.create(
            code="tg_test_bot_fourth",
            name="Telegram Test Bot 4",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=second_bot,
            external_chat_id="999999",
            is_primary=False,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        scenario = self._create_scenario(
            code="test_route_allowed_bots",
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.ALL_BOTS,
        )

        created_tasks = create_notification_event(
            scenario_code=scenario.code,
            guest=self.guest,
            dedupe_key="route:allowed:1",
            source_ref="route-allowed-1",
            event_source_type=NotificationEvent.SourceType.WEBHOOK,
            task_source_type=DispatchTask.SourceType.WEBHOOK,
            payload={"kind": "routing_allowed"},
            template_context={"first_name": "Иван", "message_text": "Проверка allowed bots"},
            fallback_message_text="Проверка allowed bots",
            route_allowed_bot_profile_ids=[self.bot_profile.id],
        )

        self.assertEqual(created_tasks, 1)
        task = DispatchTask.objects.get(notification_scenario=scenario)
        self.assertEqual(task.bot_profile_id, self.bot_profile.id)
        self.assertEqual(task.external_chat_id, "123456")

    def test_handle_api_webhook_notification_type_1_updates_visit_without_dispatch(self):
        """
        notificationType=1 обновляет VisitHistory и не создаёт задачи отправки.
        """
        restaurant = Restaurant.objects.create(
            iiko_id="rest_nt1_001",
            name="Ресторан nt1",
        )
        webhook = {
            "id": "wh_nt1_4001",
            "parsed_body": {
                "notificationType": 1,
                "phone": self.guest.phone,
                "terminalGroupId": restaurant.iiko_id,
                "changedOn": "2026-03-18T10:00:00+05:00",
            },
        }

        assigned, reason = handle_api_webhook(webhook, send_balance_notification=True)

        self.assertTrue(assigned, msg=reason)
        self.assertEqual(VisitHistory.objects.count(), 1)
        visit = VisitHistory.objects.get()
        self.assertEqual(visit.guest_id, self.guest.id)
        self.assertEqual(visit.restaurant_id, restaurant.id)
        self.assertEqual(NotificationEvent.objects.count(), 0)
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_handle_api_webhook_notification_type_5_assigns_category_without_dispatch(self):
        """
        notificationType=5 назначает категорию и не создаёт задачи отправки.
        """
        category = Category.objects.create(
            name="Любитель стейков",
            external_id="cat_nt5_001",
            is_active=True,
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        webhook = {
            "id": "wh_nt5_5001",
            "category_id_ext": category.external_id,
            "parsed_body": {
                "notificationType": 5,
                "phone": self.guest.phone,
                "changedOn": "2026-03-18T11:00:00+05:00",
            },
        }

        assigned, reason = handle_api_webhook(webhook, send_balance_notification=True)

        self.assertTrue(assigned, msg=reason)
        self.assertEqual(GuestCategory.objects.count(), 1)
        guest_category = GuestCategory.objects.get()
        self.assertEqual(guest_category.guest_id, self.guest.id)
        self.assertEqual(guest_category.category_id, category.id)
        self.assertEqual(NotificationEvent.objects.count(), 0)
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_enqueue_notification_event_respects_cooldown_limit(self):
        """
        Проверяет, что при cooldown второе событие откладывается, но не теряется.
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
        self.assertEqual(second_created, 1)
        self.assertEqual(DispatchTask.objects.count(), 2)

        first_event = NotificationEvent.objects.get(
            scenario=scenario,
            dedupe_key="cooldown:1",
        )
        deferred_event = NotificationEvent.objects.get(
            scenario=scenario,
            dedupe_key="cooldown:2",
        )
        self.assertEqual(deferred_event.status, NotificationEvent.Status.TASK_CREATED)
        self.assertGreaterEqual(
            deferred_event.planned_send_at,
            first_event.planned_send_at + timedelta(minutes=10),
        )

        second_task = DispatchTask.objects.get(notification_event=deferred_event)
        self.assertEqual(second_task.available_at, deferred_event.planned_send_at)

    def test_enqueue_notification_event_respects_daily_limit(self):
        """
        Проверяет дневной лимит: события не пропускаются, а разносятся по следующим дням.
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
        self.assertEqual(second_created, 1)
        self.assertEqual(third_created, 1)
        self.assertEqual(DispatchTask.objects.count(), 3)

        first_event = NotificationEvent.objects.get(
            scenario=scenario,
            dedupe_key="daily:1",
        )
        second_event = NotificationEvent.objects.get(
            scenario=scenario,
            dedupe_key="daily:2",
        )
        third_event = NotificationEvent.objects.get(
            scenario=scenario,
            dedupe_key="daily:3",
        )

        self.assertEqual(second_event.status, NotificationEvent.Status.TASK_CREATED)
        self.assertEqual(third_event.status, NotificationEvent.Status.TASK_CREATED)
        self.assertGreaterEqual(second_event.planned_send_at, first_event.planned_send_at + timedelta(days=1))
        self.assertGreaterEqual(third_event.planned_send_at, second_event.planned_send_at + timedelta(days=1))

        scenario_events = NotificationEvent.objects.filter(scenario=scenario)
        self.assertFalse(scenario_events.filter(status=NotificationEvent.Status.SKIPPED).exists())

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

    def test_schedule_registry_runner_executes_known_scenario(self):
        """
        Проверяет запуск schedule-сценария через реестр code -> handler.
        """
        restaurant = Restaurant.objects.create(
            iiko_id="rest_002",
            name="Тестовый ресторан №2",
        )
        VisitHistory.objects.create(
            guest=self.guest,
            restaurant=restaurant,
            visit_date=timezone.now() - timedelta(days=8),
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

        stats = run_registered_schedule_scenarios(
            scenario_codes=[scenario.code],
            limit_per_scenario=100,
        )
        scenario_stat = stats[scenario.code]

        self.assertEqual(scenario_stat.created_tasks, 1)
        self.assertGreaterEqual(scenario_stat.matched_guests, 1)

    def test_schedule_registry_runner_returns_empty_stat_for_unknown_code(self):
        """
        Для неизвестного кода реестр возвращает пустую статистику без падения.
        """
        unknown_code = "unknown_schedule_code"
        stats = run_registered_schedule_scenarios(
            scenario_codes=[unknown_code],
            limit_per_scenario=100,
        )

        self.assertIn(unknown_code, stats)
        self.assertEqual(stats[unknown_code].created_tasks, 0)
        self.assertEqual(stats[unknown_code].scenario_code, unknown_code)


class NotificationScenarioCodeRegistryTests(TestCase):
    """
    Проверки реестра кодов сценариев и валидации в админ-форме/модели.
    """

    def setUp(self):
        self.template = MessageTemplate.objects.create(
            name="REGISTRY_TEMPLATE",
            description="Шаблон для проверки реестра",
            message_text="Тестовое сообщение",
            created_by="test",
            is_active=True,
        )

    def test_notification_scenario_model_rejects_unknown_code_on_full_clean(self):
        """
        Модель не должна проходить full_clean() с незарегистрированным code.
        """
        scenario = NotificationScenario(
            code="unknown_scenario_code",
            name="Unknown scenario",
            trigger_type=NotificationScenario.TriggerType.WEBHOOK,
            template=self.template,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
        )

        with self.assertRaises(ValidationError) as exc:
            scenario.full_clean()

        self.assertIn("code", exc.exception.message_dict)

    def test_notification_scenario_model_accepts_registered_code(self):
        """
        Модель должна проходить full_clean() с кодом из реестра.
        """
        scenario = NotificationScenario.objects.get(code=SCENARIO_CODE_BALANCE_CHANGED)
        scenario.full_clean()

    def test_admin_form_code_field_uses_registered_choices(self):
        """
        Поле code в админ-форме должно использовать список кодов из реестра.
        """
        form = NotificationScenarioAdminForm()
        form_codes = {value for value, _ in form.fields["code"].choices}
        registry_codes = {value for value, _ in get_registered_notification_scenario_code_choices()}
        self.assertEqual(form_codes, registry_codes)
