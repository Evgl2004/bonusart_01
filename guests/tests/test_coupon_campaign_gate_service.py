from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from guests.models import (
    BotProfile,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    Guest,
    Mailing,
    MailingGuest,
    MessageTemplate,
    VtelemaxRecipientChannel,
    VtelemaxSyncState,
)
from guests.services.coupon_constants import (
    COUPON_MESSAGE_FOOTER,
    COUPON_VENUE_GLOBAL_CODE,
    COUPON_VENUE_GLOBAL_NAME,
)
from guests.services.coupon_campaign import CouponCampaignGateService


class CouponCampaignGateServiceTests(TestCase):
    """
    Проверки купонного pre-send gate перед постановкой строк в DispatchTask.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Coupon template",
            description="",
            message_text="Ваш персональный купон: {coupon_code}",
            created_by="test",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Coupon mailing",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now - timedelta(hours=1),
            scheduled_time_end=self.now + timedelta(hours=3),
            is_active=True,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=(self.now - timedelta(hours=1)).time(),
            send_window_end=(self.now + timedelta(hours=2)).time(),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.NORMAL,
            coupon_series="TEST",
            coupon_venue_code="DEP_1",
            coupon_venue_name="Тестовое заведение",
            coupon_promo_text="Скидка 20% на сет по купону.",
        )
        self.bot = BotProfile.objects.create(
            code="tg_coupon_gate",
            name="TG",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.mailing.bot_profiles.add(self.bot)

    def _create_row(self, suffix: str) -> MailingGuest:
        guest = Guest.objects.create(
            phone=f"+79990000{suffix}",
            first_name=f"Guest{suffix}",
            created_at=self.now,
            updated_at=self.now,
        )
        return MailingGuest.objects.create(
            mailing=self.mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="placeholder",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )

    def _create_valid_channel(self, *, guest: Guest, phone_e164: str) -> None:
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid4(),
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            phone_e164=phone_e164,
            external_id=f"chat-{guest.id}",
            rules_accepted=True,
            notifications_allowed=True,
            is_registered=True,
            registered_at=self.now,
            effective_updated_at=self.now,
            guest=guest,
        )

    def test_prepare_rows_creates_assignment_but_blocks_until_sync_ack(self):
        """
        При корректных данных сервис должен:
        1. назначить купон;
        2. поставить событие синка в статус pending;
        3. персонализировать текст строки.
        """
        row = self._create_row("1234")
        self._create_valid_channel(guest=row.guest, phone_e164="+799900001234")
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-AAA111",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
        )

        service = CouponCampaignGateService()
        ready_rows, report = service.prepare_rows_for_dispatch(
            mailing=self.mailing,
            rows=[row],
            now=self.now,
            dry_run=False,
        )

        self.assertEqual(len(ready_rows), 0)
        self.assertTrue(report.coupon_mode)
        self.assertEqual(report.rows_blocked, 1)
        self.assertEqual(report.created_assignments, 1)
        self.assertEqual(report.sync_ok, 0)
        self.assertEqual(report.issues_by_code().get("coupon_sync_event_pending"), 1)

        assignment = CouponCampaignAssignment.objects.get(campaign=self.mailing, guest=row.guest)
        self.assertEqual(assignment.coupon_id, coupon.id)
        self.assertEqual(
            assignment.vtelemax_sync_status,
            CouponCampaignAssignment.VtelemaxSyncStatus.PENDING,
        )
        self.assertIsNone(assignment.vtelemax_synced_at)
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.RESERVED)
        self.assertEqual(assignment.venue_code, "DEP_1")
        self.assertEqual(assignment.promo_text, "Скидка 20% на сет по купону.")

        queue_event = CouponVtelemaxSyncQueue.objects.filter(assignment=assignment).order_by("-id").first()
        self.assertIsNotNone(queue_event)
        self.assertEqual(queue_event.status, CouponVtelemaxSyncQueue.Status.PENDING)

        row.refresh_from_db()
        self.assertEqual(row.text_mailing_list, "placeholder")

    def test_prepare_rows_allows_dispatch_only_after_ack_and_ok_status(self):
        """
        Строка может уйти в отправку только при полном подтверждении:
        1) queue-event в статусе ACKED;
        2) assignment.vtelemax_sync_status == ok;
        3) assignment.vtelemax_synced_at заполнен.
        """
        row = self._create_row("1235")
        self._create_valid_channel(guest=row.guest, phone_e164="+799900001235")
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-AAA112",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        assignment = CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=row.guest,
            coupon=coupon,
            coupon_series="TEST",
            coupon_code="TST-AAA112",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Скидка 20% на сет по купону.",
            assigned_at=self.now,
            status=CouponCampaignAssignment.Status.RESERVED,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )
        CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
            assignment=assignment,
            payload_json={"coupon_code": "TST-AAA112"},
            status=CouponVtelemaxSyncQueue.Status.ACKED,
            attempts=1,
            next_retry_at=self.now,
            sent_at=self.now,
            ack_at=self.now,
        )

        service = CouponCampaignGateService()
        ready_rows, report = service.prepare_rows_for_dispatch(
            mailing=self.mailing,
            rows=[row],
            now=self.now,
            dry_run=False,
        )

        self.assertEqual(len(ready_rows), 1)
        self.assertEqual(report.rows_blocked, 0)
        self.assertEqual(report.sync_ok, 1)
        row.refresh_from_db()
        self.assertEqual(
            row.text_mailing_list,
            f"Ваш персональный купон: TST-AAA112\n\n{COUPON_MESSAGE_FOOTER}",
        )

    def test_prepare_rows_renders_coupon_promo_text_with_template_variables(self):
        """
        Текст акции в карточке купона использует те же переменные, что и шаблон рассылки.
        """
        self.template.message_text = "Акция: {coupon_promo_text}"
        self.template.save(update_fields=["message_text", "updated_at"])
        self.mailing.coupon_promo_text = "Здравствуйте, {first_name}. Купон {coupon_code}"
        self.mailing.save(update_fields=["coupon_promo_text", "updated_at"])

        row = self._create_row("1236")
        self._create_valid_channel(guest=row.guest, phone_e164="+799900001236")
        CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-AAA113",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
        )

        service = CouponCampaignGateService()
        ready_rows, report = service.prepare_rows_for_dispatch(
            mailing=self.mailing,
            rows=[row],
            now=self.now,
            dry_run=False,
        )

        self.assertEqual(len(ready_rows), 0)
        self.assertEqual(report.issues_by_code().get("coupon_sync_event_pending"), 1)

        assignment = CouponCampaignAssignment.objects.get(campaign=self.mailing, guest=row.guest)
        expected_promo_text = "Здравствуйте, Guest1236. Купон TST-AAA113"
        self.assertEqual(assignment.promo_text, expected_promo_text)

        queue_event = CouponVtelemaxSyncQueue.objects.get(assignment=assignment)
        self.assertEqual(queue_event.payload_json.get("promo_text"), expected_promo_text)

        assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.OK
        assignment.vtelemax_synced_at = self.now
        assignment.save(update_fields=["vtelemax_sync_status", "vtelemax_synced_at", "updated_at"])
        queue_event.status = CouponVtelemaxSyncQueue.Status.ACKED
        queue_event.ack_at = self.now
        queue_event.save(update_fields=["status", "ack_at", "updated_at"])

        ready_rows, report = service.prepare_rows_for_dispatch(
            mailing=self.mailing,
            rows=[row],
            now=self.now,
            dry_run=False,
        )

        self.assertEqual(len(ready_rows), 1)
        self.assertEqual(report.sync_ok, 1)
        row.refresh_from_db()
        self.assertEqual(
            row.text_mailing_list,
            f"Акция: {expected_promo_text}\n\n{COUPON_MESSAGE_FOOTER}",
        )

    def test_prepare_rows_blocks_when_coupons_not_enough(self):
        """
        Если купонов меньше, чем гостей без назначения, gate должен заблокировать строки.
        """
        row_1 = self._create_row("2221")
        row_2 = self._create_row("2222")
        self._create_valid_channel(guest=row_1.guest, phone_e164="+799900002221")
        self._create_valid_channel(guest=row_2.guest, phone_e164="+799900002222")

        CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-ONLY1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
        )

        service = CouponCampaignGateService()
        ready_rows, report = service.prepare_rows_for_dispatch(
            mailing=self.mailing,
            rows=[row_1, row_2],
            now=self.now,
            dry_run=False,
        )

        self.assertEqual(len(ready_rows), 0)
        self.assertGreaterEqual(report.rows_blocked, 2)
        self.assertTrue(report.global_blockers)
        self.assertEqual(CouponCampaignAssignment.objects.filter(campaign=self.mailing).count(), 0)

    @override_settings(
        VTELEMAX_SYNC_ENABLED=True,
        VTELEMAX_COUPON_SYNC_GATE_REQUIRE_FRESH_STATE=True,
        VTELEMAX_COUPON_SYNC_GATE_MAX_SYNC_AGE_MINUTES=5,
    )
    def test_prepare_rows_blocks_when_vtelemax_state_is_stale(self):
        """
        При устаревшем состоянии синка получателей vtelemax запуск должен блокироваться.
        """
        row = self._create_row("3333")
        self._create_valid_channel(guest=row.guest, phone_e164="+799900003333")
        CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-STALE1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
        )
        VtelemaxSyncState.objects.create(
            key="vtelemax_recipients",
            last_status=VtelemaxSyncState.Status.SUCCESS,
            last_success_at=self.now - timedelta(minutes=30),
        )

        service = CouponCampaignGateService()
        ready_rows, report = service.prepare_rows_for_dispatch(
            mailing=self.mailing,
            rows=[row],
            now=self.now,
            dry_run=False,
        )

        self.assertEqual(len(ready_rows), 0)
        self.assertTrue(report.global_blockers)
        self.assertIn("устарел", report.global_blockers[0])

    def test_prepare_rows_blocks_when_assignment_venue_mismatch(self):
        """
        Если у уже назначенного купона venue не совпадает с кампанией, строка блокируется.
        """
        row = self._create_row("4444")
        self._create_valid_channel(guest=row.guest, phone_e164="+799900004444")
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-MISMATCH-1",
            venue_code="DEP_X",
            venue_name="Чужое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        assignment = CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=row.guest,
            coupon=coupon,
            coupon_series="TEST",
            coupon_code="TST-MISMATCH-1",
            venue_code="DEP_X",
            venue_name="Чужое заведение",
            status=CouponCampaignAssignment.Status.RESERVED,
        )

        service = CouponCampaignGateService()
        ready_rows, report = service.prepare_rows_for_dispatch(
            mailing=self.mailing,
            rows=[row],
            now=self.now,
            dry_run=False,
        )

        self.assertEqual(len(ready_rows), 0)
        self.assertTrue(any(issue.code == "coupon_venue_mismatch" for issue in report.issues))
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.ERROR)

    def test_prepare_rows_blocks_when_sync_event_is_error(self):
        """
        Если по назначению последний sync-event завершился ошибкой,
        строка не должна попасть в отправку.
        """
        row = self._create_row("4460")
        self._create_valid_channel(guest=row.guest, phone_e164="+799900004460")
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-ERR-1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        assignment = CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=row.guest,
            coupon=coupon,
            coupon_series="TEST",
            coupon_code="TST-ERR-1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            status=CouponCampaignAssignment.Status.RESERVED,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.ERROR,
            vtelemax_sync_error="transport timeout",
        )
        CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
            assignment=assignment,
            payload_json={"coupon_code": "TST-ERR-1"},
            status=CouponVtelemaxSyncQueue.Status.ERROR,
            attempts=3,
            next_retry_at=self.now,
            last_error="transport timeout",
        )

        service = CouponCampaignGateService()
        ready_rows, report = service.prepare_rows_for_dispatch(
            mailing=self.mailing,
            rows=[row],
            now=self.now,
            dry_run=False,
        )

        self.assertEqual(len(ready_rows), 0)
        self.assertEqual(report.sync_ok, 0)
        self.assertGreaterEqual(report.sync_error, 1)
        self.assertEqual(report.issues_by_code().get("coupon_sync_event_error"), 1)

    def test_prepare_rows_global_campaign_uses_global_coupon(self):
        """
        Для общей кампании (__global__) сервис принимает общий купон.
        """
        self.mailing.coupon_venue_code = COUPON_VENUE_GLOBAL_CODE
        self.mailing.coupon_venue_name = COUPON_VENUE_GLOBAL_NAME
        self.mailing.coupon_promo_text = "Общий купон для всех заведений"
        self.mailing.save(
            update_fields=["coupon_venue_code", "coupon_venue_name", "coupon_promo_text", "updated_at"]
        )

        row = self._create_row("5555")
        self._create_valid_channel(guest=row.guest, phone_e164="+799900005555")
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="GLB-AAA111",
            venue_code=COUPON_VENUE_GLOBAL_CODE,
            venue_name=COUPON_VENUE_GLOBAL_NAME,
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
        )

        service = CouponCampaignGateService()
        ready_rows, report = service.prepare_rows_for_dispatch(
            mailing=self.mailing,
            rows=[row],
            now=self.now,
            dry_run=False,
        )

        self.assertEqual(len(ready_rows), 0)
        self.assertEqual(report.coupon_venue_code, COUPON_VENUE_GLOBAL_CODE)
        self.assertEqual(report.coupon_venue_name, COUPON_VENUE_GLOBAL_NAME)
        self.assertEqual(report.issues_by_code().get("coupon_sync_event_pending"), 1)

        assignment = CouponCampaignAssignment.objects.get(campaign=self.mailing, guest=row.guest)
        self.assertEqual(assignment.coupon_id, coupon.id)
        self.assertEqual(assignment.venue_code, COUPON_VENUE_GLOBAL_CODE)
