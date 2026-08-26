"""Изолированные проверки идемпотентной миграции отслеживаемых ссылок."""

from __future__ import annotations

import importlib

import pytest
from django.db.migrations.loader import MigrationLoader
from django.db.utils import ConnectionHandler
from django.test import override_settings


MIGRATION_NAME = "0061_message_interaction_tracked_links"
MIGRATION_MODULE = f"guests.migrations.{MIGRATION_NAME}"
PREVIOUS_MIGRATION = ("guests", "0060_message_interactions")


@pytest.fixture
def isolated_connection(tmp_path):
    """Предоставляет отдельную SQLite-базу вне основной тестовой базы."""

    handler = ConnectionHandler(
        {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": tmp_path / "tracked_link_migration.sqlite3",
            }
        }
    )
    connection = handler["default"]
    try:
        yield connection
    finally:
        connection.close()


def _state_before_migration():
    """Строит состояние моделей непосредственно после миграции `0060`."""

    with override_settings(MIGRATION_MODULES={}):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        return loader.project_state([PREVIOUS_MIGRATION])


def _create_required_previous_tables(connection, state):
    """Создаёт минимальные физические таблицы, необходимые миграции `0061`."""

    model_names = [
        "MessageTemplate",
        "Mailing",
        "NotificationScenario",
        "DispatchTask",
        "MessageInteraction",
        "MessageInteractionEvent",
    ]
    with connection.schema_editor() as schema_editor:
        for model_name in model_names:
            schema_editor.create_model(state.apps.get_model("guests", model_name))


def _apply_migration(connection, state):
    """Применяет физическую часть `0061` без таблицы истории миграций."""

    migration_module = importlib.import_module(MIGRATION_MODULE)
    migration = migration_module.Migration(MIGRATION_NAME, "guests")
    with connection.schema_editor() as schema_editor:
        return migration.apply(state.clone(), schema_editor)


def _columns(connection, table_name):
    with connection.cursor() as cursor:
        return {
            description.name
            for description in connection.introspection.get_table_description(cursor, table_name)
        }


def _constraints(connection, table_name):
    with connection.cursor() as cursor:
        return connection.introspection.get_constraints(cursor, table_name)


def _destination_rows(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT code, label_code, target_url, is_active "
            "FROM message_interaction_link_destinations ORDER BY code"
        )
        return cursor.fetchall()


@pytest.mark.django_db
class TestTrackedLinkMigration:
    """Проверяет чистую, повторную, частичную и несовместимую схему."""

    def test_clean_schema_and_repeated_apply_are_idempotent(self, isolated_connection):
        """Повторный физический запуск не создаёт дубли объектов и данных."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)

        _apply_migration(isolated_connection, previous_state)
        first_constraints = {
            table: set(_constraints(isolated_connection, table))
            for table in (
                "mailings",
                "notification_scenarios",
                "message_interactions",
                "message_interaction_link_destinations",
                "message_interaction_tracked_links",
                "message_interaction_link_transitions",
            )
        }
        first_destinations = _destination_rows(isolated_connection)

        _apply_migration(isolated_connection, previous_state)
        second_constraints = {
            table: set(_constraints(isolated_connection, table))
            for table in first_constraints
        }

        assert first_constraints == second_constraints
        assert _destination_rows(isolated_connection) == first_destinations
        assert len(first_destinations) == 9
        assert {row[0] for row in first_destinations} == {
            "delivery_main",
            "delivery_susami",
            "delivery_uzbechka",
            "delivery_gruzinka",
            "delivery_china",
            "booking_gruzinka",
            "booking_susami",
            "booking_china",
            "booking_uzbechka",
        }

    def test_schema_contains_only_approved_columns_and_index(self, isolated_connection):
        """Физическая схема не получает лишние персональные поля и индексы."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        _apply_migration(isolated_connection, previous_state)

        assert _columns(isolated_connection, "message_interaction_link_destinations") == {
            "id",
            "code",
            "name",
            "label_code",
            "target_url",
            "is_active",
            "created_at",
            "updated_at",
        }
        assert _columns(isolated_connection, "message_interaction_tracked_links") == {
            "interaction_id",
            "public_token",
            "label_code",
            "target_url",
            "created_at",
            "disabled_at",
        }
        assert _columns(isolated_connection, "message_interaction_link_transitions") == {
            "id",
            "tracked_link_id",
            "received_at",
        }
        transition_constraints = _constraints(
            isolated_connection,
            "message_interaction_link_transitions",
        )
        assert transition_constraints["milt_link_received_idx"]["columns"] == [
            "tracked_link_id",
            "received_at",
        ]
        assert transition_constraints["milt_link_received_idx"]["index"] is True
        assert not any(
            details.get("index") and details.get("columns") == ["tracked_link_id"]
            for details in transition_constraints.values()
        )

    def test_empty_compatible_partial_table_is_completed(self, isolated_connection):
        """Пустая неполная таблица справочника восстанавливается без дублей."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE message_interaction_link_destinations "
                "(id integer NOT NULL PRIMARY KEY AUTOINCREMENT)"
            )

        _apply_migration(isolated_connection, previous_state)

        assert len(_destination_rows(isolated_connection)) == 9
        assert "target_url" in _columns(
            isolated_connection,
            "message_interaction_link_destinations",
        )

    def test_incompatible_source_column_blocks_migration(self, isolated_connection):
        """Неверный тип уже существующей связи не маскируется успешным запуском."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE mailings "
                "ADD COLUMN tracked_link_destination_id text NULL"
            )

        migration_module = importlib.import_module(MIGRATION_MODULE)
        with pytest.raises(
            migration_module._base_schema.InteractionSchemaError,
            match=r"mailings.*tracked_link_destination_id.*несовместимый тип",
        ):
            _apply_migration(isolated_connection, previous_state)

    def test_missing_composite_index_is_restored(self, isolated_connection):
        """Повторный запуск восстанавливает удалённый утверждённый индекс."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        _apply_migration(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute("DROP INDEX milt_link_received_idx")

        _apply_migration(isolated_connection, previous_state)

        constraints = _constraints(
            isolated_connection,
            "message_interaction_link_transitions",
        )
        assert constraints["milt_link_received_idx"]["columns"] == [
            "tracked_link_id",
            "received_at",
        ]

    def test_conflicting_seed_row_blocks_repeated_apply(self, isolated_connection):
        """Технический код не позволяет незаметно подменить конечный адрес."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        _apply_migration(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE message_interaction_link_destinations "
                "SET target_url = %s WHERE code = %s",
                ["https://example.test/conflict", "delivery_main"],
            )

        migration_module = importlib.import_module(MIGRATION_MODULE)
        with pytest.raises(
            migration_module._base_schema.InteractionSchemaError,
            match=r"несовместимую запись.*delivery_main",
        ):
            _apply_migration(isolated_connection, previous_state)
