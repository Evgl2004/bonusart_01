from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Q

from guests.models import BotProfile, DispatchTask, HistoricalTelegramChannel, Mailing, MailingGuest


@dataclass(slots=True)
class HistoricalTelegramSyncStats:
    """
    Сводка первичного наполнения исторических Telegram-каналов.
    """

    campaign_rows_total: int = 0
    eligible_rows: int = 0
    would_create: int = 0
    created: int = 0
    would_update: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_empty_chat: int = 0
    skipped_chat_conflict: int = 0
    skipped_protected_state: int = 0


class Command(BaseCommand):
    help = (
        "Наполняет реестр исторических Telegram-каналов по успешно доставленным "
        "строкам выбранной рассылки."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--mailing-id",
            type=int,
            default=10,
            help="ID рассылки-источника. По умолчанию используется кампания #10.",
        )
        parser.add_argument(
            "--bot-profile-id",
            type=int,
            default=0,
            help="ID Telegram BotProfile. Если не указан, берётся активный Telegram-бот из рассылки.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Только показать расчёт, без записи изменений в базу.",
        )

    def handle(self, *args, **options):
        mailing_id = int(options["mailing_id"])
        bot_profile_id = int(options.get("bot_profile_id") or 0)
        dry_run = bool(options.get("dry_run"))

        mailing = Mailing.objects.filter(id=mailing_id).first()
        if mailing is None:
            raise CommandError(f"Рассылка #{mailing_id} не найдена.")

        bot_profile = self._resolve_bot_profile(mailing=mailing, bot_profile_id=bot_profile_id)
        stats = self._sync_channels(mailing=mailing, bot_profile=bot_profile, dry_run=dry_run)

        mode_label = "сухой прогон" if dry_run else "запись в базу"
        self.stdout.write(f"Источник: рассылка #{mailing.id} «{mailing.name}».")
        self.stdout.write(
            f"Telegram-бот: id={bot_profile.id}, code={bot_profile.code}, active={bot_profile.is_active}."
        )
        self.stdout.write(f"Режим: {mode_label}.")
        self.stdout.write(f"Всего строк кампании: {stats.campaign_rows_total}.")
        self.stdout.write(f"Подходящих успешных строк: {stats.eligible_rows}.")
        if dry_run:
            self.stdout.write(f"Будет создано: {stats.would_create}.")
            self.stdout.write(f"Будет обновлено: {stats.would_update}.")
        else:
            self.stdout.write(f"Создано: {stats.created}.")
            self.stdout.write(f"Обновлено: {stats.updated}.")
        self.stdout.write(f"Без изменений: {stats.unchanged}.")
        self.stdout.write(f"Пропущено без chat_id: {stats.skipped_empty_chat}.")
        self.stdout.write(f"Пропущено из-за конфликта chat_id: {stats.skipped_chat_conflict}.")
        self.stdout.write(
            "Пропущено из-за защищённого состояния "
            f"(заблокирован или исключён вручную): {stats.skipped_protected_state}."
        )
        self.stdout.write(self.style.SUCCESS("Готово."))

    @staticmethod
    def _resolve_bot_profile(*, mailing: Mailing, bot_profile_id: int) -> BotProfile:
        if bot_profile_id:
            bot_profile = BotProfile.objects.filter(id=bot_profile_id).first()
            if bot_profile is None:
                raise CommandError(f"BotProfile #{bot_profile_id} не найден.")
        else:
            bot_profile = (
                mailing.bot_profiles.filter(
                    provider_type=BotProfile.ProviderType.TELEGRAM,
                    is_active=True,
                )
                .order_by("id")
                .first()
            )
            if bot_profile is None:
                raise CommandError(
                    "В рассылке не найден активный Telegram BotProfile. "
                    "Укажите его явно через --bot-profile-id."
                )

        if bot_profile.provider_type != BotProfile.ProviderType.TELEGRAM:
            raise CommandError(f"BotProfile #{bot_profile.id} не является Telegram-ботом.")
        return bot_profile

    def _sync_channels(
        self,
        *,
        mailing: Mailing,
        bot_profile: BotProfile,
        dry_run: bool,
    ) -> HistoricalTelegramSyncStats:
        stats = HistoricalTelegramSyncStats(
            campaign_rows_total=MailingGuest.objects.filter(mailing=mailing).count()
        )
        rows = list(self._successful_rows(mailing=mailing))
        stats.eligible_rows = len(rows)
        if not rows:
            return stats

        guest_ids = [int(row["guest_id"]) for row in rows if row.get("guest_id")]
        chat_ids = [str(row["external_id"] or "").strip() for row in rows if str(row["external_id"] or "").strip()]
        existing_by_guest = {
            int(channel.guest_id): channel
            for channel in HistoricalTelegramChannel.objects.filter(
                bot_profile=bot_profile,
                guest_id__in=guest_ids,
            )
        }
        existing_by_chat = {
            str(channel.telegram_chat_id): channel
            for channel in HistoricalTelegramChannel.objects.filter(
                bot_profile=bot_profile,
                telegram_chat_id__in=chat_ids,
            )
        }

        for row in rows:
            guest_id = int(row["guest_id"])
            chat_id = str(row["external_id"] or "").strip()
            if not chat_id:
                stats.skipped_empty_chat += 1
                continue

            source_success_at = row.get("sent_at") or row.get("last_dispatch_done_at") or mailing.scheduled_time_begin
            existing_for_guest = existing_by_guest.get(guest_id)
            existing_for_chat = existing_by_chat.get(chat_id)

            if existing_for_guest is None and existing_for_chat is not None:
                stats.skipped_chat_conflict += 1
                continue

            if existing_for_guest is None:
                stats.would_create += 1
                if dry_run:
                    channel = HistoricalTelegramChannel(
                        guest_id=guest_id,
                        bot_profile=bot_profile,
                        telegram_chat_id=chat_id,
                        delivery_state=HistoricalTelegramChannel.DeliveryState.SENDABLE,
                        last_success_at=source_success_at,
                    )
                else:
                    channel = HistoricalTelegramChannel.objects.create(
                        guest_id=guest_id,
                        bot_profile=bot_profile,
                        telegram_chat_id=chat_id,
                        delivery_state=HistoricalTelegramChannel.DeliveryState.SENDABLE,
                        last_success_at=source_success_at,
                    )
                    stats.created += 1
                existing_by_guest[guest_id] = channel
                existing_by_chat[chat_id] = channel
                continue

            if existing_for_guest.delivery_state != HistoricalTelegramChannel.DeliveryState.SENDABLE:
                stats.skipped_protected_state += 1
                continue

            if existing_for_chat is not None and existing_for_chat.guest_id != guest_id:
                stats.skipped_chat_conflict += 1
                continue

            changed_fields: list[str] = []
            old_chat_id = str(existing_for_guest.telegram_chat_id or "")
            if old_chat_id != chat_id:
                existing_for_guest.telegram_chat_id = chat_id
                changed_fields.append("telegram_chat_id")
            if source_success_at and (
                existing_for_guest.last_success_at is None
                or source_success_at > existing_for_guest.last_success_at
            ):
                existing_for_guest.last_success_at = source_success_at
                changed_fields.append("last_success_at")
            if existing_for_guest.last_error_at is not None:
                existing_for_guest.last_error_at = None
                changed_fields.append("last_error_at")
            if existing_for_guest.last_error_text:
                existing_for_guest.last_error_text = None
                changed_fields.append("last_error_text")

            if not changed_fields:
                stats.unchanged += 1
                continue

            stats.would_update += 1
            if not dry_run:
                existing_for_guest.save(update_fields=[*changed_fields, "updated_at"])
                if old_chat_id:
                    existing_by_chat.pop(old_chat_id, None)
                existing_by_chat[chat_id] = existing_for_guest
                stats.updated += 1

        return stats

    @staticmethod
    def _successful_rows(*, mailing: Mailing):
        return (
            MailingGuest.objects.filter(
                mailing=mailing,
                guest_id__isnull=False,
            )
            .exclude(external_id__isnull=True)
            .exclude(external_id="")
            .filter(Q(delivery_status="done") | Q(dispatch_tasks__status=DispatchTask.Status.DONE))
            .annotate(
                last_dispatch_done_at=Max(
                    "dispatch_tasks__finished_at",
                    filter=Q(dispatch_tasks__status=DispatchTask.Status.DONE),
                )
            )
            .values("id", "guest_id", "external_id", "sent_at", "last_dispatch_done_at")
            .order_by("id")
            .distinct()
        )
