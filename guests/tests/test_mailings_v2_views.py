"""
Smoke-тесты нового контура mailings-v2.

Проверяем:
1. создание кампании через v2-форму;
2. доступность экрана редактирования и аудитории кампании;
3. корректный redirect после импорта телефонов при переданном `next`.
"""

from __future__ import annotations

from datetime import time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    GuestBotBinding,
    Mailing,
    MailingGuest,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
)


class MailingsV2ViewsTests(TestCase):
    """
    Базовые проверки CRUD-флоу кампаний в новом UI.
    """

    def setUp(self):
        now = timezone.now()
        self.now = now
        self.template = MessageTemplate.objects.create(
            name="Тестовый шаблон",
            description="Тест",
            message_text="Привет, {{ first_name }}",
            is_active=True,
        )
        self.bot = BotProfile.objects.create(
            code="tg_main_test",
            name="Telegram main",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            token="test-token",
            is_active=True,
        )

    def _create_mailing(self) -> Mailing:
        mailing = Mailing.objects.create(
            name="Кампания v2",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now,
            scheduled_time_end=self.now + timedelta(hours=1),
            is_active=False,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=time(10, 0),
            send_window_end=time(21, 0),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.BULK,
        )
        mailing.bot_profiles.add(self.bot)
        return mailing

    def test_create_campaign_v2(self):
        """
        Создание кампании через mailings-v2 должно сохранять запись и вести на v2-edit.
        """
        new_page = self.client.get(reverse("mailings_v2_campaigns_new"), secure=True)
        self.assertEqual(new_page.status_code, 200)
        self.assertEqual(new_page.context["wizard_state"]["current_step"], 1)
        self.assertEqual(len(new_page.context["wizard_state"]["steps"]), 3)

        begin = (self.now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        end = (self.now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            reverse("mailings_v2_campaigns_new"),
            {
                "name": "Новая кампания v2",
                "template": self.template.id,
                "scheduled_date": self.now.date().isoformat(),
                "scheduled_time_begin": begin,
                "scheduled_time_end": end,
                "send_window_begin": "10:00",
                "send_window_end": "20:00",
                "target_mode": Mailing.TargetMode.PRIMARY_ONLY,
                "queue_priority": Mailing.QueuePriority.BULK,
                "bot_profiles": [self.bot.id],
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        mailing = Mailing.objects.get(name="Новая кампания v2")
        self.assertEqual(
            response.url,
            reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}),
        )
        self.assertFalse(mailing.is_active)
        self.assertEqual(mailing.bot_profiles.count(), 1)

    def test_create_campaign_v2_prefills_template_from_query(self):
        """
        Страница создания кампании должна поддерживать prefill шаблона по параметру template_id.
        """
        response = self.client.get(
            reverse("mailings_v2_campaigns_new"),
            {"template_id": self.template.id},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.context["form"]["template"].value()), str(self.template.id))
        self.assertContains(response, "Новая кампания")

    def test_edit_and_audience_pages_v2(self):
        """
        Экран редактирования и экран аудитории должны открываться и показывать данные кампании.
        """
        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000001",
            first_name="Иван",
            created_at=self.now,
            updated_at=self.now,
        )
        MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Тестовое сообщение",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )

        edit_response = self.client.get(
            reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertEqual(edit_response.context["wizard_state"]["current_step"], 3)
        self.assertContains(edit_response, "Проверить аудиторию")

        audience_response = self.client.get(
            reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(audience_response.status_code, 200)
        self.assertEqual(audience_response.context["wizard_state"]["current_step"], 2)
        self.assertContains(audience_response, guest.phone)

    def test_edit_page_wizard_requires_audience_before_launch(self):
        """
        Если аудитория пустая, мастер на экране кампании должен оставаться на шаге 2.
        """
        mailing = self._create_mailing()
        response = self.client.get(
            reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard_state"]["current_step"], 2)

    def test_campaign_views_show_workbench_snapshot(self):
        """
        Для кампании, созданной из Workbench, v2-экраны должны показывать сохранённый snapshot фильтров.
        """
        mailing = self._create_mailing()
        session = self.client.session
        session["mailings_v2_workbench_snapshots"] = {
            str(mailing.id): {
                "as_of_date": self.now.date().isoformat(),
                "window_days": "30",
                "department_id": "dep-1",
                "segment_code": "active_30d",
                "focus_category_code": "sushi_rolls",
                "complex_filters": [
                    {"field": "orders_count", "operator": "gte", "value": "2"},
                ],
                "selected_total": 10,
                "selected_rows_count": 10,
                "source_layer": "category_window",
                "saved_at": self.now.isoformat(),
            }
        }
        session.save()

        edit_response = self.client.get(
            reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "Источник аудитории: Workbench")
        self.assertContains(edit_response, "active_30d")
        self.assertContains(edit_response, "sushi_rolls")
        self.assertContains(edit_response, reverse("guests_workbench"))

        audience_response = self.client.get(
            reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(audience_response.status_code, 200)
        self.assertContains(audience_response, "Аудитория собрана из Workbench")
        self.assertContains(audience_response, "active_30d")

    def test_import_phones_view_respects_next_url(self):
        """
        Импорт телефонов должен возвращать в переданный `next` URL.
        """
        mailing = self._create_mailing()
        next_url = reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id})

        response = self.client.post(
            reverse("mailing_import_phones", kwargs={"pk": mailing.id}),
            {"next": next_url},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, next_url)

    def test_campaigns_hub_filters_and_search(self):
        """
        Список кампаний v2 должен поддерживать фильтры/поиск и скрывать архив по умолчанию.
        """
        active = self._create_mailing()
        active.name = "Promo active"
        active.is_active = True
        active.save(update_fields=["name", "is_active", "updated_at"])

        archived = self._create_mailing()
        archived.name = "Promo archived"
        archived.is_archived = True
        archived.is_active = False
        archived.save(update_fields=["name", "is_archived", "is_active", "updated_at"])

        with_error = self._create_mailing()
        with_error.name = "Promo with error"
        with_error.save(update_fields=["name", "updated_at"])

        guest = Guest.objects.create(
            phone="+79990000555",
            first_name="Тест",
            created_at=self.now,
            updated_at=self.now,
        )
        MailingGuest.objects.create(
            mailing=with_error,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.ERROR,
            error_description="test",
            created_at=self.now,
        )

        default_response = self.client.get(reverse("mailings_v2_campaigns"), secure=True)
        self.assertEqual(default_response.status_code, 200)
        self.assertContains(default_response, "Promo active")
        self.assertContains(default_response, "Promo with error")
        self.assertNotContains(default_response, "Promo archived")

        archived_response = self.client.get(
            reverse("mailings_v2_campaigns"),
            {"show_archived": "1", "q": "archived"},
            secure=True,
        )
        self.assertEqual(archived_response.status_code, 200)
        self.assertContains(archived_response, "Promo archived")
        self.assertEqual(archived_response.context["campaigns_total_filtered"], 1)

        active_response = self.client.get(
            reverse("mailings_v2_campaigns"),
            {"only_active": "1"},
            secure=True,
        )
        self.assertEqual(active_response.status_code, 200)
        self.assertContains(active_response, "Promo active")
        self.assertNotContains(active_response, "Promo with error")

        error_response = self.client.get(
            reverse("mailings_v2_campaigns"),
            {"with_errors": "1"},
            secure=True,
        )
        self.assertEqual(error_response.status_code, 200)
        self.assertContains(error_response, "Promo with error")
        self.assertNotContains(error_response, "Promo active")

    def test_campaign_ops_duplicate_campaign_copies_setup_and_rows(self):
        """
        Дублирование кампании должно копировать настройки, ботов и аудиторию в planned-статус.
        """
        mailing = self._create_mailing()
        mailing.name = "Promo source"
        mailing.save(update_fields=["name", "updated_at"])

        guest1 = Guest.objects.create(
            phone="+79990000661",
            first_name="Алина",
            created_at=self.now,
            updated_at=self.now,
        )
        guest2 = Guest.objects.create(
            phone="+79990000662",
            first_name="Олег",
            created_at=self.now,
            updated_at=self.now,
        )
        MailingGuest.objects.create(
            mailing=mailing,
            guest=guest1,
            phone=guest1.phone,
            email="",
            text_mailing_list="Текст 1",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.DONE,
            delivery_status="done",
            created_at=self.now,
        )
        MailingGuest.objects.create(
            mailing=mailing,
            guest=guest2,
            phone=guest2.phone,
            email="",
            text_mailing_list="Текст 2",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.ERROR,
            error_description="error",
            created_at=self.now,
        )

        response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "duplicate_campaign"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        duplicate = Mailing.objects.exclude(id=mailing.id).get(name="Promo source (копия)")
        self.assertEqual(
            response.url,
            reverse("mailings_v2_campaigns_edit", kwargs={"pk": duplicate.id}),
        )
        self.assertFalse(duplicate.is_active)
        self.assertFalse(duplicate.is_archived)
        self.assertEqual(duplicate.bot_profiles.count(), mailing.bot_profiles.count())
        self.assertEqual(duplicate.guests_rows.count(), mailing.guests_rows.count())
        self.assertEqual(
            duplicate.guests_rows.filter(status=MailingGuest.Status.PLANNED).count(),
            mailing.guests_rows.count(),
        )
        self.assertEqual(
            duplicate.guests_rows.filter(delivery_status="duplicated_from_campaign").count(),
            mailing.guests_rows.count(),
        )

    def test_campaign_ops_archive_campaign_hides_from_default_list(self):
        """
        Архивирование должно убирать кампанию из списка по умолчанию и оставлять в show_archived.
        """
        mailing = self._create_mailing()
        mailing.name = "Promo to archive"
        mailing.is_active = True
        mailing.save(update_fields=["name", "is_active", "updated_at"])

        response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "archive_campaign", "next": reverse("mailings_v2_campaigns")},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("mailings_v2_campaigns"))

        mailing.refresh_from_db()
        self.assertTrue(mailing.is_archived)
        self.assertFalse(mailing.is_active)

        default_response = self.client.get(reverse("mailings_v2_campaigns"), secure=True)
        self.assertEqual(default_response.status_code, 200)
        self.assertNotContains(default_response, "Promo to archive")

        archived_response = self.client.get(
            reverse("mailings_v2_campaigns"),
            {"show_archived": "1", "q": "Promo to archive"},
            secure=True,
        )
        self.assertEqual(archived_response.status_code, 200)
        self.assertContains(archived_response, "Promo to archive")

    def test_campaign_ops_toggle_and_retry_rows(self):
        """
        Операционные действия v2 должны уметь запускать кампанию
        и переводить error/in_progress строки обратно в planned.
        """
        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000333",
            first_name="Ольга",
            created_at=self.now,
            updated_at=self.now,
        )
        row_error = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.ERROR,
            error_description="test error",
            created_at=self.now,
        )
        row_progress = MailingGuest.objects.create(
            mailing=mailing,
            guest=Guest.objects.create(
                phone="+79990000334",
                first_name="Ирина",
                created_at=self.now,
                updated_at=self.now,
            ),
            phone="+79990000334",
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.IN_PROGRESS,
            created_at=self.now,
        )

        toggle_response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "toggle_active"},
            secure=True,
        )
        self.assertEqual(toggle_response.status_code, 302)
        self.assertEqual(toggle_response.url, reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}))
        mailing.refresh_from_db()
        self.assertTrue(mailing.is_active)

        retry_rows_response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "retry_failed_rows"},
            secure=True,
        )
        self.assertEqual(retry_rows_response.status_code, 302)
        row_error.refresh_from_db()
        self.assertEqual(row_error.status, MailingGuest.Status.PLANNED)
        self.assertEqual(row_error.delivery_status, "retry_requested")
        self.assertIsNone(row_error.error_description)

        requeue_response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "requeue_in_progress_rows"},
            secure=True,
        )
        self.assertEqual(requeue_response.status_code, 302)
        row_progress.refresh_from_db()
        self.assertEqual(row_progress.status, MailingGuest.Status.PLANNED)
        self.assertEqual(row_progress.delivery_status, "requeued_from_ui")

    def test_campaign_ops_retry_failed_dispatch(self):
        """
        Retry failed dispatch должен переводить failed-задачи кампании обратно в pending.
        """
        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000444",
            first_name="Дмитрий",
            created_at=self.now,
            updated_at=self.now,
        )
        mailing_guest = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.DONE,
            created_at=self.now,
        )
        task = DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.FAILED,
            mailing_guest=mailing_guest,
            guest=guest,
            attempt=5,
            max_attempts=5,
            queue_name="dispatch:telegram:high",
            last_error="permanent",
            started_at=self.now,
            finished_at=self.now,
            enqueued_at=self.now,
        )

        response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "retry_failed_dispatch"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}))

        task.refresh_from_db()
        self.assertEqual(task.status, DispatchTask.Status.PENDING)
        self.assertEqual(task.attempt, 0)
        self.assertIsNone(task.queue_name)
        self.assertIsNone(task.enqueued_at)
        self.assertIsNone(task.started_at)
        self.assertIsNone(task.finished_at)
        self.assertIsNone(task.last_error)

    def test_campaign_ops_dry_run_stores_report_in_session(self):
        """
        Dry-run операция должна сохранять отчёт в сессии для отображения на форме кампании.
        """
        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000881",
            first_name="Павел",
            created_at=self.now,
            updated_at=self.now,
        )
        MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now - timedelta(minutes=1),
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )
        GuestBotBinding.objects.create(
            guest=guest,
            bot=self.bot,
            external_chat_id="tg-dry-run-1",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

        response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "dry_run_campaign"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}))

        report = self.client.session.get("mailing_ops_dry_run_report")
        self.assertIsInstance(report, dict)
        self.assertEqual(report.get("mailing_id"), mailing.id)
        self.assertEqual(report.get("ready_rows"), 1)
        self.assertEqual(report.get("ready_rows_with_targets"), 1)

    def test_campaign_ops_run_now_creates_dispatch_for_ready_rows(self):
        """
        Run-now операция должна провести one-shot постановку задач в DispatchTask.
        """
        mailing = self._create_mailing()
        mailing.send_window_begin = time(0, 0)
        mailing.send_window_end = time(23, 59)
        mailing.save(update_fields=["send_window_begin", "send_window_end", "updated_at"])

        guest = Guest.objects.create(
            phone="+79990000882",
            first_name="Никита",
            created_at=self.now,
            updated_at=self.now,
        )
        row = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now - timedelta(minutes=1),
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )
        GuestBotBinding.objects.create(
            guest=guest,
            bot=self.bot,
            external_chat_id="tg-run-now-1",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )

        response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "run_now_campaign"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}))

        row.refresh_from_db()
        self.assertEqual(row.status, MailingGuest.Status.DONE)
        self.assertEqual(row.delivery_status, "queued_to_dispatch")
        self.assertEqual(DispatchTask.objects.filter(mailing_guest=row).count(), 1)

        report = self.client.session.get("mailing_ops_run_now_report")
        self.assertIsInstance(report, dict)
        self.assertEqual(report.get("mailing_id"), mailing.id)
        self.assertEqual(report.get("processed_rows_total"), 1)
        self.assertEqual(report.get("processed_batches"), 1)

    def test_campaign_runs_page_filters_rows_and_tasks(self):
        """
        Экран запусков v2 должен фильтровать строки аудитории и dispatch-задачи.
        """
        mailing = self._create_mailing()
        guest_ok = Guest.objects.create(
            phone="+79990000666",
            first_name="Мария",
            created_at=self.now,
            updated_at=self.now,
        )
        guest_err = Guest.objects.create(
            phone="+79990000777",
            first_name="Светлана",
            created_at=self.now,
            updated_at=self.now,
        )
        row_ok = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest_ok,
            phone=guest_ok.phone,
            email="",
            text_mailing_list="Текст 1",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.DONE,
            created_at=self.now,
        )
        row_err = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest_err,
            phone=guest_err.phone,
            email="",
            text_mailing_list="Текст 2",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.ERROR,
            error_description="provider timeout",
            created_at=self.now,
        )

        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.VK,
            status=DispatchTask.Status.DONE,
            mailing_guest=row_ok,
            guest=guest_ok,
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.FAILED,
            mailing_guest=row_err,
            guest=guest_err,
            last_error="timeout 429",
        )

        response = self.client.get(
            reverse("mailings_v2_campaigns_runs", kwargs={"pk": mailing.id}),
            {
                "q": "777",
                "row_status": MailingGuest.Status.ERROR,
                "task_status": DispatchTask.Status.FAILED,
                "provider_type": BotProfile.ProviderType.TELEGRAM,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Запуски и история кампании")
        self.assertEqual(response.context["rows_filtered_total"], 1)
        self.assertEqual(response.context["tasks_filtered_total"], 1)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(len(response.context["tasks"]), 1)
        self.assertEqual(response.context["rows"][0].status, MailingGuest.Status.ERROR)
        self.assertEqual(response.context["tasks"][0].status, DispatchTask.Status.FAILED)
        self.assertEqual(response.context["tasks"][0].provider_type, BotProfile.ProviderType.TELEGRAM)

    def test_campaign_errors_page_filters_row_and_dispatch_errors(self):
        """
        Экран ошибок v2 должен фильтровать error-строки и failed dispatch по выбранным параметрам.
        """
        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000888",
            first_name="Юлия",
            created_at=self.now,
            updated_at=self.now,
        )
        row = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.ERROR,
            delivery_status="dispatch_enqueue_error",
            error_description="queue timeout",
            created_at=self.now,
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.FAILED,
            mailing_guest=row,
            guest=guest,
            last_error="provider timeout",
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.VK,
            status=DispatchTask.Status.DONE,
            mailing_guest=row,
            guest=guest,
        )

        response = self.client.get(
            reverse("mailings_v2_campaigns_errors", kwargs={"pk": mailing.id}),
            {
                "q": "888",
                "delivery_status": "dispatch_enqueue_error",
                "provider_type": BotProfile.ProviderType.TELEGRAM,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ошибки кампании")
        self.assertEqual(response.context["row_errors_total"], 1)
        self.assertEqual(response.context["failed_dispatch_total"], 1)
        self.assertEqual(len(response.context["row_errors"]), 1)
        self.assertEqual(len(response.context["failed_dispatch"]), 1)
        self.assertEqual(response.context["row_errors"][0].status, MailingGuest.Status.ERROR)
        self.assertEqual(response.context["failed_dispatch"][0].status, DispatchTask.Status.FAILED)

    def test_campaign_logs_page_builds_combined_timeline(self):
        """
        Экран логов v2 должен отдавать объединённый таймлайн по строкам аудитории и dispatch.
        """
        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000999",
            first_name="Игорь",
            created_at=self.now,
            updated_at=self.now,
        )
        row = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.DONE,
            delivery_status="queued_to_dispatch",
            sent_at=self.now,
            created_at=self.now,
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.DONE,
            mailing_guest=row,
            guest=guest,
            enqueued_at=self.now,
            started_at=self.now,
            finished_at=self.now,
        )

        response = self.client.get(
            reverse("mailings_v2_campaigns_logs", kwargs={"pk": mailing.id}),
            {
                "q": "999",
                "row_status": MailingGuest.Status.DONE,
                "task_status": DispatchTask.Status.DONE,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Логи кампании")
        self.assertEqual(response.context["rows_filtered_total"], 1)
        self.assertEqual(response.context["tasks_filtered_total"], 1)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(len(response.context["tasks"]), 1)
        timeline = response.context["timeline"]
        self.assertGreaterEqual(len(timeline), 2)
        kinds = {item.get("kind") for item in timeline}
        self.assertIn("row", kinds)
        self.assertIn("dispatch", kinds)

    def test_campaign_jobs_page_filters_and_shows_feedback(self):
        """
        Экран заданий должен фильтровать DispatchTask и показывать агрегаты обратной связи.
        """
        mailing = self._create_mailing()
        guest_ok = Guest.objects.create(
            phone="+79990000123",
            first_name="Ольга",
            created_at=self.now,
            updated_at=self.now,
        )
        guest_fail = Guest.objects.create(
            phone="+79990000456",
            first_name="Антон",
            created_at=self.now,
            updated_at=self.now,
        )
        row_ok = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest_ok,
            phone=guest_ok.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.DONE,
            delivery_status="queued_to_dispatch",
            created_at=self.now,
        )
        row_fail = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest_fail,
            phone=guest_fail.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.ERROR,
            delivery_status="dispatch_enqueue_error",
            error_description="provider timeout",
            created_at=self.now,
        )

        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.DONE,
            mailing_guest=row_ok,
            guest=guest_ok,
            queue_name="dispatch:telegram:normal",
            external_chat_id="ok-chat",
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.FAILED,
            mailing_guest=row_fail,
            guest=guest_fail,
            queue_name="dispatch:telegram:normal",
            external_chat_id="fail-chat",
            last_error="timeout 429",
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.VK,
            status=DispatchTask.Status.PENDING,
            mailing_guest=row_fail,
            guest=guest_fail,
            queue_name="dispatch:vk:bulk",
        )

        response = self.client.get(
            reverse("mailings_v2_campaigns_jobs", kwargs={"pk": mailing.id}),
            {
                "task_status": DispatchTask.Status.FAILED,
                "provider_type": BotProfile.ProviderType.TELEGRAM,
                "queue_name": "dispatch:telegram:normal",
                "q": "fail-chat",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Задания отправки кампании")
        self.assertEqual(response.context["tasks_filtered_total"], 1)
        self.assertEqual(len(response.context["tasks"]), 1)
        self.assertEqual(response.context["tasks"][0].status, DispatchTask.Status.FAILED)
        self.assertEqual(response.context["tasks"][0].provider_type, BotProfile.ProviderType.TELEGRAM)
        self.assertTrue(any(item["delivery_status"] == "dispatch_enqueue_error" for item in response.context["delivery_feedback_rows"]))
        self.assertTrue(any(item["last_error"] == "timeout 429" for item in response.context["top_errors"]))

    def test_create_template_v2_and_open_editor(self):
        """
        Создание шаблона через v2 должно вести в режим редактирования.
        """
        response = self.client.post(
            reverse("mailings_v2_templates_new"),
            {
                "name": "Шаблон v2",
                "description": "Описание шаблона",
                "message_text": "Привет, {{ first_name }}!",
                "is_active": "on",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        template_obj = MessageTemplate.objects.get(name="Шаблон v2")
        self.assertEqual(
            response.url,
            reverse("mailings_v2_templates_edit", kwargs={"pk": template_obj.id}),
        )

        detail_response = self.client.get(
            reverse("mailings_v2_templates_edit", kwargs={"pk": template_obj.id}),
            secure=True,
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Привет")
        self.assertContains(detail_response, "Создать кампанию по шаблону")
        self.assertContains(detail_response, "Проверка шаблона на госте")

    def test_flow_bridge_visible_only_on_campaigns_hub(self):
        """
        Блок маршрута маркетолога должен быть только на главном экране раздела рассылок.
        """
        hub_response = self.client.get(reverse("mailings_v2_campaigns"), secure=True)
        self.assertEqual(hub_response.status_code, 200)
        self.assertContains(hub_response, "Маршрут маркетолога")

        other_pages = [
            reverse("mailings_v2_templates"),
            reverse("mailings_v2_monitor"),
            reverse("mailings_v2_scenarios"),
        ]
        for url in other_pages:
            response = self.client.get(url, secure=True)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "Маршрут маркетолога")

    def test_template_preview_supports_django_and_format_placeholders(self):
        """
        Предпросмотр шаблона должен подставлять оба формата переменных:
        1. `{{ first_name }}`;
        2. `{days_without_visits}` и `{coupon_code}`.
        """
        template_obj = MessageTemplate.objects.create(
            name="SYSTEM_INACTIVE_30D_COUPON_TEMPLATE",
            description="Системный шаблон",
            message_text="Привет, {{ first_name }}. Вас не было {days_without_visits} дней. Купон: {coupon_code}",
            is_active=True,
        )
        guest = Guest.objects.create(
            phone="+79990000777",
            first_name="Мария",
            created_at=self.now,
            updated_at=self.now,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_preview_test",
            name="Preview test",
            template=template_obj,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
        )
        NotificationEvent.objects.create(
            scenario=scenario,
            guest=guest,
            source_type=NotificationEvent.SourceType.SCHEDULE,
            dedupe_key="preview-test-1",
            status=NotificationEvent.Status.NEW,
            event_at=self.now,
            planned_send_at=self.now,
            payload={"days_without_visits": 34},
            coupon_code="CPN-123",
        )

        response = self.client.post(
            reverse("mailings_v2_templates_edit", kwargs={"pk": template_obj.id}),
            {
                "action": "preview",
                "name": template_obj.name,
                "description": template_obj.description or "",
                "message_text": template_obj.message_text,
                "is_active": "on",
                "preview_guest_id": str(guest.id),
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Привет, Мария.")
        self.assertContains(response, "34 дней")
        self.assertContains(response, "CPN-123")
        self.assertContains(response, "Системный шаблон: неактивные 30 дней + купон")

    def test_monitor_filters_by_campaign_status_and_provider(self):
        """
        Фильтры monitor v2 должны корректно сужать выборку DispatchTask.
        """
        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000111",
            first_name="Петр",
            created_at=self.now,
            updated_at=self.now,
        )
        mailing_guest = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )
        scenario = NotificationScenario.objects.create(
            code="test_scenario_v2",
            name="Test scenario",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.MANUAL,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
        )

        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.FAILED,
            mailing_guest=mailing_guest,
            guest=guest,
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.VK,
            status=DispatchTask.Status.DONE,
            mailing_guest=mailing_guest,
            guest=guest,
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.PENDING,
            notification_scenario=scenario,
            guest=guest,
        )

        response = self.client.get(
            reverse("mailings_v2_monitor"),
            {
                "mailing_id": mailing.id,
                "status": DispatchTask.Status.FAILED,
                "provider_type": BotProfile.ProviderType.TELEGRAM,
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_tasks"], 1)
        self.assertEqual(response.context["failed_tasks"], 1)
        self.assertEqual(len(response.context["recent_rows"]), 1)

    def test_monitor_ops_retry_and_requeue_waiting(self):
        """
        Monitor v2 должен поддерживать быстрые операции:
        1. retry_failed_tasks: failed -> pending со сбросом attempt/last_error;
        2. requeue_waiting_tasks: pending/queued -> pending с очисткой очереди.
        """
        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000177",
            first_name="Нина",
            created_at=self.now,
            updated_at=self.now,
        )
        mailing_guest = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )

        failed_retryable = DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.FAILED,
            mailing_guest=mailing_guest,
            guest=guest,
            attempt=1,
            max_attempts=5,
            last_error="temporary timeout",
        )
        failed_exhausted = DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.FAILED,
            mailing_guest=mailing_guest,
            guest=guest,
            attempt=5,
            max_attempts=5,
            last_error="attempts exhausted",
        )
        pending_task = DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.VK,
            status=DispatchTask.Status.PENDING,
            mailing_guest=mailing_guest,
            guest=guest,
            attempt=2,
            max_attempts=5,
        )
        queued_task = DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.VK,
            status=DispatchTask.Status.QUEUED,
            mailing_guest=mailing_guest,
            guest=guest,
            attempt=3,
            max_attempts=5,
            queue_name="dispatch:vk:bulk",
            enqueued_at=self.now,
        )

        monitor_url = reverse("mailings_v2_monitor")
        report_query = f"mailing_id={mailing.id}"
        preview = self.client.get(
            monitor_url,
            {"mailing_id": mailing.id},
            secure=True,
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.context["retry_candidates"], 1)
        self.assertEqual(preview.context["retry_exhausted"], 1)
        self.assertEqual(preview.context["retry_in_queue"], 2)

        retry_response = self.client.post(
            monitor_url,
            {
                "action": "retry_failed_tasks",
                "return_query": report_query,
            },
            secure=True,
            follow=True,
        )
        self.assertEqual(retry_response.status_code, 200)

        failed_retryable.refresh_from_db()
        failed_exhausted.refresh_from_db()
        self.assertEqual(failed_retryable.status, DispatchTask.Status.PENDING)
        self.assertEqual(failed_retryable.attempt, 0)
        self.assertIsNone(failed_retryable.last_error)
        self.assertEqual(failed_exhausted.status, DispatchTask.Status.PENDING)
        self.assertEqual(failed_exhausted.attempt, 0)
        self.assertEqual(retry_response.context["monitor_ops_report"]["action"], "retry_failed_tasks")
        self.assertEqual(retry_response.context["monitor_ops_report"]["updated_tasks"], 2)

        requeue_response = self.client.post(
            monitor_url,
            {
                "action": "requeue_waiting_tasks",
                "return_query": report_query,
            },
            secure=True,
            follow=True,
        )
        self.assertEqual(requeue_response.status_code, 200)

        pending_task.refresh_from_db()
        queued_task.refresh_from_db()
        self.assertEqual(pending_task.status, DispatchTask.Status.PENDING)
        self.assertEqual(queued_task.status, DispatchTask.Status.PENDING)
        self.assertIsNone(queued_task.queue_name)
        self.assertIsNone(queued_task.enqueued_at)
        self.assertEqual(requeue_response.context["monitor_ops_report"]["action"], "requeue_waiting_tasks")

    def test_scenarios_hub_filters_and_manual_schedule_run(self):
        """
        Экран сценариев v2 должен:
        1. поддерживать фильтры списка;
        2. выполнять ручной one-shot запуск плановых сценариев;
        3. показывать отчет о последнем ручном запуске.
        """
        from guests.services.notification_registry import (
            SCENARIO_CODE_INACTIVE_30D_COUPON,
            SCENARIO_CODE_INACTIVE_7D,
        )

        schedule_scenario = NotificationScenario.objects.create(
            code=SCENARIO_CODE_INACTIVE_7D,
            name="Inactive 7d",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=True,
        )
        NotificationScenario.objects.create(
            code=SCENARIO_CODE_INACTIVE_30D_COUPON,
            name="Inactive 30d",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        manual_scenario = NotificationScenario.objects.create(
            code="manual_test_v2",
            name="Manual test",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.MANUAL,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=True,
        )

        guest = Guest.objects.create(
            phone="+79990000999",
            first_name="Тест",
            created_at=self.now,
            updated_at=self.now,
        )
        NotificationEvent.objects.create(
            scenario=schedule_scenario,
            guest=guest,
            source_type=NotificationEvent.SourceType.SCHEDULE,
            dedupe_key="sched-1",
            status=NotificationEvent.Status.ERROR,
            event_at=self.now,
            planned_send_at=self.now,
        )
        NotificationEvent.objects.create(
            scenario=manual_scenario,
            guest=guest,
            source_type=NotificationEvent.SourceType.MANUAL,
            dedupe_key="manual-1",
            status=NotificationEvent.Status.NEW,
            event_at=self.now,
            planned_send_at=self.now,
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.FAILED,
            notification_scenario=schedule_scenario,
            guest=guest,
        )

        filtered = self.client.get(
            reverse("mailings_v2_scenarios"),
            {
                "trigger_type": NotificationScenario.TriggerType.SCHEDULE,
                "with_errors": "1",
            },
            secure=True,
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertContains(filtered, SCENARIO_CODE_INACTIVE_7D)
        self.assertNotContains(filtered, "manual_test_v2")
        self.assertEqual(filtered.context["scenarios_total"], 1)

        with patch("guests.views_mailings_v2.run_registered_schedule_scenarios") as run_mock:
            run_mock.return_value = {
                SCENARIO_CODE_INACTIVE_7D: SimpleNamespace(
                    scanned_guests=25,
                    matched_guests=8,
                    created_tasks=3,
                    skipped_without_coupon=1,
                    skipped_duplicate_or_no_targets=4,
                )
            }
            run_response = self.client.post(
                reverse("mailings_v2_scenarios"),
                {
                    "action": "run_schedule_once",
                    "scenario_code": SCENARIO_CODE_INACTIVE_7D,
                    "limit_per_scenario": "42",
                    "return_query": "trigger_type=schedule&with_errors=1",
                },
                secure=True,
                follow=True,
            )

        self.assertEqual(run_response.status_code, 200)
        run_mock.assert_called_once_with(
            scenario_codes=[SCENARIO_CODE_INACTIVE_7D],
            limit_per_scenario=42,
        )
        self.assertIsNotNone(run_response.context["scenarios_run_report"])
        self.assertEqual(
            run_response.context["scenarios_run_report"]["total_created_tasks"],
            3,
        )
        self.assertContains(run_response, "Последний ручной запуск")
