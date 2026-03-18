"""
Тесты сервиса balance_notifications.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from guests.models import DispatchTask, Guest
from guests.services import balance_notifications
from guests.services.notification_events import ScenarioNotConfiguredError


class BalanceNotificationsServiceTests(TestCase):
    """
    Проверки веток balance_notifications: helpers + enqueue flow.
    """

    def setUp(self):
        super().setUp()
        self.guest = Guest.objects.create(
            phone="+79990007766",
            iiko_id="iiko-guest-balance",
            first_name="Баланс",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        self.balance_webhook = {
            "id": "wh-balance-100",
            "category_id_ext": balance_notifications.BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID,
            "parsed_body": {
                "phone": self.guest.phone,
                "notificationType": 9,
                "changeSum": "150",
                "text": "Баланс изменён на 150",
            },
        }

    def test_extract_balance_change_value_uses_first_non_empty_field(self):
        """
        Значение изменения баланса должно браться из первого заполненного поля.
        """
        event = {"newBalance": "", "balance": "200", "sum": "300"}
        self.assertEqual(balance_notifications._extract_balance_change_value(event), "200")
        self.assertIsNone(balance_notifications._extract_balance_change_value({}))

    def test_extract_category_external_id_from_webhook_and_event(self):
        """
        category_id_ext должен извлекаться в порядке webhook -> event.
        """
        webhook = {"category_id_ext": "A"}
        event = {"categoryExternalId": "B"}
        self.assertEqual(balance_notifications._extract_category_external_id(webhook, event), "A")
        self.assertEqual(balance_notifications._extract_category_external_id({}, event), "B")
        self.assertEqual(balance_notifications._extract_category_external_id({}, {}), "")

    def test_is_balance_webhook_true_false(self):
        """
        Проверяем positive/negative сценарии распознавания balance-webhook.
        """
        self.assertTrue(
            balance_notifications.is_balance_webhook(
                {"category_id_ext": balance_notifications.BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID},
                {},
            )
        )
        self.assertFalse(balance_notifications.is_balance_webhook({"category_id_ext": "other"}, {}))

    def test_build_balance_notification_text_fallbacks(self):
        """
        Текст должен строиться из event.text, затем из change value, затем default.
        """
        self.assertEqual(
            balance_notifications._build_balance_notification_text({"text": "  custom text  "}),
            "custom text",
        )
        self.assertEqual(
            balance_notifications._build_balance_notification_text({"newBalance": "123"}),
            "Изменение баланса: 123",
        )
        self.assertEqual(
            balance_notifications._build_balance_notification_text({}),
            "Произошло изменение баланса.",
        )

    def test_find_guest_by_phone_text_and_customer_id(self):
        """
        _find_guest должен искать по phone, затем по text (regex), затем customerId.
        """
        by_phone = balance_notifications._find_guest({"phone": self.guest.phone})
        self.assertEqual(by_phone.id, self.guest.id)

        by_text = balance_notifications._find_guest({"text": "Имя (Guest.Name): +79990007766"})
        self.assertEqual(by_text.id, self.guest.id)

        by_customer = balance_notifications._find_guest({"customerId": self.guest.iiko_id})
        self.assertEqual(by_customer.id, self.guest.id)

    def test_get_or_create_guest_from_iiko_paths(self):
        """
        _get_or_create_guest_from_iiko должен создавать гостя, а при отсутствии данных возвращать None.
        """
        fake_client = types.SimpleNamespace(
            get_customer_by_phone=lambda _phone: {
                "customer": {
                    "id": "iiko-created-1",
                    "phone": "+79998887766",
                    "name": "Новый",
                    "surname": "Гость",
                    "email": "new@example.com",
                }
            }
        )
        fake_module = types.SimpleNamespace(iiko_client=fake_client)

        with patch.dict(sys.modules, {"guests.services.iiko_client": fake_module}):
            created = balance_notifications._get_or_create_guest_from_iiko("+79998887766")
        self.assertIsNotNone(created)
        self.assertEqual(created.iiko_id, "iiko-created-1")

        fake_client_empty = types.SimpleNamespace(get_customer_by_phone=lambda _phone: None)
        fake_module_empty = types.SimpleNamespace(iiko_client=fake_client_empty)
        with patch.dict(sys.modules, {"guests.services.iiko_client": fake_module_empty}):
            missing = balance_notifications._get_or_create_guest_from_iiko("+79990001100")
        self.assertIsNone(missing)

    def test_build_balance_dedupe_key_with_webhook_id_and_fallback(self):
        """
        dedupe key должен быть детерминированным и использовать webhook id при наличии.
        """
        direct = balance_notifications._build_balance_dedupe_key(
            webhook={"id": "wh-123"},
            event={},
            guest_id=self.guest.id,
        )
        self.assertEqual(direct, "balance:webhook:wh-123")

        event = {
            "changedOn": "2026-03-18T10:00:00+05:00",
            "notificationType": 9,
            "changeSum": "50",
            "categoryExternalId": balance_notifications.BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID,
        }
        fallback_1 = balance_notifications._build_balance_dedupe_key(
            webhook={},
            event=event,
            guest_id=self.guest.id,
        )
        fallback_2 = balance_notifications._build_balance_dedupe_key(
            webhook={},
            event=json.loads(json.dumps(event)),
            guest_id=self.guest.id,
        )
        self.assertTrue(fallback_1.startswith("balance:fallback:"))
        self.assertEqual(fallback_1, fallback_2)

    def test_enqueue_balance_notification_main_flow_uses_create_notification_event(self):
        """
        Основной flow должен вызывать create_notification_event и вернуть его результат.
        """
        with patch(
            "guests.services.balance_notifications.create_notification_event",
            return_value=2,
        ) as mocked_create:
            created = balance_notifications.enqueue_balance_notification_from_webhook(
                self.balance_webhook,
                is_enabled=True,
                priority=DispatchTask.Priority.HIGH,
                primary_only=True,
            )

        self.assertEqual(created, 2)
        self.assertEqual(mocked_create.call_args.kwargs["scenario_code"], "balance_changed")
        self.assertEqual(mocked_create.call_args.kwargs["route_target_mode"], "primary_only")

    def test_enqueue_balance_notification_fallback_when_scenario_not_configured(self):
        """
        При ScenarioNotConfiguredError должен использоваться fallback enqueue_guest_notification_tasks.
        """
        with (
            patch(
                "guests.services.balance_notifications.create_notification_event",
                side_effect=ScenarioNotConfiguredError("missing"),
            ),
            patch(
                "guests.services.balance_notifications.enqueue_guest_notification_tasks",
                return_value=1,
            ) as mocked_fallback,
        ):
            created = balance_notifications.enqueue_balance_notification_from_webhook(
                self.balance_webhook,
                is_enabled=True,
                priority=DispatchTask.Priority.NORMAL,
                primary_only=False,
            )

        self.assertEqual(created, 1)
        self.assertEqual(mocked_fallback.call_args.kwargs["primary_only"], False)

    def test_enqueue_balance_notification_guard_branches_return_zero(self):
        """
        Guard-ветки (disabled/не balance/некорректный event/guest missing) должны возвращать 0.
        """
        self.assertEqual(
            balance_notifications.enqueue_balance_notification_from_webhook(
                self.balance_webhook,
                is_enabled=False,
            ),
            0,
        )

        non_balance = {
            "id": "wh-other",
            "category_id_ext": "other",
            "parsed_body": {"phone": self.guest.phone},
        }
        self.assertEqual(balance_notifications.enqueue_balance_notification_from_webhook(non_balance), 0)

        invalid_event = {
            "id": "wh-invalid",
            "category_id_ext": balance_notifications.BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID,
            "parsed_body": "not-a-dict",
        }
        self.assertEqual(balance_notifications.enqueue_balance_notification_from_webhook(invalid_event), 0)

        missing_guest = {
            "id": "wh-missing-guest",
            "category_id_ext": balance_notifications.BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID,
            "parsed_body": {"phone": "+70000000000"},
        }
        with patch("guests.services.balance_notifications._get_or_create_guest_from_iiko", return_value=None):
            self.assertEqual(balance_notifications.enqueue_balance_notification_from_webhook(missing_guest), 0)

