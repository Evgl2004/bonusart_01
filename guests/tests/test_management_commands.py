"""
Тесты management-команд проекта.

Проверяем команды запуска воркеров, монитора и служебных утилит.
"""

from __future__ import annotations

import io
import sqlite3
import signal
import tempfile
from datetime import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from guests.management.commands import (
    import_bot_user_phones as import_cmd,
    init_schema as init_schema_cmd,
    mailing_worker as mailing_worker_cmd,
    run_webhook_worker as webhook_cmd,
)
from guests.models import (
    BotProfile,
    Guest,
    GuestBotBinding,
    Mailing,
    MailingGuest,
    MessageTemplate,
)
from guests.services.universal_queue.mailing_producer import MailingDispatchSummary


class DispatchUniversalTasksCommandTests(SimpleTestCase):
    """
    Тесты команды dispatch_universal_tasks.
    """

    def test_handle_once_runs_single_iteration_and_closes_queue(self):
        """
        В режиме --once команда должна выполнить одну итерацию и закрыть Redis queue.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_dispatcher = Mock()
        fake_dispatcher.enqueue_pending_tasks.return_value = SimpleNamespace(
            claimed=5,
            enqueued=4,
            failed=1,
        )

        with (
            patch("guests.management.commands.dispatch_universal_tasks.signal.signal"),
            patch("guests.management.commands.dispatch_universal_tasks.ProviderLaneQueue", return_value=fake_queue),
            patch("guests.management.commands.dispatch_universal_tasks.UniversalTaskDispatcher", return_value=fake_dispatcher),
        ):
            call_command(
                "dispatch_universal_tasks",
                "--once",
                "--batch-size=17",
                "--provider=telegram",
                "--namespace=uq:test",
                stdout=output,
            )

        fake_dispatcher.enqueue_pending_tasks.assert_called_once_with(batch_size=17)
        fake_queue.close.assert_called_once()

    def test_signal_handler_sets_stop_flag(self):
        """
        Обработчик сигнала должен выставлять флаг should_stop.
        """
        from guests.management.commands.dispatch_universal_tasks import Command

        command = Command()
        self.assertFalse(command.should_stop)
        command._signal_handler(signal.SIGTERM, None)
        self.assertTrue(command.should_stop)


class RunProviderWorkerCommandTests(SimpleTestCase):
    """
    Тесты команды run_provider_worker.
    """

    def test_handle_builds_worker_and_runs_asyncio(self):
        """
        Команда должна собрать зависимости, запустить asyncio.run и закрыть queue.
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
            patch("guests.management.commands.run_provider_worker.asyncio.run") as mocked_asyncio_run,
        ):
            call_command(
                "run_provider_worker",
                "--provider=telegram",
                "--once",
                "--redis-url=redis://test",
                "--namespace=uq:test",
                "--fair-high=2",
                "--fair-normal=1",
                "--fair-bulk=1",
                stdout=output,
            )

        fake_worker.bind_signal_handlers.assert_called_once()
        mocked_asyncio_run.assert_called_once()
        fake_queue.close.assert_called_once()


class RunUniversalQueueMonitorCommandTests(SimpleTestCase):
    """
    Тесты команды run_universal_queue_monitor.
    """

    def test_handle_once_runs_recovery_and_health_snapshot(self):
        """
        В режиме --once команда должна выполнить один проход и закрыть queue.
        """
        output = io.StringIO()
        fake_queue = Mock()
        fake_maintenance = Mock()
        fake_maintenance.recover_stale_tasks.return_value = SimpleNamespace(
            recovered_queued=2,
            recovered_in_progress=1,
            failed_in_progress=0,
        )
        fake_maintenance.collect_health_snapshots.return_value = {
            "telegram": SimpleNamespace(
                redis_lane_lengths={"high": 1, "normal": 2, "bulk": 3},
                db_status_counts={"pending": 5},
            )
        }

        with (
            patch("guests.management.commands.run_universal_queue_monitor.signal.signal"),
            patch("guests.management.commands.run_universal_queue_monitor.ProviderLaneQueue", return_value=fake_queue),
            patch(
                "guests.management.commands.run_universal_queue_monitor.UniversalQueueMaintenanceService",
                return_value=fake_maintenance,
            ),
        ):
            call_command(
                "run_universal_queue_monitor",
                "--once",
                "--provider=telegram",
                "--redis-url=redis://test",
                stdout=output,
            )

        fake_maintenance.recover_stale_tasks.assert_called_once()
        fake_maintenance.collect_health_snapshots.assert_called_once_with(provider_type="telegram")
        fake_queue.close.assert_called_once()

    def test_handle_raises_on_empty_redis_url(self):
        """
        Пустой redis-url должен приводить к CommandError.
        """
        with self.assertRaises(CommandError):
            call_command("run_universal_queue_monitor", "--once", "--redis-url=", stdout=io.StringIO())


