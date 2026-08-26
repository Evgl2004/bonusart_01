from __future__ import annotations

from datetime import timedelta

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from guests.models import (
    CouponCampaignAssignment,
    CouponRegistryEntry,
    Guest,
    IikoCustomerCategorySyncEvent,
    Mailing,
    MessageTemplate,
)
from guests.services.iiko_customer_category_sync import (
    IikoCustomerCategorySyncService,
    enqueue_iiko_category_add_for_assignment,
    enqueue_iiko_category_remove_if_last_coupon,
)
from guests.services.iiko_customer_category_client import (
    IikoCustomerCategoryApiError,
    IikoCustomerCategoryClient,
)


class _FakeIikoHttpResponse:
    """
    Минимальный HTTP-ответ для проверки клиента iikoCard без сети.
    """

    def __init__(self, *, status_code: int = 200, text: str = "", json_body=None):
        self.status_code = status_code
        self.text = text
        self._json_body = json_body

    def json(self):
        return self._json_body


class _FakeIikoHttpSession:
    """
    Тестовая requests.Session с заранее заданной очередью ответов.
    """

    def __init__(self, responses: list[_FakeIikoHttpResponse]):
        self.responses = list(responses)
        self.posts: list[dict[str, object]] = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def close(self):
        return None


class _FakeIikoTokenProvider:
    """Поставщик фиксированного токена без сетевой авторизации."""

    def get_token(self):
        return "token-1"

    def invalidate_token(self, *, expected_token=None):
        return None

    def close(self):
        return None


class IikoCustomerCategoryClientTests(SimpleTestCase):
    """
    Проверки HTTP-клиента iikoCard для категорий гостей.
    """

    def test_successful_empty_response_is_treated_as_ack(self):
        """
        Успешный пустой ответ iikoCard не должен превращаться в ложный retry.
        """
        session = _FakeIikoHttpSession(
            [
                _FakeIikoHttpResponse(text="", json_body=None),
            ]
        )
        client = IikoCustomerCategoryClient(
            base_url="https://iiko.example.test",
            organization_id="org-1",
            token_provider=_FakeIikoTokenProvider(),
            max_retries=0,
        )
        client._session = session
        client._transport._session = session

        result = client.add_customer_category(
            customer_id="customer-1",
            category_id="cat-active-coupon",
        )

        self.assertEqual(result, {})
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(
            session.posts[0]["url"],
            "https://iiko.example.test/api/1/loyalty/iiko/customer_category/add",
        )

    def test_mutating_error_is_structured_without_response_body_in_text(self):
        private_message = "private-response-body"
        session = _FakeIikoHttpSession(
            [
                _FakeIikoHttpResponse(
                    status_code=400,
                    text="json",
                    json_body={
                        "errorCode": "BAD_CATEGORY",
                        "correlationId": "corr-1",
                        "message": private_message,
                    },
                )
            ]
        )
        client = IikoCustomerCategoryClient(
            base_url="https://iiko.example.test/api/1",
            organization_id="org-1",
            token_provider=_FakeIikoTokenProvider(),
            max_retries=2,
        )
        client._session = session
        client._transport._session = session

        with self.assertRaises(IikoCustomerCategoryApiError) as error_context:
            client.add_customer_category(customer_id="customer-1", category_id="category-1")

        error = error_context.exception
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(error.error_code, "BAD_CATEGORY")
        self.assertEqual(error.correlation_id, "corr-1")
        self.assertEqual(error.body["message"], private_message)
        self.assertNotIn(private_message, str(error))

    @override_settings(
        IIKO_AUTH_MODE="v2",
        IIKO_APP_ID="app-test",
        IIKO_CLIENT_SECRET="secret-test",
        IIKO_API_KEY="key-test",
        IIKO_API_BASE_URL="https://iiko.example/api/1",
        IIKO_ORGANIZATION_ID="org-1",
        IIKO_ACTIVE_COUPON_CATEGORY_ID="category-1",
    )
    def test_service_from_settings_builds_selected_mode_locally(self):
        service = IikoCustomerCategorySyncService.from_settings()

        self.assertEqual(service.client._token_provider.mode, "v2")
        self.assertEqual(service.client.base_url, "https://iiko.example/api/1")
        service.client.close()

    @override_settings(
        IIKO_AUTH_MODE="",
        IIKO_API_BASE_URL="https://iiko.example/api/1",
        IIKO_ORGANIZATION_ID="org-1",
        IIKO_ACTIVE_COUPON_CATEGORY_ID="category-1",
    )
    def test_service_from_settings_reports_mode_error_only_when_called(self):
        with self.assertRaisesRegex(ValueError, "IIKO_AUTH_MODE"):
            IikoCustomerCategorySyncService.from_settings()


