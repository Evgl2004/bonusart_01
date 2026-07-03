"""
Bug-seeking тесты для management-команд.

Покрываем аварийные и пограничные сценарии:
- некорректные CLI-аргументы;
- исключения внутри основного цикла;
- проверка, что ресурсы закрываются в finally;
- проверка clamp-логики для защитных минимумов.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings


class RunProviderWorkerCommandBugSeekingTests(SimpleTestCase):
    """
    Негативные сценарии команды run_provider_worker.
    """

    def test_handle_rejects_empty_redis_url(self):
        """
        Пустой --redis-url должен завершаться CommandError до старта зависимостей.
        """
        with self.assertRaises(CommandError):
            call_command(
                "run_provider_worker",
                "--provider=telegram",
                "--once",
                "--redis-url=",
                "--namespace=uq:test",
                stdout=io.StringIO(),
            )

    def test_handle_rejects_empty_namespace(self):
        """
        Пустой --namespace должен завершаться CommandError.
        """
        with self.assertRaises(CommandError):
            call_command(
                "run_provider_worker",
                "--provider=telegram",
                "--once",
                "--redis-url=redis://test",
                "--namespace=",
                stdout=io.StringIO(),
            )

    def test_handle_clamps_edge_runtime_values(self):
        """
        Нулевые/отрицательные значения должны быть приведены к безопасным минимумам.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_queue.redis = Mock()
        fake_worker = Mock()
        fake_rate_limiter = Mock()

        with (
            patch("guests.management.commands.run_provider_worker.ProviderLaneQueue", return_value=fake_queue),
            patch("guests.management.commands.run_provider_worker.CentralizedRedisRateLimiter", return_value=fake_rate_limiter),
            patch("guests.management.commands.run_provider_worker.AsyncProviderWorker", return_value=fake_worker) as mocked_worker_ctor,
            patch("guests.management.commands.run_provider_worker.asyncio.run"),
        ):
            call_command(
                "run_provider_worker",
                "--provider=telegram",
                "--once",
                "--redis-url=redis://test",
                "--namespace=uq:test",
                "--block-timeout=0",
                "--idle-sleep=0",
                "--retry-base=0",
                "--retry-max=0",
                "--fair-high=0",
                "--fair-normal=-10",
                "--fair-bulk=0",
                stdout=output,
            )

        config = mocked_worker_ctor.call_args.kwargs["config"]
        self.assertEqual(config.block_timeout_seconds, 1)
        self.assertEqual(config.idle_sleep_seconds, 0.05)
        self.assertEqual(config.retry_base_seconds, 1.0)
        self.assertEqual(config.retry_max_seconds, 1.0)
        self.assertEqual(config.fair_policy.high, 1)
        self.assertEqual(config.fair_policy.normal, 1)
        self.assertEqual(config.fair_policy.bulk, 1)
        self.assertTrue(config.once)

    def test_handle_closes_queue_when_asyncio_run_fails(self):
        """
        Если asyncio.run падает, Redis queue всё равно должен закрываться.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_queue.redis = Mock()
        fake_worker = Mock()
        fake_rate_limiter = Mock()

        with (
            patch("guests.management.commands.run_provider_worker.ProviderLaneQueue", return_value=fake_queue),
            patch("guests.management.commands.run_provider_worker.CentralizedRedisRateLimiter", return_value=fake_rate_limiter),
            patch("guests.management.commands.run_provider_worker.AsyncProviderWorker", return_value=fake_worker),
            patch("guests.management.commands.run_provider_worker.asyncio.run", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                call_command(
                    "run_provider_worker",
                    "--provider=telegram",
                    "--once",
                    "--redis-url=redis://test",
                    "--namespace=uq:test",
                    stdout=output,
                )

        fake_queue.close.assert_called_once()

    def test_rate_limits_use_defaults_when_settings_invalid(self):
        """
        При невалидных лимитах в settings команда должна брать безопасные default-значения.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_queue.redis = Mock()
        fake_worker = Mock()
        fake_rate_limiter = Mock()

        with (
            patch.object(settings, "UNIVERSAL_RATE_LIMIT_TELEGRAM_PER_SECOND", "oops", create=True),
            patch.object(settings, "UNIVERSAL_RATE_LIMIT_MAX_PER_SECOND", None, create=True),
            patch.object(settings, "UNIVERSAL_RATE_LIMIT_VK_PER_SECOND", "NaN", create=True),
            patch("guests.management.commands.run_provider_worker.ProviderLaneQueue", return_value=fake_queue),
            patch("guests.management.commands.run_provider_worker.CentralizedRedisRateLimiter", return_value=fake_rate_limiter) as mocked_limiter_ctor,
            patch("guests.management.commands.run_provider_worker.AsyncProviderWorker", return_value=fake_worker),
            patch("guests.management.commands.run_provider_worker.asyncio.run"),
        ):
            call_command(
                "run_provider_worker",
                "--provider=telegram",
                "--once",
                "--redis-url=redis://test",
                "--namespace=uq:test",
                stdout=output,
            )

        provider_policies = mocked_limiter_ctor.call_args.kwargs["provider_policies"]
        self.assertEqual(provider_policies["telegram"].rate_per_second, 28.0)
        self.assertEqual(provider_policies["max"].rate_per_second, 20.0)
        self.assertEqual(provider_policies["vk"].rate_per_second, 20.0)

    def test_health_check_success_and_queue_closed(self):
        """
        При --health-check команда должна завершаться успехом и закрывать Redis queue.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_queue.lane_lengths.return_value = {"high": 0, "normal": 0, "bulk": 0}

        fake_connection = MagicMock()
        fake_cursor = Mock()
        fake_connection.cursor.return_value.__enter__.return_value = fake_cursor

        with (
            patch("guests.management.commands.run_provider_worker.connections", {"default": fake_connection}),
            patch("guests.management.commands.run_provider_worker.ProviderLaneQueue", return_value=fake_queue),
        ):
            with self.assertRaises(SystemExit) as exc:
                call_command(
                    "run_provider_worker",
                    "--provider=telegram",
                    "--health-check",
                    "--redis-url=redis://test",
                    "--namespace=uq:test",
                    stdout=output,
                )

        self.assertEqual(exc.exception.code, 0)
        fake_queue.ping.assert_called_once()
        fake_queue.close.assert_called_once()


class DispatchUniversalTasksCommandBugSeekingTests(SimpleTestCase):
    """
    Негативные сценарии команды dispatch_universal_tasks.
    """

    def test_handle_closes_queue_on_dispatch_exception(self):
        """
        При падении диспетчеризации queue должна закрываться через finally.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_dispatcher = Mock()
        fake_dispatcher.enqueue_pending_tasks.side_effect = RuntimeError("redis push failed")

        with (
            patch("guests.management.commands.dispatch_universal_tasks.signal.signal"),
            patch("guests.management.commands.dispatch_universal_tasks.ProviderLaneQueue", return_value=fake_queue),
            patch("guests.management.commands.dispatch_universal_tasks.UniversalTaskDispatcher", return_value=fake_dispatcher),
        ):
            with self.assertRaises(RuntimeError):
                call_command(
                    "dispatch_universal_tasks",
                    "--once",
                    "--redis-url=redis://test",
                    "--namespace=uq:test",
                    stdout=output,
                )

        fake_queue.close.assert_called_once()

    def test_health_check_success_with_lightweight_metrics(self):
        """
        Health-check диспетчера должен отрабатывать без запуска основного цикла.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_queue.lane_lengths.return_value = {"high": 0, "normal": 0, "bulk": 0}

        fake_connection = MagicMock()
        fake_cursor = Mock()
        fake_connection.cursor.return_value.__enter__.return_value = fake_cursor

        fake_pending_qs = Mock()
        fake_pending_qs.exists.return_value = False
        fake_queued_qs = Mock()
        fake_queued_qs.exists.return_value = False

        with (
            patch("guests.management.commands.dispatch_universal_tasks.connections", {"default": fake_connection}),
            patch("guests.management.commands.dispatch_universal_tasks.ProviderLaneQueue", return_value=fake_queue),
            patch(
                "guests.management.commands.dispatch_universal_tasks.DispatchTask.objects.filter",
                side_effect=[fake_pending_qs, fake_queued_qs],
            ),
        ):
            with self.assertRaises(SystemExit) as exc:
                call_command(
                    "dispatch_universal_tasks",
                    "--health-check",
                    "--redis-url=redis://test",
                    "--namespace=uq:test",
                    stdout=output,
                )

        self.assertEqual(exc.exception.code, 0)
        fake_queue.ping.assert_called_once()
        fake_queue.close.assert_called_once()


class RunUniversalQueueMonitorCommandBugSeekingTests(SimpleTestCase):
    """
    Негативные сценарии команды run_universal_queue_monitor.
    """

    def test_handle_closes_queue_when_recovery_fails(self):
        """
        Если восстановление stale-задач падает, queue должна закрыться.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_maintenance = Mock()
        fake_maintenance.recover_stale_tasks.side_effect = RuntimeError("db is locked")

        with (
            patch("guests.management.commands.run_universal_queue_monitor.signal.signal"),
            patch("guests.management.commands.run_universal_queue_monitor.ProviderLaneQueue", return_value=fake_queue),
            patch("guests.management.commands.run_universal_queue_monitor.UniversalQueueMaintenanceService", return_value=fake_maintenance),
        ):
            with self.assertRaises(RuntimeError):
                call_command(
                    "run_universal_queue_monitor",
                    "--once",
                    "--redis-url=redis://test",
                    "--namespace=uq:test",
                    stdout=output,
                )

        fake_queue.close.assert_called_once()

    def test_handle_clamps_monitor_ttl_values(self):
        """
        Нулевые/отрицательные TTL должны приводиться к минимуму 1 секунда.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_maintenance = Mock()
        fake_maintenance.recover_stale_tasks.return_value = SimpleNamespace(
            recovered_queued=0,
            recovered_in_progress=0,
            failed_in_progress=0,
        )
        fake_maintenance.collect_health_snapshots.return_value = {}

        with (
            patch("guests.management.commands.run_universal_queue_monitor.signal.signal"),
            patch("guests.management.commands.run_universal_queue_monitor.ProviderLaneQueue", return_value=fake_queue),
            patch("guests.management.commands.run_universal_queue_monitor.UniversalQueueMaintenanceService", return_value=fake_maintenance),
        ):
            call_command(
                "run_universal_queue_monitor",
                "--once",
                "--redis-url=redis://test",
                "--namespace=uq:test",
                "--stale-queued-seconds=0",
                "--stale-in-progress-seconds=-5",
                stdout=output,
            )

        fake_maintenance.recover_stale_tasks.assert_called_once_with(
            queued_stale_seconds=1,
            in_progress_stale_seconds=1,
            provider_type=None,
        )

    def test_health_check_success_and_queue_closed(self):
        """
        Health-check монитора должен завершаться успехом и закрывать Redis queue.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_queue.lane_lengths.return_value = {"high": 0, "normal": 0, "bulk": 0}

        fake_connection = MagicMock()
        fake_cursor = Mock()
        fake_connection.cursor.return_value.__enter__.return_value = fake_cursor

        with (
            patch("guests.management.commands.run_universal_queue_monitor.connections", {"default": fake_connection}),
            patch("guests.management.commands.run_universal_queue_monitor.ProviderLaneQueue", return_value=fake_queue),
        ):
            with self.assertRaises(SystemExit) as exc:
                call_command(
                    "run_universal_queue_monitor",
                    "--health-check",
                    "--provider=telegram",
                    "--redis-url=redis://test",
                    "--namespace=uq:test",
                    stdout=output,
                )

        self.assertEqual(exc.exception.code, 0)
        fake_queue.ping.assert_called_once()
        fake_queue.close.assert_called_once()


