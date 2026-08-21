"""Положительные и отрицательные тесты пакетного приёма нажатий vtelemax."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    InteractionButtonSet,
    MessageInteraction,
    MessageInteractionEvent,
)
from guests.services.message_interaction_inbound import (
    MESSAGE_INTERACTION_CALLBACK_PATH,
    build_vtelemax_message_interaction_signature,
)


TEST_SECRET = "message-interaction-test-secret"


class MessageInteractionSignatureFixtureTests(SimpleTestCase):
    """Общая байтовая фикстура HMAC для сверки с командой vtelemax."""

    def test_fixed_hmac_fixture(self):
        signature = build_vtelemax_message_interaction_signature(
            secret="fixture-secret",
            method="POST",
            path=MESSAGE_INTERACTION_CALLBACK_PATH,
            timestamp="1787200000",
            body=b'{"fixture":true}',
        )

        self.assertEqual(
            signature,
            "5606137647dfe4d99585d6773f70c5194bfc5f2bdfee45e509a9a483b872fe07",
        )


@override_settings(
    VTELEMAX_MESSAGE_INTERACTION_CALLBACK_ENABLED=True,
    VTELEMAX_MESSAGE_INTERACTION_CALLBACK_HMAC_SECRET=TEST_SECRET,
    VTELEMAX_MESSAGE_INTERACTION_CALLBACK_REQUIRE_HTTPS=True,
    VTELEMAX_MESSAGE_INTERACTION_CALLBACK_TIMESTAMP_TOLERANCE_SECONDS=300,
    VTELEMAX_MESSAGE_INTERACTION_CALLBACK_MAX_BODY_BYTES=65536,
    VTELEMAX_MESSAGE_INTERACTION_CALLBACK_RATE_LIMIT_PER_MINUTE=1000,
    SECURE_SSL_REDIRECT=False,
)
class MessageInteractionInboundTests(TestCase):
    """Проверяет полный HTTP-контракт и сохранение событий."""

    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.menu_interaction = self._create_interaction(
            button_set=InteractionButtonSet.RATING_MENU,
            suffix="menu",
        )
        self.coupon_interaction = self._create_interaction(
            button_set=InteractionButtonSet.RATING_COUPONS,
            suffix="coupon",
        )

    def tearDown(self):
        cache.clear()

    @staticmethod
    def _create_interaction(*, button_set: str, suffix: str) -> MessageInteraction:
        task = DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            priority=DispatchTask.Priority.NORMAL,
            status=DispatchTask.Status.DONE,
            external_chat_id=f"chat-{suffix}",
            message_text="Сообщение с кнопками",
            payload={},
        )
        return MessageInteraction.objects.create(
            dispatch_task=task,
            button_set=button_set,
        )

    def _time_text(self, value=None) -> str:
        current = value or (self.now - timedelta(minutes=1))
        return current.isoformat().replace("+00:00", "Z")

    def _item(
        self,
        *,
        interaction_id: int | None = None,
        action: str = "l",
        event_id: str | None = None,
        occurred_at: str | None = None,
        provider_message_id: str | None = "provider-message",
    ) -> dict[str, object]:
        item: dict[str, object] = {
            "event_id": event_id or str(uuid.uuid4()),
            "interaction_id": interaction_id or self.menu_interaction.id,
            "action": action,
            "occurred_at": occurred_at or self._time_text(),
        }
        if provider_message_id is not None:
            item["provider_message_id"] = provider_message_id
        return item

    def _payload(
        self,
        items: list[object],
        *,
        request_id: str | None = None,
        schema_version: object = 1,
    ) -> dict[str, object]:
        return {
            "request_id": request_id or str(uuid.uuid4()),
            "schema_version": schema_version,
            "sent_at": self._time_text(self.now),
            "items": items,
        }

    def _post_payload(
        self,
        payload: dict[str, object],
        *,
        header_request_id: str | None = None,
        timestamp: str | None = None,
        signature: str | None = None,
        content_type: str = "application/json; charset=utf-8",
        secure: bool = True,
        secret: str = TEST_SECRET,
    ):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._post_raw(
            body,
            header_request_id=header_request_id or str(payload.get("request_id") or ""),
            timestamp=timestamp,
            signature=signature,
            content_type=content_type,
            secure=secure,
            secret=secret,
        )

    def _post_raw(
        self,
        body: bytes,
        *,
        header_request_id: str,
        timestamp: str | None = None,
        signature: str | None = None,
        content_type: str = "application/json",
        secure: bool = True,
        secret: str = TEST_SECRET,
        include_signature_headers: bool = True,
    ):
        timestamp_value = timestamp or str(int(timezone.now().timestamp()))
        signature_value = signature or build_vtelemax_message_interaction_signature(
            secret=secret,
            method="POST",
            path=MESSAGE_INTERACTION_CALLBACK_PATH,
            timestamp=timestamp_value,
            body=body,
        )
        headers = {
            "HTTP_X_VTELEMAX_REQUEST_ID": header_request_id,
        }
        if include_signature_headers:
            headers.update(
                {
                    "HTTP_X_VTELEMAX_TIMESTAMP": timestamp_value,
                    "HTTP_X_VTELEMAX_SIGNATURE": signature_value,
                }
            )
        return self.client.post(
            MESSAGE_INTERACTION_CALLBACK_PATH,
            data=body,
            content_type=content_type,
            secure=secure,
            **headers,
        )

    def test_accepts_valid_mixed_batch_and_returns_item_results(self):
        like_event_id = str(uuid.uuid4())
        menu_event_id = str(uuid.uuid4())
        payload = self._payload(
            [
                self._item(event_id=like_event_id, action="l"),
                self._item(event_id=menu_event_id, action="m"),
            ]
        )

        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertTrue(response_data["ok"])
        self.assertEqual(response_data["status"], "accepted")
        self.assertEqual(
            response_data["results"],
            [
                {
                    "index": 0,
                    "event_id": like_event_id,
                    "status": "accepted",
                    "result": "accepted",
                },
                {
                    "index": 1,
                    "event_id": menu_event_id,
                    "status": "accepted",
                    "result": "accepted",
                },
            ],
        )
        self.assertEqual(MessageInteractionEvent.objects.count(), 2)

    def test_partial_batch_does_not_roll_back_valid_item(self):
        accepted_event_id = str(uuid.uuid4())
        missing_event_id = str(uuid.uuid4())
        disallowed_event_id = str(uuid.uuid4())
        unsupported_event_id = str(uuid.uuid4())
        payload = self._payload(
            [
                self._item(event_id=accepted_event_id, action="l"),
                self._item(
                    event_id=missing_event_id,
                    interaction_id=MAX_SIGNED_TEST_ID,
                    action="m",
                ),
                self._item(event_id=disallowed_event_id, action="c"),
                self._item(event_id=unsupported_event_id, action="x"),
            ]
        )

        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "partial")
        self.assertEqual(
            [item["result"] for item in response.json()["results"]],
            [
                "accepted",
                "interaction_not_found",
                "action_not_allowed_for_button_set",
                "action_unsupported",
            ],
        )
        self.assertTrue(MessageInteractionEvent.objects.filter(event_id=accepted_event_id).exists())
        self.assertEqual(MessageInteractionEvent.objects.count(), 1)

    def test_only_first_rating_is_accepted_but_second_is_stored(self):
        first_event_id = str(uuid.uuid4())
        second_event_id = str(uuid.uuid4())
        payload = self._payload(
            [
                self._item(event_id=first_event_id, action="l"),
                self._item(event_id=second_event_id, action="d"),
            ]
        )

        response = self._post_payload(payload)

        self.assertEqual(
            [item["result"] for item in response.json()["results"]],
            ["accepted", "rating_already_recorded"],
        )
        first = MessageInteractionEvent.objects.get(event_id=first_event_id)
        second = MessageInteractionEvent.objects.get(event_id=second_event_id)
        self.assertEqual(first.result, MessageInteractionEvent.Result.ACCEPTED)
        self.assertEqual(second.result, MessageInteractionEvent.Result.RATING_ALREADY_RECORDED)

    def test_repeated_navigation_with_new_event_ids_is_stored_each_time(self):
        first_event_id = str(uuid.uuid4())
        second_event_id = str(uuid.uuid4())
        payload = self._payload(
            [
                self._item(event_id=first_event_id, action="m"),
                self._item(event_id=second_event_id, action="m"),
            ]
        )

        response = self._post_payload(payload)

        self.assertEqual(
            [item["result"] for item in response.json()["results"]],
            ["accepted", "accepted"],
        )
        self.assertEqual(
            MessageInteractionEvent.objects.filter(
                interaction=self.menu_interaction,
                action=MessageInteractionEvent.Action.MENU,
            ).count(),
            2,
        )

    def test_identical_event_is_duplicate_across_different_batch_metadata(self):
        event_id = str(uuid.uuid4())
        occurred_at = self._time_text()
        first_payload = self._payload(
            [
                self._item(
                    event_id=event_id,
                    occurred_at=occurred_at,
                    provider_message_id="  provider-message  ",
                )
            ]
        )
        second_payload = self._payload(
            [
                self._item(
                    event_id=event_id,
                    occurred_at=occurred_at.replace("Z", "+00:00"),
                    provider_message_id="provider-message",
                )
            ],
            request_id=str(uuid.uuid4()),
        )

        first_response = self._post_payload(first_payload)
        second_response = self._post_payload(second_payload)

        self.assertEqual(first_response.json()["results"][0]["result"], "accepted")
        self.assertEqual(second_response.json()["results"][0]["result"], "duplicate")
        self.assertEqual(MessageInteractionEvent.objects.filter(event_id=event_id).count(), 1)

    def test_duplicate_event_lookup_does_not_join_interaction_table(self):
        """Для сравнения повтора достаточно сохранённого ``interaction_id`` события."""

        event_id = str(uuid.uuid4())
        payload = self._payload([self._item(event_id=event_id)])
        first_response = self._post_payload(payload)
        self.assertEqual(first_response.status_code, 200)

        with CaptureQueriesContext(connection) as captured:
            duplicate_response = self._post_payload(
                self._payload(
                    [self._item(event_id=event_id)],
                    request_id=str(uuid.uuid4()),
                )
            )

        event_queries = [
            query["sql"]
            for query in captured.captured_queries
            if "message_interaction_events" in query["sql"]
        ]
        self.assertEqual(duplicate_response.json()["results"][0]["result"], "duplicate")
        self.assertEqual(len(event_queries), 1)
        self.assertNotIn("JOIN", event_queries[0].upper())

    def test_same_event_id_with_other_significant_content_is_conflict(self):
        variations = (
            {"interaction_id": self.coupon_interaction.id},
            {"action": "d"},
            {"occurred_at": self._time_text(self.now - timedelta(minutes=2))},
            {"provider_message_id": "other-provider-message"},
        )

        for variation in variations:
            with self.subTest(variation=variation):
                event_id = str(uuid.uuid4())
                original_item = self._item(event_id=event_id, action="l")
                conflict_item = dict(original_item)
                conflict_item.update(variation)

                self._post_payload(self._payload([original_item]))
                conflict_response = self._post_payload(
                    self._payload([conflict_item], request_id=str(uuid.uuid4()))
                )

                result = conflict_response.json()["results"][0]
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["result"], "event_id_conflict")
                self.assertEqual(
                    MessageInteractionEvent.objects.filter(event_id=event_id).count(),
                    1,
                )

    def test_old_occurrence_has_no_expiry(self):
        event_id = str(uuid.uuid4())
        old_time = self._time_text(self.now - timedelta(days=365))

        response = self._post_payload(
            self._payload(
                [self._item(event_id=event_id, action="m", occurred_at=old_time)]
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["result"], "accepted")
        self.assertTrue(MessageInteractionEvent.objects.filter(event_id=event_id).exists())

    def test_accepts_maximum_batch_of_100_navigation_events(self):
        payload = self._payload(
            [self._item(action="m", event_id=str(uuid.uuid4())) for _ in range(100)]
        )

        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "accepted")
        self.assertEqual(len(response.json()["results"]), 100)
        self.assertEqual(MessageInteractionEvent.objects.count(), 100)

    def test_rejects_invalid_items_independently(self):
        future_time = self._time_text(self.now + timedelta(minutes=6))
        invalid_items = [
            {"event_id": str(uuid.uuid4())},
            self._item(event_id="not-a-uuid"),
            {
                **self._item(event_id=str(uuid.uuid4())),
                "interaction_id": True,
            },
            self._item(event_id=str(uuid.uuid4()), occurred_at="2026-08-20T10:00:00"),
            self._item(event_id=str(uuid.uuid4()), occurred_at=future_time),
            {
                **self._item(event_id=str(uuid.uuid4())),
                "provider_message_id": None,
            },
            self._item(
                event_id=str(uuid.uuid4()),
                provider_message_id="x" * 256,
            ),
            {
                **self._item(event_id=str(uuid.uuid4())),
                "unexpected": True,
            },
        ]

        response = self._post_payload(self._payload(invalid_items))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(
            [item["result"] for item in response.json()["results"]],
            [
                "invalid_item",
                "invalid_item",
                "invalid_item",
                "invalid_item",
                "invalid_item",
                "invalid_item",
                "invalid_item",
                "invalid_item",
            ],
        )
        self.assertEqual(MessageInteractionEvent.objects.count(), 0)

    def test_rejects_method_disabled_callback_and_insecure_request(self):
        get_response = self.client.get(MESSAGE_INTERACTION_CALLBACK_PATH, secure=True)
        self.assertEqual(get_response.status_code, 405)
        self.assertEqual(get_response["Allow"], "POST")

        payload = self._payload([self._item()])
        with override_settings(VTELEMAX_MESSAGE_INTERACTION_CALLBACK_ENABLED=False):
            disabled_response = self._post_payload(payload)
        self.assertEqual(disabled_response.status_code, 503)
        self.assertEqual(disabled_response.json()["code"], "callback_disabled")

        insecure_response = self._post_payload(payload, secure=False)
        self.assertEqual(insecure_response.status_code, 403)
        self.assertEqual(insecure_response.json()["code"], "https_required")

    def test_rejects_content_type_large_body_and_invalid_json(self):
        payload = self._payload([self._item()])
        content_type_response = self._post_payload(payload, content_type="text/plain")
        self.assertEqual(content_type_response.status_code, 415)
        self.assertEqual(content_type_response.json()["code"], "unsupported_content_type")

        with override_settings(VTELEMAX_MESSAGE_INTERACTION_CALLBACK_MAX_BODY_BYTES=1024):
            large_body_response = self._post_raw(
                b"{" + (b"x" * 1100) + b"}",
                header_request_id=str(payload["request_id"]),
            )
        self.assertEqual(large_body_response.status_code, 413)
        self.assertEqual(large_body_response.json()["code"], "body_too_large")

        invalid_json_response = self._post_raw(
            b"{invalid-json",
            header_request_id=str(payload["request_id"]),
        )
        self.assertEqual(invalid_json_response.status_code, 400)
        self.assertEqual(invalid_json_response.json()["code"], "json_invalid")

    def test_rejects_missing_invalid_and_expired_signature(self):
        payload = self._payload([self._item()])
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        missing_response = self._post_raw(
            body,
            header_request_id=str(payload["request_id"]),
            include_signature_headers=False,
        )
        self.assertEqual(missing_response.status_code, 401)
        self.assertEqual(missing_response.json()["code"], "signature_headers_missing")

        invalid_response = self._post_raw(
            body,
            header_request_id=str(payload["request_id"]),
            signature="0" * 64,
        )
        self.assertEqual(invalid_response.status_code, 401)
        self.assertEqual(invalid_response.json()["code"], "signature_invalid")

        valid_timestamp = str(int(timezone.now().timestamp()))
        valid_signature = build_vtelemax_message_interaction_signature(
            secret=TEST_SECRET,
            method="POST",
            path=MESSAGE_INTERACTION_CALLBACK_PATH,
            timestamp=valid_timestamp,
            body=body,
        )
        uppercase_response = self._post_raw(
            body,
            header_request_id=str(payload["request_id"]),
            timestamp=valid_timestamp,
            signature=valid_signature.upper(),
        )
        self.assertEqual(uppercase_response.status_code, 401)
        self.assertEqual(uppercase_response.json()["code"], "signature_invalid")

        old_timestamp = str(int((timezone.now() - timedelta(minutes=6)).timestamp()))
        expired_response = self._post_raw(
            body,
            header_request_id=str(payload["request_id"]),
            timestamp=old_timestamp,
        )
        self.assertEqual(expired_response.status_code, 401)
        self.assertEqual(expired_response.json()["code"], "timestamp_out_of_window")

        invalid_timestamp_response = self._post_raw(
            body,
            header_request_id=str(payload["request_id"]),
            timestamp="not-a-timestamp",
        )
        self.assertEqual(invalid_timestamp_response.status_code, 401)
        self.assertEqual(invalid_timestamp_response.json()["code"], "timestamp_invalid")

    @override_settings(
        VTELEMAX_MESSAGE_INTERACTION_CALLBACK_HMAC_SECRET="",
        VTELEMAX_SYNC_HMAC_SECRET="",
    )
    def test_rejects_request_when_both_secrets_are_empty(self):
        payload = self._payload([self._item(action="m")])

        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "callback_secret_missing")

    @override_settings(
        VTELEMAX_MESSAGE_INTERACTION_CALLBACK_HMAC_SECRET="",
        VTELEMAX_SYNC_HMAC_SECRET="fallback-secret",
    )
    def test_uses_common_secret_when_specialized_secret_is_empty(self):
        payload = self._payload([self._item(action="m")])

        response = self._post_payload(payload, secret="fallback-secret")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["result"], "accepted")

    def test_rejects_invalid_envelope_mismatch_version_and_item_count(self):
        base_payload = self._payload([self._item()])

        unknown_field_payload = dict(base_payload)
        unknown_field_payload["unexpected"] = True
        unknown_response = self._post_payload(unknown_field_payload)
        self.assertEqual(unknown_response.status_code, 400)
        self.assertEqual(unknown_response.json()["code"], "request_invalid")

        mismatch_response = self._post_payload(
            base_payload,
            header_request_id=str(uuid.uuid4()),
        )
        self.assertEqual(mismatch_response.status_code, 400)
        self.assertEqual(mismatch_response.json()["code"], "request_id_mismatch")

        version_response = self._post_payload(
            self._payload([self._item()], schema_version=2)
        )
        self.assertEqual(version_response.status_code, 409)
        self.assertEqual(version_response.json()["code"], "schema_version_unsupported")

        boolean_version_response = self._post_payload(
            self._payload([self._item()], schema_version=True)
        )
        self.assertEqual(boolean_version_response.status_code, 400)
        self.assertEqual(boolean_version_response.json()["code"], "request_invalid")

        empty_response = self._post_payload(self._payload([]))
        self.assertEqual(empty_response.status_code, 400)
        self.assertEqual(empty_response.json()["code"], "request_invalid")

        too_many_response = self._post_payload(
            self._payload([self._item(action="m") for _ in range(101)])
        )
        self.assertEqual(too_many_response.status_code, 400)
        self.assertEqual(too_many_response.json()["code"], "request_invalid")

    @override_settings(VTELEMAX_MESSAGE_INTERACTION_CALLBACK_RATE_LIMIT_PER_MINUTE=1)
    def test_rate_limit_returns_retry_after(self):
        cache.clear()
        first_response = self._post_payload(
            self._payload([self._item(action="m")])
        )
        second_response = self._post_payload(
            self._payload([self._item(action="m")])
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 429)
        self.assertEqual(second_response.json()["code"], "rate_limited")
        self.assertGreaterEqual(int(second_response["Retry-After"]), 1)

    def test_unexpected_error_returns_safe_response_without_request_body(self):
        marker = "SECRET-BODY-MARKER"
        payload = self._payload(
            [self._item(provider_message_id=marker)]
        )

        with (
            patch(
                "guests.views_message_interactions.receive_vtelemax_message_interaction_events",
                side_effect=RuntimeError("forced"),
            ),
            self.assertLogs("guests.views_message_interactions", level="ERROR") as captured,
        ):
            response = self._post_payload(payload)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "internal_error")
        self.assertNotIn(marker, "\n".join(captured.output))


MAX_SIGNED_TEST_ID = 9_223_372_036_854_775_807
