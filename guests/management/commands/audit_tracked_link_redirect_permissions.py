"""Разовая проверка минимальных прав публичной службы переходов."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand
from django.db import DatabaseError, connection


_REQUIRED_OBJECTS_SQL = """
SELECT
    to_regclass('public.message_interaction_tracked_links')::text,
    to_regclass('public.message_interaction_link_transitions')::text,
    to_regclass('public.message_interaction_link_transitions_id_seq')::text
"""

_CONNECTION_PRIVILEGES_SQL = """
SELECT
    has_database_privilege(current_user, current_database(), 'CONNECT'),
    has_schema_privilege(current_user, 'public', 'USAGE')
"""

_COLUMN_PRIVILEGES_SQL = """
WITH checks(code, relation_name, column_name, privilege_name, expected) AS (
    VALUES
        ('snapshot_select_interaction_id', 'public.message_interaction_tracked_links', 'interaction_id', 'SELECT', TRUE),
        ('snapshot_select_public_token', 'public.message_interaction_tracked_links', 'public_token', 'SELECT', TRUE),
        ('snapshot_select_target_url', 'public.message_interaction_tracked_links', 'target_url', 'SELECT', TRUE),
        ('snapshot_select_disabled_at', 'public.message_interaction_tracked_links', 'disabled_at', 'SELECT', TRUE),
        ('snapshot_select_label_code', 'public.message_interaction_tracked_links', 'label_code', 'SELECT', FALSE),
        ('snapshot_select_created_at', 'public.message_interaction_tracked_links', 'created_at', 'SELECT', FALSE),
        ('transition_insert_tracked_link_id', 'public.message_interaction_link_transitions', 'tracked_link_id', 'INSERT', TRUE),
        ('transition_insert_received_at', 'public.message_interaction_link_transitions', 'received_at', 'INSERT', TRUE),
        ('transition_insert_id', 'public.message_interaction_link_transitions', 'id', 'INSERT', FALSE),
        ('transition_select_id', 'public.message_interaction_link_transitions', 'id', 'SELECT', TRUE),
        ('transition_select_tracked_link_id', 'public.message_interaction_link_transitions', 'tracked_link_id', 'SELECT', FALSE),
        ('transition_select_received_at', 'public.message_interaction_link_transitions', 'received_at', 'SELECT', FALSE)
)
SELECT
    code,
    has_column_privilege(current_user, relation_name, column_name, privilege_name),
    expected
FROM checks
ORDER BY code
"""

_FORBIDDEN_RELATION_PRIVILEGES_SQL = """
WITH checks(code, relation_name, privilege_name, include_columns) AS (
    VALUES
        ('snapshot_insert', 'public.message_interaction_tracked_links', 'INSERT', TRUE),
        ('snapshot_update', 'public.message_interaction_tracked_links', 'UPDATE', TRUE),
        ('snapshot_references', 'public.message_interaction_tracked_links', 'REFERENCES', TRUE),
        ('snapshot_delete', 'public.message_interaction_tracked_links', 'DELETE', FALSE),
        ('snapshot_truncate', 'public.message_interaction_tracked_links', 'TRUNCATE', FALSE),
        ('snapshot_trigger', 'public.message_interaction_tracked_links', 'TRIGGER', FALSE),
        ('transition_update', 'public.message_interaction_link_transitions', 'UPDATE', TRUE),
        ('transition_references', 'public.message_interaction_link_transitions', 'REFERENCES', TRUE),
        ('transition_delete', 'public.message_interaction_link_transitions', 'DELETE', FALSE),
        ('transition_truncate', 'public.message_interaction_link_transitions', 'TRUNCATE', FALSE),
        ('transition_trigger', 'public.message_interaction_link_transitions', 'TRIGGER', FALSE)
)
SELECT
    code,
    has_table_privilege(current_user, relation_name, privilege_name)
    OR (
        include_columns
        AND has_any_column_privilege(current_user, relation_name, privilege_name)
    )