class RunNotificationScenariosCommandBugSeekingTests(SimpleTestCase):
    """
    Негативные сценарии команды run_notification_scenarios.
    """

    def test_handle_clamps_limit_per_scenario(self):
        """
        При --limit-per-scenario <= 0 команда должна использовать минимум 1.
        """
        output = io.StringIO()
        with (
            patch("guests.management.commands.run_notification_scenarios.signal.signal"),
            patch("guests.management.commands.run_notification_scenarios.run_registered_schedule_scenarios", return_value={}) as mocked_runner,
        ):
            call_command(
                "run_notification_scenarios",
                "--once",
                "--scenario-code=inactive_7d",
                "--limit-per-scenario=0",
                stdout=output,
            )

        mocked_runner.assert_called_once_with(
            scenario_codes=["inactive_7d"],
            limit_per_scenario=1,
        )

    def test_loop_mode_does_not_sleep_after_stop_flag(self):
        """
        Если stop-флаг выставлен после итерации, команда не должна уходить в sleep.
        """
        from guests.management.commands.run_notification_scenarios import Command

        command = Command()

        def _stop_after_first(*, scenario_codes, limit_per_scenario):
            command.should_stop = True
            return {}

        with (
            patch.object(command, "_setup_signal_handlers"),
            patch.object(command, "_run_single_iteration", side_effect=_stop_after_first) as mocked_iteration,
            patch("guests.management.commands.run_notification_scenarios.time.sleep") as mocked_sleep,
        ):
            command.handle(
                once=False,
                sleep_seconds=300.0,
                limit_per_scenario=100,
                scenario_codes=["inactive_7d"],
            )

        mocked_iteration.assert_called_once_with(
            scenario_codes=["inactive_7d"],
            limit_per_scenario=100,
        )
        mocked_sleep.assert_not_called()


