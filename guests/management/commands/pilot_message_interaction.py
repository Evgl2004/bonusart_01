"""Команда безопасного пилота интерактивного сообщения."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from guests.models import InteractionButtonSet
from guests.services.message_interaction_operations import (
    MessageInteractionOperationError,
    run_message_interaction_pilot,
)


DEFAULT_PILOT_TEXT = "ТЕСТ SAGUR — интерактивные кнопки."


class Command(BaseCommand):
    """Проверяет цель и только с ``--confirm`` создаёт задачу в очереди."""

    help = (
        "Проверяет одного гостя и бота; с --confirm ставит одно интерактивное "
        "сообщение в штатную очередь."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--guest-id", type=int, required=True, help="Внутренний идентификатор гостя.")
        parser.add_argument("--bot-code", required=True, help="Точный код профиля бота.")
        parser.add_argument(
            "--button-set",
            choices=(
                InteractionButtonSet.RATING_MENU,
                InteractionButtonSet.RATING_COUPONS,
                InteractionButtonSet.RATING_MENU_LINK,
            ),
            default=InteractionButtonSet.RATING_MENU,
            help="Набор кнопок пилотного сообщения.",
        )
        parser.add_argument(
            "--tracked-link-destination-code",
            default="",
            help=(
                "Точный код активного назначения из справочника; обязателен только "
                "для набора rating_menu_link."
            ),
        )
        parser.add_argument(
            "--message-text",
            default=DEFAULT_PILOT_TEXT,
            help="Текст пилотного сообщения без персональных данных.",
        )
        parser.add_argument(
            "--run-id",
            default="",
            help="Уникальный идентификатор подтверждённого запуска.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Создать штатную задачу; без флага выполняется только чтение.",
        )
        parser.add_argument("--as-json", action="store_true", help="Вывести результат в JSON.")

    def handle(self, *args, **options) -> None:
        try:
            result = run_message_interaction_pilot(
                guest_id=options["guest_id"],
                bot_code=options["bot_code"],
                button_set=options["button_set"],
                tracked_link_destination_code=options[
                    "tracked_link_destination_code"
                ],
                message_text=options["message_text"],
                run_id=options["run_id"],
                confirm=bool(options["confirm"]),
            )
        except MessageInteractionOperationError as error:
            raise CommandError(str(error)) from error

        if options.get("as_json"):
            self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            self._print_human_result(result)

    def _print_human_result(self, result: dict[str, Any]) -> None:
        self.stdout.write("=== Пилот интерактивного сообщения SAGUR ===")
        self.stdout.write(f"Гость: {result['guest_id']}")
        self.stdout.write(f"Бот: {result['bot_code']}")
        self.stdout.write(f"Платформа: {result['provider'] or '-'}")
        self.stdout.write(f"Набор кнопок: {result['button_set']}")
        if result.get("tracked_link_destination_code"):
            self.stdout.write(
                "Назначение ссылки: "
                f"{result['tracked_link_destination_code']}"
            )
        self.stdout.write(f"Готовность: {'да' if result['ready'] else 'нет'}")
        for blocker in result["blockers"]:
            self.stdout.write(f"[БЛОКИРОВКА] {blocker}")
        for warning in result["warnings"]:
            self.stdout.write(f"[ПРЕДУПРЕЖДЕНИЕ] {warning}")

        if result["dry_run"]:
            self.stdout.write("Режим: сухой расчёт; база данных не изменена.")
        elif result["already_exists"]:
            self.stdout.write("Режим: повторный запуск; новая задача не создана.")
        else:
            self.stdout.write("Режим: задача создана и ожидает штатного диспетчера.")
        if result["dispatch_task_id"] is not None:
            self.stdout.write(f"Задача: {result['dispatch_task_id']}")
            self.stdout.write(f"Интерактивность: {result['interaction_id']}")
