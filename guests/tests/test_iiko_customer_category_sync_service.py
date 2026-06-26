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
                _FakeIikoHttpResponse(text='{"token":"token-1"}', json_body={"token": "token-1"}),
                _FakeIikoHttpResponse(text="", json_body=None),
            ]
        )
        client = IikoCustomerCategoryClient(
            api_key="api-key",
            base_url="https://iiko.example.test",
            organization_id="org-1",
        )
        client._session = session

        result = client.add_customer_category(
            customer_id="customer-1",
            category_id="cat-active-coupon",
        )

        self.assertEqual(result, {})
        self.assertEqual(len(session.posts), 2)
        self.assertEqual(
            session.posts[1]["url"],
            "https://iiko.example.test/api/1/loyalty/iiko/customer_category/add",
        )


class _FakeIikoCategoryClient:
    """
    Тестовый клиент iikoCard без сетевых запросов.
    """

    def __init__(self, *, remove_error: Exception | None = None):
        self.add_calls: list[dict[str, str]] = []
        self.remove_calls: list[dict[str, str]] = []
        self.customer_by_phone_calls: list[str] = []
        self.remove_error = remove_error

    def get_customer_by_phone(self, *, phone: str):
        self.customer_by_phone_calls.append(phone)
        return {"id": "iiko-resolved-by-phone"}

    def add_customer_category(self, *, customer_id: str, category_id: str):
        self.add_calls.append({"customer_id": customer_id, "category_id": category_id})
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

    def _service(self, client: _FakeIikoCategoryClient) -> IikoCustomerCategorySyncService:
        return IikoCustomerCategorySyncService(
            client=client,
            category_id="cat-active-coupon",
            max_attempts=8,
            retry_base_seconds=30,
            retry_max_seconds=3600,
            request_interval_seconds=0,
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