class GracefulSleepHelpersTests(SimpleTestCase):
    """
    Тесты helper-пауз для быстрого завершения циклов по stop-флагу.
    """

    def test_dispatch_command_sleep_helper_returns_immediately_when_stopped(self):
        from guests.management.commands.dispatch_universal_tasks import Command

        command = Command()
        command.should_stop = True

        with patch("guests.management.commands.dispatch_universal_tasks.time.sleep") as mocked_sleep:
            command._sleep_with_stop(10.0)

        mocked_sleep.assert_not_called()

    def test_monitor_command_sleep_helper_returns_immediately_when_stopped(self):
        from guests.management.commands.run_universal_queue_monitor import Command

        command = Command()
        command.should_stop = True

        with patch("guests.management.commands.run_universal_queue_monitor.time.sleep") as mocked_sleep:
            command._sleep_with_stop(30.0)

        mocked_sleep.assert_not_called()

    def test_notification_scenarios_sleep_helper_returns_immediately_when_stopped(self):
        from guests.management.commands.run_notification_scenarios import Command

        command = Command()
        command.should_stop = True

        with patch("guests.management.commands.run_notification_scenarios.time.sleep") as mocked_sleep:
            command._sleep_with_stop(300.0)

        mocked_sleep.assert_not_called()


class SmokePostDeployCommandTests(SimpleTestCase):
    """
    Тесты orchestration-логики команды smoke_post_deploy.
    """

    def test_smoke_command_success_path(self):
        """
        При отсутствии ошибок команда должна завершаться успешно.
        """
        output = io.StringIO()
        with (
            patch("guests.management.commands.smoke_post_deploy.Command._check_django_system"),
            patch("guests.management.commands.smoke_post_deploy.Command._check_databases"),
            patch("guests.management.commands.smoke_post_deploy.Command._check_migrations"),
            patch("guests.management.commands.smoke_post_deploy.Command._check_redis"),
            patch("guests.management.commands.smoke_post_deploy.Command._check_notification_scenarios"),
            patch("guests.management.commands.smoke_post_deploy.Command._check_active_bot_tokens"),
        ):
            call_command("smoke_post_deploy", stdout=output)

        self.assertIn("Post-deploy smoke-check успешно завершён.", output.getvalue())

    def test_smoke_command_raises_when_errors_present(self):
        """
        Если любая проверка добавляет ошибки, команда должна завершаться CommandError.
        """
        output = io.StringIO()

        def _inject_error(errors):
            errors.append("broken check")

        with (
            patch("guests.management.commands.smoke_post_deploy.Command._check_django_system", side_effect=_inject_error),
            patch("guests.management.commands.smoke_post_deploy.Command._check_databases"),
            patch("guests.management.commands.smoke_post_deploy.Command._check_migrations"),
            patch("guests.management.commands.smoke_post_deploy.Command._check_redis"),
            patch("guests.management.commands.smoke_post_deploy.Command._check_notification_scenarios"),
            patch("guests.management.commands.smoke_post_deploy.Command._check_active_bot_tokens"),
        ):
            with self.assertRaises(CommandError):
                call_command("smoke_post_deploy", stdout=output, stderr=io.StringIO())

    def test_smoke_command_skip_flags_bypass_checks(self):
        """
        Skip-флаги должны отключать соответствующие проверки.
        """
        with (
            patch("guests.management.commands.smoke_post_deploy.Command._check_django_system") as mocked_django,
            patch("guests.management.commands.smoke_post_deploy.Command._check_databases") as mocked_db,
            patch("guests.management.commands.smoke_post_deploy.Command._check_migrations") as mocked_migrations,
            patch("guests.management.commands.smoke_post_deploy.Command._check_redis") as mocked_redis,
            patch("guests.management.commands.smoke_post_deploy.Command._check_notification_scenarios") as mocked_scenarios,
            patch("guests.management.commands.smoke_post_deploy.Command._check_active_bot_tokens") as mocked_tokens,
        ):
            call_command(
                "smoke_post_deploy",
                "--skip-django-check",
                "--skip-db",
                "--skip-migrations",
                "--skip-redis",
                "--skip-scenarios",
                "--skip-bot-tokens",
                stdout=io.StringIO(),
            )

        mocked_django.assert_not_called()
        mocked_db.assert_not_called()
        mocked_migrations.assert_not_called()
        mocked_redis.assert_not_called()
        mocked_scenarios.assert_not_called()
        mocked_tokens.assert_not_called()

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            },
            "webhooks": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            },
        },
        SMOKE_POST_DEPLOY_MIGRATION_DATABASES=("default",),
    )
    def test_smoke_migration_aliases_do_not_include_webhooks_by_default(self):
        """
        База `webhooks` не должна случайно проверяться как владелец схемы `guests`.
        """
        from guests.management.commands.smoke_post_deploy import Command

        self.assertEqual(Command._migration_db_aliases(), ["default"])

    def test_smoke_migration_check_uses_only_declared_migration_aliases(self):
        """
        Проверка миграций должна обходить только явно разрешённые алиасы БД.
        """
        from guests.management.commands.smoke_post_deploy import Command

        command = Command()
        errors: list[str] = []
        default_connection = object()
        connections_mock = MagicMock()
        connections_mock.__getitem__.side_effect = {"default": default_connection}.__getitem__
        executor_mock = MagicMock()
        executor_mock.loader.graph.leaf_nodes.return_value = []
        executor_mock.migration_plan.return_value = []

        with (
            patch.object(Command, "_migration_db_aliases", return_value=["default"]),
            patch("guests.management.commands.smoke_post_deploy.connections", connections_mock),
            patch(
                "guests.management.commands.smoke_post_deploy.MigrationExecutor",
                return_value=executor_mock,
            ) as migration_executor_mock,
        ):
            command._check_migrations(errors)

        self.assertEqual(errors, [])
        connections_mock.__getitem__.assert_called_once_with("default")
        migration_executor_mock.assert_called_once_with(default_connection)