FROM checks
ORDER BY code
"""

_SEQUENCE_PRIVILEGES_SQL = """
WITH checks(code, privilege_name, expected) AS (
    VALUES
        ('sequence_usage', 'USAGE', TRUE),
        ('sequence_select', 'SELECT', FALSE),
        ('sequence_update', 'UPDATE', FALSE)
)
SELECT
    code,
    has_sequence_privilege(
        current_user,
        'public.message_interaction_link_transitions_id_seq',
        privilege_name
    ),
    expected
FROM checks
ORDER BY code
"""

_ROLE_ATTRIBUTES_SQL = """
SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls
FROM pg_roles
WHERE rolname = current_user
"""

_ROLE_MEMBERSHIP_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM pg_auth_members membership
    JOIN pg_roles member_role ON member_role.oid = membership.member
    WHERE member_role.rolname = current_user
)
"""

_OTHER_TABLE_PRIVILEGES_SQL = """
SELECT namespace.nspname, relation.relname
FROM pg_class relation
JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
  AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND relation.relname NOT IN (
      'message_interaction_tracked_links',
      'message_interaction_link_transitions'
  )
  AND (
      has_table_privilege(current_user, relation.oid, 'SELECT')
      OR has_table_privilege(current_user, relation.oid, 'INSERT')
      OR has_table_privilege(current_user, relation.oid, 'UPDATE')
      OR has_table_privilege(current_user, relation.oid, 'DELETE')
      OR has_table_privilege(current_user, relation.oid, 'TRUNCATE')
      OR has_table_privilege(current_user, relation.oid, 'REFERENCES')
      OR has_table_privilege(current_user, relation.oid, 'TRIGGER')
      OR has_any_column_privilege(current_user, relation.oid, 'SELECT')
      OR has_any_column_privilege(current_user, relation.oid, 'INSERT')
      OR has_any_column_privilege(current_user, relation.oid, 'UPDATE')
      OR has_any_column_privilege(current_user, relation.oid, 'REFERENCES')
  )
ORDER BY namespace.nspname, relation.relname
"""

_OWNED_OBJECTS_SQL = """
SELECT namespace.nspname, relation.relname
FROM pg_class relation
JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
  AND relation.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
  AND pg_get_userbyid(relation.relowner) = current_user
ORDER BY namespace.nspname, relation.relname
"""


def _check(code: str, ok: bool, message: str) -> dict[str, Any]:
    """Создаёт безопасную запись результата без имён ролей и секретов."""

    return {
        "code": code,
        "status": "ok" if ok else "blocked",
        "message": message,
    }


