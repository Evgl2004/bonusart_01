"""
Smoke-тесты нового контура mailings-v2.

Проверяем:
1. создание кампании через v2-форму;
2. доступность экрана редактирования и аудитории кампании;
3. корректный redirect после импорта телефонов при переданном `next`.
"""

from __future__ import annotations

from datetime import time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from guests.models import BotProfile, DispatchTask, Guest, Mailing, MailingGuest, MessageTemplate, NotificationScenario


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
        self.assertContains(edit_response, "Проверить аудиторию")

        audience_response = self.client.get(
            reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(audience_response.status_code, 200)
        self.assertContains(audience_response, guest.phone)

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

    def test_create_template_v2_and_open_detail(self):
        """
        Создание шаблона через v2 должно вести на v2-карточку шаблона.
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
            reverse("mailings_v2_templates_detail", kwargs={"pk": template_obj.id}),
        )

        detail_response = self.client.get(
            reverse("mailings_v2_templates_detail", kwargs={"pk": template_obj.id}),
            secure=True,
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Привет")

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