class RunNotificationScenariosCommandTests(SimpleTestCase):
    """
    Тесты команды run_notification_scenarios.
    """

    def test_normalize_scenario_codes_returns_default_for_empty(self):
        """
        При пустом списке кодов команда должна использовать default-коды.
        """
        from guests.management.commands.run_notification_scenarios import (
            Command,
            DEFAULT_SCHEDULE_SCENARIO_CODES,
        )

        normalized = Command._normalize_scenario_codes(["", "  "])
        self.assertEqual(normalized, list(DEFAULT_SCHEDULE_SCENARIO_CODES))

    def test_handle_once_calls_registry_runner(self):
        """
        В режиме --once команда должна вызвать реестр сценариев один раз.
        """
        output = io.StringIO()
        fake_stats = {
            "inactive_7d": SimpleNamespace(
                inactive_days_threshold=7,
                scanned_guests=10,
                matched_guests=3,
                created_tasks=3,
                skipped_without_coupon=0,
                skipped_duplicate_or_no_targets=1,
            )
        }
        with (
            patch("guests.management.commands.run_notification_scenarios.signal.signal"),
            patch(
                "guests.management.commands.run_notification_scenarios.run_registered_schedule_scenarios",
                return_value=fake_stats,
            ) as mocked_runner,
        ):
            call_command(
                "run_notification_scenarios",
                "--once",
                "--scenario-code=inactive_7d",
                "--limit-per-scenario=50",
                stdout=output,
            )

        mocked_runner.assert_called_once_with(
            scenario_codes=["inactive_7d"],
            limit_per_scenario=50,
        )


class RunWebhookWorkerCommandTests(SimpleTestCase):
    """
    Тесты команды run_webhook_worker.
    """

    def test_health_check_healthy_exits_success(self):
        """
        При healthy-статусе health-check должен завершаться EXIT_SUCCESS.
        """
        fake_worker = Mock()
        fake_worker.health_check.return_value = {
            "status": "healthy",
            "redis_connected": True,
            "queue_length": 1,
            "dlq_length": 0,
            "should_stop": False,
            "metrics": {},
        }

        with (
            patch("guests.management.commands.run_webhook_worker.signal.signal"),
            patch("guests.management.commands.run_webhook_worker.WebhookWorker", return_value=fake_worker),
        ):
            with self.assertRaises(SystemExit) as exc:
                call_command("run_webhook_worker", "--health-check", stdout=io.StringIO())

        self.assertEqual(exc.exception.code, webhook_cmd.Command.EXIT_SUCCESS)

    def test_signal_handler_sets_flags_on_command_and_worker(self):
        """
        _signal_handler должен выставлять stop-флаг и команде, и worker.
        """
        command = webhook_cmd.Command()
        command.worker = SimpleNamespace(should_stop=False)
        command._signal_handler(signal.SIGINT, None)

        self.assertTrue(command.should_stop)
        self.assertTrue(command.worker.should_stop)


