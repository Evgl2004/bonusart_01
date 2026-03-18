"""
Тесты сервисного слоя webhooks.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from guests.models import Category, Guest, GuestCategory, GuestCategoryAssignment, Restaurant, VisitHistory
from guests.services import webhooks


class WebhookSagurHelpersTests(SimpleTestCase):
    """
    Тесты низкоуровневых helper-функций SAGUR API.
    """

    def setUp(self):
        super().setUp()
        self._old_access_token = webhooks.ACCESS_TOKEN
        self._old_token_expires_at = webhooks.TOKEN_EXPIRES_AT

    def tearDown(self):
        webhooks.ACCESS_TOKEN = self._old_access_token
        webhooks.TOKEN_EXPIRES_AT = self._old_token_expires_at
        super().tearDown()

    @staticmethod
    def _response(*, status_code=200, text="ok", json_data=None):
        response = Mock()
        response.status_code = status_code
        response.text = text
        response.json.return_value = json_data if json_data is not None else {}
        response.raise_for_status.return_value = None
        return response

    def test_verify_token_success_and_failure_and_exception(self):
        """
        verify токена должен корректно обрабатывать success/non-200/exception.
        """
        with patch("guests.services.webhooks.requests.post", return_value=self._response(status_code=200)):
            self.assertTrue(webhooks._verify_token("token123"))

        with patch("guests.services.webhooks.requests.post", return_value=self._response(status_code=403, text="forbidden")):
            self.assertFalse(webhooks._verify_token("token123"))

        with patch("guests.services.webhooks.requests.post", side_effect=RuntimeError("network down")):
            self.assertFalse(webhooks._verify_token("token123"))

    def test_get_new_access_token_extracts_access(self):
        """
        Получение нового токена должно возвращать поле `access`.
        """
        with patch(
            "guests.services.webhooks.requests.post",
            return_value=self._response(json_data={"access": "new-access-token"}),
        ):
            token = webhooks._get_new_access_token()
        self.assertEqual(token, "new-access-token")

    def test_get_sagur_access_token_cached_uses_cache_or_refresh(self):
        """
        Функция должна возвращать кэш, а при истечении — получать новый токен.
        """
        with patch("guests.services.webhooks.time.time", return_value=1000.0):
            webhooks.ACCESS_TOKEN = "cached-token"
            webhooks.TOKEN_EXPIRES_AT = 1200.0
            with patch("guests.services.webhooks._get_new_access_token") as mocked_get:
                self.assertEqual(webhooks._get_sagur_access_token_cached(), "cached-token")
                mocked_get.assert_not_called()

        with patch("guests.services.webhooks.time.time", return_value=2000.0):
            webhooks.ACCESS_TOKEN = "expired-token"
            webhooks.TOKEN_EXPIRES_AT = 1500.0
            with patch("guests.services.webhooks._get_new_access_token", return_value="fresh-token") as mocked_get:
                self.assertEqual(webhooks._get_sagur_access_token_cached(), "fresh-token")
                mocked_get.assert_called_once()
                self.assertEqual(webhooks.ACCESS_TOKEN, "fresh-token")
                self.assertEqual(webhooks.TOKEN_EXPIRES_AT, 2000.0 + 14 * 60)

    def test_iter_pending_webhooks_supports_pagination_list_and_unexpected(self):
        """
        Итерирование pending-webhooks должно обрабатывать dict/list/неожиданный формат.
        """
        page_1 = self._response(json_data={"results": [{"id": 1}], "next": "https://next-url"})
        page_2 = self._response(json_data={"results": [{"id": 2}], "next": None})

        with patch("guests.services.webhooks.requests.get", side_effect=[page_1, page_2]):
            items = list(webhooks._iter_pending_webhooks("token", page_size=100))
        self.assertEqual([item["id"] for item in items], [1, 2])

        with patch("guests.services.webhooks.requests.get", return_value=self._response(json_data=[{"id": 10}, {"id": 11}])):
            items = list(webhooks._iter_pending_webhooks("token", page_size=100))
        self.assertEqual([item["id"] for item in items], [10, 11])

        with patch("guests.services.webhooks.requests.get", return_value=self._response(json_data={"unexpected": "format"})):
            items = list(webhooks._iter_pending_webhooks("token", page_size=100))
        self.assertEqual(items, [])

    def test_update_webhook_business_status_success(self):
        """
        PATCH статуса вебхука должен выполняться с корректным payload.
        """
        mocked_response = self._response(status_code=200)
        with patch("guests.services.webhooks.requests.patch", return_value=mocked_response) as mocked_patch:
            webhooks._update_webhook_business_status("access-token", 42, "complete", "ok")

        call_kwargs = mocked_patch.call_args.kwargs
        self.assertEqual(call_kwargs["json"]["business_status"], "complete")
        self.assertEqual(call_kwargs["json"]["error_description"], "ok")

    def test_update_webhook_business_status_401_resets_cached_token(self):
        """
        При HTTP 401 кэш access-токена должен сбрасываться.
        """
        err_response = Mock()
        err_response.status_code = 401
        err_response.text = "unauthorized"
        http_error = requests.exceptions.HTTPError(response=err_response)

        mocked_response = self._response(status_code=401)
        mocked_response.raise_for_status.side_effect = http_error
        webhooks.ACCESS_TOKEN = "old-token"
        webhooks.TOKEN_EXPIRES_AT = 99999

        with patch("guests.services.webhooks.requests.patch", return_value=mocked_response):
            with self.assertRaises(requests.exceptions.HTTPError):
                webhooks._update_webhook_business_status("access-token", 43, "complete")

        self.assertIsNone(webhooks.ACCESS_TOKEN)
        self.assertEqual(webhooks.TOKEN_EXPIRES_AT, 0)

    def test_update_webhook_business_status_non_401_raises_without_reset(self):
        """
        Любая HTTP-ошибка, кроме 401, должна пробрасываться без сброса кэша.
        """
        err_response = Mock()
        err_response.status_code = 500
        err_response.text = "server error"
        http_error = requests.exceptions.HTTPError(response=err_response)
        mocked_response = self._response(status_code=500)
        mocked_response.raise_for_status.side_effect = http_error

        webhooks.ACCESS_TOKEN = "keep-token"
        webhooks.TOKEN_EXPIRES_AT = 12345

        with patch("guests.services.webhooks.requests.patch", return_value=mocked_response):
            with self.assertRaises(requests.exceptions.HTTPError):
                webhooks._update_webhook_business_status("access-token", 44, "failed")

        self.assertEqual(webhooks.ACCESS_TOKEN, "keep-token")
        self.assertEqual(webhooks.TOKEN_EXPIRES_AT, 12345)


class WebhookGuestAndCategoryTests(TestCase):
    """
    Тесты логики поиска гостя, iiko helper и обработки категорий/визитов.
    """

    def setUp(self):
        super().setUp()
        self.guest = Guest.objects.create(
            phone="+79990009999",
            iiko_id="iiko-guest-1",
            first_name="Гость",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        self.restaurant = Restaurant.objects.create(iiko_id="rest-1", name="Ресторан 1")
        self.category = Category.objects.create(name="Категория 1", external_id="cat-1", is_active=True)

    def test_find_guest_by_phone_text_and_customer_id(self):
        """
        Поиск гостя должен работать по phone, text и customerId.
        """
        by_phone = webhooks.find_guest({"phone": self.guest.phone})
        self.assertEqual(by_phone.id, self.guest.id)

        by_text = webhooks.find_guest({"text": "Имя (Guest.Name): +79990009999"})
        self.assertEqual(by_text.id, self.guest.id)

        by_customer = webhooks.find_guest({"customerId": self.guest.iiko_id})
        self.assertEqual(by_customer.id, self.guest.id)

    def test_is_staff_notification_helper(self):
        """
        staff helper должен срабатывать только при customerId без phone.
        """
        self.assertTrue(webhooks._is_staff_notification({"customerId": "staff-1", "phone": None}))
        self.assertFalse(webhooks._is_staff_notification({"customerId": "staff-1", "phone": "+79990000000"}))

    def test_get_or_create_guest_from_iiko_happy_path_and_no_data(self):
        """
        get_or_create_guest_from_iiko: создание нового гостя и ветка `нет данных`.
        """
        fake_client = Mock()
        fake_client.get_customer_by_phone.side_effect = [
            {
                "customer": {
                    "id": "iiko-new-1",
                    "phone": "+79991112233",
                    "name": "Новый",
                    "surname": "Гость",
                    "email": "new@example.com",
                    "sex": "male",
                }
            },
            None,
        ]
        fake_module = types.SimpleNamespace(iiko_client=fake_client)

        with patch.dict(sys.modules, {"guests.services.iiko_client": fake_module}):
            created = webhooks.get_or_create_guest_from_iiko("+79991112233")
            not_found = webhooks.get_or_create_guest_from_iiko("+79994445566")

        self.assertIsNotNone(created)
        self.assertEqual(created.iiko_id, "iiko-new-1")
        self.assertIsNone(not_found)

    def test_get_or_create_guest_from_iiko_updates_existing_empty_fields(self):
        """
        Для существующего гостя функция должна дозаполнять пустые поля.
        """
        guest = Guest.objects.create(
            iiko_id="iiko-update-1",
            phone=None,
            first_name="",
            last_name="",
            email="",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        fake_client = Mock()
        fake_client.get_customer_by_phone.return_value = {
            "customer": {
                "id": "iiko-update-1",
                "phone": "+70000000000",
                "name": "Имя",
                "surname": "Фамилия",
                "email": "upd@example.com",
                "sex": "female",
            }
        }
        fake_module = types.SimpleNamespace(iiko_client=fake_client)

        with patch.dict(sys.modules, {"guests.services.iiko_client": fake_module}):
            updated_guest = webhooks.get_or_create_guest_from_iiko("+70000000000")

        updated_guest.refresh_from_db()
        self.assertEqual(updated_guest.id, guest.id)
        self.assertEqual(updated_guest.phone, "+70000000000")
        self.assertEqual(updated_guest.first_name, "Имя")
        self.assertEqual(updated_guest.last_name, "Фамилия")
        self.assertEqual(updated_guest.email, "upd@example.com")

    def test_apply_category_from_api_webhook_branches_and_success(self):
        """
        Проверяем типовые ветки category-webhook: skip/error/success.
        """
        assigned, reason = webhooks.apply_category_from_api_webhook(
            {"id": "wh-1", "parsed_body": {"notificationType": 1, "phone": self.guest.phone}}
        )
        self.assertFalse(assigned)
        self.assertIn("ожидаем 5", reason)

        assigned, reason = webhooks.apply_category_from_api_webhook(
            {"id": "wh-2", "parsed_body": {"notificationType": 5, "phone": self.guest.phone}}
        )
        self.assertFalse(assigned)
        self.assertIn("нет category_id_ext", reason)

        assigned, reason = webhooks.apply_category_from_api_webhook(
            {
                "id": "wh-3",
                "category_id_ext": "cat-1",
                "parsed_body": {
                    "notificationType": 5,
                    "phone": self.guest.phone,
                    "terminalGroupId": self.restaurant.iiko_id,
                    "changedOn": "bad-datetime",
                },
            }
        )
        self.assertTrue(assigned, msg=reason)
        self.assertEqual(GuestCategoryAssignment.objects.count(), 1)
        self.assertEqual(GuestCategory.objects.count(), 1)

    def test_update_visit_history_from_event_create_and_update_paths(self):
        """
        Проверяем создание визита, инкремент счётчика и обновление даты.
        """
        event_create = {
            "notificationType": 1,
            "phone": self.guest.phone,
            "terminalGroupId": self.restaurant.iiko_id,
            "changedOn": "2026-03-18T10:00:00+05:00",
        }
        ok, reason = webhooks.update_visit_history_from_event(event_create)
        self.assertTrue(ok, msg=reason)
        self.assertEqual(VisitHistory.objects.count(), 1)

        event_old = {
            "notificationType": 1,
            "phone": self.guest.phone,
            "terminalGroupId": self.restaurant.iiko_id,
            "changedOn": "2026-03-18T09:00:00+05:00",
        }
        ok, reason = webhooks.update_visit_history_from_event(event_old)
        self.assertTrue(ok, msg=reason)

        event_new = {
            "notificationType": 1,
            "phone": self.guest.phone,
            "terminalGroupId": self.restaurant.iiko_id,
            "changedOn": "2026-03-18T12:00:00+05:00",
        }
        ok, reason = webhooks.update_visit_history_from_event(event_new)
        self.assertTrue(ok, msg=reason)

        visit = VisitHistory.objects.get()
        self.assertEqual(visit.visit_count, 3)
        expected_ts = datetime.fromisoformat("2026-03-18T12:00:00+05:00").timestamp()
        self.assertEqual(visit.visit_date.timestamp(), expected_ts)

    def test_update_visit_history_from_event_error_branches(self):
        """
        Ветка ошибок: отсутствует restaurant id, ресторан не найден, гость не найден.
        """
        ok, reason = webhooks.update_visit_history_from_event({"notificationType": 1, "phone": self.guest.phone})
        self.assertFalse(ok)
        self.assertIn("Не указан идентификатор ресторана", reason)

        ok, reason = webhooks.update_visit_history_from_event(
            {"notificationType": 1, "phone": self.guest.phone, "terminalGroupId": "rest-missing"}
        )
        self.assertFalse(ok)
        self.assertIn("не найден в БД", reason)

        ok, reason = webhooks.update_visit_history_from_event(
            {"notificationType": 1, "phone": "+79990000001", "terminalGroupId": self.restaurant.iiko_id}
        )
        self.assertFalse(ok)
        self.assertIn("Гость не найден", reason)

    def test_enqueue_balance_notification_from_webhook_delegates(self):
        """
        Wrapper для balance enqueue должен просто делегировать вызов в профильный сервис.
        """
        webhook = {"id": "wh-balance-1", "parsed_body": {"phone": self.guest.phone}}
        with patch(
            "guests.services.balance_notifications.enqueue_balance_notification_from_webhook",
            return_value=3,
        ) as mocked:
            result = webhooks.enqueue_balance_notification_from_webhook(
                webhook,
                is_enabled=True,
                priority="high",
                primary_only=True,
            )
        self.assertEqual(result, 3)
        mocked.assert_called_once()

    def test_handle_api_webhook_unknown_notification_type(self):
        """
        Неизвестный notificationType должен возвращать `False` и причину.
        """
        assigned, reason = webhooks.handle_api_webhook(
            {"id": "wh-unknown", "parsed_body": {"notificationType": 999}}
        )
        self.assertFalse(assigned)
        self.assertIn("Неизвестный notificationType", reason)


class WebhookProcessRecentTests(SimpleTestCase):
    """
    Тесты оркестратора process_recent_webhooks.
    """

    def test_process_recent_webhooks_returns_zero_on_token_error(self):
        """
        Если токен SAGUR получить нельзя, обработчик должен вернуть 0.
        """
        with patch("guests.services.webhooks._get_sagur_access_token_cached", side_effect=RuntimeError("token err")):
            processed = webhooks.process_recent_webhooks()
        self.assertEqual(processed, 0)

    def test_process_recent_webhooks_updates_statuses_for_success_fail_and_exception(self):
        """
        Оркестратор должен выставлять complete/failed и переживать исключения в handle_api_webhook.
        """
        pending = [{"id": 1}, {"id": 2}, {"id": 3}]

        with (
            patch("guests.services.webhooks._get_sagur_access_token_cached", return_value="token"),
            patch("guests.services.webhooks._iter_pending_webhooks", return_value=pending),
            patch(
                "guests.services.webhooks.handle_api_webhook",
                side_effect=[(True, "ok"), (False, "bad"), RuntimeError("boom")],
            ),
            patch("guests.services.webhooks._update_webhook_business_status") as mocked_update,
        ):
            processed = webhooks.process_recent_webhooks()

        self.assertEqual(processed, 1)
        calls = mocked_update.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].args[2], "complete")
        self.assertEqual(calls[1].args[2], "failed")
        self.assertEqual(calls[2].args[2], "failed")

    def test_process_recent_webhooks_respects_limit(self):
        """
        Обработчик не должен проходить больше LIMIT за один запуск.
        """
        pending = [{"id": 10}, {"id": 11}]

        with (
            patch.object(webhooks, "LIMIT", 1),
            patch("guests.services.webhooks._get_sagur_access_token_cached", return_value="token"),
            patch("guests.services.webhooks._iter_pending_webhooks", return_value=pending),
            patch("guests.services.webhooks.handle_api_webhook", return_value=(True, "ok")) as mocked_handle,
            patch("guests.services.webhooks._update_webhook_business_status"),
        ):
            processed = webhooks.process_recent_webhooks()

        self.assertEqual(processed, 1)
        mocked_handle.assert_called_once()
