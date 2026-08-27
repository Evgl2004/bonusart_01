"""Проверки разового аудита минимальной роли службы переходов."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

from guests.management.commands.audit_tracked_link_redirect_permissions import (
    _audit_cursor,
)


class _ScriptedCursor:
    """Возвращает заранее заданные ответы последовательных запросов."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.current = None

    def execute(self, sql):
        self.current = self.responses.pop(0)

    def fetchone(self):
        kind, value = self.current
        assert kind == "one"
        return value

    def fetchall(self):
        kind, value = self.current
        assert kind == "all"
        return value


def _positive_responses(*, required_column_granted: bool = True):
    column_checks = [
        ("required_column", required_column_granted, True),
        ("forbidden_column", False, False),
    ]
    return [
        (
            "one",
            (
                "message_interaction_tracked_links",
                "message_interaction_link_transitions",
                "message_interaction_link_transitions_id_seq",
            ),
        ),
        ("one", (True, True)),
        ("all", column_checks),
        ("all", [("forbidden_relation", False)]),
        ("all", [("sequence_usage", True, True), ("sequence_select", False, False)]),
        ("one", (False, False, False, False, False)),
        ("one", (False,)),
        ("all", []),
        ("all", []),
    ]


class TrackedLinkRedirectPermissionAuditTests(SimpleTestCase):
    """Проверяет положительный и блокирующие результаты без настоящей базы."""

    def test_minimal_role_passes_all_checks(self):
        checks = _audit_cursor(_ScriptedCursor(_positive_responses()))

        self.assertTrue(checks)
        self.assertTrue(all(item["status"] == "ok" for item in checks))

    def test_missing_object_stops_privilege_queries(self):
        cursor = _ScriptedCursor(
            [
                (
                    "one",
                    (
                        "message_interaction_tracked_links",
                        None,
                        "message_interaction_link_transitions_id_seq",
                    ),
                )
            ]
        )

        checks = _audit_cursor(cursor)

        self.assertEqual(checks[0]["code"], "required_objects")
        self.assertEqual(checks[0]["status"], "blocked")
        self.assertEqual(cursor.responses, [])

    def test_missing_required_column_privilege_is_blocking(self):
        checks = _audit_cursor(
            _ScriptedCursor(
                _positive_responses(required_column_granted=False)
            )
        )

        required = next(item for item in checks if item["code"] == "required_column")
        self.assertEqual(required["status"], "blocked")

    def test_command_returns_nonzero_for_blocked_report(self):
        blocked_report = {
            "summary": {
                "overall_status": "blocked",
                "checks_total": 1,
                "checks_ok": 0,
                "checks_blocked": 1,
            },
            "checks": [
                {
                    "code": "blocked",
                    "status": "blocked",
                    "message": "Проверка заблокирована.",
                }
            ],
        }
        with patch(
            "guests.management.commands.audit_tracked_link_redirect_permissions."
            "build_redirect_role_readiness_report",
            return_value=blocked_report,
        ):
            with self.assertRaises(SystemExit) as error:
                call_command(
                    "audit_tracked_link_redirect_permissions",
                    "--as-json",
                    stdout=StringIO(),
                )

        self.assertEqual(error.exception.code, 1)
