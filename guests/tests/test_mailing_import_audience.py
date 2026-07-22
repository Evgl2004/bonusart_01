"""
Интеграционные и регрессионные тесты импорта аудитории из Excel.

Проверяем, что импорт и штатный планировщик доставки применяют одинаковые
правила для новых и исторических каналов.
"""

from __future__ import annotations

import uuid
from datetime import time, timedelta
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from guests.forms import MailingImportPhonesForm
from guests.models import (
    BotProfile,
    Guest,
    GuestBotBinding,
    HistoricalTelegramChannel,
    Mailing,
    MailingGuest,
    MessageTemplate,
    VtelemaxRecipientChannel,
)
from guests.services.mailing_import_audience import (
    MAILING_IMPORT_AUDIENCE_ALL_SENDABLE,
    MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM,
    MAILING_IMPORT_AUDIENCE_NEW_BOTS,
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

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["phone", "telegram_external_id"])
        for phone, telegram_external_id in rows:
            worksheet.append([phone, telegram_external_id])

        buffer = BytesIO()
        workbook.save(buffer)
        return SimpleUploadedFile(
            "audience.xlsx",
            buffer.getvalue(),
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
