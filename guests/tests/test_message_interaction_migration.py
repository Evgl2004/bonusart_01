"""Изолированные проверки идемпотентной миграции интерактивных сообщений."""

import importlib

import pytest
from django.db.migrations.loader import MigrationLoader
from django.db.utils import ConnectionHandler
from django.test import override_settings


MIGRATION_NAME = "0060_message_interactions"
MIGRATION_MODULE = f"guests.migrations.{MIGRATION_NAME}"
PREVIOUS_MIGRATION = ("guests", "0059_seed_welcome_coupon_autoscenario")


@pytest.fixture
def isolated_connection(tmp_path):
    """Предоставляет отдельную SQLite-базу, не связанную с тестовой БД pytest."""

    handler = ConnectionHandler(
        {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": tmp_path / "message_interaction_migration.sqlite3",
            }
        }
    )
    connection = handler["default"]
    try:
        yield connection
    finally:
        connection.close()


def _state_before_migration():
    """Строит состояние моделей непосредственно перед миграцией `0060`."""

    with override_settings(MIGRATION_MODULES={}):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        return loader.project_state([PREVIOUS_MIGRATION])


def _create_required_previous_tables(connection, state):
    """Создаёт минимальный набор таблиц, от которого зависит миграция `0060`."""

    model_names = [
        "MessageTemplate",
        "Mailing",
        "NotificationScenario",
        "DispatchTask",
    ]
    with connection.schema_editor() as schema_editor:
        for model_name in model_names:
            schema_editor.create_model(state.apps.get_model("guests", model_name))


def _apply_migration(connection, state):
    """Применяет `0060` без таблицы истории, проверяя собственную идемпотентность."""

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


