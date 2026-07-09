import json
import uuid

from django.test import TestCase, override_settings
from django.utils import timezone

from guests.models import GuestWelcomeRegistrationEvent
from guests.services.vtelemax_registration_callback import (
    VTELEMAX_REGISTRATION_CALLBACK_PATH,
    build_vtelemax_registration_signature,
)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    SECURE_SSL_REDIRECT=False,
    VTELEMAX_REGISTRATION_CALLBACK_ENABLED=True,
    VTELEMAX_REGISTRATION_CALLBACK_HMAC_SECRET="callback-secret",
    VTELEMAX_REGISTRATION_CALLBACK_REQUIRE_HTTPS=False,
    VTELEMAX_REGISTRATION_CALLBACK_TIMESTAMP_TOLERANCE_SECONDS=300,
    VTELEMAX_REGISTRATION_CALLBACK_MAX_BODY_BYTES=65536,
    WELCOME_COUPON_ACCEPT_REGISTRATIONS_FROM="",
)
class VtelemaxRegistrationCallbackViewTests(TestCase):
    def _payload(self, **overrides):
        payload = {
            "request_id": "req-welcome-1",
            "event_id": "evt-welcome-1",
            "event_type": "guest_registered",
            "person_id": str(uuid.uuid4()),
            "platform": "telegram",
            "phone_e164": "+79224800001",
            "customerId": "iiko-customer-1",
            "external_id": "tg-chat-1",
            "rules_accepted": True,
            "notifications_allowed": True,
            "is_registered": True,
            "registered_at": "2026-07-09T08:10:00Z",
            "state_updated_at": "2026-07-09T08:11:00Z",
            "account_created_at": "2026-07-09T08:00:00Z",
            "effective_updated_at": "2026-07-09T08:11:00Z",
            "profile": {
                "first_name": "Анна",
                "last_name": "Петрова",
            },
        }
        payload.update(overrides)
        return payload

    def _body(self, payload):
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _headers(self, *, body, request_id="req-welcome-1", timestamp=None, signature=None):
        timestamp = timestamp or str(int(timezone.now().timestamp()))
        signature = signature or build_vtelemax_registration_signature(
            secret="callback-secret",
            method="POST",
            path=VTELEMAX_REGISTRATION_CALLBACK_PATH,
            timestamp=timestamp,
            body=body,
        )
        return {
            "HTTP_X_VTELEMAX_TIMESTAMP": timestamp,
            "HTTP_X_VTELEMAX_SIGNATURE": signature,
            "HTTP_X_VTELEMAX_REQUEST_ID": request_id,
        }

    def _post_payload(self, payload, *, content_type="application/json", timestamp=None, signature=None):
        body = self._body(payload)
        return self.client.post(
            VTELEMAX_REGISTRATION_CALLBACK_PATH,
            data=body,
            content_type=content_type,
            **self._headers(
                body=body,
                request_id=str(payload.get("request_id", "")),
                timestamp=timestamp,
                signature=signature,
            ),
        )

    def test_callback_accepts_valid_registration_event(self):
        response = self._post_payload(self._payload())

        self.assertEqual(response.status_code, 202)
        response_json = response.json()
        self.assertTrue(response_json["ok"])
        self.assertFalse(response_json["duplicate"])
        self.assertEqual(response_json["event_id"], "evt-welcome-1")

        event = GuestWelcomeRegistrationEvent.objects.get()
        self.assertEqual(event.event_id, "evt-welcome-1")
        self.assertEqual(event.request_id, "req-welcome-1")
        self.assertEqual(event.platform, "telegram")
        self.assertEqual(event.phone_e164, "+79224800001")
        self.assertEqual(event.iiko_customer_id, "iiko-customer-1")
        self.assertEqual(event.external_id, "tg-chat-1")
        self.assertTrue(event.rules_accepted)
        self.assertTrue(event.notifications_allowed)
        self.assertTrue(event.is_registered)
        self.assertEqual(event.status, GuestWelcomeRegistrationEvent.Status.NEW)
        self.assertEqual(event.profile["first_name"], "Анна")
        self.assertEqual(event.payload_json["customerId"], "iiko-customer-1")
        self.assertEqual(len(event.payload_sha256), 64)

    def test_callback_returns_duplicate_for_repeated_same_event(self):
        payload = self._payload()

        first_response = self._post_payload(payload)
        second_response = self._post_payload(payload)

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertFalse(first_response.json()["duplicate"])
        self.assertTrue(second_response.json()["duplicate"])
        self.assertEqual(
            first_response.json()["welcome_event_id"],
            second_response.json()["welcome_event_id"],
        )
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 1)

    def test_callback_rejects_same_event_id_with_different_payload(self):
        self.assertEqual(self._post_payload(self._payload()).status_code, 202)

        response = self._post_payload(
            self._payload(
                phone_e164="+79224800002",
                external_id="tg-chat-2",
            )
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "event_id_payload_conflict")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 1)

    @override_settings(VTELEMAX_REGISTRATION_CALLBACK_ENABLED=False)
    def test_callback_rejects_when_feature_flag_disabled(self):
        response = self._post_payload(self._payload())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "callback_disabled")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)

    @override_settings(VTELEMAX_REGISTRATION_CALLBACK_REQUIRE_HTTPS=True)
    def test_callback_rejects_plain_http_when_https_required(self):
        response = self._post_payload(self._payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "https_required")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)

    def test_callback_rejects_invalid_signature(self):
        response = self._post_payload(self._payload(), signature="bad-signature")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "signature_invalid")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)

    def test_callback_rejects_timestamp_out_of_window(self):
        payload = self._payload()
        body = self._body(payload)
        timestamp = str(int(timezone.now().timestamp()) - 1000)
        signature = build_vtelemax_registration_signature(
            secret="callback-secret",
            method="POST",
            path=VTELEMAX_REGISTRATION_CALLBACK_PATH,
            timestamp=timestamp,
            body=body,
        )

        response = self.client.post(
            VTELEMAX_REGISTRATION_CALLBACK_PATH,
            data=body,
            content_type="application/json",
            **self._headers(body=body, timestamp=timestamp, signature=signature),
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "timestamp_out_of_window")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)

    def test_callback_rejects_request_id_mismatch(self):
        payload = self._payload(request_id="req-body")
        body = self._body(payload)

        response = self.client.post(
            VTELEMAX_REGISTRATION_CALLBACK_PATH,
            data=body,
            content_type="application/json",
            **self._headers(body=body, request_id="req-header"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "request_id_mismatch")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)

    def test_callback_rejects_payload_without_customer_id(self):
        payload = self._payload()
        payload.pop("customerId")

        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "customerId_missing")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)

    def test_callback_rejects_string_boolean(self):
        response = self._post_payload(self._payload(rules_accepted="true"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "rules_accepted_invalid")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)

    def test_callback_rejects_invalid_phone(self):
        response = self._post_payload(self._payload(phone_e164="9224800001"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "phone_e164_invalid")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)

    def test_callback_rejects_unsupported_content_type(self):
        response = self._post_payload(self._payload(), content_type="text/plain")

        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["code"], "unsupported_content_type")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)

    @override_settings(VTELEMAX_REGISTRATION_CALLBACK_MAX_BODY_BYTES=40)
    def test_callback_rejects_too_large_body(self):
        response = self._post_payload(self._payload())

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "body_too_large")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)

    def test_callback_rejects_get_method_with_json_response(self):
        response = self.client.get(VTELEMAX_REGISTRATION_CALLBACK_PATH)

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["code"], "method_not_allowed")
        self.assertEqual(response["Allow"], "POST")

    @override_settings(WELCOME_COUPON_ACCEPT_REGISTRATIONS_FROM="2026-07-09T00:00:00Z")
    def test_callback_marks_old_registration_as_skipped(self):
        response = self._post_payload(
            self._payload(
                registered_at="2026-07-08T23:59:59Z",
                state_updated_at="2026-07-08T23:59:59Z",
                effective_updated_at="2026-07-08T23:59:59Z",
            )
        )

        self.assertEqual(response.status_code, 202)
        event = GuestWelcomeRegistrationEvent.objects.get()
        self.assertEqual(event.status, GuestWelcomeRegistrationEvent.Status.SKIPPED)
        self.assertEqual(event.skip_reason, "registration_before_accept_from")
        self.assertIsNotNone(event.processed_at)

    @override_settings(WELCOME_COUPON_ACCEPT_REGISTRATIONS_FROM="некорректная дата")
    def test_callback_rejects_invalid_accept_from_setting(self):
        response = self._post_payload(self._payload())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "accept_registrations_from_invalid")
        self.assertEqual(GuestWelcomeRegistrationEvent.objects.count(), 0)
