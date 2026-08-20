"""Команда аудита готовности интерактивных сообщений."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand

from guests.services.message_interaction_operations import (
    build_message_interaction_readiness_report,
)


class Command(BaseCommand):
    """Выполняет только читающую проверку настроек и данных."""

    help = "Проверяет готовность SAGUR к формированию и приёму интерактивных сообщений."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--as-json", action="store_true", help="Вывести результат в JSON.")
        parser.add_argument(
            "--fail-on-blocked",
            action="store_true",
            help="Завершить команду с кодом 1 при блокирующей проверке.",
        )
        parser.add_argument(
            "--require-enabled",
            action="store_true",
            help="Считать выключенные входящий и исходящий контуры блокировкой.",
        )

    def handle(self, *args, **options) -> None:
        report = build_message_interaction_readiness_report(
            require_enabled=bool(options.get("require_enabled")),
        )
        if options.get("as_json"):
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            self._print_human_report(report)

        if options.get("fail_on_blocked") and report["summary"]["overall_status"] == "blocked":
            raise SystemExit(1)

    def _print_human_report(self, report: dict[str, Any]) -> None:
        summary = report["summary"]
        labels = {"ready": "готово", "warning": "требует внимания", "blocked": "заблокировано"}
        self.stdout.write("=== Готовность интерактивных сообщений SAGUR ===")
        self.stdout.write(f"Итог: {labels[summary['overall_status']]}")
        self.stdout.write(
            "Проверки: "
            f"успешно={summary['checks_ok']} "
            f"предупреждения={summary['checks_warning']} "
            f"блокировки={summary['checks_blocked']}"
        )
        for item in report["checks"]:
            details = " ".join(f"{key}={value}" for key, value in item["details"].items())
            self.stdout.write(
                f"[{item['status'].upper()}] {item['code']}: {item['message']} ({details})"
            )
        observations = report["observations"]
        self.stdout.write("Наблюдения: " + " ".join(f"{k}={v}" for k, v in observations.items()))
