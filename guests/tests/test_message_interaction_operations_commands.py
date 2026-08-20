"""Положительные и отрицательные проверки эксплуатационных команд."""

from __future__ import annotations

import json
import uuid
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    GuestBotBinding,
    InteractionButtonSet,
    MessageInteraction,
    MessageInteractionEvent,
)


READY_SETTINGS = {
    "MESSAGE_INTERACTIONS_ENABLED": True,
    "MESSAGE_INTERACTIONS_ALLOWED_PROVIDERS": {"telegram"},
    "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_ENABLED": True,
    "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_HMAC_SECRET": "test-secret",
    "VTELEMAX_MESSAGE_INTERACTION_CALLBACK_REQUIRE_HTTPS": True,
}


@override_settings(**READY_SETTINGS)
class MessageInteractionOperationsCommandTests(TestCase):
    """Проверяет безопасность, идемпотентность и диагностический вывод."""

    def setUp(self) -> None:
        self.guest = Guest.objects.create(phone="+79990000001", first_name="Пилот")
        self.bot = BotProfile.objects.create(
            code="pilot_telegram_bot",
            name="Пилотный Telegram",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            token="test-token",
            is_active=True,
        )
        self.binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot,
            external_chat_id="secret-recipient-id",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

    def _call_pilot(self, *extra_arguments: str) -> dict[str, object]:
        output = StringIO()
        call_command(
            "pilot_message_interaction",
            "--guest-id",
            str(self.guest.id),
            "--bot-code",
            self.bot.code,
            "--as-json",
            *extra_arguments,
            stdout=output,
        )
        return json.loads(output.getvalue())

    def test_readiness_reports_ready_without_exposing_secret(self):
        """Полная корректная конфигурация проходит строгий аудит."""

        output = StringIO()
        call_command(
            "audit_message_interactions_readiness",
            "--as-json",
            "--require-enabled",
            stdout=output,
        )

        raw_output = output.getvalue()
        payload = json.loads(raw_output)
        self.assertEqual(payload["summary"]["overall_status"], "ready")
        self.assertNotIn("test-secret", raw_output)
        self.assertNotIn("test-token", raw_output)
        self.assertNotIn("secret-recipient-id", raw_output)

    @override_settings(
        MESSAGE_INTERACTIONS_ENABLED=False,
        MESSAGE_INTERACTIONS_ALLOWED_PROVIDERS={"telegram", "unknown"},
        VTELEMAX_MESSAGE_INTERACTION_CALLBACK_ENABLED=False,
        VTELEMAX_MESSAGE_INTERACTION_CALLBACK_HMAC_SECRET="",
    )
    def test_readiness_blocks_disabled_and_unknown_configuration(self):
        """Строгий аудит отличает отключение от готового состояния."""

        output = StringIO()
        call_command(
            "audit_message_interactions_readiness",
            "--as-json",
            "--require-enabled",
            stdout=output,
        )

        payload = json.loads(output.getvalue())
        checks = {item["code"]: item for item in payload["checks"]}
        self.assertEqual(payload["summary"]["overall_status"], "blocked")
        self.assertEqual(checks["formation_enabled"]["status"], "blocked")
        self.assertEqual(checks["allowed_providers"]["status"], "blocked")
        self.assertEqual(checks["callback_enabled"]["status"], "blocked")
        self.assertEqual(checks["callback_secret"]["status"], "blocked")

        with self.assertRaises(SystemExit) as error:
            call_command(
                "audit_message_interactions_readiness",
                "--fail-on-blocked",
                "--require-enabled",
                stdout=StringIO(),
            )
        self.assertEqual(error.exception.code, 1)

    def test_readiness_turns_provider_query_failure_into_safe_blocker(self):
        """Неполная схема не должна приводить к необработанному исключению."""

        output = StringIO()
        with patch(
            "guests.services.message_interaction_operations.BotProfile.objects.filter",
            side_effect=DatabaseError("secret database diagnostics"),
        ):
            call_command(
                "audit_message_interactions_readiness",
                "--as-json",
                "--require-enabled",
                stdout=output,
            )

        raw_output = output.getvalue()
        payload = json.loads(raw_output)
        checks = {item["code"]: item for item in payload["checks"]}
        self.assertEqual(checks["provider_telegram"]["status"], "blocked")
        self.assertEqual(
            checks["provider_telegram"]["details"]["error_type"],
            "DatabaseError",
        )
        self.assertNotIn("secret database diagnostics", raw_output)

    def test_pilot_is_read_only_without_confirm(self):
        """Сухой запуск не создаёт задачу или интерактивность."""

        result = self._call_pilot()

        self.assertTrue(result["dry_run"])
        self.assertTrue(result["ready"])
        self.assertEqual(DispatchTask.objects.count(), 0)
        self.assertEqual(MessageInteraction.objects.count(), 0)

    def test_confirmed_pilot_creates_one_regular_interactive_task(self):
        """Подтверждение использует штатную атомарную службу создания задачи."""

        result = self._call_pilot(
            "--confirm",
            "--run-id",
            "pilot-20260820-01",
            "--message-text",
            "Безопасная проверка кнопок",
        )

        task = DispatchTask.objects.select_related("message_interaction").get()
        self.assertTrue(result["created"])
        self.assertEqual(result["dispatch_task_id"], task.id)
        self.assertEqual(task.source_type, DispatchTask.SourceType.MANUAL)
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertEqual(task.guest_binding, self.binding)
        self.assertEqual(task.message_text, "Безопасная проверка кнопок")
        self.assertEqual(
            task.message_interaction.button_set,
            InteractionButtonSet.RATING_MENU,
        )

    def test_repeated_pilot_is_idempotent_but_parameter_conflict_is_rejected(self):
        """Один идентификатор запуска не создаёт дубль и не маскирует конфликт."""

        arguments = (
            "--confirm",
            "--run-id",
            "pilot-20260820-idempotent",
            "--message-text",
            "Одинаковый пилот",
        )
        first = self._call_pilot(*arguments)
        second = self._call_pilot(*arguments)

        self.assertTrue(first["created"])
        self.assertTrue(second["already_exists"])
        self.assertEqual(DispatchTask.objects.count(), 1)
        self.assertEqual(MessageInteraction.objects.count(), 1)

        with self.assertRaisesMessage(
            CommandError,
            "Идентификатор запуска уже использован с другими параметрами.",
        ):
            self._call_pilot(
                "--confirm",
                "--run-id",
                "pilot-20260820-idempotent",
                "--message-text",
                "Другой текст",
            )

    def test_confirm_requires_run_id_and_permitted_modern_binding(self):
        """Подтверждение не допускает неидемпотентную или запрещённую цель."""

        with self.assertRaisesMessage(CommandError, "идентификатор запуска"):
            self._call_pilot("--confirm")

        self.binding.is_opt_in = False
        self.binding.save(update_fields=["is_opt_in"])
        with self.assertRaisesMessage(CommandError, "не разрешил отправку"):
            self._call_pilot(
                "--confirm",
                "--run-id",
                "pilot-no-permission",
            )
        self.assertEqual(DispatchTask.objects.count(), 0)

    @override_settings(MESSAGE_INTERACTIONS_ALLOWED_PROVIDERS={"vk"})
    def test_confirm_rejects_provider_disabled_by_allowlist(self):
        """Активного бота недостаточно без разрешения платформы настройкой."""

        with self.assertRaisesMessage(CommandError, "выключено для выбранной платформы"):
            self._call_pilot(
                "--confirm",
                "--run-id",
                "pilot-provider-disabled",
            )
        self.assertEqual(DispatchTask.objects.count(), 0)

    def test_diagnosis_counts_events_and_hides_message_and_recipient(self):
        """Диагностика показывает семантику событий, но не содержимое сообщения."""

        pilot = self._call_pilot(
            "--confirm",
            "--run-id",
            "pilot-diagnostics",
            "--message-text",
            "secret-message-text",
        )
        interaction = MessageInteraction.objects.get(pk=pilot["interaction_id"])
        now = timezone.now()
        events = [
            ("l", MessageInteractionEvent.Result.ACCEPTED),
            ("d", MessageInteractionEvent.Result.RATING_ALREADY_RECORDED),
            ("m", MessageInteractionEvent.Result.ACCEPTED),
            ("m", MessageInteractionEvent.Result.ACCEPTED),
        ]
        created_events = [
            MessageInteractionEvent.objects.create(
                event_id=uuid.uuid4(),
                interaction=interaction,
                action=action,
                result=result,
                occurred_at=now,
                provider_message_id="secret-provider-message-id",
            )
            for action, result in events
        ]

        output = StringIO()
        call_command(
            "diagnose_message_interactions",
            "--interaction-id",
            str(interaction.id),
            "--as-json",
            stdout=output,
        )
        raw_output = output.getvalue()
        payload = json.loads(raw_output)
        row = payload["interactions"][0]
        self.assertEqual(row["events_total"], 4)
        self.assertEqual(row["accepted_ratings_total"], 1)
        self.assertEqual(row["repeated_ratings_total"], 1)
        self.assertEqual(row["menu_actions_total"], 2)
        self.assertNotIn("secret-message-text", raw_output)
        self.assertNotIn("secret-recipient-id", raw_output)
        self.assertNotIn("secret-provider-message-id", raw_output)

        event_output = StringIO()
        call_command(
            "diagnose_message_interactions",
            "--event-id",
            str(created_events[0].event_id),
            "--as-json",
            stdout=event_output,
        )
        event_payload = json.loads(event_output.getvalue())
        self.assertTrue(event_payload["selected_event"]["provider_message_id_present"])
        self.assertEqual(event_payload["selected_event"]["action"], "l")

    def test_diagnosis_requires_exactly_one_valid_selector(self):
        """Пустой, множественный и некорректный поиск завершаются ошибкой."""

        with self.assertRaisesMessage(CommandError, "ровно один критерий"):
            call_command("diagnose_message_interactions", stdout=StringIO())
        with self.assertRaisesMessage(CommandError, "ровно один критерий"):
            call_command(
                "diagnose_message_interactions",
                "--interaction-id",
                "1",
                "--mailing-id",
                "1",
                stdout=StringIO(),
            )
        with self.assertRaisesMessage(CommandError, "корректным UUID"):
            call_command(
                "diagnose_message_interactions",
                "--event-id",
                "not-a-uuid",
                stdout=StringIO(),
            )
