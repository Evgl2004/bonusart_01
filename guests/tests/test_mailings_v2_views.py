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
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from guests.models import (
    BotProfile,
    CouponAutomationConfig,
    CouponAutomationRule,
    CouponAutoscenarioAssignment,
    CouponAutoscenarioRun,
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    DispatchTask,
    Guest,
    GuestBotBinding,
    Mailing,
    MailingGuest,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
    TerminalDepartmentMap,
    VtelemaxRecipientChannel,
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
        self.assertContains(new_page, "Параметры запуска кампании")

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

    def test_create_campaign_v2_uses_operator_friendly_defaults(self):
        """
        Новая кампания открывается с безопасными рабочими датами и окном отправки.
        """
        response = self.client.get(reverse("mailings_v2_campaigns_new"), secure=True)

        today = timezone.localdate()
        period_end = today + timedelta(days=14)
        form = response.context["form"]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(form.initial["scheduled_date"], today.isoformat())
        self.assertEqual(form.initial["scheduled_time_begin"], f"{today.isoformat()}T00:00")
        self.assertEqual(form.initial["scheduled_time_end"], f"{period_end.isoformat()}T23:59")
        self.assertEqual(form.initial["send_window_begin"], "09:00")
        self.assertEqual(form.initial["send_window_end"], "21:00")

    def test_campaign_form_exposes_template_texts_for_coupon_promo_autofill(self):
        """
        Форма кампании отдаёт тексты шаблонов для автозаполнения текста акции.
        """
        response = self.client.get(reverse("mailings_v2_campaigns_new"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["template_texts_by_id"][str(self.template.id)],
            self.template.message_text,
        )
        self.assertContains(response, "mail-template-texts")
        self.assertContains(response, "Открыть реестр купонов")
        self.assertContains(response, 'id="mail-template-edit"', html=False)
        self.assertContains(response, 'target="_blank"', html=False)
        self.assertContains(response, "syncTemplateEditButton")
        self.assertContains(response, "mailings-v2/templates/0/edit")
        self.assertContains(response, "templateSelect.addEventListener('change', function ()")
        self.assertContains(response, "couponSeriesSelect.addEventListener('change', function ()")
        self.assertContains(response, "couponModeEnabled")
        self.assertContains(response, "couponDetails.hidden = !couponModeEnabled()")
        self.assertContains(response, "Высокий — для срочных точечных сообщений")
        self.assertContains(response, 'rows="7"', html=False)

    def test_campaign_form_renders_bot_profiles_as_checkboxes(self):
        """
        Выбор ботов должен быть явным списком чекбоксов с быстрыми действиями.
        """
        response = self.client.get(reverse("mailings_v2_campaigns_new"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="mail-bot-select-all"', html=False)
        self.assertContains(response, 'id="mail-bot-clear-all"', html=False)
        self.assertContains(response, 'type="checkbox" name="bot_profiles"', html=False)
        self.assertContains(response, self.bot.name)
        self.assertNotContains(response, '<select name="bot_profiles"', html=False)
        self.assertContains(response, "flex-direction: column")

    def test_campaign_form_renders_coupon_venue_options(self):
        """
        Справочник заведений должен попадать не только в поле формы, но и в HTML select.
        """
        TerminalDepartmentMap.objects.create(
            terminal_group_id="terminal-sami",
            department_id="DEP_SAMI",
            department_name="Сами Сусами",
            is_active=True,
        )

        response = self.client.get(reverse("mailings_v2_campaigns_new"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<select name="coupon_venue_code"', html=False)
        self.assertContains(response, '<option value="DEP_SAMI">Сами Сусами (DEP_SAMI)</option>', html=False)

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
        self.assertContains(edit_response, "Параметры запуска кампании")

        audience_response = self.client.get(
            reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(audience_response.status_code, 200)
        self.assertContains(audience_response, guest.phone)

    def test_audience_page_shows_campaign_row_number_not_physical_id(self):
        """
        В таблице аудитории показываем понятный номер внутри кампании, а не PK общей таблицы.
        """
        other_mailing = self._create_mailing()
        other_guest = Guest.objects.create(
            phone="+79990000011",
            first_name="Пётр",
            created_at=self.now,
            updated_at=self.now,
        )
        MailingGuest.objects.create(
            mailing=other_mailing,
            guest=other_guest,
            phone=other_guest.phone,
            email="",
            text_mailing_list="Другая кампания",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )

        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000012",
            first_name="Анна",
            created_at=self.now,
            updated_at=self.now,
        )
        row = MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Тестовое сообщение",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )

        response = self.client.get(
            reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.id}),
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(row.id, 1)
        self.assertContains(response, "<th>№</th>", html=True)
        self.assertNotContains(response, "ID строки")
        self.assertContains(response, f'data-row-id="{row.id}"', html=False)
        self.assertContains(response, "<td>1</td>", html=True)

    def test_edit_page_shows_campaign_nav_without_wizard(self):
        """
        Экран параметров должен показывать каркас кампании без мастера.
        """
        mailing = self._create_mailing()
        response = self.client.get(
            reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Каркас кампании")
        self.assertContains(response, "Статус")
        self.assertNotContains(response, "Мастер запуска кампании")

    def test_campaign_views_show_workbench_snapshot(self):
        """
        Для кампании, созданной из Workbench, v2-экраны должны показывать сохранённый snapshot фильтров.
        """
        mailing = self._create_mailing()
        mailing.source_filter_snapshot = {
            "as_of_date": self.now.date().isoformat(),
            "window_days": "30",
            "department_id": "dep-1",
            "venue_selection_mode": "visited_once",
            "segment_code": "active_30d",
            "focus_category_code": "sushi_rolls",
            "audience_channel_group": "new_bots_sendable",
            "complex_filters": [
                {"field": "orders_count", "operator": "gte", "value": "2"},
            ],
            "selected_total": 10,
            "selected_rows_count": 10,
            "source_layer": "category_window",
            "saved_at": self.now.isoformat(),
        }
        mailing.save(update_fields=["source_filter_snapshot", "updated_at"])

        edit_response = self.client.get(
            reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "Параметры запуска кампании")

        audience_response = self.client.get(
            reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(audience_response.status_code, 200)
        self.assertContains(audience_response, "Аудитория собрана из экрана «Гости»")
        self.assertContains(audience_response, "active_30d")
        self.assertContains(audience_response, "Связь с заведением: Был хотя бы 1 раз")
        self.assertContains(audience_response, "Аудитория: Доступна рассылка в новых ботах")
        self.assertContains(audience_response, reverse("guests_workbench"))
        self.assertContains(audience_response, "venue_selection_mode=visited_once")
        self.assertContains(audience_response, "audience_channel_group=new_bots_sendable")

        status_response = self.client.get(
            reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertContains(status_response, "Источник аудитории")
        self.assertContains(status_response, "Открыть фильтры в экране «Гости»")

    def test_completed_campaign_shows_computed_status(self):
        """
        Кампания с полностью обработанной аудиторией должна отображаться как завершённая.
        """
        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000501",
            first_name="Завершён",
            created_at=self.now,
            updated_at=self.now,
        )
        MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now,
            status=MailingGuest.Status.DONE,
            delivery_status="done",
            sent_at=self.now,
            created_at=self.now,
        )

        status_response = self.client.get(
            reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertContains(status_response, "завершена")

        hub_response = self.client.get(reverse("mailings_v2_campaigns"), secure=True)
        self.assertEqual(hub_response.status_code, 200)
        self.assertContains(hub_response, "завершена")

    def test_future_active_campaign_shows_scheduled_status(self):
        """
        Включённая кампания с будущими строками должна отображаться как запланированная.
        """
        mailing = self._create_mailing()
        mailing.is_active = True
        mailing.scheduled_time_begin = self.now + timedelta(days=1)
        mailing.scheduled_time_end = self.now + timedelta(days=2)
        mailing.save(update_fields=["is_active", "scheduled_time_begin", "scheduled_time_end", "updated_at"])

        guest = Guest.objects.create(
            phone="+79990000502",
            first_name="Будущий",
            created_at=self.now,
            updated_at=self.now,
        )
        MailingGuest.objects.create(
            mailing=mailing,
            guest=guest,
            phone=guest.phone,
            email="",
            text_mailing_list="Текст",
            scheduled_datetime=self.now + timedelta(days=1),
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )

        status_response = self.client.get(
            reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}),
            secure=True,
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.context["mailing_ui_status"]["code"], "scheduled")
        self.assertContains(status_response, "запланирована")

        hub_response = self.client.get(reverse("mailings_v2_campaigns"), secure=True)
        self.assertEqual(hub_response.status_code, 200)
        self.assertEqual(hub_response.context["campaigns"][0].ui_status["code"], "scheduled")
        self.assertContains(hub_response, "запланирована")

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

    def test_campaign_ops_cancel_campaign_releases_reserved_coupons(self):
        """
        Safe cancel должен остановить кампанию и освободить reserved-купоны обратно в пул.
        """
        mailing = self._create_mailing()
        mailing.name = "Promo to cancel"
        mailing.is_active = True
        mailing.coupon_series = "TEST"
        mailing.coupon_venue_code = "DEP_1"
        mailing.coupon_venue_name = "Тестовое заведение"
        mailing.coupon_promo_text = "Скидка 20%"
        mailing.save(
            update_fields=[
                "name",
                "is_active",
                "coupon_series",
                "coupon_venue_code",
                "coupon_venue_name",
                "coupon_promo_text",
                "updated_at",
            ]
        )

        guest = Guest.objects.create(
            phone="+79990000663",
            first_name="Анна",
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
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.MAILING,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.PENDING,
            mailing_guest=row,
            guest=guest,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-CANCEL-VIEW-1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
            assigned_at=self.now,
        )
        assignment = CouponCampaignAssignment.objects.create(
            campaign=mailing,
            guest=guest,
            coupon=coupon,
            coupon_series="TEST",
            coupon_code="TST-CANCEL-VIEW-1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Скидка 20%",
            status=CouponCampaignAssignment.Status.RESERVED,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.OK,
            vtelemax_synced_at=self.now,
        )

        response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "cancel_campaign"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}))

        mailing.refresh_from_db()
        self.assertFalse(mailing.is_active)

        row.refresh_from_db()
        self.assertEqual(row.status, MailingGuest.Status.ERROR)
        self.assertEqual(row.delivery_status, "campaign_canceled")
        task = DispatchTask.objects.get(mailing_guest=row)
        self.assertEqual(task.status, DispatchTask.Status.CANCELED)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, CouponCampaignAssignment.Status.CANCELED)
        self.assertEqual(assignment.vtelemax_sync_status, CouponCampaignAssignment.VtelemaxSyncStatus.PENDING)
        assignment.coupon.refresh_from_db()
        self.assertFalse(assignment.coupon.is_active)
        self.assertEqual(assignment.coupon.pool_status, CouponRegistryEntry.PoolStatus.ASSIGNED)
        self.assertIsNotNone(assignment.coupon.assigned_at)

        self.assertEqual(
            CouponVtelemaxSyncQueue.objects.filter(
                assignment=assignment,
                direction=CouponVtelemaxSyncQueue.Direction.STATUS_UPDATE,
            ).count(),
            1,
        )

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
        self.assertEqual(toggle_response.url, reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}))
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
        self.assertEqual(response.url, reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}))

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
        self.assertEqual(response.url, reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}))

        report = self.client.session.get("mailing_ops_dry_run_report")
        self.assertIsInstance(report, dict)
        self.assertEqual(report.get("mailing_id"), mailing.id)
        self.assertEqual(report.get("ready_rows"), 1)
        self.assertEqual(report.get("ready_rows_with_targets"), 1)

    def test_campaign_ops_dry_run_counts_legacy_telegram_target(self):
        mailing = self._create_mailing()
        guest = Guest.objects.create(
            phone="+79990000886",
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
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid4(),
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            phone_e164=guest.phone,
            external_id="legacy-tg-dry-run-1",
            rules_accepted=True,
            notifications_allowed=True,
            is_registered=True,
            registered_at=self.now,
            guest=guest,
        )

        response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "dry_run_campaign"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        report = self.client.session.get("mailing_ops_dry_run_report")
        self.assertIsInstance(report, dict)
        self.assertEqual(report.get("ready_rows"), 1)
        self.assertEqual(report.get("ready_rows_with_targets"), 1)
        self.assertEqual(report.get("ready_rows_without_targets"), 0)

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
        self.assertEqual(response.url, reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.id}))

        row.refresh_from_db()
        self.assertEqual(row.status, MailingGuest.Status.DONE)
        self.assertEqual(row.delivery_status, "queued_to_dispatch")
        self.assertEqual(DispatchTask.objects.filter(mailing_guest=row).count(), 1)

        report = self.client.session.get("mailing_ops_run_now_report")
        self.assertIsInstance(report, dict)
        self.assertEqual(report.get("mailing_id"), mailing.id)
        self.assertEqual(report.get("processed_rows_total"), 1)
        self.assertEqual(report.get("processed_batches"), 1)

    def test_campaign_ops_run_now_coupon_mode_blocks_when_pool_is_empty(self):
        """
        В купонном режиме run-now должен блокироваться, если в серии нет доступных купонов.
        """
        mailing = self._create_mailing()
        mailing.coupon_series = "TEST"
        mailing.coupon_venue_code = "DEP_1"
        mailing.coupon_venue_name = "Тестовое заведение"
        mailing.coupon_promo_text = "Скидка по купону"
        mailing.send_window_begin = time(0, 0)
        mailing.send_window_end = time(23, 59)
        mailing.save(
            update_fields=[
                "coupon_series",
                "coupon_venue_code",
                "coupon_venue_name",
                "coupon_promo_text",
                "send_window_begin",
                "send_window_end",
                "updated_at",
            ]
        )

        guest = Guest.objects.create(
            phone="+79990000883",
            first_name="Олег",
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
            external_chat_id="tg-run-now-coupon-empty",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid4(),
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            phone_e164="+79990000883",
            external_id="chat-883",
            rules_accepted=True,
            notifications_allowed=True,
            is_registered=True,
            registered_at=self.now,
            effective_updated_at=self.now,
            guest=guest,
        )

        response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "run_now_campaign"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        row.refresh_from_db()
        self.assertEqual(row.status, MailingGuest.Status.ERROR)
        self.assertEqual(row.delivery_status, "coupon_sync_gate_blocked")
        self.assertEqual(DispatchTask.objects.filter(mailing_guest=row).count(), 0)
        self.assertEqual(
            CouponRegistryEntry.objects.filter(series="TEST", pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED).count(),
            0,
        )

    def test_campaign_ops_run_now_coupon_mode_soft_block_stays_planned_until_ack(self):
        """
        Если купон назначен, но sync-event ещё без ACK, строка не должна уходить в dispatch
        и остаётся в planned (мягкая блокировка до подтверждения).
        """
        mailing = self._create_mailing()
        mailing.coupon_series = "TEST"
        mailing.coupon_venue_code = "DEP_1"
        mailing.coupon_venue_name = "Тестовое заведение"
        mailing.coupon_promo_text = "Скидка по купону"
        mailing.send_window_begin = time(0, 0)
        mailing.send_window_end = time(23, 59)
        mailing.save(
            update_fields=[
                "coupon_series",
                "coupon_venue_code",
                "coupon_venue_name",
                "coupon_promo_text",
                "send_window_begin",
                "send_window_end",
                "updated_at",
            ]
        )

        guest = Guest.objects.create(
            phone="+79990000884",
            first_name="Иван",
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
            external_chat_id="tg-run-now-coupon-pending",
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid4(),
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            phone_e164="+79990000884",
            external_id="chat-884",
            rules_accepted=True,
            notifications_allowed=True,
            is_registered=True,
            registered_at=self.now,
            effective_updated_at=self.now,
            guest=guest,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="TEST",
            code="TST-PENDING-1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
        )
        assignment = CouponCampaignAssignment.objects.create(
            campaign=mailing,
            guest=guest,
            coupon=coupon,
            coupon_series="TEST",
            coupon_code="TST-PENDING-1",
            venue_code="DEP_1",
            venue_name="Тестовое заведение",
            promo_text="Скидка по купону",
            assigned_at=self.now,
            status=CouponCampaignAssignment.Status.RESERVED,
            vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.PENDING,
            vtelemax_synced_at=None,
        )
        CouponVtelemaxSyncQueue.objects.create(
            direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
            assignment=assignment,
            payload_json={"coupon_code": "TST-PENDING-1"},
            status=CouponVtelemaxSyncQueue.Status.PENDING,
            attempts=0,
            next_retry_at=self.now,
        )

        response = self.client.post(
            reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.id}),
            {"action": "run_now_campaign"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)

        row.refresh_from_db()
        self.assertEqual(row.status, MailingGuest.Status.PLANNED)
        self.assertEqual(row.delivery_status, "coupon_sync_gate_blocked")
        self.assertIn("ожидает обработки очередью", (row.error_description or "").lower())
        self.assertEqual(DispatchTask.objects.filter(mailing_guest=row).count(), 0)
        report = self.client.session.get("mailing_ops_run_now_report")
        self.assertIsInstance(report, dict)
        self.assertEqual(report.get("processed_rows_total"), 1)
        self.assertEqual(report.get("processed_batches"), 1)
        self.assertEqual(int(report.get("coupon_gate_blocked_rows") or 0), 1)
        self.assertFalse(bool(report.get("reached_batch_limit")))
        self.assertTrue(bool(report.get("stopped_on_coupon_sync_gate_wait")))
        self.assertTrue(bool(report.get("coupon_gate_blocked_reasons")))

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
        self.assertContains(response, "ID строки")
        self.assertContains(response, "ID задачи")
        self.assertContains(response, "Задача #")
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
        self.assertContains(response, "ID строки")
        self.assertContains(response, "ID задачи")
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
        self.assertContains(response, "ID строки")
        self.assertContains(response, "ID задачи")
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
        self.assertContains(response, "ID задачи")
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

    def test_create_template_v2_prefills_from_source_template(self):
        """
        Кнопка «создать на основе» должна открывать создание копии без изменения оригинала.
        """
        response = self.client.get(
            reverse("mailings_v2_templates_new"),
            {"source_template_id": self.template.id},
            secure=True,
        )

        form = response.context["form"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(form.initial["name"], "Тестовый шаблон (копия)")
        self.assertEqual(form.initial["description"], self.template.description)
        self.assertEqual(form.initial["message_text"], self.template.message_text)
        self.assertContains(response, "Исходный шаблон не будет изменён")

    def test_template_preview_on_create_without_save(self):
        """
        Предпросмотр на форме создания шаблона должен работать без сохранения.
        """
        guest = Guest.objects.create(
            phone="+79990000888",
            first_name="Ирина",
            created_at=self.now,
            updated_at=self.now,
        )

        response = self.client.post(
            reverse("mailings_v2_templates_new"),
            {
                "action": "preview",
                "name": "Черновой шаблон",
                "description": "Проверка без сохранения",
                "message_text": "Здравствуйте, {{ first_name }}!",
                "is_active": "on",
                "preview_guest_id": str(guest.id),
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Проверка шаблона на госте")
        self.assertContains(response, "Здравствуйте, Ирина!")
        self.assertFalse(MessageTemplate.objects.filter(name="Черновой шаблон").exists())

    def test_template_detail_shows_coupon_autoscenario_usage(self):
        """
        Карточка шаблона должна показывать, где он задействован в купонных автосценариях.
        """
        scenario = NotificationScenario.objects.create(
            code="template_usage_active_coupon",
            name="Шаблон используется в активном купонном сценарии",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.BULK,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            scenario_type=CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
            execution_mode=CouponAutomationConfig.ExecutionMode.AUTOMATIC,
        )

        response = self.client.get(
            reverse("mailings_v2_templates_detail", kwargs={"pk": self.template.id}),
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Использование в купонных автосценариях")
        self.assertContains(response, "Шаблон используется в активном купонном сценарии")
        self.assertContains(response, "template_usage_active_coupon")
        self.assertContains(response, "редактирование заблокировано")
        self.assertContains(
            response,
            reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk}),
        )

    def test_template_edit_locked_by_active_coupon_autoscenario_does_not_save(self):
        """
        Шаблон, используемый активным купонным автосценарием, нельзя изменить даже прямым POST.
        """
        scenario = NotificationScenario.objects.create(
            code="template_usage_lock_coupon",
            name="Активный купонный автосценарий",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.BULK,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
        )
        CouponAutomationConfig.objects.create(
            scenario=scenario,
            scenario_type=CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
        )

        edit_response = self.client.get(
            reverse("mailings_v2_templates_edit", kwargs={"pk": self.template.id}),
            secure=True,
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "Редактирование шаблона заблокировано")
        self.assertContains(edit_response, "readonly")
        self.assertNotContains(
            edit_response,
            '<button type="submit" class="btn btn-primary">Сохранить</button>',
            html=False,
        )

        response = self.client.post(
            reverse("mailings_v2_templates_edit", kwargs={"pk": self.template.id}),
            {
                "name": "Изменённое имя",
                "description": "Изменённое описание",
                "message_text": "Нельзя сохранить этот текст",
                "is_active": "on",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("mailings_v2_templates_detail", kwargs={"pk": self.template.id}),
        )
        self.template.refresh_from_db()
        self.assertEqual(self.template.name, "Тестовый шаблон")
        self.assertEqual(self.template.message_text, "Привет, {{ first_name }}")

    def test_template_edit_allows_report_only_coupon_autoscenario_template(self):
        """
        Черновой купонный автосценарий не должен блокировать правку шаблона.
        """
        scenario = NotificationScenario.objects.create(
            code="template_usage_draft_coupon",
            name="Черновой купонный автосценарий",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.BULK,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
        )
        CouponAutomationConfig.objects.create(
            scenario=scenario,
            scenario_type=CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
        )

        response = self.client.post(
            reverse("mailings_v2_templates_edit", kwargs={"pk": self.template.id}),
            {
                "name": "Разрешённое имя",
                "description": "Разрешённое описание",
                "message_text": "Разрешённый текст {coupon_code}",
                "is_active": "on",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.template.refresh_from_db()
        self.assertEqual(self.template.name, "Разрешённое имя")
        self.assertEqual(self.template.message_text, "Разрешённый текст {coupon_code}")

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

    def test_scenarios_hub_coupon_autoscenario_plan_preview(self):
        """
        Экран автосценариев показывает купонные настройки и строит только безопасный план.
        """
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon",
            name="Остывшие 30 дней",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            coupon_series="AUTO_30D",
            venue_code="DEP_1",
            venue_name="Сами Сусами",
            audience_venue_filter_mode=(
                CouponAutomationConfig.AudienceVenueFilterMode.VISITED_ONCE_AND_INACTIVE
            ),
            audience_venue_code="DEP_1",
            audience_venue_name="Сами Сусами",
            coupon_validity_days=14,
            max_recipients_per_run=1,
            cooldown_days=30,
        )

        with patch("guests.views_mailings_v2.build_coupon_autoscenario_execution_plan") as plan_mock:
            plan_mock.return_value = SimpleNamespace(
                as_dict=lambda: {
                    "scenario_code": scenario.code,
                    "execution_mode": CouponAutomationConfig.ExecutionMode.PILOT,
                    "can_execute": True,
                    "coupon_series": "AUTO_30D",
                    "venue_code": "DEP_1",
                    "venue_name": "Сами Сусами",
                    "audience_venue_filter_mode": (
                        CouponAutomationConfig.AudienceVenueFilterMode.VISITED_ONCE_AND_INACTIVE
                    ),
                    "audience_venue_code": "DEP_1",
                    "audience_venue_name": "Сами Сусами",
                    "inactive_days_threshold": 30,
                    "scanned_guests": 5000,
                    "segment_matched_guests": 99,
                    "matched_guests": 100,
                    "bot_bound_guests": 12,
                    "blocked_without_bot_binding": 88,
                    "sendable_guests": 10,
                    "blocked_without_channel": 90,
                    "message_target_guests": 8,
                    "blocked_without_message_target": 2,
                    "blocked_without_message_permission": 4,
                    "blocked_existing_active_coupon": 0,
                    "blocked_by_cooldown": 0,
                    "blocked_by_pilot_filter": 9,
                    "pilot_phone_filters": ["+79129923438"],
                    "pilot_guest_id_filters": [],
                    "used_default_pilot_phone": False,
                    "pilot_forced_guests": 1,
                    "eligible_guests": 1,
                    "planned_assignments": 1,
                    "available_coupons": 1,
                    "coupon_shortage": 0,
                    "blockers": [],
                    "warnings": ["Пилотный режим."],
                    "plan_items": [
                        {
                            "guest_id": 133569,
                            "phone": "+79129923438",
                            "first_name": "Андрей",
                            "last_name": "",
                            "sendable_channels": ["telegram"],
                            "coupon_series": "AUTO_30D",
                            "coupon_code": "REL-1",
                            "last_visit_at": "2026-05-01T00:00:00+00:00",
                            "last_visit_at_display": "01.05.2026 05:00",
                            "days_without_visits": 30,
                            "days_without_visits_label": "30 (пилотное значение)",
                            "is_pilot_forced": True,
                            "valid_until": "2026-06-22T00:00:00+00:00",
                            "valid_until_display": "22.06.2026 05:00",
                        }
                    ],
                }
            )
            response = self.client.get(
                reverse("mailings_v2_scenarios"),
                {
                    "coupon_scenario_code": scenario.code,
                    "coupon_check": "1",
                    "coupon_scan_limit": "5000",
                    "coupon_sample_limit": "5",
                },
                secure=True,
            )

        self.assertEqual(response.status_code, 200)
        plan_mock.assert_called_once_with(scenario_code=scenario.code, scan_limit=5000)
        self.assertContains(response, "Купонный автосценарий")
        self.assertContains(response, "Проверить расчёт")
        self.assertContains(response, "AUTO_30D")
        self.assertContains(response, "REL-1")
        self.assertContains(response, "Состояние")
        self.assertContains(response, "Пилот")
        self.assertContains(response, "Как выбирается купон")
        self.assertContains(response, "последнее заведение гостя")
        self.assertContains(response, "Вся сеть")
        self.assertContains(response, "Источник последнего заведения")
        self.assertContains(response, "история заказов")
        self.assertContains(response, "Гостей в новых ботах")
        self.assertContains(response, "С согласием на рассылку")
        self.assertContains(response, "можно отправить сообщение")
        self.assertContains(response, "Нет привязки к новым ботам")
        self.assertContains(response, "Нет согласия на рассылку")
        self.assertContains(response, "Как получены эти числа")
        self.assertContains(response, "последний заказ был")
        self.assertContains(
            response,
            "Отбор гостей: был в заведении «Сами Сусами» хотя бы 1 раз и не был там 30+ дней.",
        )
        self.assertContains(response, "Добавлено вне основного сегмента: 1")
        self.assertContains(response, "К выдаче сейчас")
        self.assertContains(response, "30 (пилотное значение)")
        self.assertContains(response, "контрольный гость пилота")
        self.assertContains(response, "22.06.2026 05:00")
        self.assertEqual(response.context["coupon_plan"]["planned_assignments"], 1)
        self.assertEqual(response.context["coupon_plan"]["sample_plan_items"][0]["days_without_visits"], 30)

    def test_scenarios_hub_birthday_plan_separates_database_count_from_recipients(self):
        scenario = NotificationScenario.objects.create(
            code="birthday_coupon",
            name="День рождения + купон",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="BDAY",
            venue_code="__global__",
            venue_name="Вся сеть",
            coupon_validity_days=14,
            max_recipients_per_run=100,
            cooldown_days=365,
            settings={"birthday_preparation_window_days": 7},
        )

        with patch("guests.views_mailings_v2.build_coupon_autoscenario_execution_plan") as plan_mock:
            plan_mock.return_value = SimpleNamespace(
                as_dict=lambda: {
                    "scenario_code": scenario.code,
                    "execution_mode": CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
                    "can_execute": False,
                    "coupon_series": "BDAY",
                    "venue_code": "__global__",
                    "venue_name": "Вся сеть",
                    "inactive_days_threshold": 0,
                    "birthday_preparation_window_days": 7,
                    "scanned_guests": 1711,
                    "segment_matched_guests": 1711,
                    "matched_guests": 1711,
                    "bot_bound_guests": 24,
                    "blocked_without_bot_binding": 1687,
                    "sendable_guests": 24,
                    "blocked_without_channel": 1687,
                    "message_target_guests": 24,
                    "blocked_without_message_target": 0,
                    "blocked_without_message_permission": 0,
                    "blocked_existing_active_coupon": 0,
                    "blocked_existing_trigger": 0,
                    "blocked_by_cooldown": 0,
                    "blocked_by_pilot_filter": 0,
                    "pilot_phone_filters": [],
                    "pilot_guest_id_filters": [],
                    "used_default_pilot_phone": False,
                    "pilot_forced_guests": 0,
                    "eligible_guests": 24,
                    "planned_assignments": 0,
                    "available_coupons": 0,
                    "coupon_shortage": 24,
                    "blockers": ["Черновик."],
                    "warnings": [],
                    "plan_items": [],
                }
            )
            response = self.client.get(
                reverse("mailings_v2_scenarios"),
                {
                    "coupon_check": "1",
                    "coupon_scenario_code": scenario.code,
                    "coupon_scan_limit": "5000",
                },
                secure=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Именинников в новых ботах")
        self.assertContains(response, "день рождения в периоде")
        self.assertContains(response, "С согласием на рассылку")
        self.assertContains(response, "можно отправить сообщение")
        self.assertNotContains(response, "Всего с днём рождения в окне")
        self.assertNotContains(response, "вся гостевая база")
        self.assertNotContains(response, "В базе с днём рождения в окне")
        self.assertNotContains(response, "После проверки канала")
        self.assertNotContains(response, "1711")
        self.assertEqual(response.context["coupon_plan"]["message_target_guests"], 24)

    def test_scenarios_hub_runs_coupon_autoscenario_pilot(self):
        """
        Пробный запуск из UI вызывает только защищённый executor автосценария.
        """
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon",
            name="Остывшие 30 дней",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            coupon_series="AUTO_30D",
            venue_code="DEP_1",
            venue_name="Сами Сусами",
            coupon_validity_days=14,
            max_recipients_per_run=1,
            cooldown_days=30,
        )

        with patch("guests.views_mailings_v2.execute_coupon_autoscenario_pilot") as execute_mock:
            execute_mock.return_value = SimpleNamespace(
                run_id=7,
                created_assignments=1,
                queue_events_created=1,
                plan=SimpleNamespace(
                    scenario_code=scenario.code,
                    planned_assignments=1,
                    coupon_series="AUTO_30D",
                    venue_name="Сами Сусами",
                ),
            )
            response = self.client.post(
                reverse("mailings_v2_scenarios"),
                {
                    "action": "run_coupon_pilot",
                    "scenario_code": scenario.code,
                    "coupon_scan_limit": "5000",
                },
                secure=True,
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        execute_mock.assert_called_once_with(
            scenario_code=scenario.code,
            scan_limit=5000,
            confirm=True,
        )
        self.assertContains(response, "Пробный запуск создан")
        self.assertContains(response, "номер запуска")
        self.assertEqual(response.context["coupon_pilot_report"]["run_id"], 7)

    def test_scenarios_hub_cleans_coupon_autoscenario_pilot(self):
        """
        Очистка пилота из UI вызывает защищённый cleanup-сервис.
        """
        with patch("guests.views_mailings_v2.cleanup_coupon_autoscenario_pilot_assignment") as cleanup_mock:
            cleanup_mock.return_value = SimpleNamespace(
                assignment_id=15,
                queue_event_id=21,
                queue_event_created=True,
                coupon_series="AUTO_30D",
                coupon_code="REL-1",
            )
            response = self.client.post(
                reverse("mailings_v2_scenarios"),
                {
                    "action": "cleanup_coupon_pilot",
                    "assignment_id": "15",
                },
                secure=True,
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        cleanup_mock.assert_called_once_with(
            assignment_id=15,
            reason="pilot_cleanup_from_ui",
        )
        self.assertContains(response, "Отмена пилотного купона поставлена в очередь")
        self.assertContains(response, "AUTO_30D:REL-1")
        self.assertEqual(response.context["coupon_cleanup_report"]["queue_event_id"], 21)

    def test_coupon_autoscenario_create_view_creates_user_draft(self):
        """
        Создание купонного автосценария из UI должно завести пользовательский сценарий,
        новый шаблон и черновой CouponAutomationConfig без выдачи купонов.
        """
        hub_response = self.client.get(reverse("mailings_v2_scenarios"), secure=True)
        self.assertEqual(hub_response.status_code, 200)
        self.assertContains(hub_response, "Создать автосценарий")

        response = self.client.get(reverse("mailings_v2_coupon_autoscenario_create"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Код автосценария")
        self.assertContains(response, "Тип расчёта")
        self.assertContains(response, "Использовать существующий шаблон")
        self.assertContains(response, "Разрешённые боты")

        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_create"),
            {
                "code": "SAMI_SUSAMI_KANPETI_30D",
                "name": "Сами Сусами: не был 30 дней + Канпети",
                "scenario_type": CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
                "inactive_days": "30",
                "birthday_preparation_window_days": "",
                "template_name": "Сами Сусами: Канпети для остывших гостей",
                "template_description": "Боевой шаблон для автосценария Сами Сусами.",
                "template_text": "Гамарджоба, {{ first_name }}! Купон: {coupon_code}",
                "notification_bot_profiles": [str(self.bot.id)],
            },
            secure=True,
        )

        scenario = NotificationScenario.objects.get(code="sami_susami_kanpeti_30d")
        config = scenario.coupon_automation_config
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk}),
        )
        self.assertFalse(scenario.is_active)
        self.assertFalse(scenario.is_system)
        self.assertEqual(scenario.trigger_type, NotificationScenario.TriggerType.SCHEDULE)
        self.assertEqual(scenario.priority, NotificationScenario.Priority.BULK)
        self.assertEqual(scenario.settings["inactive_days"], 30)
        self.assertTrue(scenario.settings["coupon_required"])
        self.assertEqual(scenario.template.name, "Сами Сусами: Канпети для остывших гостей")
        self.assertEqual(scenario.template.created_by, "mailings_v2_user")
        self.assertIn("Создано для купонного автосценария: sami_susami_kanpeti_30d", scenario.template.description)
        self.assertEqual(list(scenario.bot_profiles.values_list("id", flat=True)), [self.bot.id])
        self.assertEqual(
            config.scenario_type,
            CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
        )
        self.assertEqual(config.execution_mode, CouponAutomationConfig.ExecutionMode.REPORT_ONLY)
        self.assertEqual(config.cooldown_days, 30)
        self.assertEqual(config.settings, {})
        self.assertEqual(DispatchTask.objects.count(), 0)
        self.assertEqual(NotificationEvent.objects.count(), 0)

    def test_coupon_autoscenario_create_view_uses_existing_template(self):
        """
        Оператор может привязать активный существующий шаблон без создания дубля.
        """
        existing_template = MessageTemplate.objects.create(
            name="Готовый шаблон купона",
            description="Уже согласованный текст.",
            message_text="Привет, {{ first_name }}! Ваш купон: {coupon_code}",
            created_by="operator",
            is_active=True,
        )
        templates_before = MessageTemplate.objects.count()

        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_create"),
            {
                "code": "existing_template_coupon",
                "name": "Сценарий с готовым шаблоном",
                "scenario_type": CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
                "inactive_days": "30",
                "birthday_preparation_window_days": "",
                "template_mode": "existing",
                "existing_template": str(existing_template.id),
                "template_name": "",
                "template_description": "",
                "template_text": "",
                "notification_bot_profiles": [str(self.bot.id)],
            },
            secure=True,
        )

        scenario = NotificationScenario.objects.get(code="existing_template_coupon")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(scenario.template_id, existing_template.id)
        self.assertEqual(MessageTemplate.objects.count(), templates_before)

    def test_coupon_autoscenario_create_view_exposes_existing_template_preview_actions(self):
        """
        При выборе существующего шаблона оператор должен видеть текст, предпросмотр и безопасные действия.
        """
        existing_template = MessageTemplate.objects.create(
            name="Готовый шаблон предпросмотра",
            description="Согласованный текст для купона.",
            message_text="Привет, {{ first_name }}! Купон: {coupon_code}",
            created_by="operator",
            is_active=True,
        )

        response = self.client.get(
            reverse("mailings_v2_coupon_autoscenario_create"),
            secure=True,
        )

        payload = response.context["existing_template_payload"]
        valid_payload = payload[str(existing_template.id)]
        invalid_payload = payload[str(self.template.id)]
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "coupon-existing-template-card")
        self.assertContains(response, "Редактировать шаблон")
        self.assertContains(response, "Создать на основе")
        self.assertContains(response, "coupon-existing-template-data")
        self.assertTrue(valid_payload["has_coupon_code"])
        self.assertIn("TEST123", valid_payload["preview_text"])
        self.assertEqual(
            valid_payload["edit_url"],
            reverse("mailings_v2_templates_edit", kwargs={"pk": existing_template.id}),
        )
        self.assertFalse(invalid_payload["has_coupon_code"])
        self.assertIn("должен быть параметр", invalid_payload["coupon_code_status"])

    def test_coupon_autoscenario_create_view_requires_coupon_code_in_new_template(self):
        """
        Новый купонный автосценарий нельзя создать с шаблоном без {coupon_code}.
        """
        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_create"),
            {
                "code": "missing_coupon_code",
                "name": "Без кода купона",
                "scenario_type": CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
                "inactive_days": "30",
                "birthday_preparation_window_days": "",
                "template_name": "Шаблон без купона",
                "template_description": "",
                "template_text": "Привет, {{ first_name }}!",
                "notification_bot_profiles": [str(self.bot.id)],
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "должен быть параметр")
        self.assertFalse(NotificationScenario.objects.filter(code="missing_coupon_code").exists())

    def test_coupon_autoscenario_create_view_rejects_wrong_coupon_placeholder(self):
        """
        Похожие, но неверные варианты плейсхолдера не должны проходить валидацию.
        """
        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_create"),
            {
                "code": "wrong_coupon_placeholder",
                "name": "Неверный код купона",
                "scenario_type": CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
                "inactive_days": "30",
                "birthday_preparation_window_days": "",
                "template_name": "Шаблон с неверным купоном",
                "template_description": "",
                "template_text": "Привет, {{ first_name }}! Купон: {{ coupon_code }}",
                "notification_bot_profiles": [str(self.bot.id)],
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Найден похожий, но неверный вариант")
        self.assertFalse(NotificationScenario.objects.filter(code="wrong_coupon_placeholder").exists())

    def test_coupon_autoscenario_create_view_rejects_existing_template_without_coupon_code(self):
        """
        Существующий шаблон тоже обязан содержать точный {coupon_code}.
        """
        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_create"),
            {
                "code": "existing_template_without_coupon",
                "name": "Готовый шаблон без купона",
                "scenario_type": CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON,
                "inactive_days": "30",
                "birthday_preparation_window_days": "",
                "template_mode": "existing",
                "existing_template": str(self.template.id),
                "template_name": "",
                "template_description": "",
                "template_text": "",
                "notification_bot_profiles": [str(self.bot.id)],
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "должен быть параметр")
        self.assertFalse(NotificationScenario.objects.filter(code="existing_template_without_coupon").exists())

    def test_coupon_autoscenario_create_view_stores_birthday_window_by_type(self):
        """
        Пользовательский код birthday-сценария должен брать настройки по scenario_type,
        а не по системному коду birthday_coupon.
        """
        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_create"),
            {
                "code": "custom_birthday_coupon_2026",
                "name": "День рождения: пользовательский купон",
                "scenario_type": CouponAutomationConfig.ScenarioType.BIRTHDAY_COUPON,
                "inactive_days": "",
                "birthday_preparation_window_days": "10",
                "template_name": "Пользовательский день рождения",
                "template_description": "",
                "template_text": "Поздравляем, {{ first_name }}! Купон: {coupon_code}",
                "notification_bot_profiles": [str(self.bot.id)],
            },
            secure=True,
        )

        scenario = NotificationScenario.objects.get(code="custom_birthday_coupon_2026")
        config = scenario.coupon_automation_config
        self.assertEqual(response.status_code, 302)
        self.assertEqual(config.scenario_type, CouponAutomationConfig.ScenarioType.BIRTHDAY_COUPON)
        self.assertEqual(config.execution_mode, CouponAutomationConfig.ExecutionMode.REPORT_ONLY)
        self.assertEqual(config.cooldown_days, 365)
        self.assertEqual(config.settings["birthday_preparation_window_days"], 10)
        self.assertTrue(scenario.settings["coupon_required"])
        self.assertNotIn("inactive_days", scenario.settings)

        settings_response = self.client.get(
            reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk}),
            secure=True,
        )
        self.assertEqual(settings_response.status_code, 200)
        self.assertContains(settings_response, "Окно подготовки ко дню рождения")
        self.assertContains(settings_response, "сегодня + 10 дн. включительно")

    def test_coupon_autoscenario_control_view_shows_panel_and_history(self):
        """
        Пульт купонного автосценария показывает состояние, действия, готовность и историю запусков.
        """
        coupon_template = MessageTemplate.objects.create(
            name="Шаблон пульта",
            message_text="Привет! Купон: {coupon_code}",
            is_active=True,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon",
            name="Остывшие 30 дней",
            template=coupon_template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        scenario.bot_profiles.add(self.bot)
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="AUTO_30D",
            venue_code="DEP_1",
            venue_name="Сами Сусами",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=30,
        )
        run = CouponAutoscenarioRun.objects.create(
            scenario=scenario,
            config=config,
            execution_mode=config.execution_mode,
            matched_guests=3,
            created_assignments=1,
        )
        guest = Guest.objects.create(
            phone="+79990002635",
            first_name="Пётр",
            created_at=self.now,
            updated_at=self.now,
        )
        coupon = CouponRegistryEntry.objects.create(
            series="AUTO_30D",
            code="AUTO-CONTROL-1",
            venue_code="DEP_1",
            venue_name="Сами Сусами",
            source=CouponRegistryEntry.SourceType.GENERATED,
            is_active=False,
            pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED,
            assigned_at=self.now,
        )
        CouponAutoscenarioAssignment.objects.create(
            run=run,
            scenario=scenario,
            config=config,
            guest=guest,
            coupon=coupon,
            phone_e164=guest.phone,
            coupon_series="AUTO_30D",
            coupon_code="AUTO-CONTROL-1",
            venue_code="DEP_1",
            venue_name="Сами Сусами",
            status=CouponAutoscenarioAssignment.Status.RESERVED,
            vtelemax_sync_status=CouponAutoscenarioAssignment.VtelemaxSyncStatus.PENDING,
        )
        event = NotificationEvent.objects.create(
            scenario=scenario,
            guest=guest,
            source_type=NotificationEvent.SourceType.SCHEDULE,
            dedupe_key="control-diagnostic-event",
            status=NotificationEvent.Status.ERROR,
            error_text="Тестовая ошибка события",
        )
        DispatchTask.objects.create(
            source_type=DispatchTask.SourceType.SYSTEM,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=DispatchTask.Status.FAILED,
            notification_scenario=scenario,
            notification_event=event,
            guest=guest,
            message_text="Тестовая доставка",
        )

        response = self.client.get(
            reverse("mailings_v2_coupon_autoscenario_control", kwargs={"pk": config.pk}),
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Пульт управления автосценарием")
        self.assertContains(response, "Маршруты оператора")
        self.assertContains(response, "#settings-state")
        self.assertContains(response, "#settings-messages")
        self.assertContains(response, "#settings-coupons")
        self.assertContains(response, "#settings-pilot")
        self.assertContains(response, "#settings-advanced")
        self.assertContains(response, "Расчёт аудитории")
        self.assertContains(response, "Отчёт запусков")
        self.assertContains(response, "Мастер запуска")
        self.assertContains(response, "Шаг 1")
        self.assertContains(response, "Проверить основу")
        self.assertContains(response, "Проверить расчёт")
        self.assertContains(response, "Ожидает проверки")
        self.assertContains(response, "Настроить пилот")
        self.assertContains(response, "Нужно включить пилот")
        self.assertContains(response, "Боевой режим")
        self.assertContains(response, "После пилота")
        self.assertContains(response, "Центр управления")
        self.assertContains(response, "Структурная готовность")
        self.assertContains(response, "Настроить")
        self.assertContains(response, "Основной купонный автосценарий")
        self.assertContains(response, "Настроить этап")
        self.assertContains(response, "Диагностика результата")
        self.assertContains(response, "Технические запуски")
        self.assertContains(response, "завершено: 0, ожидает vtelemax: 0, ошибок: 0")
        self.assertContains(response, "Назначения купонов")
        self.assertContains(response, "отправлено: 0, использовано: 0, резерв: 1")
        self.assertContains(response, "События уведомлений")
        self.assertContains(response, "задач создано: 0, пропущено: 0, ошибок: 1")
        self.assertContains(response, "Задачи доставки")
        self.assertContains(response, "ожидает: 0, в очереди: 0, доставлено: 0")
        self.assertContains(response, "ошибки")
        self.assertContains(response, "Последние запуски")
        self.assertContains(response, f"#{run.id}")
        self.assertContains(response, "Проверить готовность")
        self.assertContains(response, "Планировщик уведомлений")
        self.assertNotContains(response, "#settings-chain")

    def test_coupon_autoscenario_control_view_builds_readiness_plan_on_request(self):
        """
        Проверка готовности в пульте использует существующий сервис построения плана без запуска.
        """
        coupon_template = MessageTemplate.objects.create(
            name="Шаблон проверки пульта",
            message_text="Купон: {coupon_code}",
            is_active=True,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon",
            name="Остывшие 30 дней",
            template=coupon_template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=True,
        )
        scenario.bot_profiles.add(self.bot)
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            coupon_series="AUTO_30D",
            venue_code="DEP_1",
            venue_name="Сами Сусами",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=30,
        )

        with patch("guests.views_mailings_v2.build_coupon_autoscenario_execution_plan") as plan_mock:
            plan_mock.return_value = SimpleNamespace(
                as_dict=lambda: {
                    "scenario_code": scenario.code,
                    "execution_mode": CouponAutomationConfig.ExecutionMode.PILOT,
                    "can_execute": True,
                    "coupon_series": "AUTO_30D",
                    "scanned_guests": 20,
                    "matched_guests": 5,
                    "planned_assignments": 2,
                    "coupon_shortage": 0,
                    "blockers": [],
                }
            )
            response = self.client.get(
                reverse("mailings_v2_coupon_autoscenario_control", kwargs={"pk": config.pk}),
                {"check": "1"},
                secure=True,
            )

        self.assertEqual(response.status_code, 200)
        plan_mock.assert_called_once_with(scenario_code=scenario.code, scan_limit=5000)
        self.assertContains(response, "можно выполнить")
        self.assertContains(response, "Мастер запуска")
        self.assertContains(response, "План можно выполнить: к выдаче 2")
        self.assertContains(response, "Готов к пилоту")
        self.assertContains(response, 'value="run_pilot"')
        self.assertContains(response, "К выдаче")
        self.assertContains(response, "2")

    def test_coupon_autoscenario_control_view_shows_fill_birthday_chain(self):
        """
        Составной сценарий заполнения даты рождения должен отображаться как цепочка этапов.
        """
        from guests.services.notification_registry import (
            SCENARIO_CODE_FILL_BIRTHDAY_COUPON,
            SCENARIO_CODE_FILL_BIRTHDAY_REQUEST,
        )

        request_template = MessageTemplate.objects.create(
            name="Шаблон просьбы заполнить дату рождения",
            message_text="Заполните дату рождения",
            is_active=True,
        )
        request_scenario = NotificationScenario.objects.create(
            code=SCENARIO_CODE_FILL_BIRTHDAY_REQUEST,
            name="Заполнить дату рождения",
            template=request_template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.BULK,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=True,
        )
        request_scenario.bot_profiles.add(self.bot)
        coupon_template = MessageTemplate.objects.create(
            name="Шаблон купона за дату рождения",
            message_text="Спасибо! Купон: {coupon_code}",
            is_active=True,
        )
        coupon_scenario = NotificationScenario.objects.create(
            code=SCENARIO_CODE_FILL_BIRTHDAY_COUPON,
            name="Заполнил дату рождения + купон",
            template=coupon_template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.BULK,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        coupon_scenario.bot_profiles.add(self.bot)
        config = CouponAutomationConfig.objects.create(
            scenario=coupon_scenario,
            scenario_type=CouponAutomationConfig.ScenarioType.BIRTHDATE_FILLED_COUPON,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="BIRTHDAY_FILLED",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=365,
        )

        response = self.client.get(
            reverse("mailings_v2_coupon_autoscenario_control", kwargs={"pk": config.pk}),
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Просьба заполнить дату рождения")
        self.assertContains(response, "Купон после заполнения даты рождения")
        self.assertContains(response, SCENARIO_CODE_FILL_BIRTHDAY_REQUEST)
        self.assertContains(response, SCENARIO_CODE_FILL_BIRTHDAY_COUPON)
        self.assertContains(response, "#settings-chain")
        self.assertContains(response, "Цепочка")
        self.assertContains(response, "Настроить этап")

    def test_coupon_autoscenario_control_rejects_unknown_action(self):
        """
        Неизвестное действие пульта не должно менять состояние автосценария.
        """
        coupon_template = MessageTemplate.objects.create(
            name="Шаблон неизвестного действия",
            message_text="Купон: {coupon_code}",
            is_active=True,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon",
            name="Остывшие 30 дней",
            template=coupon_template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="AUTO_30D",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=30,
        )

        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_control", kwargs={"pk": config.pk}),
            {"action": "unknown_control_action"},
            secure=True,
            follow=True,
        )

        scenario.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(scenario.is_active)
        self.assertEqual(config.execution_mode, CouponAutomationConfig.ExecutionMode.REPORT_ONLY)
        self.assertContains(response, "Неизвестное действие пульта автосценария")

    def test_coupon_autoscenario_control_shows_pilot_error_without_crash(self):
        """
        Ошибка сервиса пилота должна показываться оператору без падения страницы.
        """
        from guests.services.coupon_autoscenarios import CouponAutoscenarioPreviewError

        coupon_template = MessageTemplate.objects.create(
            name="Шаблон ошибки пилота",
            message_text="Купон: {coupon_code}",
            is_active=True,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon",
            name="Остывшие 30 дней",
            template=coupon_template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        scenario.bot_profiles.add(self.bot)
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.PILOT,
            coupon_series="AUTO_30D",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=30,
        )

        with patch(
            "guests.views_mailings_v2.execute_coupon_autoscenario_pilot",
            side_effect=CouponAutoscenarioPreviewError("Нельзя выполнить пробный запуск"),
        ) as execute_mock:
            response = self.client.post(
                reverse("mailings_v2_coupon_autoscenario_control", kwargs={"pk": config.pk}),
                {"action": "run_pilot"},
                secure=True,
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        execute_mock.assert_called_once_with(
            scenario_code=scenario.code,
            scan_limit=5000,
            confirm=True,
        )
        self.assertContains(response, "Нельзя выполнить пробный запуск")

    def test_coupon_autoscenario_settings_view_updates_safe_pilot_fields(self):
        """
        Отдельная страница настроек сохраняет правила пилота без запуска отправок.
        """
        coupon_template = MessageTemplate.objects.create(
            name="Купонный шаблон пилота",
            message_text="Привет, {{ first_name }}! Купон: {coupon_code}",
            is_active=True,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon",
            name="Остывшие 30 дней",
            template=coupon_template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="AUTO_30D",
            venue_code="DEP_1",
            venue_name="Сами Сусами",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=30,
            settings={},
        )
        TerminalDepartmentMap.objects.create(
            terminal_group_id="terminal-dep-1",
            department_id="DEP_1",
            department_name="Сами Сусами",
            is_active=True,
        )

        url = reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk})
        response = self.client.get(url, secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Настройки купонного автосценария")
        self.assertContains(response, "Состояние автосценария")
        self.assertContains(response, "Черновик")
        self.assertContains(response, "Разделы настроек автосценария")
        self.assertContains(response, 'data-settings-tab-target="state"')
        self.assertContains(response, 'data-settings-tab-target="messages"')
        self.assertContains(response, 'data-settings-tab-target="coupons"')
        self.assertContains(response, 'data-settings-tab-target="pilot"')
        self.assertContains(response, 'data-settings-tab-target="advanced"')
        self.assertContains(response, 'data-settings-tab-pane="state"')
        self.assertContains(response, 'data-settings-tab-pane="messages"')
        self.assertContains(response, 'data-settings-tab-pane="coupons"')
        self.assertContains(response, 'data-settings-tab-pane="pilot"')
        self.assertContains(response, 'data-settings-tab-pane="advanced"')
        self.assertContains(response, "settingsTabButtons")
        self.assertContains(response, "novalidate")
        self.assertNotContains(response, 'data-settings-tab-target="chain"')
        self.assertContains(response, "Заведения и серии купонов")
        self.assertContains(response, "Укажите, какую серию купонов выдавать для каждого заведения")
        self.assertContains(response, "Добавить правило")
        self.assertContains(response, "Заведение или вся сеть")
        self.assertContains(response, "Вся сеть")
        self.assertNotContains(response, "Для кого действует")
        self.assertContains(response, "последнее заведение гостя")
        self.assertContains(response, "Отбор гостей по заведению")
        self.assertContains(response, "Как отбирать гостей по заведению")
        self.assertContains(response, "Был хотя бы 1 раз и не был N+ дней")
        self.assertContains(response, "venue-selection-locked-hint")
        self.assertContains(response, "Отбор гостей по заведению включён")
        self.assertContains(response, "venueSelectionMode.disabled = true")
        self.assertContains(response, "Резерв без правил")
        self.assertContains(response, "Дополнительно: условия iikoCard и карточка купона")
        self.assertContains(response, "Шаблон сообщения гостю")
        self.assertContains(response, "Редактировать шаблон")
        self.assertContains(response, "Предпросмотр с тестовым купоном")
        self.assertContains(response, "TEST123")
        self.assertContains(response, "Окно отправки сообщений")
        self.assertContains(response, "Режим отправки сообщений")
        self.assertContains(response, "Каналы отправки сообщений")
        self.assertContains(response, "Куда отправлять сообщение")
        self.assertContains(response, "Разрешённые боты")
        self.assertNotContains(response, "Первое сообщение: просьба заполнить дату рождения")
        self.assertContains(response, "Telegram main")
        self.assertContains(response, "Сразу")
        self.assertContains(response, "Если “Текст карточки купона” оставить пустым")
        self.assertContains(response, "Если пусто, используется общее название из блока ниже")
        self.assertContains(response, "Контрольные телефоны пилота")

        response = self.client.post(
            url,
            {
                "execution_mode": CouponAutomationConfig.ExecutionMode.PILOT,
                "venue_selection_mode": CouponAutomationConfig.VenueSelectionMode.ALL_VISITED,
                "audience_venue_filter_mode": (
                    CouponAutomationConfig.AudienceVenueFilterMode.VISITED_ONCE_AND_INACTIVE
                ),
                "audience_venue_code": "DEP_1",
                "coupon_series": "AUTO_30D",
                "venue_code": "DEP_1",
                "coupon_validity_days": "21",
                "max_recipients_per_run": "1",
                "cooldown_days": "45",
                "pilot_phones": "+79129923438",
                "pilot_include_unmatched": "on",
                "min_order_amount": "200.00",
                "iikocard_action_note": "Подарок при заказе от 200 ₽.",
                "coupon_title_template": "Общее название купона",
                "coupon_promo_text_template": "Тестовый купон автосценария.",
                "notification_distribution_mode": NotificationScenario.DistributionMode.UNIFORM,
                "notification_target_mode": NotificationScenario.TargetMode.ALL_BOTS,
                "notification_bot_profiles": [str(self.bot.id)],
                "notification_send_window_begin": "10:00",
                "notification_send_window_end": "20:00",
                "notification_timezone": "Asia/Yekaterinburg",
                "coupon_rules-TOTAL_FORMS": "3",
                "coupon_rules-INITIAL_FORMS": "0",
                "coupon_rules-MIN_NUM_FORMS": "0",
                "coupon_rules-MAX_NUM_FORMS": "1000",
                "coupon_rules-0-is_active": "on",
                "coupon_rules-0-venue_code": "DEP_1",
                "coupon_rules-0-coupon_series": "AUTO_30D",
                "coupon_rules-0-coupon_validity_days": "",
                "coupon_rules-0-priority": "100",
                "coupon_rules-0-min_order_amount": "",
                "coupon_rules-0-iikocard_action_note": "",
                "coupon_rules-0-coupon_title_template": "Сет «Канпети»",
                "coupon_rules-0-coupon_promo_text_template": "",
                "coupon_rules-1-is_active": "on",
                "coupon_rules-1-venue_code": "__global__",
                "coupon_rules-1-coupon_series": "AUTO_GLOBAL",
                "coupon_rules-1-coupon_validity_days": "",
                "coupon_rules-1-priority": "100",
                "coupon_rules-1-min_order_amount": "",
                "coupon_rules-1-iikocard_action_note": "",
                "coupon_rules-1-coupon_title_template": "",
                "coupon_rules-1-coupon_promo_text_template": "",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        config.refresh_from_db()
        self.assertEqual(config.execution_mode, CouponAutomationConfig.ExecutionMode.PILOT)
        self.assertEqual(config.venue_selection_mode, CouponAutomationConfig.VenueSelectionMode.LAST_ORDER)
        self.assertEqual(
            config.audience_venue_filter_mode,
            CouponAutomationConfig.AudienceVenueFilterMode.VISITED_ONCE_AND_INACTIVE,
        )
        self.assertEqual(config.audience_venue_code, "DEP_1")
        self.assertEqual(config.audience_venue_name, "Сами Сусами")
        self.assertEqual(config.coupon_validity_days, 21)
        self.assertEqual(config.max_recipients_per_run, 1)
        self.assertEqual(config.cooldown_days, 45)
        self.assertEqual(config.venue_name, "Сами Сусами")
        self.assertEqual(config.coupon_title_template, "Общее название купона")
        self.assertEqual(config.settings["pilot_phones"], ["+79129923438"])
        self.assertTrue(config.settings["pilot_include_unmatched"])
        scenario.refresh_from_db()
        self.assertEqual(scenario.distribution_mode, NotificationScenario.DistributionMode.UNIFORM)
        self.assertEqual(scenario.target_mode, NotificationScenario.TargetMode.ALL_BOTS)
        self.assertEqual(list(scenario.bot_profiles.values_list("id", flat=True)), [self.bot.id])
        self.assertEqual(scenario.send_window_begin, time(10, 0))
        self.assertEqual(scenario.send_window_end, time(20, 0))
        self.assertEqual(scenario.timezone, "Asia/Yekaterinburg")
        rules = list(config.coupon_rules.order_by("priority", "id"))
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0].scope_type, CouponAutomationRule.ScopeType.VENUE)
        self.assertEqual(rules[0].venue_code, "DEP_1")
        self.assertEqual(rules[0].venue_name, "Сами Сусами")
        self.assertEqual(rules[0].coupon_series, "AUTO_30D")
        self.assertEqual(rules[0].coupon_title_template, "Сет «Канпети»")
        self.assertIsNone(rules[0].coupon_validity_days)
        self.assertIsNone(rules[0].min_order_amount)
        self.assertEqual(rules[1].scope_type, CouponAutomationRule.ScopeType.GLOBAL)
        self.assertEqual(rules[1].venue_code, "__global__")
        self.assertEqual(rules[1].venue_name, "Вся сеть")
        self.assertEqual(rules[1].coupon_series, "AUTO_GLOBAL")
        self.assertIsNone(rules[1].coupon_title_template)
        self.assertEqual(DispatchTask.objects.count(), 0)
        self.assertEqual(NotificationEvent.objects.count(), 0)

    def test_coupon_autoscenario_settings_blocks_pilot_without_coupon_rules(self):
        """
        Пилот нельзя сохранить без активного правила купона и без резервной серии.
        """
        coupon_template = MessageTemplate.objects.create(
            name="Купонный шаблон без правил",
            message_text="Привет, {{ first_name }}! Купон: {coupon_code}",
            is_active=True,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon_no_rules",
            name="Остывшие 30 дней без правил",
            template=coupon_template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=30,
            settings={},
        )

        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk}),
            {
                "execution_mode": CouponAutomationConfig.ExecutionMode.PILOT,
                "venue_selection_mode": CouponAutomationConfig.VenueSelectionMode.LAST_ORDER,
                "audience_venue_filter_mode": CouponAutomationConfig.AudienceVenueFilterMode.DISABLED,
                "coupon_series": "",
                "venue_code": "",
                "coupon_validity_days": "14",
                "max_recipients_per_run": "10",
                "cooldown_days": "30",
                "pilot_phones": "+79129923438",
                "min_order_amount": "",
                "iikocard_action_note": "",
                "coupon_promo_text_template": "",
                "notification_distribution_mode": NotificationScenario.DistributionMode.UNIFORM,
                "notification_target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "notification_bot_profiles": [str(self.bot.id)],
                "notification_send_window_begin": "10:00",
                "notification_send_window_end": "20:00",
                "notification_timezone": "Asia/Yekaterinburg",
                "coupon_rules-TOTAL_FORMS": "0",
                "coupon_rules-INITIAL_FORMS": "0",
                "coupon_rules-MIN_NUM_FORMS": "0",
                "coupon_rules-MAX_NUM_FORMS": "1000",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "добавьте хотя бы одно активное правило")
        config.refresh_from_db()
        self.assertEqual(config.execution_mode, CouponAutomationConfig.ExecutionMode.REPORT_ONLY)

    def test_coupon_autoscenario_settings_allows_pilot_with_active_rule_without_fallback(self):
        """
        Активное правило купона достаточно для пилота даже без резервной серии.
        """
        coupon_template = MessageTemplate.objects.create(
            name="Купонный шаблон с правилом",
            message_text="Привет, {{ first_name }}! Купон: {coupon_code}",
            is_active=True,
        )
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon_rule_only",
            name="Остывшие 30 дней с правилом",
            template=coupon_template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=30,
            settings={},
        )
        TerminalDepartmentMap.objects.create(
            terminal_group_id="terminal-rule-only",
            department_id="DEP_RULE_ONLY",
            department_name="Сами Сусами",
            is_active=True,
        )

        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk}),
            {
                "execution_mode": CouponAutomationConfig.ExecutionMode.PILOT,
                "venue_selection_mode": CouponAutomationConfig.VenueSelectionMode.LAST_ORDER,
                "audience_venue_filter_mode": CouponAutomationConfig.AudienceVenueFilterMode.DISABLED,
                "coupon_series": "",
                "venue_code": "",
                "coupon_validity_days": "14",
                "max_recipients_per_run": "10",
                "cooldown_days": "30",
                "pilot_phones": "+79129923438",
                "min_order_amount": "",
                "iikocard_action_note": "",
                "coupon_promo_text_template": "",
                "notification_distribution_mode": NotificationScenario.DistributionMode.UNIFORM,
                "notification_target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "notification_bot_profiles": [str(self.bot.id)],
                "notification_send_window_begin": "10:00",
                "notification_send_window_end": "20:00",
                "notification_timezone": "Asia/Yekaterinburg",
                "coupon_rules-TOTAL_FORMS": "1",
                "coupon_rules-INITIAL_FORMS": "0",
                "coupon_rules-MIN_NUM_FORMS": "0",
                "coupon_rules-MAX_NUM_FORMS": "1000",
                "coupon_rules-0-is_active": "on",
                "coupon_rules-0-venue_code": "DEP_RULE_ONLY",
                "coupon_rules-0-coupon_series": "AUTO_RULE_ONLY",
                "coupon_rules-0-coupon_validity_days": "",
                "coupon_rules-0-priority": "100",
                "coupon_rules-0-min_order_amount": "",
                "coupon_rules-0-iikocard_action_note": "",
                "coupon_rules-0-coupon_promo_text_template": "",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        config.refresh_from_db()
        self.assertEqual(config.execution_mode, CouponAutomationConfig.ExecutionMode.PILOT)
        self.assertEqual(config.coupon_series, "")
        self.assertEqual(config.coupon_rules.get().coupon_series, "AUTO_RULE_ONLY")

    def test_coupon_autoscenario_settings_blocks_pilot_without_coupon_code_placeholder(self):
        """
        Сценарий с шаблоном без {coupon_code} нельзя перевести в пилот.
        """
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon_bad_template",
            name="Остывшие 30 дней с плохим шаблоном",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="AUTO_30D",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=30,
            settings={},
        )

        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk}),
            {
                "execution_mode": CouponAutomationConfig.ExecutionMode.PILOT,
                "venue_selection_mode": CouponAutomationConfig.VenueSelectionMode.LAST_ORDER,
                "audience_venue_filter_mode": CouponAutomationConfig.AudienceVenueFilterMode.DISABLED,
                "coupon_series": "AUTO_30D",
                "venue_code": "",
                "coupon_validity_days": "14",
                "max_recipients_per_run": "10",
                "cooldown_days": "30",
                "pilot_phones": "+79129923438",
                "min_order_amount": "",
                "iikocard_action_note": "",
                "coupon_promo_text_template": "",
                "notification_distribution_mode": NotificationScenario.DistributionMode.UNIFORM,
                "notification_target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "notification_bot_profiles": [str(self.bot.id)],
                "notification_send_window_begin": "10:00",
                "notification_send_window_end": "20:00",
                "notification_timezone": "Asia/Yekaterinburg",
                "coupon_rules-TOTAL_FORMS": "0",
                "coupon_rules-INITIAL_FORMS": "0",
                "coupon_rules-MIN_NUM_FORMS": "0",
                "coupon_rules-MAX_NUM_FORMS": "1000",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Нельзя перевести купонный автосценарий")
        self.assertContains(response, "должен быть параметр")
        config.refresh_from_db()
        self.assertEqual(config.execution_mode, CouponAutomationConfig.ExecutionMode.REPORT_ONLY)

    def test_fill_birthday_coupon_settings_updates_request_scenario_block(self):
        """
        Для сценария "дата рождения заполнена + купон" та же страница настраивает
        отдельное первое сообщение с просьбой заполнить дату рождения.
        """
        request_template = MessageTemplate.objects.create(
            name="SYSTEM_FILL_BIRTHDAY_REQUEST_TEMPLATE",
            description="Запрос даты рождения",
            message_text="Укажите дату рождения в боте.",
            is_active=True,
        )
        coupon_template = MessageTemplate.objects.create(
            name="SYSTEM_FILL_BIRTHDAY_COUPON_TEMPLATE",
            description="Купон за дату рождения",
            message_text="Спасибо, дарим купон {coupon_code}.",
            is_active=True,
        )
        request_scenario, _ = NotificationScenario.objects.update_or_create(
            code="fill_birthday_request",
            defaults={
                "name": "Системный сценарий: заполнить дату рождения",
                "template": request_template,
                "trigger_type": NotificationScenario.TriggerType.SCHEDULE,
                "priority": NotificationScenario.Priority.NORMAL,
                "target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "distribution_mode": NotificationScenario.DistributionMode.UNIFORM,
                "send_window_begin": time(9, 0),
                "send_window_end": time(21, 0),
                "timezone": "Asia/Yekaterinburg",
                "is_active": False,
                "is_system": True,
                "settings": {"request_repeat_days": 30},
            },
        )
        request_scenario.bot_profiles.set([])
        coupon_scenario, _ = NotificationScenario.objects.update_or_create(
            code="fill_birthday_coupon",
            defaults={
                "name": "Системный сценарий: дата рождения заполнена + купон",
                "template": coupon_template,
                "trigger_type": NotificationScenario.TriggerType.SCHEDULE,
                "priority": NotificationScenario.Priority.NORMAL,
                "target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "distribution_mode": NotificationScenario.DistributionMode.UNIFORM,
                "send_window_begin": time(9, 0),
                "send_window_end": time(21, 0),
                "timezone": "Asia/Yekaterinburg",
                "is_active": False,
                "is_system": True,
            },
        )
        coupon_scenario.bot_profiles.add(self.bot)
        config, _ = CouponAutomationConfig.objects.update_or_create(
            scenario=coupon_scenario,
            defaults={
                "execution_mode": CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
                "coupon_series": "AUTO_FILL_BIRTHDAY",
                "venue_code": "",
                "venue_name": "",
                "coupon_validity_days": 14,
                "max_recipients_per_run": 100,
                "cooldown_days": 365,
                "settings": {},
            },
        )

        url = reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk})
        response = self.client.get(url, secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-settings-tab-target="chain"')
        self.assertContains(response, 'data-settings-tab-pane="chain"')
        self.assertContains(response, "Цепочка")
        self.assertContains(response, "Первое сообщение: просьба заполнить дату рождения")
        self.assertContains(response, "Укажите дату рождения в боте.")
        self.assertContains(response, "Пауза перед повторной просьбой")

        response = self.client.post(
            url,
            {
                "execution_mode": CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
                "venue_selection_mode": CouponAutomationConfig.VenueSelectionMode.LAST_ORDER,
                "coupon_series": "AUTO_FILL_BIRTHDAY",
                "venue_code": "",
                "coupon_validity_days": "14",
                "max_recipients_per_run": "100",
                "cooldown_days": "365",
                "pilot_phones": "",
                "min_order_amount": "",
                "iikocard_action_note": "",
                "coupon_promo_text_template": "",
                "notification_distribution_mode": NotificationScenario.DistributionMode.UNIFORM,
                "notification_target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "notification_bot_profiles": [str(self.bot.id)],
                "notification_send_window_begin": "10:00",
                "notification_send_window_end": "20:00",
                "notification_timezone": "Asia/Yekaterinburg",
                "fill_birthday_request-is_active": "on",
                "fill_birthday_request-request_repeat_days": "15",
                "fill_birthday_request-distribution_mode": NotificationScenario.DistributionMode.UNIFORM,
                "fill_birthday_request-target_mode": NotificationScenario.TargetMode.ALL_BOTS,
                "fill_birthday_request-send_window_begin": "11:00",
                "fill_birthday_request-send_window_end": "18:00",
                "fill_birthday_request-timezone": "Asia/Yekaterinburg",
                "fill_birthday_request-bot_profiles": [str(self.bot.id)],
                "coupon_rules-TOTAL_FORMS": "0",
                "coupon_rules-INITIAL_FORMS": "0",
                "coupon_rules-MIN_NUM_FORMS": "0",
                "coupon_rules-MAX_NUM_FORMS": "1000",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        coupon_scenario.refresh_from_db()
        request_scenario.refresh_from_db()
        self.assertEqual(coupon_scenario.send_window_begin, time(10, 0))
        self.assertEqual(coupon_scenario.send_window_end, time(20, 0))
        self.assertFalse(coupon_scenario.is_active)
        self.assertTrue(request_scenario.is_active)
        self.assertEqual(request_scenario.distribution_mode, NotificationScenario.DistributionMode.UNIFORM)
        self.assertEqual(request_scenario.target_mode, NotificationScenario.TargetMode.ALL_BOTS)
        self.assertEqual(request_scenario.send_window_begin, time(11, 0))
        self.assertEqual(request_scenario.send_window_end, time(18, 0))
        self.assertEqual(request_scenario.settings["request_repeat_days"], 15)
        self.assertEqual(list(request_scenario.bot_profiles.values_list("id", flat=True)), [self.bot.id])
        self.assertEqual(DispatchTask.objects.count(), 0)
        self.assertEqual(NotificationEvent.objects.count(), 0)

    def test_coupon_autoscenario_settings_requires_notification_bot(self):
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon",
            name="Остывшие 30 дней",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        scenario.bot_profiles.add(self.bot)
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="AUTO_30D",
            venue_code="DEP_1",
            venue_name="Сами Сусами",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=30,
            settings={},
        )

        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk}),
            {
                "execution_mode": CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
                "coupon_series": "AUTO_30D",
                "venue_code": "DEP_1",
                "coupon_validity_days": "14",
                "max_recipients_per_run": "10",
                "cooldown_days": "30",
                "pilot_phones": "",
                "min_order_amount": "",
                "iikocard_action_note": "",
                "coupon_promo_text_template": "",
                "notification_distribution_mode": NotificationScenario.DistributionMode.IMMEDIATE,
                "notification_target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "notification_send_window_begin": "",
                "notification_send_window_end": "",
                "notification_timezone": "Asia/Yekaterinburg",
                "coupon_rules-TOTAL_FORMS": "0",
                "coupon_rules-INITIAL_FORMS": "0",
                "coupon_rules-MIN_NUM_FORMS": "0",
                "coupon_rules-MAX_NUM_FORMS": "1000",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выберите хотя бы один бот для отправки сообщений.")
        scenario.refresh_from_db()
        self.assertEqual(list(scenario.bot_profiles.values_list("id", flat=True)), [self.bot.id])

    def test_coupon_autoscenario_settings_blocks_active_immediate_delivery(self):
        """
        Боевой автосценарий нельзя сохранить с отправкой "Сразу": иначе ACK vtelemax
        мог бы привести к ночной отправке сообщения.
        """
        scenario = NotificationScenario.objects.create(
            code="inactive_30d_coupon",
            name="Остывшие 30 дней",
            template=self.template,
            trigger_type=NotificationScenario.TriggerType.SCHEDULE,
            priority=NotificationScenario.Priority.NORMAL,
            target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
            distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
            timezone="Asia/Yekaterinburg",
            is_active=False,
        )
        config = CouponAutomationConfig.objects.create(
            scenario=scenario,
            execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
            coupon_series="AUTO_30D",
            venue_code="DEP_1",
            venue_name="Сами Сусами",
            coupon_validity_days=14,
            max_recipients_per_run=10,
            cooldown_days=30,
            settings={},
        )

        response = self.client.post(
            reverse("mailings_v2_coupon_autoscenario_settings", kwargs={"pk": config.pk}),
            {
                "execution_mode": CouponAutomationConfig.ExecutionMode.AUTOMATIC,
                "coupon_series": "AUTO_30D",
                "venue_code": "DEP_1",
                "coupon_validity_days": "14",
                "max_recipients_per_run": "100",
                "cooldown_days": "30",
                "notification_distribution_mode": NotificationScenario.DistributionMode.IMMEDIATE,
                "notification_timezone": "Asia/Yekaterinburg",
                "coupon_rules-TOTAL_FORMS": "0",
                "coupon_rules-INITIAL_FORMS": "0",
                "coupon_rules-MIN_NUM_FORMS": "0",
                "coupon_rules-MAX_NUM_FORMS": "1000",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Настройки не сохранены.")
        self.assertContains(response, "но в базе они не применены")
        self.assertContains(response, "Для состояния «Активен» выберите «Равномерно в окне»")
        config.refresh_from_db()
        scenario.refresh_from_db()
        self.assertEqual(config.execution_mode, CouponAutomationConfig.ExecutionMode.REPORT_ONLY)
        self.assertEqual(scenario.distribution_mode, NotificationScenario.DistributionMode.IMMEDIATE)