def _audit_cursor(cursor) -> list[dict[str, Any]]:
    """Выполняет немутирующие проверки текущей роли PostgreSQL."""

    checks: list[dict[str, Any]] = []

    cursor.execute(_REQUIRED_OBJECTS_SQL)
    object_names = cursor.fetchone() or (None, None, None)
    expected_objects = (
        "message_interaction_tracked_links",
        "message_interaction_link_transitions",
        "message_interaction_link_transitions_id_seq",
    )
    objects_ready = tuple(object_names) == expected_objects
    checks.append(
        _check(
            "required_objects",
            objects_ready,
            "Таблицы и последовательность присутствуют."
            if objects_ready
            else "Не найдены обязательные таблицы или последовательность.",
        )
    )
    if not objects_ready:
        return checks

    cursor.execute(_CONNECTION_PRIVILEGES_SQL)
    can_connect, can_use_schema = cursor.fetchone() or (False, False)
    checks.extend(
        (
            _check(
                "database_connect",
                bool(can_connect),
                "Подключение к базе разрешено."
                if can_connect
                else "Подключение к базе не разрешено.",
            ),
            _check(
                "schema_usage",
                bool(can_use_schema),
                "Использование схемы public разрешено."
                if can_use_schema
                else "Использование схемы public не разрешено.",
            ),
        )
    )

    cursor.execute(_COLUMN_PRIVILEGES_SQL)
    for code, actual, expected in cursor.fetchall():
        checks.append(
            _check(
                str(code),
                bool(actual) is bool(expected),
                "Столбцовое право соответствует минимальному контракту."
                if bool(actual) is bool(expected)
                else "Столбцовое право не соответствует минимальному контракту.",
            )
        )

    cursor.execute(_FORBIDDEN_RELATION_PRIVILEGES_SQL)
    for code, granted in cursor.fetchall():
        checks.append(
            _check(
                str(code),
                not bool(granted),
                "Запрещённое право отсутствует."
                if not granted
                else "Обнаружено запрещённое право на таблицу.",
            )
        )

    cursor.execute(_SEQUENCE_PRIVILEGES_SQL)
    for code, actual, expected in cursor.fetchall():
        checks.append(
            _check(
                str(code),
                bool(actual) is bool(expected),
                "Право последовательности соответствует минимальному контракту."
                if bool(actual) is bool(expected)
                else "Право последовательности не соответствует минимальному контракту.",
            )
        )

    cursor.execute(_ROLE_ATTRIBUTES_SQL)
    role_attributes = cursor.fetchone()
    special_attributes_absent = bool(role_attributes) and not any(role_attributes)
    checks.append(
        _check(
            "role_special_attributes",
            special_attributes_absent,
            "У роли отсутствуют специальные атрибуты."
            if special_attributes_absent
            else "У роли обнаружены специальные атрибуты.",
        )
    )

    cursor.execute(_ROLE_MEMBERSHIP_SQL)
    has_membership = bool((cursor.fetchone() or (True,))[0])
    checks.append(
        _check(
            "role_membership",
            not has_membership,
            "Роль не наследует права других ролей."
            if not has_membership
            else "Роль состоит в другой роли.",
        )
    )

    cursor.execute(_OTHER_TABLE_PRIVILEGES_SQL)
    other_tables = cursor.fetchall()
    checks.append(
        _check(
            "other_table_privileges",
            not other_tables,
            "Права на остальные таблицы схемы public отсутствуют."
            if not other_tables
            else "Обнаружены права на другие таблицы схемы public.",
        )
    )

    cursor.execute(_OWNED_OBJECTS_SQL)
    owned_objects = cursor.fetchall()
    checks.append(
        _check(
            "owned_objects",
            not owned_objects,
            "Роль не владеет объектами схемы public."
            if not owned_objects
            else "Роль владеет объектами схемы public.",
        )
    )
    return checks


def build_redirect_role_readiness_report() -> dict[str, Any]:
    """Строит безопасный отчёт о готовности ограниченной роли."""

    if connection.vendor != "postgresql":
        checks = [
            _check(
                "database_backend",
                False,
                "Проверка прав поддерживает только PostgreSQL.",
            )
        ]
    else:
        try:
            with connection.cursor() as cursor:
                checks = _audit_cursor(cursor)
        except DatabaseError as error:
            checks = [
                _check(
                    "database_audit",
                    False,
                    f"Проверка PostgreSQL завершилась ошибкой типа {type(error).__name__}.",
                )
            ]

    blocked = sum(item["status"] == "blocked" for item in checks)
    return {
        "summary": {
            "overall_status": "blocked" if blocked else "ready",
            "checks_total": len(checks),
            "checks_ok": len(checks) - blocked,
            "checks_blocked": blocked,
        },
        "checks": checks,
    }


class Command(BaseCommand):
    """Проверяет права текущей роли и ничего не записывает в базу."""

    help = "Проверяет минимальные права роли публичной службы переходов."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--as-json",
            action="store_true",
            help="Вывести результат в JSON.",
        )

    def handle(self, *args, **options) -> None:
        report = build_redirect_role_readiness_report()
        if options.get("as_json"):
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            self._print_human_report(report)
        if report["summary"]["overall_status"] == "blocked":
            raise SystemExit(1)

    def _print_human_report(self, report: dict[str, Any]) -> None:
        """Выводит краткий результат без имени роли и объектов-нарушителей."""

        summary = report["summary"]
        self.stdout.write("=== Права службы отслеживаемых ссылок ===")
        self.stdout.write(
            "Итог: "
            + ("готово" if summary["overall_status"] == "ready" else "заблокировано")
        )
        self.stdout.write(
            f"Проверки: успешно={summary['checks_ok']} "
            f"блокировки={summary['checks_blocked']}"
        )
        for item in report["checks"]:
            self.stdout.write(
                f"[{item['status'].upper()}] {item['code']}: {item['message']}"
            )