class _FakeIikoCategoryClient:
    """
    Тестовый клиент iikoCard без сетевых запросов.
    """

    def __init__(
        self,
        *,
        add_error: Exception | None = None,
        remove_error: Exception | None = None,
        customer_by_phone_body: dict | None = None,
        organization_id: str = "org-1",
    ):
        self.add_calls: list[dict[str, str]] = []
        self.remove_calls: list[dict[str, str]] = []
        self.customer_by_phone_calls: list[str] = []
        self.add_error = add_error
        self.remove_error = remove_error
        self.customer_by_phone_body = customer_by_phone_body or {"id": "iiko-resolved-by-phone"}
        self.organization_id = organization_id

    def get_customer_by_phone(self, *, phone: str):
        self.customer_by_phone_calls.append(phone)
        return dict(self.customer_by_phone_body)

    def add_customer_category(self, *, customer_id: str, category_id: str):
        self.add_calls.append({"customer_id": customer_id, "category_id": category_id})
        if self.add_error is not None:
            raise self.add_error
        return {"ok": True}

    def remove_customer_category(self, *, customer_id: str, category_id: str):
        self.remove_calls.append({"customer_id": customer_id, "category_id": category_id})
        if self.remove_error is not None:
            raise self.remove_error
        return {"ok": True}