class MailingWorkerCommandTests(TestCase):
    """
    Тесты ключевых функций команды mailing_worker.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="MAILING_WORKER_TEMPLATE",
            description="template",
            message_text="Привет!",
            created_by="tests",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Mailing Worker Test",
            template=self.template,
            scheduled_date=now.date(),
            scheduled_time_begin=now - timedelta(hours=1),
            scheduled_time_end=now + timedelta(hours=1),
            is_active=True,
            created_at=now,
            updated_at=now,
            send_window_begin=(now - timedelta(hours=1)).time(),
            send_window_end=(now + timedelta(hours=1)).time(),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.BULK,
        )
        self.guest = Guest.objects.create(
            phone="+79990005544",
            first_name="Тест",
            created_at=now,
            updated_at=now,
        )
        self.bot = BotProfile.objects.create(
            code="tg_mailing_worker",
            name="TG mailing worker",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.mailing.bot_profiles.add(self.bot)
        self.binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot,
            external_chat_id="mw-chat",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

    def test_process_one_mailing_skips_outside_send_window(self):
        """
        Если текущее время вне send-window, обработка рассылки должна пропускаться.
        """
        local_now = timezone.localtime(timezone.now())
        self.mailing.send_window_begin = (local_now - timedelta(hours=3)).time()
        self.mailing.send_window_end = (local_now - timedelta(hours=2)).time()
        self.mailing.save(update_fields=["send_window_begin", "send_window_end", "updated_at"])

        processed = mailing_worker_cmd.process_one_mailing(mailing=self.mailing, now=timezone.now())
        self.assertEqual(processed, 0)

    def test_run_iteration_requeues_stuck_rows_to_planned(self):
        """
        run_iteration должен возвращать stuck IN_PROGRESS обратно в PLANNED.
        """
        row = MailingGuest.objects.create(
            mailing=self.mailing,
            guest=self.guest,
            phone=self.guest.phone,
            email="test@example.com",
            text_mailing_list="hello",
            scheduled_datetime=timezone.now() - timedelta(minutes=1),
            status=MailingGuest.Status.IN_PROGRESS,
            created_at=timezone.now(),
        )

        with patch("guests.management.commands.mailing_worker.process_one_mailing", return_value=0):
            processed = mailing_worker_cmd.run_iteration()

        row.refresh_from_db()
        self.assertEqual(processed, 0)
        self.assertEqual(row.status, MailingGuest.Status.PLANNED)
        self.assertEqual(row.delivery_status, "requeued")

    def test_process_one_mailing_calls_enqueue_dispatch(self):
        """
        Для готовых строк команда должна делегировать постановку в enqueue_mailing_rows_as_dispatch_tasks.
        """
        self.mailing.send_window_begin = time(0, 0, 0)
        self.mailing.send_window_end = time(23, 59, 59)
        self.mailing.save(update_fields=["send_window_begin", "send_window_end", "updated_at"])

        row = MailingGuest.objects.create(
            mailing=self.mailing,
            guest=self.guest,
            phone=self.guest.phone,
            email="test@example.com",
            text_mailing_list="hello",
            scheduled_datetime=timezone.now() - timedelta(minutes=1),
            status=MailingGuest.Status.PLANNED,
            created_at=timezone.now(),
        )

        summary = MailingDispatchSummary(
            rows_total=1,
            rows_queued=1,
            rows_failed=0,
            tasks_created=1,
            tasks_duplicates=0,
        )
        with patch(
            "guests.management.commands.mailing_worker.enqueue_mailing_rows_as_dispatch_tasks",
            return_value=summary,
        ) as mocked_enqueue:
            processed = mailing_worker_cmd.process_one_mailing(mailing=self.mailing, now=timezone.now())

        self.assertEqual(processed, 1)
        mocked_enqueue.assert_called_once()
        row.refresh_from_db()
        self.assertEqual(row.status, MailingGuest.Status.IN_PROGRESS)


class InitSchemaCommandTests(SimpleTestCase):
    """
    Тесты команды init_schema.
    """

    def test_handle_apply_calls_migrate(self):
        """
        Опция --apply должна запускать call_command('migrate', interactive=False).
        """
        with patch("guests.management.commands.init_schema.call_command") as mocked_call:
            call_command("init_schema", "--apply", stdout=io.StringIO())
        mocked_call.assert_called_once_with("migrate", interactive=False)


class ImportBotUserPhonesHelpersTests(SimpleTestCase):
    """
    Тесты helper-функций import_bot_user_phones.
    """

    def test_norm_phone_variants(self):
        """
        norm_phone должен корректно нормализовать типовые форматы номера.
        """
        self.assertEqual(import_cmd.norm_phone("+7 (999) 123-45-67"), "79991234567")
        self.assertEqual(import_cmd.norm_phone("8 (999) 123-45-67"), "79991234567")
        self.assertEqual(import_cmd.norm_phone("9991234567"), "79991234567")
        self.assertIsNone(import_cmd.norm_phone("abc"))

    def test_phone10_from_digits_variants(self):
        """
        phone10_from_digits должен возвращать последние 10 цифр или None.
        """
        self.assertEqual(import_cmd.phone10_from_digits("79991234567"), "9991234567")
        self.assertEqual(import_cmd.phone10_from_digits("+7 (999) 123-45-67"), "9991234567")
        self.assertIsNone(import_cmd.phone10_from_digits("12345"))

    def test_resolve_bot_profile_id_prefers_new_arg_and_supports_deprecated(self):
        """
        _resolve_bot_profile_id должен брать --bot-profile-id и поддерживать --channel-id.
        """
        command = import_cmd.Command()
        self.assertEqual(command._resolve_bot_profile_id({"bot_profile_id": 7, "channel_id": 9}), 7)
        self.assertEqual(command._resolve_bot_profile_id({"bot_profile_id": None, "channel_id": 11}), 11)
        self.assertIsNone(command._resolve_bot_profile_id({"bot_profile_id": None, "channel_id": None}))


class RunWebhookWorkerCommandExtendedTests(SimpleTestCase):
    """
    Расширенные тесты run_webhook_worker для ключевых веток остановки и ошибок.
    """

    def test_get_signal_name_returns_fallback_for_unknown_signal(self):
        """
        Для неизвестного номера сигнала должен возвращаться безопасный fallback.
        """
        self.assertEqual(webhook_cmd.Command._get_signal_name(99999), "SIGNAL(99999)")

    def test_run_health_check_unhealthy_exits_failure(self):
        """
        При статусе unhealthy команда должна завершаться с кодом EXIT_FAILURE.
        """
        command = webhook_cmd.Command()
        command.worker = Mock()
        command.worker.health_check.return_value = {"status": "unhealthy", "error": "redis down"}
        with patch.object(command, "_print_health_details") as mocked_details:
            with self.assertRaises(SystemExit) as exc:
                command._run_health_check(verbose=False)
        self.assertEqual(exc.exception.code, command.EXIT_FAILURE)
        mocked_details.assert_called_once()

    def test_run_health_check_unknown_status_exits_failure(self):
        """
        Неизвестный статус health-check должен завершать команду с ошибкой.
        """
        command = webhook_cmd.Command()
        command.worker = Mock()
        command.worker.health_check.return_value = {"status": "mystery"}
        with patch.object(command, "_print_health_details") as mocked_details:
            with self.assertRaises(SystemExit) as exc:
                command._run_health_check(verbose=True)
        self.assertEqual(exc.exception.code, command.EXIT_FAILURE)
        mocked_details.assert_called_once()

    def test_run_health_check_handles_exception(self):
        """
        Исключение в health_check должно приводить к EXIT_FAILURE.
        """
        command = webhook_cmd.Command()
        command.worker = Mock()
        command.worker.health_check.side_effect = RuntimeError("boom")
        with self.assertRaises(SystemExit) as exc:
            command._run_health_check(verbose=False)
        self.assertEqual(exc.exception.code, command.EXIT_FAILURE)

    def test_run_worker_exits_success_when_finished_without_signal(self):
        """
        Штатное завершение worker.run без stop-флага должно возвращать EXIT_SUCCESS.
        """
        command = webhook_cmd.Command()
        command.worker = Mock()
        command.should_stop = False
        with self.assertRaises(SystemExit) as exc:
            command._run_worker(verbose=False)
        self.assertEqual(exc.exception.code, command.EXIT_SUCCESS)

    def test_run_worker_exits_signal_when_stop_flag_set(self):
        """
        Если установлен stop-флаг, команда должна завершаться как по сигналу.
        """
        command = webhook_cmd.Command()
        command.worker = Mock()
        command.should_stop = True
        with self.assertRaises(SystemExit) as exc:
            command._run_worker(verbose=False)
        self.assertEqual(exc.exception.code, command.EXIT_SIGNAL)

    def test_run_worker_keyboard_interrupt_exits_signal(self):
        """
        KeyboardInterrupt внутри worker.run должен приводить к EXIT_SIGNAL.
        """
        command = webhook_cmd.Command()
        command.worker = Mock()
        command.worker.run.side_effect = KeyboardInterrupt()
        with self.assertRaises(SystemExit) as exc:
            command._run_worker(verbose=False)
        self.assertEqual(exc.exception.code, command.EXIT_SIGNAL)

    def test_run_worker_system_exit_is_reraised(self):
        """
        Внутренний SystemExit должен пробрасываться наверх без изменения кода.
        """
        command = webhook_cmd.Command()
        command.worker = Mock()
        command.worker.run.side_effect = SystemExit(77)
        with self.assertRaises(SystemExit) as exc:
            command._run_worker(verbose=False)
        self.assertEqual(exc.exception.code, 77)

    def test_run_worker_exception_verbose_prints_health_and_exits_failure(self):
        """
        При критической ошибке и verbose=True команда должна вывести health-данные и завершиться ошибкой.
        """
        command = webhook_cmd.Command()
        command.worker = Mock()
        command.worker.run.side_effect = RuntimeError("critical")
        command.worker.health_check.return_value = {"status": "unhealthy", "metrics": {}}
        with patch.object(command, "_print_health_details") as mocked_details:
            with self.assertRaises(SystemExit) as exc:
                command._run_worker(verbose=True)
        self.assertEqual(exc.exception.code, command.EXIT_FAILURE)
        mocked_details.assert_called_once()

    def test_run_worker_exception_verbose_handles_health_error(self):
        """
        Если health_check при диагностике падает, команда все равно должна завершиться EXIT_FAILURE.
        """
        command = webhook_cmd.Command()
        command.worker = Mock()
        command.worker.run.side_effect = RuntimeError("critical")
        command.worker.health_check.side_effect = RuntimeError("health failed")
        with self.assertRaises(SystemExit) as exc:
            command._run_worker(verbose=True)
        self.assertEqual(exc.exception.code, command.EXIT_FAILURE)

    def test_handle_routes_to_health_check(self):
        """
        handle должен вызывать _run_health_check, если передан --health-check.
        """
        fake_worker = Mock()
        with (
            patch("guests.management.commands.run_webhook_worker.WebhookWorker", return_value=fake_worker),
            patch.object(webhook_cmd.Command, "_setup_signal_handlers"),
            patch.object(webhook_cmd.Command, "_run_health_check") as mocked_health,
            patch.object(webhook_cmd.Command, "_run_worker") as mocked_worker,
        ):
            mocked_health.return_value = None
            mocked_worker.return_value = None
            call_command("run_webhook_worker", "--health-check", stdout=io.StringIO())
        mocked_health.assert_called_once_with(verbose=False)
        mocked_worker.assert_not_called()

    def test_handle_routes_to_worker_run_in_verbose_mode(self):
        """
        handle должен вызывать _run_worker, если health-check не запрошен.
        """
        fake_worker = Mock()
        with (
            patch("guests.management.commands.run_webhook_worker.WebhookWorker", return_value=fake_worker),
            patch.object(webhook_cmd.Command, "_setup_signal_handlers"),
            patch.object(webhook_cmd.Command, "_run_health_check") as mocked_health,
            patch.object(webhook_cmd.Command, "_run_worker") as mocked_worker,
        ):
            mocked_health.return_value = None
            mocked_worker.return_value = None
            call_command("run_webhook_worker", "--verbose", stdout=io.StringIO())
        mocked_worker.assert_called_once_with(verbose=True)
        mocked_health.assert_not_called()

    def test_execute_delegates_to_base_command(self):
        """
        Переопределенный execute должен делегировать выполнение в BaseCommand.execute.
        """
        command = webhook_cmd.Command()
        with patch.object(BaseCommand, "execute", return_value="ok") as mocked_execute:
            result = command.execute("--verbose")
        self.assertEqual(result, "ok")
        mocked_execute.assert_called_once()


class ImportBotUserPhonesCommandTests(TestCase):
    """
    Интеграционные тесты команды import_bot_user_phones.
    """

    def setUp(self):
        super().setUp()
        self.telegram_bot = BotProfile.objects.create(
            code="tg-import-test",
            name="TG Import Test",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.max_bot = BotProfile.objects.create(
            code="max-import-test",
            name="MAX Import Test",
            provider_type=BotProfile.ProviderType.MAX,
            is_active=True,
        )
        self.guest_one = Guest.objects.create(phone="+79991234567", first_name="Гость 1")
        self.guest_two = Guest.objects.create(phone="+79991112233", first_name="Гость 2")

    @staticmethod
    def _create_sqlite_file(rows: list[tuple[str, str, str]]) -> str:
        """
        Создает временный SQLite-файл с таблицей user_phones и переданными строками.
        """
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        handle.close()
        path = handle.name
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """
                CREATE TABLE user_phones (
                    user_id TEXT,
                    phone TEXT,
                    created_at TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO user_phones (user_id, phone, created_at) VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_handle_requires_bot_profile_id(self):
        """
        Команда должна завершиться с ошибкой в stderr, если не передан bot-profile-id.
        """
        sqlite_path = self._create_sqlite_file([])
        stdout = io.StringIO()
        stderr = io.StringIO()
        call_command(
            "import_bot_user_phones",
            "--sqlite",
            sqlite_path,
            stdout=stdout,
            stderr=stderr,
        )
        self.assertIn("--bot-profile-id", stderr.getvalue())

    def test_handle_errors_when_bot_profile_not_found(self):
        """
        Если BotProfile не найден, команда должна сообщить об этом в stderr.
        """
        sqlite_path = self._create_sqlite_file([])
        stdout = io.StringIO()
        stderr = io.StringIO()
        call_command(
            "import_bot_user_phones",
            "--sqlite",
            sqlite_path,
            "--bot-profile-id=999999",
            stdout=stdout,
            stderr=stderr,
        )
        self.assertIn("not found", stderr.getvalue())

    def test_dry_run_reads_rows_writes_reports_without_db_changes(self):
        """
        В dry-run команда должна сформировать отчеты и не писать GuestBotBinding в БД.
        """
        sqlite_path = self._create_sqlite_file(
            [
                ("chat-1", "+7 (999) 123-45-67", "2026-03-18 09:00:00"),
                ("chat-2", "не номер", "2026-03-18 10:00:00"),
                ("chat-3", "+7 (999) 000-00-00", "2026-03-18 11:00:00"),
            ]
        )
        with tempfile.TemporaryDirectory() as dump_dir:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch(
                "guests.management.commands.import_bot_user_phones.load_guest_phone10_map",
                return_value={"9991234567": self.guest_one.id},
            ):
                call_command(
                    "import_bot_user_phones",
                    "--sqlite",
                    sqlite_path,
                    f"--bot-profile-id={self.telegram_bot.id}",
                    "--dry-run",
                    f"--dump-dir={dump_dir}",
                    stdout=stdout,
                    stderr=stderr,
                )

            invalid_path = Path(dump_dir) / "invalid_phones.txt"
            not_found_path = Path(dump_dir) / "not_found_phones.txt"
            self.assertTrue(invalid_path.exists())
            self.assertTrue(not_found_path.exists())
            self.assertIn("не номер", invalid_path.read_text(encoding="utf-8"))
            self.assertIn("9990000000", not_found_path.read_text(encoding="utf-8"))
            self.assertEqual(GuestBotBinding.objects.count(), 0)

    def test_handle_creates_and_updates_bindings(self):
        """
        Команда должна обновлять существующие привязки и создавать новые.
        """
        existing = GuestBotBinding.objects.create(
            guest=self.guest_one,
            bot=self.telegram_bot,
            external_chat_id="old-chat",
            is_primary=False,
            is_active=False,
            is_opt_in=False,
            is_stop_sending=True,
        )

        sqlite_path = self._create_sqlite_file(
            [
                ("new-chat-1", "+7 (999) 123-45-67", "2026-03-18 09:00:00"),
                ("new-chat-2", "+7 (999) 111-22-33", "2026-03-18 09:05:00"),
            ]
        )

        with tempfile.TemporaryDirectory() as dump_dir:
            with patch(
                "guests.management.commands.import_bot_user_phones.load_guest_phone10_map",
                return_value={
                    "9991234567": self.guest_one.id,
                    "9991112233": self.guest_two.id,
                },
            ):
                call_command(
                    "import_bot_user_phones",
                    "--sqlite",
                    sqlite_path,
                    f"--bot-profile-id={self.telegram_bot.id}",
                    f"--dump-dir={dump_dir}",
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

        existing.refresh_from_db()
        self.assertEqual(existing.external_chat_id, "new-chat-1")
        self.assertTrue(existing.is_active)
        self.assertTrue(existing.is_opt_in)
        self.assertFalse(existing.is_stop_sending)

        created = GuestBotBinding.objects.get(guest=self.guest_two, bot=self.telegram_bot)
        self.assertEqual(created.external_chat_id, "new-chat-2")
        self.assertTrue(created.is_primary)
        self.assertTrue(created.is_active)
        self.assertTrue(created.is_opt_in)
        self.assertFalse(created.is_stop_sending)

    def test_handle_only_missing_chat_does_not_override_filled_chat(self):
        """
        Опция --only-missing-chat не должна изменять уже заполненный external_chat_id.
        """
        binding = GuestBotBinding.objects.create(
            guest=self.guest_one,
            bot=self.telegram_bot,
            external_chat_id="already-set",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        sqlite_path = self._create_sqlite_file(
            [("new-chat-ignored", "+7 (999) 123-45-67", "2026-03-18 09:00:00")]
        )
        with tempfile.TemporaryDirectory() as dump_dir:
            with patch(
                "guests.management.commands.import_bot_user_phones.load_guest_phone10_map",
                return_value={"9991234567": self.guest_one.id},
            ):
                call_command(
                    "import_bot_user_phones",
                    "--sqlite",
                    sqlite_path,
                    f"--bot-profile-id={self.telegram_bot.id}",
                    "--only-missing-chat",
                    f"--dump-dir={dump_dir}",
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

        binding.refresh_from_db()
        self.assertEqual(binding.external_chat_id, "already-set")

    def test_handle_warns_when_target_bot_is_not_telegram(self):
        """
        Для не-Telegram BotProfile команда должна вывести предупреждение, но продолжить dry-run.
        """
        sqlite_path = self._create_sqlite_file([])
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as dump_dir:
            with patch(
                "guests.management.commands.import_bot_user_phones.load_guest_phone10_map",
                return_value={},
            ):
                call_command(
                    "import_bot_user_phones",
                    "--sqlite",
                    sqlite_path,
                    f"--bot-profile-id={self.max_bot.id}",
                    "--dry-run",
                    f"--dump-dir={dump_dir}",
                    stdout=stdout,
                    stderr=io.StringIO(),
                )

        self.assertIn("не Telegram", stdout.getvalue())
