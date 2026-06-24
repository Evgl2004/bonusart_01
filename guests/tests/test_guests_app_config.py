"""
Тесты стартовой инициализации приложения guests.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.test import override_settings

import guests
import guests.apps as guests_apps
from guests.apps import GuestsConfig


class GuestsConfigAutosyncTests(SimpleTestCase):
    """
    Проверяет, что autosync Django Q не ходит в БД прямо из AppConfig.ready().
    """

    def setUp(self):
        super().setUp()
        self._reset_autosync_flags()

    def tearDown(self):
        self._reset_autosync_flags()
        super().tearDown()

    @staticmethod
    def _reset_autosync_flags():
        guests_apps._DJANGO_Q_AUTOSYNC_HANDLER_CONNECTED = False
        guests_apps._DJANGO_Q_AUTOSYNC_DONE = False

    @staticmethod
    def _build_config() -> GuestsConfig:
        return GuestsConfig("guests", guests)

    @override_settings(DJANGO_Q_SCHEDULE_AUTOSYNC_ON_QCLUSTER_START=True)
    @patch("guests.services.django_q_schedule_sync.sync_django_q_schedule_from_settings")
    @patch.object(guests_apps.connection_created, "connect")
    @patch.object(guests_apps.sys, "argv", ["manage.py", "qcluster"])
    def test_ready_registers_connection_handler_without_syncing_db(self, connect_mock, sync_mock):
        self._build_config().ready()

        sync_mock.assert_not_called()
        connect_mock.assert_called_once()
        self.assertEqual(
            connect_mock.call_args.kwargs["dispatch_uid"],
            "guests.django_q_schedule_autosync",
        )
        self.assertIs(connect_mock.call_args.kwargs["weak"], False)

    @override_settings(DJANGO_Q_SCHEDULE_AUTOSYNC_ON_QCLUSTER_START=True)
    @patch.object(guests_apps.connection_created, "connect")
    @patch.object(guests_apps.sys, "argv", ["manage.py", "check"])
    def test_ready_ignores_non_qcluster_commands(self, connect_mock):
        self._build_config().ready()

        connect_mock.assert_not_called()

    @override_settings(DJANGO_Q_SCHEDULE_AUTOSYNC_ON_QCLUSTER_START=False)
    @patch.object(guests_apps.connection_created, "connect")
    @patch.object(guests_apps.sys, "argv", ["manage.py", "qcluster"])
    def test_ready_respects_autosync_flag(self, connect_mock):
        self._build_config().ready()

        connect_mock.assert_not_called()

    @override_settings(DJANGO_Q_SCHEDULE_AUTOSYNC_PRUNE_STALE=False)
    @patch("guests.services.django_q_schedule_sync.sync_django_q_schedule_from_settings")
    def test_connection_handler_runs_sync_once_for_default_db(self, sync_mock):
        sync_mock.return_value = SimpleNamespace(
            source_entries=1,
            created=0,
            updated=0,
            unchanged=1,
            deleted=0,
            renamed_by_func=0,
            skipped_invalid=0,
        )
        connection = SimpleNamespace(alias="default")

        guests_apps._sync_django_q_schedule_after_connection_created(
            sender=None,
            connection=connection,
        )
        guests_apps._sync_django_q_schedule_after_connection_created(
            sender=None,
            connection=connection,
        )

        sync_mock.assert_called_once_with(dry_run=False, prune_stale=False)

    @patch("guests.services.django_q_schedule_sync.sync_django_q_schedule_from_settings")
    def test_connection_handler_ignores_non_default_db(self, sync_mock):
        guests_apps._sync_django_q_schedule_after_connection_created(
            sender=None,
            connection=SimpleNamespace(alias="webhooks"),
        )

        sync_mock.assert_not_called()