class IikoCustomerCategorySyncServiceTests(TestCase):
    """
    Проверки очереди синхронизации категорий гостей iikoCard.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Iiko category template",
            description="",
            message_text="Купон {coupon_code}",
            created_by="test",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Iiko category campaign",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(hours=1),
            scheduled_time_end=self.now + timedelta(hours=3),
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=(self.now - timedelta(hours=1)).time(),
            send_window_end=(self.now + timedelta(hours=1)).time(),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.NORMAL,
            coupon_series="TEST",
            coupon_venue_code="DEP_1",
            coupon_venue_name="Тестовое заведение",
            coupon_promo_text="Промо",
        )

    def _another_mailing(self, *, name: str) -> Mailing:
        return Mailing.objects.create(
            name=name,
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(hours=1),
            scheduled_time_end=self.now + timedelta(hours=3),
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=(self.now - timedelta(hours=1)).time(),
            send_window_end=(self.now + timedelta(hours=1)).time(),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.NORMAL,
            coupon_series="TEST",
            coupon_venue_code="DEP_1",
            coupon_venue_name="Тестовое заведение",
            coupon_promo_text="Промо",
        )

    def _assignment(
        self,
        *,
        phone: str = "+79990000111",
        iiko_id: str | None = "iiko-guest-1",
        code: str = "IIKO-1",
        status: str = CouponCampaignAssignment.Status.RESERVED,
    ) -> CouponCampaignAssignment:
        guest = Guest.objects.create(
            phone=phone,
            iiko_id=iiko_id,
            first_name="Иван",
            created_at=self.now,
            updated_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code=code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        return CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=guest,
            coupon=coupon,
            phone_e164=phone,
            coupon_series=coupon.series,
            coupon_code=coupon.code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Промо",
            assigned_at=self.now,
            lifetime_expires_at=self.now + timedelta(days=3),
            status=status,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )

    def _service(
        self,
        client: _FakeIikoCategoryClient,
        *,
        category_id: str = "cat-active-coupon",
    ) -> IikoCustomerCategorySyncService:
        return IikoCustomerCategorySyncService(
            client=client,
            category_id=category_id,
            max_attempts=8,
            retry_base_seconds=30,
            retry_max_seconds=3600,
            request_interval_seconds=0,
        )

    @staticmethod
    def _already_bound_error(*, category_id: str, customer_id: str, message: str | None = None):
        error_message = message or (
            f"Category with id={category_id} already binded to customer with id={customer_id}."
        )
        body = {
            "code": None,
            "errorCode": "Common_CategoryBindedToAnotherCustomer",
            "message": error_message,
            "description": "Category binded to another customer",
            "httpStatusCode": 400,
        }
        return IikoCustomerCategoryApiError(
            "iikoCard API `/loyalty/iiko/customer_category/add` вернул status=400",
            status_code=400,
            path="/loyalty/iiko/customer_category/add",
            body=body,
            error_code="Common_CategoryBindedToAnotherCustomer",
        )

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="cat-active-coupon",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_add_event_is_acked_and_marks_assignment_ok(self):
        assignment = self._assignment()
        enqueue_result = enqueue_iiko_category_add_for_assignment(
            assignment=assignment,
            now=self.now,
        )

        self.assertTrue(enqueue_result.created)
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.iiko_category_add_status,
            CouponCampaignAssignment.IikoCategorySyncStatus.PENDING,
        )

        client = _FakeIikoCategoryClient()
        stats = self._service(client).process_batch(limit=10, now=self.now + timedelta(seconds=1))

        self.assertEqual(stats.to_dict()["acked"], 1)
        self.assertEqual(client.add_calls, [{"customer_id": "iiko-guest-1", "category_id": "cat-active-coupon"}])
        event = IikoCustomerCategorySyncEvent.objects.get(id=enqueue_result.event.id)
        self.assertEqual(event.status, IikoCustomerCategorySyncEvent.Status.ACKED)
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.iiko_category_add_status,
            CouponCampaignAssignment.IikoCategorySyncStatus.OK,
        )
        self.assertIsNotNone(assignment.iiko_category_add_synced_at)

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="cat-active-coupon",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_add_event_resolves_customer_id_by_phone(self):
        assignment = self._assignment(iiko_id=None, phone="+79990000112", code="IIKO-2")
        enqueue_iiko_category_add_for_assignment(assignment=assignment, now=self.now)

        client = _FakeIikoCategoryClient()
        self._service(client).process_batch(limit=10, now=self.now + timedelta(seconds=1))

        self.assertEqual(client.customer_by_phone_calls, ["+79990000112"])
        self.assertEqual(
            client.add_calls,
            [{"customer_id": "iiko-resolved-by-phone", "category_id": "cat-active-coupon"}],
        )
        event = IikoCustomerCategorySyncEvent.objects.get()
        self.assertEqual(event.iiko_customer_id, "iiko-resolved-by-phone")

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="2037968c-39c9-4020-a795-07249cad50e8",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_add_already_bound_to_same_customer_is_idempotent_ack(self):
        category_id = "2037968c-39c9-4020-a795-07249cad50e8"
        customer_id = "b79638dd-f77b-11e8-80d5-d8d385655247"
        assignment = self._assignment(iiko_id=customer_id, code="IIKO-IDEMPOTENT")
        enqueue_result = enqueue_iiko_category_add_for_assignment(assignment=assignment, now=self.now)
        client = _FakeIikoCategoryClient(
            add_error=self._already_bound_error(category_id=category_id, customer_id=customer_id)
        )

        stats = self._service(client, category_id=category_id).process_batch(
            limit=10,
            now=self.now + timedelta(seconds=1),
        )

        self.assertEqual(stats.to_dict()["acked"], 1)
        self.assertEqual(stats.to_dict()["failed"], 0)
        self.assertEqual(stats.to_dict()["add_acked"], 1)
        event = IikoCustomerCategorySyncEvent.objects.get(id=enqueue_result.event.id)
        self.assertEqual(event.status, IikoCustomerCategorySyncEvent.Status.ACKED)
        self.assertEqual(event.iiko_customer_id, customer_id)
        self.assertIsNotNone(event.ack_at)
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.iiko_category_add_status,
            CouponCampaignAssignment.IikoCategorySyncStatus.OK,
        )
        self.assertIsNotNone(assignment.iiko_category_add_synced_at)

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="2037968c-39c9-4020-a795-07249cad50e8",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_add_already_bound_to_different_customer_stays_error(self):
        category_id = "2037968c-39c9-4020-a795-07249cad50e8"
        requested_customer_id = "b79638dd-f77b-11e8-80d5-d8d385655247"
        bound_customer_id = "08a228a8-8e44-11e8-80e1-d8d38565926f"
        assignment = self._assignment(iiko_id=requested_customer_id, code="IIKO-MISMATCH")
        enqueue_result = enqueue_iiko_category_add_for_assignment(assignment=assignment, now=self.now)
        client = _FakeIikoCategoryClient(
            add_error=self._already_bound_error(
                category_id=category_id,
                customer_id=bound_customer_id,
            )
        )

        stats = self._service(client, category_id=category_id).process_batch(
            limit=10,
            now=self.now + timedelta(seconds=1),
        )

        self.assertEqual(stats.to_dict()["acked"], 0)
        self.assertEqual(stats.to_dict()["failed"], 1)
        event = IikoCustomerCategorySyncEvent.objects.get(id=enqueue_result.event.id)
        self.assertEqual(event.status, IikoCustomerCategorySyncEvent.Status.ERROR)
        self.assertEqual(event.attempts, 1)
        self.assertIsNone(event.ack_at)
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.iiko_category_add_status,
            CouponCampaignAssignment.IikoCategorySyncStatus.ERROR,
        )

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="2037968c-39c9-4020-a795-07249cad50e8",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_add_already_bound_for_different_category_stays_error(self):
        category_id = "2037968c-39c9-4020-a795-07249cad50e8"
        other_category_id = "11111111-2222-3333-4444-555555555555"
        customer_id = "b79638dd-f77b-11e8-80d5-d8d385655247"
        assignment = self._assignment(iiko_id=customer_id, code="IIKO-CATEGORY-MISMATCH")
        enqueue_result = enqueue_iiko_category_add_for_assignment(assignment=assignment, now=self.now)
        client = _FakeIikoCategoryClient(
            add_error=self._already_bound_error(
                category_id=other_category_id,
                customer_id=customer_id,
            )
        )

        stats = self._service(client, category_id=category_id).process_batch(
            limit=10,
            now=self.now + timedelta(seconds=1),
        )

        self.assertEqual(stats.to_dict()["acked"], 0)
        self.assertEqual(stats.to_dict()["failed"], 1)
        event = IikoCustomerCategorySyncEvent.objects.get(id=enqueue_result.event.id)
        self.assertEqual(event.status, IikoCustomerCategorySyncEvent.Status.ERROR)
        self.assertEqual(event.attempts, 1)
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.iiko_category_add_status,
            CouponCampaignAssignment.IikoCategorySyncStatus.ERROR,
        )

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="2037968c-39c9-4020-a795-07249cad50e8",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_add_already_bound_with_malformed_message_stays_error(self):
        category_id = "2037968c-39c9-4020-a795-07249cad50e8"
        customer_id = "b79638dd-f77b-11e8-80d5-d8d385655247"
        assignment = self._assignment(iiko_id=customer_id, code="IIKO-MALFORMED")
        enqueue_result = enqueue_iiko_category_add_for_assignment(assignment=assignment, now=self.now)
        client = _FakeIikoCategoryClient(
            add_error=self._already_bound_error(
                category_id=category_id,
                customer_id=customer_id,
                message="Category binded to another customer without identifiers",
            )
        )

        stats = self._service(client, category_id=category_id).process_batch(
            limit=10,
            now=self.now + timedelta(seconds=1),
        )

        self.assertEqual(stats.to_dict()["acked"], 0)
        self.assertEqual(stats.to_dict()["failed"], 1)
        event = IikoCustomerCategorySyncEvent.objects.get(id=enqueue_result.event.id)
        self.assertEqual(event.status, IikoCustomerCategorySyncEvent.Status.ERROR)
        self.assertEqual(event.attempts, 1)

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="2037968c-39c9-4020-a795-07249cad50e8",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_phone_resolved_customer_is_persisted_and_used_for_idempotent_ack(self):
        category_id = "2037968c-39c9-4020-a795-07249cad50e8"
        resolved_customer_id = "b79638dd-f77b-11e8-80d5-d8d385655247"
        assignment = self._assignment(
            iiko_id=None,
            phone="+79990000113",
            code="IIKO-PHONE-IDEMPOTENT",
        )
        enqueue_result = enqueue_iiko_category_add_for_assignment(assignment=assignment, now=self.now)
        client = _FakeIikoCategoryClient(
            add_error=self._already_bound_error(
                category_id=category_id,
                customer_id=resolved_customer_id,
            ),
            customer_by_phone_body={"id": resolved_customer_id},
        )

        stats = self._service(client, category_id=category_id).process_batch(
            limit=10,
            now=self.now + timedelta(seconds=1),
        )

        self.assertEqual(stats.to_dict()["acked"], 1)
        self.assertEqual(client.customer_by_phone_calls, ["+79990000113"])
        event = IikoCustomerCategorySyncEvent.objects.get(id=enqueue_result.event.id)
        self.assertEqual(event.status, IikoCustomerCategorySyncEvent.Status.ACKED)
        self.assertEqual(event.iiko_customer_id, resolved_customer_id)

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="2037968c-39c9-4020-a795-07249cad50e8",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_unrelated_iiko_api_error_remains_retryable(self):
        category_id = "2037968c-39c9-4020-a795-07249cad50e8"
        customer_id = "b79638dd-f77b-11e8-80d5-d8d385655247"
        assignment = self._assignment(iiko_id=customer_id, code="IIKO-RETRYABLE")
        enqueue_result = enqueue_iiko_category_add_for_assignment(assignment=assignment, now=self.now)
        client = _FakeIikoCategoryClient(
            add_error=IikoCustomerCategoryApiError(
                "temporary iikoCard error",
                status_code=503,
                path="/loyalty/iiko/customer_category/add",
                body={"errorCode": "Common_ServiceUnavailable"},
                error_code="Common_ServiceUnavailable",
            )
        )

        stats = self._service(client, category_id=category_id).process_batch(
            limit=10,
            now=self.now + timedelta(seconds=1),
        )

        self.assertEqual(stats.to_dict()["acked"], 0)
        self.assertEqual(stats.to_dict()["failed"], 1)
        event = IikoCustomerCategorySyncEvent.objects.get(id=enqueue_result.event.id)
        self.assertEqual(event.status, IikoCustomerCategorySyncEvent.Status.ERROR)
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.next_retry_at, self.now + timedelta(seconds=31))

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="cat-active-coupon",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_remove_is_not_enqueued_when_guest_has_another_live_coupon(self):
        used_assignment = self._assignment(code="IIKO-USED", status=CouponCampaignAssignment.Status.USED)
        live_coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="IIKO-LIVE",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        CouponCampaignAssignment.objects.create(
            campaign=self._another_mailing(name="Iiko category second live campaign"),
            guest=used_assignment.guest,
            coupon=live_coupon,
            phone_e164=used_assignment.phone_e164,
            coupon_series=live_coupon.series,
            coupon_code=live_coupon.code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            assigned_at=self.now,
            lifetime_expires_at=self.now + timedelta(days=3),
            status=CouponCampaignAssignment.Status.RESERVED,
        )

        result = enqueue_iiko_category_remove_if_last_coupon(
            assignment=used_assignment,
            now=self.now,
        )

        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "guest_has_another_live_coupon")
        self.assertEqual(IikoCustomerCategorySyncEvent.objects.count(), 0)

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="cat-active-coupon",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_worker_skips_queued_remove_when_new_live_coupon_appeared(self):
        closed_assignment = self._assignment(code="IIKO-CLOSED", status=CouponCampaignAssignment.Status.USED)
        remove_result = enqueue_iiko_category_remove_if_last_coupon(
            assignment=closed_assignment,
            now=self.now,
        )
        self.assertTrue(remove_result.created)

        live_coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="IIKO-NEW-LIVE",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        CouponCampaignAssignment.objects.create(
            campaign=self._another_mailing(name="Iiko category new live campaign"),
            guest=closed_assignment.guest,
            coupon=live_coupon,
            phone_e164=closed_assignment.phone_e164,
            coupon_series=live_coupon.series,
            coupon_code=live_coupon.code,
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            assigned_at=self.now,
            lifetime_expires_at=self.now + timedelta(days=3),
            status=CouponCampaignAssignment.Status.RESERVED,
        )

        client = _FakeIikoCategoryClient()
        stats = self._service(client).process_batch(limit=10, now=self.now + timedelta(seconds=1))

        self.assertEqual(stats.to_dict()["skipped"], 1)
        self.assertEqual(client.remove_calls, [])
        event = IikoCustomerCategorySyncEvent.objects.get(id=remove_result.event.id)
        self.assertEqual(event.status, IikoCustomerCategorySyncEvent.Status.SKIPPED)

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="cat-active-coupon",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_worker_skips_remove_when_iiko_reports_customer_has_no_category(self):
        """
        Если iikoCard сообщает, что категории у гостя уже нет, remove не должен
        уходить в повторные ошибки: целевое состояние достигнуто, но причина
        сохраняется в очереди для диагностики.
        """
        closed_assignment = self._assignment(
            code="IIKO-NO-CATEGORY",
            status=CouponCampaignAssignment.Status.USED,
        )
        remove_result = enqueue_iiko_category_remove_if_last_coupon(
            assignment=closed_assignment,
            now=self.now,
        )
        self.assertTrue(remove_result.created)

        client = _FakeIikoCategoryClient(
            remove_error=IikoCustomerCategoryApiError(
                "iikoCard API `/loyalty/iiko/customer_category/remove` вернул status=400",
                status_code=400,
                path="/loyalty/iiko/customer_category/remove",
                body={"errorCode": "Customer_CustomerHasNoCategory"},
                error_code="Customer_CustomerHasNoCategory",
            )
        )
        stats = self._service(client).process_batch(limit=10, now=self.now + timedelta(seconds=1))

        summary = stats.to_dict()
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(
            client.remove_calls,
            [{"customer_id": "iiko-guest-1", "category_id": "cat-active-coupon"}],
        )
        event = IikoCustomerCategorySyncEvent.objects.get(id=remove_result.event.id)
        self.assertEqual(event.status, IikoCustomerCategorySyncEvent.Status.SKIPPED)
        self.assertIn("Customer_CustomerHasNoCategory", event.last_error)

    @override_settings(
        IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True,
        IIKO_ACTIVE_COUPON_CATEGORY_ID="cat-active-coupon",
        IIKO_ORGANIZATION_ID="org-1",
    )
    def test_worker_skips_add_for_closed_assignment(self):
        assignment = self._assignment(code="IIKO-CANCELED", status=CouponCampaignAssignment.Status.CANCELED)
        enqueue_result = enqueue_iiko_category_add_for_assignment(assignment=assignment, now=self.now)

        client = _FakeIikoCategoryClient()
        stats = self._service(client).process_batch(limit=10, now=self.now + timedelta(seconds=1))

        self.assertEqual(stats.to_dict()["skipped"], 1)
        self.assertEqual(client.add_calls, [])
        event = IikoCustomerCategorySyncEvent.objects.get(id=enqueue_result.event.id)
        self.assertEqual(event.status, IikoCustomerCategorySyncEvent.Status.SKIPPED)
