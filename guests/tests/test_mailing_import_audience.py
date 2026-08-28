"""
Интеграционные и регрессионные тесты импорта аудитории из Excel.

Проверяем, что импорт и штатный планировщик доставки применяют одинаковые
правила для новых и исторических каналов.
"""

from __future__ import annotations

import uuid
from threading import Barrier, Thread
from datetime import time, timedelta
from io import BytesIO

from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from guests.forms import MailingImportPhonesForm
from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    GuestBotBinding,
    HistoricalTelegramChannel,
    InteractionButtonSet,
    InteractionLinkLabelCode,
    Mailing,
    MailingGuest,
    MessageInteractionLinkDestination,
    MessageTemplate,
    VtelemaxRecipientChannel,
)
from guests.services.mailing_import_audience import (
    MAILING_IMPORT_AUDIENCE_ALL_SENDABLE,
    MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM,
    MAILING_IMPORT_AUDIENCE_NEW_BOTS,
)
from guests.services.mailing_reverse_import import (
    MAILING_IMPORT_OPERATION_EXCLUDE,
    MAILING_IMPORT_OPERATION_INCLUDE,
    REVERSE_IMPORT_SIGNING_SALT,
)


class MailingImportAudienceTests(TestCase):
    """
    Проверяем весь путь: Excel, поиск гостя, выбор канала и MailingGuest.
    """

    def setUp(self):
        self.now = timezone.now()
        self.template = MessageTemplate.objects.create(
            name="Шаблон тестовой рассылки",
            message_text="Здравствуйте, {{ first_name }}!",
            is_active=True,
        )
        self.telegram_bot = BotProfile.objects.create(
            code="mailing_import_tg",
            name="Telegram для импорта",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.vk_bot = BotProfile.objects.create(
            code="mailing_import_vk",
            name="VK для импорта",
            provider_type=BotProfile.ProviderType.VK,
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Проверка импорта аудитории",
            template=self.template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now + timedelta(hours=1),
            scheduled_time_end=self.now + timedelta(days=1),
            is_active=False,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=time(9, 0),
            send_window_end=time(21, 0),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.BULK,
        )
        self.mailing.bot_profiles.add(self.telegram_bot, self.vk_bot)

    def test_form_defaults_to_new_bot_audience_for_backward_compatibility(self):
        """
        Старый запрос без нового поля выбора сохраняет прежний режим импорта.
        """

        form = MailingImportPhonesForm(
            data={},
            files={"file": self._xlsx_file([("+79990000101", "")])},
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["audience_channel_group"],
            MAILING_IMPORT_AUDIENCE_NEW_BOTS,
        )
        self.assertEqual(
            form.cleaned_data["import_operation"],
            MAILING_IMPORT_OPERATION_INCLUDE,
        )

    def test_default_import_adds_new_bot_guest_and_not_historical_guest(self):
        """
        Регрессия: в primary_only рабочая неосновная привязка тоже допустима.

        Ранее импорт требовал is_primary=True, хотя штатный планировщик при
        отсутствии основной привязки выбирает первую доступную.
        """

        new_bot_guest = self._guest("+79990000111")
        historical_guest = self._guest("+79990000112")
        self._binding(
            new_bot_guest,
            self.telegram_bot,
            external_chat_id="new-111",
            is_primary=False,
        )
        self._historical_channel(historical_guest, chat_id="old-112")

        response = self._post_import([new_bot_guest.phone, historical_guest.phone])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(
                MailingGuest.objects.filter(mailing=self.mailing).values_list(
                    "guest_id",
                    flat=True,
                )
            ),
            [new_bot_guest.id],
        )
        report = self.client.session["mailing_import_report"]
        self.assertEqual(report["sendable_new_bots"], 1)
        self.assertEqual(report["sendable_historical"], 1)
        self.assertEqual(report["excluded_by_audience_group"], 1)
        self.assertEqual(report["added"], 1)

    def test_historical_import_adds_only_sendable_old_bot_guests(self):
        """
        Историческая группа исключает новые, заблокированные и уже перешедшие каналы.
        """

        new_bot_guest = self._guest("+79990000121")
        historical_guest = self._guest("+79990000122")
        blocked_historical_guest = self._guest("+79990000123")
        migrated_guest = self._guest("+79990000124")

        self._binding(new_bot_guest, self.telegram_bot, external_chat_id="new-121")
        self._historical_channel(historical_guest, chat_id="old-122")
        self._historical_channel(
            blocked_historical_guest,
            chat_id="old-123",
            delivery_state=HistoricalTelegramChannel.DeliveryState.BLOCKED,
        )
        self._historical_channel(migrated_guest, chat_id="old-124")
        VtelemaxRecipientChannel.objects.create(
            person_id=uuid.uuid4(),
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            phone_e164=migrated_guest.phone,
            external_id="new-124",
            is_registered=True,
            notifications_allowed=False,
            guest=migrated_guest,
        )

        self._post_import(
            [
                new_bot_guest.phone,
                historical_guest.phone,
                blocked_historical_guest.phone,
                migrated_guest.phone,
            ],
            audience_group=MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM,
        )

        self.assertEqual(
            set(
                MailingGuest.objects.filter(mailing=self.mailing).values_list(
                    "guest_id",
                    flat=True,
                )
            ),
            {historical_guest.id},
        )
        report = self.client.session["mailing_import_report"]
        self.assertEqual(report["sendable_new_bots"], 1)
        self.assertEqual(report["sendable_historical"], 1)
        self.assertEqual(report["sendable_total"], 2)
        self.assertEqual(report["excluded_without_channel"], 2)
        self.assertEqual(report["excluded_by_audience_group"], 1)
        self.assertEqual(report["added"], 1)
        self.mailing.refresh_from_db()
        self.assertEqual(
            self.mailing.source_filter_snapshot["mailing_import_audience_groups"],
            [MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM],
        )
        self.assertTrue(
            self.mailing.source_filter_snapshot["mailing_import_contains_historical"]
        )

    def test_historical_import_is_blocked_for_mailing_with_buttons(self):
        guest = self._guest("+79990000125")
        self._historical_channel(guest, chat_id="old-125")
        self.mailing.button_set = InteractionButtonSet.RATING_MENU
        self.mailing.save(update_fields=["button_set"])

        self._post_import(
            [guest.phone],
            audience_group=MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM,
        )

        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())
        self.mailing.refresh_from_db()
        self.assertEqual(self.mailing.source_filter_snapshot, {})

    def test_all_sendable_import_with_historical_guest_is_blocked_for_buttons(self):
        new_guest = self._guest("+79990000126")
        historical_guest = self._guest("+79990000127")
        self._binding(new_guest, self.telegram_bot, external_chat_id="new-126")
        self._historical_channel(historical_guest, chat_id="old-127")
        self.mailing.button_set = InteractionButtonSet.RATING_MENU
        self.mailing.save(update_fields=["button_set"])

        self._post_import(
            [new_guest.phone, historical_guest.phone],
            audience_group=MAILING_IMPORT_AUDIENCE_ALL_SENDABLE,
        )

        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())

    def test_all_sendable_import_combines_new_and_historical_guests(self):
        """
        Объединённая группа добавляет оба маршрута, но не гостя без канала.
        """

        new_bot_guest = self._guest("+79990000131")
        historical_guest = self._guest("+79990000132")
        no_channel_guest = self._guest("+79990000133")
        self._binding(new_bot_guest, self.vk_bot, external_chat_id="vk-131")
        self._historical_channel(historical_guest, chat_id="old-132")

        self._post_import(
            [new_bot_guest.phone, historical_guest.phone, no_channel_guest.phone],
            audience_group=MAILING_IMPORT_AUDIENCE_ALL_SENDABLE,
        )

        self.assertEqual(
            set(
                MailingGuest.objects.filter(mailing=self.mailing).values_list(
                    "guest_id",
                    flat=True,
                )
            ),
            {new_bot_guest.id, historical_guest.id},
        )
        report = self.client.session["mailing_import_report"]
        self.assertEqual(report["sendable_total"], 2)
        self.assertEqual(report["excluded_without_channel"], 1)
        self.assertEqual(report["excluded_by_audience_group"], 0)
        self.assertEqual(report["added"], 2)

    def test_repeated_import_does_not_duplicate_campaign_guests(self):
        """
        Повторная дозагрузка того же файла не создаёт дубли MailingGuest.
        """

        new_bot_guest = self._guest("+79990000141")
        historical_guest = self._guest("+79990000142")
        self._binding(new_bot_guest, self.telegram_bot, external_chat_id="new-141")
        self._historical_channel(historical_guest, chat_id="old-142")
        phones = [new_bot_guest.phone, historical_guest.phone]

        self._post_import(
            phones,
            audience_group=MAILING_IMPORT_AUDIENCE_ALL_SENDABLE,
        )
        self._post_import(
            phones,
            audience_group=MAILING_IMPORT_AUDIENCE_ALL_SENDABLE,
        )

        self.assertEqual(MailingGuest.objects.filter(mailing=self.mailing).count(), 2)
        report = self.client.session["mailing_import_report"]
        self.assertEqual(report["added"], 0)
        self.assertEqual(report["already"], 2)

    def test_file_telegram_id_is_available_only_as_historical_fallback(self):
        """
        Telegram ID из файла не должен обходить запрет в новом контуре.
        """

        historical_guest = self._guest("+79990000151")
        blocked_new_guest = self._guest("+79990000152")
        self._binding(
            blocked_new_guest,
            self.telegram_bot,
            external_chat_id="new-152",
            is_opt_in=False,
        )
        rows = [
            (historical_guest.phone, "file-chat-151"),
            (blocked_new_guest.phone, "file-chat-152"),
        ]

        self._post_import(
            rows,
            audience_group=MAILING_IMPORT_AUDIENCE_NEW_BOTS,
        )
        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())

        self._post_import(
            rows,
            audience_group=MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM,
        )

        mailing_guest = MailingGuest.objects.get(
            mailing=self.mailing,
            guest=historical_guest,
        )
        self.assertEqual(mailing_guest.external_id, "file-chat-151")
        self.assertFalse(
            MailingGuest.objects.filter(
                mailing=self.mailing,
                guest=blocked_new_guest,
            ).exists()
        )
        report = self.client.session["mailing_import_report"]
        self.assertEqual(report["legacy_external_id"], 1)

    def test_historical_channel_requires_selected_telegram_bot(self):
        """
        Исторического гостя нельзя добавить, если Telegram-бот не выбран в кампании.
        """

        guest = self._guest("+79990000161")
        self._historical_channel(guest, chat_id="old-161")
        self.mailing.bot_profiles.set([self.vk_bot])

        self._post_import(
            [guest.phone],
            audience_group=MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM,
        )

        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())
        report = self.client.session["mailing_import_report"]
        self.assertEqual(report["sendable_historical"], 0)
        self.assertEqual(report["excluded_without_channel"], 1)

    def test_audience_page_displays_localized_import_selector(self):
        """
        На экране аудитории видны все три понятных оператору варианта импорта.
        """

        response = self.client.get(
            reverse("mailings_v2_campaigns_audience", kwargs={"pk": self.mailing.id}),
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Кого добавить из Excel")
        self.assertContains(response, "Гости новых ботов")
        self.assertContains(response, "Исторические Telegram-гости (старый бот)")
        self.assertContains(response, "Все гости с доступным каналом отправки")
        self.assertContains(response, 'name="audience_channel_group"', html=False)
        self.assertContains(response, "Добавить гостей, указанных в Excel")
        self.assertContains(response, "Добавить всех допустимых, кроме указанных в Excel")
        self.assertContains(response, 'name="import_operation"', html=False)

    def test_import_forms_show_progress_and_block_repeated_submission(self):
        """
        Обе формы сообщают о выполнении импорта и защищены от повторной отправки.
        """

        page_urls = (
            reverse(
                "mailings_v2_campaigns_audience",
                kwargs={"pk": self.mailing.id},
            ),
            reverse("mailing_edit", kwargs={"pk": self.mailing.id}),
        )

        for page_url in page_urls:
            with self.subTest(page_url=page_url):
                response = self.client.get(page_url, secure=True)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "data-mailing-import-form", html=False)
                self.assertContains(response, "data-mailing-import-submit", html=False)
                self.assertContains(response, "data-mailing-import-progress", html=False)
                self.assertContains(response, "Импорт выполняется…")
                self.assertContains(
                    response,
                    "Не закрывайте страницу и не повторяйте импорт",
                )
                self.assertContains(response, "button.disabled = true", html=False)
                self.assertContains(response, "event.preventDefault()", html=False)
                self.assertContains(response, 'aria-busy", "true', html=False)

    def test_reverse_preview_and_confirmation_create_exact_complement(self):
        excluded_guest = self._guest("+79990000201")
        telegram_guest = self._guest("+79990000202")
        vk_guest = self._guest("+79990000203")
        max_bot = BotProfile.objects.create(
            code="mailing_import_max",
            name="MAX для импорта",
            provider_type=BotProfile.ProviderType.MAX,
            is_active=True,
        )
        max_guest = self._guest("+79990000204")
        file_only_guest = self._guest("+79990000205")
        self.mailing.bot_profiles.add(max_bot)
        self._binding(excluded_guest, self.telegram_bot, external_chat_id="tg-201")
        self._binding(telegram_guest, self.telegram_bot, external_chat_id="tg-202")
        self._binding(vk_guest, self.vk_bot, external_chat_id="vk-203")
        self._binding(max_guest, max_bot, external_chat_id="max-204")

        rows = [
            (excluded_guest.phone, "must-be-ignored"),
            (file_only_guest.phone, "file-route-must-not-be-created"),
        ]
        file_bytes = self._xlsx_bytes(rows)
        preview_response = self._post_reverse_preview(file_bytes)

        self.assertEqual(preview_response.status_code, 200)
        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())
        preview = preview_response.context["preview"]
        self.assertEqual(preview["source_deliverable_guests"], 4)
        self.assertEqual(preview["excluded_guest_records"], 1)
        self.assertEqual(preview["matched_guest_records"], 2)
        self.assertEqual(preview["final_recipients"], 3)
        self.assertEqual(preview["final_share_percent"], 75.0)
        self.assertTrue(preview["warning_large_audience"])
        self.assertEqual(preview["provider_telegram"], 1)
        self.assertEqual(preview["provider_vk"], 1)
        self.assertEqual(preview["provider_max"], 1)
        preview_token = preview_response.context["preview_token"]
        token_payload = signing.loads(
            preview_token,
            salt=REVERSE_IMPORT_SIGNING_SALT,
        )
        serialized_token_payload = str(token_payload)
        for phone_fragment in ("9990000201", "79990000201", "79990000205"):
            self.assertNotIn(phone_fragment, serialized_token_payload)

        confirm_response = self._post_reverse_confirm(
            file_bytes,
            preview_token=preview_token,
        )

        self.assertEqual(confirm_response.status_code, 302)
        self.assertEqual(
            set(
                MailingGuest.objects.filter(mailing=self.mailing).values_list(
                    "guest_id", flat=True
                )
            ),
            {telegram_guest.id, vk_guest.id, max_guest.id},
        )
        self.assertFalse(
            MailingGuest.objects.filter(mailing=self.mailing)
            .exclude(external_id__isnull=True)
            .exists()
        )
        report = self.client.session["mailing_import_report"]
        self.assertEqual(report["import_operation"], MAILING_IMPORT_OPERATION_EXCLUDE)
        self.assertEqual(report["added"], 3)
        serialized_report = str(report)
        for phone_fragment in ("9990000201", "79990000201", "79990000205"):
            self.assertNotIn(phone_fragment, serialized_report)
        self.mailing.refresh_from_db()
        audit = self.mailing.source_filter_snapshot["mailing_excel_import"]
        self.assertEqual(audit["operation"], MAILING_IMPORT_OPERATION_EXCLUDE)
        self.assertEqual(audit["final_recipients"], 3)
        serialized_snapshot = str(self.mailing.source_filter_snapshot)
        self.assertNotIn("9990000201", serialized_snapshot)
        self.assertNotIn("79990000201", serialized_snapshot)
        self.assertNotIn("79990000205", serialized_snapshot)

    def test_reverse_preview_reports_duplicate_and_invalid_excel_rows(self):
        excluded_guest = self._guest("+79990000206")
        survivor = self._guest("+79990000207")
        self._binding(excluded_guest, self.telegram_bot, external_chat_id="tg-206")
        self._binding(survivor, self.telegram_bot, external_chat_id="tg-207")

        response = self._post_reverse_preview(
            self._xlsx_bytes(
                [
                    (excluded_guest.phone, ""),
                    ("8 (999) 000-02-06", ""),
                    ("bad-phone", ""),
                    ("", ""),
                ]
            )
        )
        preview = response.context["preview"]

        self.assertEqual(preview["total_rows"], 4)
        self.assertEqual(preview["valid_rows"], 2)
        self.assertEqual(preview["invalid_rows"], 2)
        self.assertEqual(preview["duplicate_rows"], 1)
        self.assertEqual(preview["unique_phones"], 1)
        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())

    def test_reverse_excludes_all_guest_records_with_duplicate_phone(self):
        duplicate_a = self._guest("+79990000211")
        duplicate_b = self._guest("8 (999) 000-02-11")
        survivor = self._guest("+79990000212")
        for index, guest in enumerate((duplicate_a, duplicate_b, survivor), start=1):
            self._binding(guest, self.telegram_bot, external_chat_id=f"tg-21{index}")

        file_bytes = self._xlsx_bytes([(duplicate_a.phone, "")])
        preview_response = self._post_reverse_preview(file_bytes)
        preview = preview_response.context["preview"]

        self.assertEqual(preview["matched_phones"], 1)
        self.assertEqual(preview["matched_guest_records"], 2)
        self.assertEqual(preview["excluded_guest_records"], 2)
        self.assertEqual(preview["final_recipients"], 1)

        self._post_reverse_confirm(
            file_bytes,
            preview_token=preview_response.context["preview_token"],
        )
        self.assertEqual(
            list(
                MailingGuest.objects.filter(mailing=self.mailing).values_list(
                    "guest_id", flat=True
                )
            ),
            [survivor.id],
        )

    def test_reverse_excludes_deliverable_guest_without_normalizable_phone(self):
        excluded_guest = self._guest("+79990000221")
        survivor = self._guest("+79990000222")
        no_phone_guest = self._guest("")
        for index, guest in enumerate((excluded_guest, survivor, no_phone_guest), start=1):
            self._binding(guest, self.telegram_bot, external_chat_id=f"tg-22{index}")

        file_bytes = self._xlsx_bytes([(excluded_guest.phone, "")])
        preview_response = self._post_reverse_preview(file_bytes)
        preview = preview_response.context["preview"]

        self.assertEqual(preview["source_deliverable_guests"], 3)
        self.assertEqual(preview["guests_without_normalized_phone"], 1)
        self.assertEqual(preview["final_recipients"], 1)

        self._post_reverse_confirm(
            file_bytes,
            preview_token=preview_response.context["preview_token"],
        )
        self.assertEqual(
            list(
                MailingGuest.objects.filter(mailing=self.mailing).values_list(
                    "guest_id", flat=True
                )
            ),
            [survivor.id],
        )

    def test_reverse_blocks_when_no_excel_phone_matches_database(self):
        sendable_guest = self._guest("+79990000231")
        self._binding(sendable_guest, self.telegram_bot, external_chat_id="tg-231")

        response = self._post_reverse_preview(
            self._xlsx_bytes([("+79990000999", "")])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["preview_token"], "")
        self.assertContains(response, "Ни один телефон из Excel не найден в базе")
        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())

    def test_reverse_rejects_corrupt_file_and_missing_phone_header(self):
        corrupt_response = self.client.post(
            reverse("mailing_import_phones", kwargs={"pk": self.mailing.id}),
            data={
                "import_operation": MAILING_IMPORT_OPERATION_EXCLUDE,
                "audience_channel_group": MAILING_IMPORT_AUDIENCE_NEW_BOTS,
                "file": SimpleUploadedFile("broken.xlsx", b"not-an-xlsx"),
            },
            secure=True,
        )
        self.assertEqual(corrupt_response.status_code, 302)
        self.assertIn("Не удалось прочитать Excel", self.client.session["mailing_import_error"])

        missing_header = self._xlsx_bytes(
            [("+79990000241", "")],
            headers=("customer", "comment"),
        )
        missing_header_response = self._post_reverse_preview(missing_header)
        self.assertEqual(missing_header_response.status_code, 302)
        self.assertIn(
            "обязательный столбец phone",
            self.client.session["mailing_import_error"],
        )
        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())

    def test_reverse_rejects_excel_without_data_rows(self):
        response = self._post_reverse_preview(self._xlsx_bytes([]))

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "не содержит строк с данными",
            self.client.session["mailing_import_error"],
        )
        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())

    def test_reverse_confirmation_rejects_changed_file(self):
        excluded_a = self._guest("+79990000251")
        excluded_b = self._guest("+79990000252")
        survivor = self._guest("+79990000253")
        for index, guest in enumerate((excluded_a, excluded_b, survivor), start=1):
            self._binding(guest, self.telegram_bot, external_chat_id=f"tg-25{index}")
        preview_bytes = self._xlsx_bytes([(excluded_a.phone, "")])
        changed_bytes = self._xlsx_bytes([(excluded_b.phone, "")])
        preview_response = self._post_reverse_preview(preview_bytes)

        confirm_response = self._post_reverse_confirm(
            changed_bytes,
            preview_token=preview_response.context["preview_token"],
        )

        self.assertEqual(confirm_response.status_code, 302)
        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())
        self.assertIn(
            "Файл или параметры импорта изменились",
            self.client.session["mailing_import_error"],
        )

    def test_reverse_confirmation_rejects_campaign_or_audience_change(self):
        excluded_guest = self._guest("+79990000261")
        survivor = self._guest("+79990000262")
        self._binding(excluded_guest, self.telegram_bot, external_chat_id="tg-261")
        self._binding(survivor, self.telegram_bot, external_chat_id="tg-262")
        file_bytes = self._xlsx_bytes([(excluded_guest.phone, "")])
        preview_response = self._post_reverse_preview(file_bytes)

        added_after_preview = self._guest("+79990000263")
        self._binding(
            added_after_preview,
            self.telegram_bot,
            external_chat_id="tg-263",
        )
        confirm_response = self._post_reverse_confirm(
            file_bytes,
            preview_token=preview_response.context["preview_token"],
        )

        self.assertEqual(confirm_response.status_code, 302)
        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())
        self.assertIn(
            "доступная аудитория изменились",
            self.client.session["mailing_import_error"],
        )

    def test_reverse_confirmation_rejects_campaign_setting_change(self):
        excluded_guest = self._guest("+79990000264")
        survivor = self._guest("+79990000265")
        self._binding(excluded_guest, self.telegram_bot, external_chat_id="tg-264")
        self._binding(survivor, self.telegram_bot, external_chat_id="tg-265")
        file_bytes = self._xlsx_bytes([(excluded_guest.phone, "")])
        preview_response = self._post_reverse_preview(file_bytes)

        self.mailing.queue_priority = Mailing.QueuePriority.HIGH
        self.mailing.save(update_fields=["queue_priority"])
        confirm_response = self._post_reverse_confirm(
            file_bytes,
            preview_token=preview_response.context["preview_token"],
        )

        self.assertEqual(confirm_response.status_code, 302)
        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())
        self.assertIn(
            "Настройки или доступная аудитория изменились",
            self.client.session["mailing_import_error"],
        )

    def test_reverse_confirmation_is_single_use_after_rows_are_created(self):
        excluded_guest = self._guest("+79990000271")
        survivor = self._guest("+79990000272")
        self._binding(excluded_guest, self.telegram_bot, external_chat_id="tg-271")
        self._binding(survivor, self.telegram_bot, external_chat_id="tg-272")
        file_bytes = self._xlsx_bytes([(excluded_guest.phone, "")])
        preview_response = self._post_reverse_preview(file_bytes)
        token = preview_response.context["preview_token"]

        first_response = self._post_reverse_confirm(file_bytes, preview_token=token)
        second_response = self._post_reverse_confirm(file_bytes, preview_token=token)

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(MailingGuest.objects.filter(mailing=self.mailing).count(), 1)
        self.assertIn(
            "кампании без аудитории",
            self.client.session["mailing_import_error"],
        )

    def test_reverse_preview_blocks_active_archived_and_nonempty_campaigns(self):
        excluded_guest = self._guest("+79990000281")
        survivor = self._guest("+79990000282")
        binding = self._binding(
            excluded_guest,
            self.telegram_bot,
            external_chat_id="tg-281",
        )
        self._binding(survivor, self.telegram_bot, external_chat_id="tg-282")
        file_bytes = self._xlsx_bytes([(excluded_guest.phone, "")])

        self.mailing.is_active = True
        self.mailing.save(update_fields=["is_active"])
        active_response = self._post_reverse_preview(file_bytes)
        self.assertContains(active_response, "только для выключенной кампании")

        self.mailing.is_active = False
        self.mailing.is_archived = True
        self.mailing.save(update_fields=["is_active", "is_archived"])
        archived_response = self._post_reverse_preview(file_bytes)
        self.assertContains(archived_response, "архивной кампании")

        self.mailing.is_archived = False
        self.mailing.save(update_fields=["is_archived"])
        mailing_guest = MailingGuest.objects.create(
            mailing=self.mailing,
            guest=survivor,
            phone=survivor.phone,
            text_mailing_list="Тест",
            scheduled_datetime=self.mailing.scheduled_time_begin,
            status=MailingGuest.Status.PLANNED,
            created_at=self.now,
        )
        DispatchTask.objects.create(
            provider_type=BotProfile.ProviderType.TELEGRAM,
            guest=survivor,
            mailing_guest=mailing_guest,
            bot_profile=self.telegram_bot,
            guest_binding=binding,
            external_chat_id="tg-282",
        )
        nonempty_response = self._post_reverse_preview(file_bytes)
        self.assertContains(nonempty_response, "кампании без аудитории")
        self.assertContains(nonempty_response, "существуют задачи отправки")

    def test_reverse_preview_blocks_historical_routes_with_buttons(self):
        excluded_guest = self._guest("+79990000291")
        historical_guest = self._guest("+79990000292")
        self._historical_channel(excluded_guest, chat_id="old-291")
        self._historical_channel(historical_guest, chat_id="old-292")
        link_destination = MessageInteractionLinkDestination.objects.create(
            code="reverse_import_test_link",
            name="Тестовая ссылка обратного импорта",
            label_code=InteractionLinkLabelCode.DELIVERY,
            target_url="https://example.test/delivery",
            is_active=True,
        )
        self.mailing.button_set = InteractionButtonSet.RATING_MENU_LINK
        self.mailing.tracked_link_destination = link_destination
        self.mailing.save(update_fields=["button_set", "tracked_link_destination"])

        response = self._post_reverse_preview(
            self._xlsx_bytes([(excluded_guest.phone, "ignored-external-id")]),
            audience_group=MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM,
        )

        self.assertEqual(response.context["preview_token"], "")
        self.assertContains(response, "исторические Telegram-маршруты")
        self.assertFalse(MailingGuest.objects.filter(mailing=self.mailing).exists())

    def test_reverse_all_bots_reports_unique_guests_per_provider(self):
        excluded_guest = self._guest("+79990000301")
        multi_guest = self._guest("+79990000302")
        self._binding(excluded_guest, self.telegram_bot, external_chat_id="tg-301")
        self._binding(multi_guest, self.telegram_bot, external_chat_id="tg-302")
        self._binding(multi_guest, self.vk_bot, external_chat_id="vk-302", is_primary=False)
        self.mailing.target_mode = Mailing.TargetMode.ALL_BOTS
        self.mailing.save(update_fields=["target_mode"])

        response = self._post_reverse_preview(
            self._xlsx_bytes([(excluded_guest.phone, "")])
        )
        preview = response.context["preview"]

        self.assertEqual(preview["final_recipients"], 1)
        self.assertEqual(preview["provider_telegram"], 1)
        self.assertEqual(preview["provider_vk"], 1)

    def test_reverse_base_respects_permissions_and_selected_bots(self):
        excluded_guest = self._guest("+79990000311")
        allowed_guest = self._guest("+79990000312")
        opt_out_guest = self._guest("+79990000313")
        stopped_guest = self._guest("+79990000314")
        unselected_guest = self._guest("+79990000315")
        unselected_bot = BotProfile.objects.create(
            code="mailing_import_unselected",
            name="Невыбранный бот",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self._binding(excluded_guest, self.telegram_bot, external_chat_id="tg-311")
        self._binding(allowed_guest, self.telegram_bot, external_chat_id="tg-312")
        self._binding(
            opt_out_guest,
            self.telegram_bot,
            external_chat_id="tg-313",
            is_opt_in=False,
        )
        self._binding(
            stopped_guest,
            self.telegram_bot,
            external_chat_id="tg-314",
            is_stop_sending=True,
        )
        self._binding(unselected_guest, unselected_bot, external_chat_id="tg-315")

        response = self._post_reverse_preview(
            self._xlsx_bytes([(excluded_guest.phone, "")])
        )
        preview = response.context["preview"]

        self.assertEqual(preview["source_deliverable_guests"], 2)
        self.assertEqual(preview["excluded_guest_records"], 1)
        self.assertEqual(preview["final_recipients"], 1)

    def test_reverse_warning_starts_at_exactly_sixty_percent(self):
        guests = [self._guest(f"+7999000032{index}") for index in range(5)]
        for index, guest in enumerate(guests):
            self._binding(guest, self.telegram_bot, external_chat_id=f"tg-32{index}")

        response = self._post_reverse_preview(
            self._xlsx_bytes([(guests[0].phone, ""), (guests[1].phone, "")])
        )
        preview = response.context["preview"]

        self.assertEqual(preview["final_recipients"], 3)
        self.assertEqual(preview["final_share_percent"], 60.0)
        self.assertTrue(preview["warning_large_audience"])
        self.assertContains(response, "Внимание: большая аудитория")

    def test_direct_import_keeps_legacy_first_column_without_header(self):
        guest = self._guest("+79990000331")
        self._binding(guest, self.telegram_bot, external_chat_id="tg-331")
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append([guest.phone])
        buffer = BytesIO()
        workbook.save(buffer)

        response = self.client.post(
            reverse("mailing_import_phones", kwargs={"pk": self.mailing.id}),
            data={"file": self._uploaded_xlsx(buffer.getvalue())},
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            MailingGuest.objects.filter(mailing=self.mailing, guest=guest).exists()
        )

    def _post_reverse_preview(
        self,
        file_bytes: bytes,
        *,
        audience_group: str = MAILING_IMPORT_AUDIENCE_NEW_BOTS,
    ):
        return self.client.post(
            reverse("mailing_import_phones", kwargs={"pk": self.mailing.id}),
            data={
                "import_operation": MAILING_IMPORT_OPERATION_EXCLUDE,
                "import_action": "preview",
                "audience_channel_group": audience_group,
                "file": self._uploaded_xlsx(file_bytes),
            },
            secure=True,
        )

    def _post_reverse_confirm(
        self,
        file_bytes: bytes,
        *,
        preview_token: str,
        audience_group: str = MAILING_IMPORT_AUDIENCE_NEW_BOTS,
    ):
        return self.client.post(
            reverse("mailing_import_phones", kwargs={"pk": self.mailing.id}),
            data={
                "import_operation": MAILING_IMPORT_OPERATION_EXCLUDE,
                "import_action": "confirm",
                "audience_channel_group": audience_group,
                "preview_token": preview_token,
                "file": self._uploaded_xlsx(file_bytes),
            },
            secure=True,
        )

    def _post_import(self, rows, *, audience_group: str | None = None):
        """
        Выполняет импорт тестового Excel в текущую кампанию.
        """

        normalized_rows = [
            row if isinstance(row, tuple) else (row, "")
            for row in rows
        ]
        data = {}
        if audience_group is not None:
            data["audience_channel_group"] = audience_group
        return self.client.post(
            reverse("mailing_import_phones", kwargs={"pk": self.mailing.id}),
            data={**data, "file": self._xlsx_file(normalized_rows)},
            secure=True,
        )

    @staticmethod
    def _xlsx_file(rows) -> SimpleUploadedFile:
        """
        Создаёт минимальный корректный Excel-файл в памяти.
        """

        return MailingImportAudienceTests._uploaded_xlsx(
            MailingImportAudienceTests._xlsx_bytes(rows)
        )

    @staticmethod
    def _xlsx_bytes(
        rows,
        *,
        headers=("phone", "telegram_external_id"),
    ) -> bytes:
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(list(headers))
        for phone, telegram_external_id in rows:
            worksheet.append([phone, telegram_external_id])
        buffer = BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    @staticmethod
    def _uploaded_xlsx(file_bytes: bytes) -> SimpleUploadedFile:
        return SimpleUploadedFile(
            "audience.xlsx",
            file_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )

    def _guest(self, phone: str) -> Guest:
        """
        Создаёт гостя с датами, используемыми в шаблоне и отчётах.
        """

        return Guest.objects.create(
            phone=phone,
            first_name="Тест",
            created_at=self.now,
            updated_at=self.now,
        )

    @staticmethod
    def _binding(
        guest: Guest,
        bot: BotProfile,
        *,
        external_chat_id: str,
        is_primary: bool = True,
        is_opt_in: bool = True,
        is_stop_sending: bool = False,
    ) -> GuestBotBinding:
        """
        Создаёт разрешённую активную привязку к новому боту.
        """

        return GuestBotBinding.objects.create(
            guest=guest,
            bot=bot,
            external_chat_id=external_chat_id,
            is_primary=is_primary,
            is_active=True,
            is_opt_in=is_opt_in,
            is_stop_sending=is_stop_sending,
        )

    def _historical_channel(
        self,
        guest: Guest,
        *,
        chat_id: str,
        delivery_state: str = HistoricalTelegramChannel.DeliveryState.SENDABLE,
    ) -> HistoricalTelegramChannel:
        """
        Создаёт исторический Telegram-канал с заданным состоянием доставки.
        """

        return HistoricalTelegramChannel.objects.create(
            guest=guest,
            bot_profile=self.telegram_bot,
            telegram_chat_id=chat_id,
            delivery_state=delivery_state,
            last_success_at=self.now,
        )


@skipUnlessDBFeature("has_select_for_update")
class MailingReverseImportConcurrencyTests(TransactionTestCase):
    """На PostgreSQL два одновременных подтверждения не смешивают аудиторию."""

    reset_sequences = True

    def setUp(self):
        self.now = timezone.now()
        template = MessageTemplate.objects.create(
            name="Параллельный обратный импорт",
            message_text="Здравствуйте, {{ first_name }}!",
            is_active=True,
        )
        self.bot = BotProfile.objects.create(
            code="reverse_concurrency_tg",
            name="Telegram конкурентного теста",
            provider_type=BotProfile.ProviderType.TELEGRAM,
            is_active=True,
        )
        self.mailing = Mailing.objects.create(
            name="Параллельное подтверждение",
            template=template,
            scheduled_date=self.now.date(),
            scheduled_time_begin=self.now + timedelta(hours=1),
            scheduled_time_end=self.now + timedelta(days=1),
            is_active=False,
            is_archived=False,
            created_at=self.now,
            updated_at=self.now,
            send_window_begin=time(9, 0),
            send_window_end=time(21, 0),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.BULK,
        )
        self.mailing.bot_profiles.add(self.bot)
        self.excluded_guest = self._guest("+79990000401", "tg-401")
        self.survivor = self._guest("+79990000402", "tg-402")
        self.file_bytes = MailingImportAudienceTests._xlsx_bytes(
            [(self.excluded_guest.phone, "")]
        )

    def test_two_parallel_confirmations_create_one_exact_audience(self):
        preview_client = Client()
        preview_response = preview_client.post(
            reverse("mailing_import_phones", kwargs={"pk": self.mailing.id}),
            data={
                "import_operation": MAILING_IMPORT_OPERATION_EXCLUDE,
                "import_action": "preview",
                "audience_channel_group": MAILING_IMPORT_AUDIENCE_NEW_BOTS,
                "file": MailingImportAudienceTests._uploaded_xlsx(self.file_bytes),
            },
            secure=True,
        )
        self.assertEqual(preview_response.status_code, 200)
        token = preview_response.context["preview_token"]

        barrier = Barrier(2)
        results: list[str] = []
        errors: list[BaseException] = []

        def confirm_in_thread():
            close_old_connections()
            client = Client()
            try:
                barrier.wait(timeout=10)
                response = client.post(
                    reverse(
                        "mailing_import_phones",
                        kwargs={"pk": self.mailing.id},
                    ),
                    data={
                        "import_operation": MAILING_IMPORT_OPERATION_EXCLUDE,
                        "import_action": "confirm",
                        "audience_channel_group": MAILING_IMPORT_AUDIENCE_NEW_BOTS,
                        "preview_token": token,
                        "file": MailingImportAudienceTests._uploaded_xlsx(
                            self.file_bytes
                        ),
                    },
                    secure=True,
                )
                if response.status_code != 302:
                    raise AssertionError(f"unexpected status={response.status_code}")
                session = client.session
                results.append(
                    "success" if "mailing_import_report" in session else "blocked"
                )
            except BaseException as exc:  # noqa: BLE001 - передаём ошибку в основной поток теста.
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [Thread(target=confirm_in_thread) for _index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(errors, errors)
        self.assertEqual(sorted(results), ["blocked", "success"])
        self.assertEqual(
            list(
                MailingGuest.objects.filter(mailing=self.mailing).values_list(
                    "guest_id", flat=True
                )
            ),
            [self.survivor.id],
        )

    def _guest(self, phone: str, external_chat_id: str) -> Guest:
        guest = Guest.objects.create(
            phone=phone,
            first_name="Тест",
            created_at=self.now,
            updated_at=self.now,
        )
        GuestBotBinding.objects.create(
            guest=guest,
            bot=self.bot,
            external_chat_id=external_chat_id,
            is_primary=True,
            is_active=True,
            is_opt_in=True,
            is_stop_sending=False,
        )
        return guest
