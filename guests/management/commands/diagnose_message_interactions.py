"""Команда безопасной диагностики интерактивных сообщений."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from guests.services.message_interaction_operations import (
    MessageInteractionOperationError,
    build_message_interaction_diagnostic_report,
)


class Command(BaseCommand):
    """Ищет интерактивности без текста, адресатов, токенов и конечных адресов."""

    help = "Диагностирует интерактивности по сообщению, событию, рассылке или сценарию."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--interaction-id", type=int, default=None)
        parser.add_argument("--event-id", default="")
        parser.add_argument("--mailing-id", type=int, default=None)
        parser.add_argument("--scenario-id", type=int, default=None)
        parser.add_argument("--limit", type=int, default=50, help="Не более 100 строк.")
        parser.add_argument("--as-json", action="store_true", help="Вывести результат в JSON.")

    def handle(self, *args, **options) -> None:
        try:
            report = build_message_interaction_diagnostic_report(
                interaction_id=options["interaction_id"],
                event_id=options["event_id"],
                mailing_id=options["mailing_id"],
                scenario_id=options["scenario_id"],
                limit=options["limit"],
            )
        except MessageInteractionOperationError as error:
            raise CommandError(str(error)) from error

        if options.get("as_json"):
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            self._print_human_report(report)

    def _print_human_report(self, report: dict[str, Any]) -> None:
        self.stdout.write("=== Диагностика интерактивных сообщений SAGUR ===")
        self.stdout.write(f"Критерий: {report['selector']}")
        self.stdout.write(
            f"Найдено: {report['total']}; показано: {len(report['interactions'])}; "
            f"обрезано: {'да' if report['truncated'] else 'нет'}"
        )
        for item in report["interactions"]:
            tracked_link = item["tracked_link"]
            disabled_text = (
                "да"
                if tracked_link["disabled"] is True
                else "нет" if tracked_link["disabled"] is False else "—"
            )
            self.stdout.write(
                "Интерактивность={interaction_id} задача={dispatch_task_id} "
                "платформа={provider} статус={task_status} набор={button_set} "
                "события={events_total} оценки={accepted_ratings_total} "
                "повторные_оценки={repeated_ratings_total} "
                "купоны={coupon_actions_total} меню={menu_actions_total} "
                "ссылка={link_exists} подпись_ссылки={link_label_code} "
                "переходы={transitions_total} ссылка_отключена={link_disabled} "
                "первый_переход={first_transition_at} "
                "последний_переход={last_transition_at}".format(
                    **item,
                    link_exists="да" if tracked_link["exists"] else "нет",
                    link_label_code=tracked_link["label_code"] or "—",
                    transitions_total=tracked_link["transitions_total"],
                    link_disabled=disabled_text,
                    first_transition_at=tracked_link["first_transition_at"] or "—",
                    last_transition_at=tracked_link["last_transition_at"] or "—",
                )
            )
        if report["selected_event"] is not None:
            event = report["selected_event"]
            self.stdout.write(
                "Событие={event_id} действие={action} результат={result} "
                "идентификатор_платформы_присутствует={provider_message_id_present}".format(**event)
            )