@pytest.mark.django_db
class TestMessageInteractionMigration:
    """Проверяет чистую, повторную, частичную и ошибочную схему."""

    def test_clean_schema_and_repeated_apply_are_idempotent(self, isolated_connection):
        """Повторный фактический запуск не создаёт таблицы и ограничения заново."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)

        _apply_migration(isolated_connection, previous_state)
        first_constraints = {
            "message_interactions": _constraints(
                isolated_connection,
                "message_interactions",
            ),
            "message_interaction_events": _constraints(
                isolated_connection,
                "message_interaction_events",
            ),
        }

        _apply_migration(isolated_connection, previous_state)
        second_constraints = {
            "message_interactions": _constraints(
                isolated_connection,
                "message_interactions",
            ),
            "message_interaction_events": _constraints(
                isolated_connection,
                "message_interaction_events",
            ),
        }

        assert _columns(isolated_connection, "mailings") >= {"button_set"}
        assert _columns(isolated_connection, "notification_scenarios") >= {"button_set"}
        assert _columns(isolated_connection, "message_interactions") == {
            "id",
            "dispatch_task_id",
            "button_set",
            "created_at",
        }
        assert _columns(isolated_connection, "message_interaction_events") == {
            "id",
            "event_id",
            "interaction_id",
            "action",
            "occurred_at",
            "received_at",
            "result",
            "provider_message_id",
        }
        assert first_constraints.keys() == second_constraints.keys()
        assert {
            name: set(constraints)
            for name, constraints in first_constraints.items()
        } == {
            name: set(constraints)
            for name, constraints in second_constraints.items()
        }

    def test_empty_compatible_partial_table_is_completed(self, isolated_connection):
        """Пустая неполная таблица восстанавливается без дублирования данных."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE message_interactions "
                "(id integer NOT NULL PRIMARY KEY AUTOINCREMENT)"
            )

        _apply_migration(isolated_connection, previous_state)

        assert _columns(isolated_connection, "message_interactions") == {
            "id",
            "dispatch_task_id",
            "button_set",
            "created_at",
        }

    def test_incompatible_source_column_blocks_migration(self, isolated_connection):
        """Неверный тип уже существующего поля не маскируется успешным запуском."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute("ALTER TABLE mailings ADD COLUMN button_set integer NULL")

        migration_module = importlib.import_module(MIGRATION_MODULE)
        with pytest.raises(
            migration_module.InteractionSchemaError,
            match=r"mailings.*button_set.*несовместимый тип",
        ):
            _apply_migration(isolated_connection, previous_state)

    def test_incompatible_partial_table_blocks_recreation(self, isolated_connection):
        """Пустая таблица с неверным первичным ключом не удаляется молча."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE message_interactions "
                "(id text NOT NULL PRIMARY KEY)"
            )

        migration_module = importlib.import_module(MIGRATION_MODULE)
        with pytest.raises(
            migration_module.InteractionSchemaError,
            match=r"message_interactions.*id.*несовместимый тип",
        ):
            _apply_migration(isolated_connection, previous_state)

    def test_missing_compatible_index_is_restored(self, isolated_connection):
        """Отсутствующий индекс внешнего ключа создаётся повторно."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        _apply_migration(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute("DROP INDEX mie_interaction_idx")

        _apply_migration(isolated_connection, previous_state)

        constraints = _constraints(
            isolated_connection,
            "message_interaction_events",
        )
        assert constraints["mie_interaction_idx"]["columns"] == ["interaction_id"]
        assert constraints["mie_interaction_idx"]["index"] is True

    def test_index_with_correct_name_and_wrong_column_blocks_migration(
        self,
        isolated_connection,
    ):
        """Совпадение имени не позволяет принять индекс другого столбца."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        _apply_migration(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute("DROP INDEX mie_interaction_idx")
            cursor.execute(
                "CREATE INDEX mie_interaction_idx "
                "ON message_interaction_events (action)"
            )

        migration_module = importlib.import_module(MIGRATION_MODULE)
        with pytest.raises(
            migration_module.InteractionSchemaError,
            match=r"mie_interaction_idx.*несовместимую форму",
        ):
            _apply_migration(isolated_connection, previous_state)

    def test_partial_unique_with_correct_name_and_wrong_condition_is_blocked(
        self,
        isolated_connection,
    ):
        """Правильные имя и столбец не маскируют неверное условие индекса."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        _apply_migration(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute("DROP INDEX mie_one_accepted_rating_uniq")
            cursor.execute(
                "CREATE UNIQUE INDEX mie_one_accepted_rating_uniq "
                "ON message_interaction_events (interaction_id) "
                "WHERE action = 'm'"
            )

        migration_module = importlib.import_module(MIGRATION_MODULE)
        with pytest.raises(
            migration_module.InteractionSchemaError,
            match=r"mie_one_accepted_rating_uniq.*несовместимую форму",
        ):
            _apply_migration(isolated_connection, previous_state)

    def test_full_unique_with_partial_unique_name_is_blocked(
        self,
        isolated_connection,
    ):
        """Полная уникальность не принимается вместо утверждённой частичной."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        _apply_migration(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute("DROP INDEX mie_one_accepted_rating_uniq")
            cursor.execute(
                "CREATE UNIQUE INDEX mie_one_accepted_rating_uniq "
                "ON message_interaction_events (interaction_id)"
            )

        migration_module = importlib.import_module(MIGRATION_MODULE)
        with pytest.raises(
            migration_module.InteractionSchemaError,
            match=r"mie_one_accepted_rating_uniq.*несовместимую форму",
        ):
            _apply_migration(isolated_connection, previous_state)

    def test_equivalent_partial_unique_under_other_name_blocks_duplicate(
        self,
        isolated_connection,
    ):
        """Эквивалентный индекс под другим именем не дублируется автоматически."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        _apply_migration(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute("DROP INDEX mie_one_accepted_rating_uniq")
            cursor.execute(
                "CREATE UNIQUE INDEX existing_rating_uniq "
                "ON message_interaction_events (interaction_id) "
                "WHERE action IN ('l', 'd') AND result = 'accepted'"
            )

        migration_module = importlib.import_module(MIGRATION_MODULE)
        with pytest.raises(
            migration_module.InteractionSchemaError,
            match=r"existing_rating_uniq.*mie_one_accepted_rating_uniq.*дубликата",
        ):
            _apply_migration(isolated_connection, previous_state)

    def test_incompatible_unique_under_other_name_blocks_duplicate(
        self,
        isolated_connection,
    ):
        """Несовместимая полная уникальность не остаётся рядом с частичной."""

        previous_state = _state_before_migration()
        _create_required_previous_tables(isolated_connection, previous_state)
        _apply_migration(isolated_connection, previous_state)
        with isolated_connection.cursor() as cursor:
            cursor.execute("DROP INDEX mie_one_accepted_rating_uniq")
            cursor.execute(
                "CREATE UNIQUE INDEX existing_full_rating_uniq "
                "ON message_interaction_events (interaction_id)"
            )

        migration_module = importlib.import_module(MIGRATION_MODULE)
        with pytest.raises(
            migration_module.InteractionSchemaError,
            match=r"existing_full_rating_uniq.*mie_one_accepted_rating_uniq.*дубликата",
        ):
            _apply_migration(isolated_connection, previous_state)


def test_postgresql_condition_normalization_matches_django_condition():
    """Синтаксис ``ANY(ARRAY)`` PostgreSQL равен исходному условию ``IN``."""

    migration_module = importlib.import_module(MIGRATION_MODULE)
    expected = '"action" IN (\'l\', \'d\') AND "result" = \'accepted\''
    actual = (
        "(((action)::text = ANY ((ARRAY['l'::character varying, "
        "'d'::character varying])::text[])) AND ((result)::text = 'accepted'::text))"
    )

    assert migration_module._canonical_condition_sql(actual) == (
        migration_module._canonical_condition_sql(expected)
    )
