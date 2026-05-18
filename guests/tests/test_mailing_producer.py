"""
Тесты producer-слоя массовых рассылок для universal queue.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from guests.models import (
    BotProfile,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    DispatchTask,
    Guest,
    GuestBotBinding,
    Mailing,
    MailingGuest,
    MessageTemplate,
)
from guests.services.universal_queue.mailing_producer import (
    _resolve_priority_for_mailing,
    _resolve_selected_bot_profiles,
    _resolve_target_mode_for_mailing,
    _targets_from_bindings,
    enqueue_mailing_rows_as_dispatch_tasks,
)


class MailingProducerTests(TestCase):
    """
    Интеграционно-юнит проверки постановки MailingGuest -> DispatchTask.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="MAILING_TEMPLATE",
            description="Шаблон для тестов producer",
            message_text="Тестовое сообщение",
            created_by="test",
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Рассылка TEST",
            template=self.template,
            scheduled_date=now.date(),
            scheduled_time_begin=now,
            scheduled_time_end=now + timedelta(hours=1),
            is_active=True,
            created_at=now,
            updated_at=now,
            send_window_begin=now.time(),
            send_window_end=(now + timedelta(hours=2)).time(),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.NORMAL,
        )
        self.guest = Guest.objects.create(
            phone="+79990001234",
            first_name="Тест",
            created_at=now,
            updated_at=now,
        )
        self.bot_tg = BotProfile.objects.create(
            code="tg_mailing_main",
            name="TG mailing",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.bot_vk = BotProfile.objects.create(
            code="vk_mailing_alt",
            name="VK mailing",
            provider_type=BotProfile.ProviderType.VK,
            is_active=True,
        )
        self.mailing.bot_profiles.add(self.bot_tg, self.bot_vk)

    def _create_row(self, *, scheduled_datetime=None) -> MailingGuest:
        return MailingGuest.objects.create(
            mailing=self.mailing,
            guest=self.guest,
            phone=self.guest.phone,
            email="test@example.com",
            text_mailing_list="Текст рассылки",
            scheduled_datetime=scheduled_datetime or timezone.now(),
            status=MailingGuest.Status.PLANNED,
            created_at=timezone.now(),
        )

    def test_resolve_target_mode_and_priority_fallbacks(self):
        """
        Невалидные значения маршрутизации должны безопасно откатываться в default.
        """
        self.mailing.target_mode = "unsupported"
        self.mailing.queue_priority = "unsupported"
        self.assertEqual(_resolve_target_mode_for_mailing(self.mailing), "primary_only")
        self.assertEqual(_resolve_priority_for_mailing(self.mailing), DispatchTask.Priority.BULK)

    def test_resolve_selected_bot_profiles_uses_only_active(self):
        """
        В рассылку попадают только активные BotProfile.
        """
        self.bot_vk.is_active = False
        self.bot_vk.save(update_fields=["is_active"])
        ids, providers = _resolve_selected_bot_profiles(self.mailing)

        self.assertEqual(ids, {self.bot_tg.id})
        self.assertEqual(providers, {"telegram"})

    def test_targets_from_bindings_primary_only_prefers_primary(self):
        """
        primary_only должен выбирать только основную привязку, если она есть.
        """
        primary_binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_tg,
            external_chat_id="tg-chat-1",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        secondary_binding = GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_vk,
            external_chat_id="vk-chat-1",
            is_primary=False,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

        targets = _targets_from_bindings([secondary_binding, primary_binding], target_mode="primary_only")

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["external_chat_id"], "tg-chat-1")
        self.assertEqual(targets[0]["provider_type"], "telegram")

    def test_enqueue_rows_without_selected_bots_marks_rows_error(self):
        """
        Если в рассылке нет активных ботов, строки переходят в ERROR.
        """
        self.bot_tg.is_active = False
        self.bot_vk.is_active = False
        self.bot_tg.save(update_fields=["is_active"])
        self.bot_vk.save(update_fields=["is_active"])
        row = self._create_row()

        summary = enqueue_mailing_rows_as_dispatch_tasks(self.mailing, [row])

        row.refresh_from_db()
        self.assertEqual(summary.rows_total, 1)
        self.assertEqual(summary.rows_failed, 1)
        self.assertEqual(summary.rows_queued, 0)
        self.assertEqual(row.status, MailingGuest.Status.ERROR)
        self.assertEqual(row.delivery_status, "dispatch_no_bot_profiles")

    def test_enqueue_rows_without_bindings_marks_dispatch_no_targets(self):
        """
        При отсутствии активных привязок GuestBotBinding строка уходит в ERROR.
        """
        row = self._create_row()

        summary = enqueue_mailing_rows_as_dispatch_tasks(self.mailing, [row])

        row.refresh_from_db()
        self.assertEqual(summary.rows_failed, 1)
        self.assertEqual(summary.tasks_created, 0)
        self.assertEqual(row.status, MailingGuest.Status.ERROR)
        self.assertEqual(row.delivery_status, "dispatch_no_targets")

    def test_enqueue_rows_creates_dispatch_task_with_future_schedule(self):
        """
        Для будущего scheduled_datetime задача должна получить available_at из строки рассылки.
        """
        future_dt = timezone.now() + timedelta(hours=3)
        row = self._create_row(scheduled_datetime=future_dt)
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_tg,
            external_chat_id="tg-chat-future",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        self.mailing.target_mode = Mailing.TargetMode.PRIMARY_ONLY
        self.mailing.queue_priority = Mailing.QueuePriority.HIGH
        self.mailing.save(update_fields=["target_mode", "queue_priority", "updated_at"])

        summary = enqueue_mailing_rows_as_dispatch_tasks(self.mailing, [row], now=timezone.now())

        row.refresh_from_db()
        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertEqual(summary.rows_queued, 1)
        self.assertEqual(summary.tasks_created, 1)
        self.assertEqual(task.priority, DispatchTask.Priority.HIGH)
        self.assertEqual(task.available_at, future_dt)
        self.assertEqual(task.external_chat_id, "tg-chat-future")
        self.assertEqual(row.status, MailingGuest.Status.DONE)

    def test_enqueue_rows_all_bots_creates_task_per_binding(self):
        """
        В режиме all_bots строка должна поставить задачу на каждую активную привязку.
        """
        row = self._create_row()
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_tg,
            external_chat_id="tg-all",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_vk,
            external_chat_id="vk-all",
            is_primary=False,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        self.mailing.target_mode = Mailing.TargetMode.ALL_BOTS
        self.mailing.save(update_fields=["target_mode", "updated_at"])

        summary = enqueue_mailing_rows_as_dispatch_tasks(self.mailing, [row])

        tasks = list(DispatchTask.objects.filter(mailing_guest=row).order_by("id"))
        self.assertEqual(summary.tasks_created, 2)
        self.assertEqual(summary.rows_queued, 1)
        self.assertEqual(len(tasks), 2)
        self.assertEqual({task.provider_type for task in tasks}, {"telegram", "vk"})

    def test_enqueue_rows_for_max_sets_user_id_payload(self):
        """
        Для MAX-задачи producer должен передать user_id в payload отправителя.
        """
        bot_max = BotProfile.objects.create(
            code="max_mailing_main",
            name="MAX mailing",
            provider_type=BotProfile.ProviderType.MAX,
            is_active=True,
        )
        self.mailing.bot_profiles.add(bot_max)
        row = self._create_row()
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=bot_max,
            external_chat_id="chat-70880299",
            external_user_id="263475680",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

        summary = enqueue_mailing_rows_as_dispatch_tasks(self.mailing, [row], now=timezone.now())

        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertEqual(summary.tasks_created, 1)
        self.assertEqual(task.provider_type, BotProfile.ProviderType.MAX)
        self.assertEqual(task.external_chat_id, "chat-70880299")
        self.assertEqual(task.payload.get("max_user_id"), "263475680")

    def test_enqueue_rows_second_run_counts_duplicates(self):
        """
        Повторный запуск по той же строке должен учитывать duplicate без падения в ERROR.
        """
        row = self._create_row()
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_tg,
            external_chat_id="tg-dup",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

        first = enqueue_mailing_rows_as_dispatch_tasks(self.mailing, [row])
        with patch(
            "guests.services.universal_queue.mailing_producer.DispatchTask.objects.create",
            side_effect=IntegrityError("duplicate key"),
        ):
            second = enqueue_mailing_rows_as_dispatch_tasks(self.mailing, [row])

        row.refresh_from_db()
        self.assertEqual(first.tasks_created, 1)
        self.assertEqual(second.tasks_created, 0)
        self.assertEqual(second.tasks_duplicates, 1)
        self.assertEqual(row.status, MailingGuest.Status.DONE)

    def test_enqueue_rows_when_create_crashes_sets_dispatch_enqueue_error(self):
        """
        Любая не-Integrity ошибка create должна помечать строку как dispatch_enqueue_error.
        """
        row = self._create_row()
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_tg,
            external_chat_id="tg-err",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

        with patch(
            "guests.services.universal_queue.mailing_producer.DispatchTask.objects.create",
            side_effect=RuntimeError("db is down"),
        ):
            summary = enqueue_mailing_rows_as_dispatch_tasks(self.mailing, [row])

        row.refresh_from_db()
        self.assertEqual(summary.rows_failed, 1)
        self.assertEqual(summary.tasks_created, 0)
        self.assertEqual(row.status, MailingGuest.Status.ERROR)
        self.assertEqual(row.delivery_status, "dispatch_enqueue_error")

    def test_enqueue_rows_integrity_error_path_direct(self):
        """
        IntegrityError обрабатывается как duplicate, строка при этом считается queued.
        """
        row = self._create_row()
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_tg,
            external_chat_id="tg-int",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

        with patch(
            "guests.services.universal_queue.mailing_producer.DispatchTask.objects.create",
            side_effect=IntegrityError("duplicate key"),
        ):
            summary = enqueue_mailing_rows_as_dispatch_tasks(self.mailing, [row])

        row.refresh_from_db()
        self.assertEqual(summary.tasks_created, 0)
        self.assertEqual(summary.tasks_duplicates, 1)
        self.assertEqual(summary.rows_queued, 1)
        self.assertEqual(row.status, MailingGuest.Status.DONE)

    def test_enqueue_rows_marks_coupon_assignment_sent_and_sets_payload(self):
        """
        Для купонной кампании при постановке строки:
        1. назначение купона переходит в status=sent;
        2. код и серия купона попадают в payload DispatchTask.
        """
        self.mailing.coupon_series = "TEST"
        self.mailing.coupon_venue_code = "DEP_1"
        self.mailing.coupon_venue_name = "Тестовый ресторан"
        self.mailing.coupon_promo_text = "Скидка 20% на сет по купону."
        self.mailing.save(
            update_fields=[
                "coupon_series",
                "coupon_venue_code",
                "coupon_venue_name",
                "coupon_promo_text",
                "updated_at",
            ]
        )

        row = self._create_row()
        GuestBotBinding.objects.create(
            guest=self.guest,
            bot=self.bot_tg,
            external_chat_id="tg-coupon",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-PL-001",
            venue_code="DEP_1",
            venue_name="Тестовый ресторан",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        assignment = CouponCampaignAssignment.objects.create(
            campaign=self.mailing,
            guest=self.guest,
            coupon=coupon,
            coupon_series="TEST",
            coupon_code="TST-PL-001",
            venue_code="DEP_1",
            venue_name="Тестовый ресторан",
            promo_text="Скидка 20% на сет по купону.",
            status=CouponCampaignAssignment.Status.RESERVED,
        )

        summary = enqueue_mailing_rows_as_dispatch_tasks(self.mailing, [row], now=timezone.now())
        self.assertEqual(summary.rows_queued, 1)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.SENT)
        self.assertIsNotNone(assignment.sent_at)

        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertEqual(task.payload.get("coupon_series"), "TEST")
        self.assertEqual(task.payload.get("coupon_code"), "TST-PL-001")
        self.assertEqual(task.payload.get("coupon_venue_code"), "DEP_1")
        self.assertEqual(task.payload.get("coupon_venue_name"), "Тестовый ресторан")
        self.assertEqual(task.payload.get("coupon_promo_text"), "Скидка 20% на сет по купону.")
