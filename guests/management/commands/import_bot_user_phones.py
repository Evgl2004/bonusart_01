import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from guests.models import BotProfile, GuestBotBinding


def norm_phone(raw: str) -> Optional[str]:
    """
    Нормализует телефон: оставляет только цифры и приводит к формату 7XXXXXXXXXX.
    """
    if raw is None:
        return None
    digits = re.sub(r"\D+", "", str(raw))
    if not digits:
        return None

    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]

    if len(digits) == 11 and digits.startswith("7"):
        return digits

    if len(digits) == 10:
        return "7" + digits

    return None


def phone10_from_digits(digits: str) -> Optional[str]:
    """
    Возвращает последние 10 цифр номера.
    """
    if not digits:
        return None
    pure = re.sub(r"\D+", "", str(digits))
    if len(pure) < 10:
        return None
    return pure[-10:]


def load_guest_phone10_map() -> Dict[str, int]:
    """
    Загружает карту `phone10 -> guest_id` из таблицы гостей.
    """
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id,
                   RIGHT(REGEXP_REPLACE(phone, '\\D', '', 'g'), 10) AS phone10
            FROM guests
            WHERE phone IS NOT NULL
              AND phone <> ''
            """
        )
        result: Dict[str, int] = {}
        for guest_id, phone10 in cur.fetchall():
            if phone10:
                result[str(phone10)] = int(guest_id)
        return result


class Command(BaseCommand):
    """
    Импортирует phone<->chat_id из SQLite в новую модель `GuestBotBinding`.

    Команда предназначена в первую очередь для Telegram-ботов, но технически
    может записывать привязки и в любой другой `BotProfile`.
    """

    help = "Import phone<->tg_user_id mappings from bot SQLite into guest_bot_bindings."

    def add_arguments(self, parser):
        parser.add_argument("--sqlite", required=True, help="Path to SQLite db file (e.g. bot_requests.db)")
        parser.add_argument(
            "--bot-profile-id",
            type=int,
            required=False,
            help="BotProfile.id, в который будут записываться привязки гостей.",
        )
        parser.add_argument(
            "--channel-id",
            type=int,
            required=False,
            help="DEPRECATED alias для --bot-profile-id (оставлен для обратной совместимости).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Do not write to Postgres, only show stats")
        parser.add_argument("--limit", type=int, default=0, help="Optional limit rows from SQLite (0 = no limit)")
        parser.add_argument(
            "--only-missing-chat",
            action="store_true",
            help="Update only rows where external_chat_id is empty/null",
        )
        parser.add_argument(
            "--dump-dir",
            default=".",
            help="Directory to save invalid_phones.txt and not_found_phones.txt (default: current dir)",
        )

    def _resolve_bot_profile_id(self, opts) -> Optional[int]:
        """
        Определяет целевой `bot_profile_id` из аргументов команды.
        """
        direct_id = opts.get("bot_profile_id")
        deprecated_id = opts.get("channel_id")

        if direct_id:
            return int(direct_id)

        if deprecated_id:
            self.stdout.write(
                self.style.WARNING(
                    "Параметр --channel-id устарел. Используйте --bot-profile-id."
                )
            )
            return int(deprecated_id)

        return None

    def handle(self, *args, **opts):
        sqlite_path: str = opts["sqlite"]
        dry_run: bool = opts["dry_run"]
        limit: int = opts["limit"]
        only_missing_chat: bool = opts["only_missing_chat"]
        dump_dir = Path(opts["dump_dir"]).resolve()
        bot_profile_id = self._resolve_bot_profile_id(opts)

        if not bot_profile_id:
            self.stderr.write(self.style.ERROR("Укажите --bot-profile-id (или deprecated --channel-id)."))
            return

        dump_dir.mkdir(parents=True, exist_ok=True)

        bot = BotProfile.objects.filter(id=bot_profile_id).first()
        if bot is None:
            self.stderr.write(self.style.ERROR(f"BotProfile id={bot_profile_id} not found"))
            return

        self.stdout.write(f"SQLite: {sqlite_path}")
        self.stdout.write(
            "Target bot: id=%s provider=%s code=%s active=%s"
            % (bot.id, bot.provider_type, bot.code, bot.is_active)
        )
        if bot.provider_type != BotProfile.ProviderType.TELEGRAM:
            self.stdout.write(
                self.style.WARNING(
                    "Выбранный бот не Telegram. Импорт из user_phones обычно используется для Telegram."
                )
            )
        self.stdout.write(f"dry_run={dry_run} limit={limit or 'ALL'} only_missing_chat={only_missing_chat}")
        self.stdout.write(f"dump_dir={dump_dir}")

        # 1) Читаем SQLite.
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        sql = "SELECT user_id, phone, created_at FROM user_phones"
        if limit and limit > 0:
            sql += f" LIMIT {int(limit)}"

        rows = cur.execute(sql).fetchall()
        conn.close()

        self.stdout.write(f"Read rows from SQLite: {len(rows)}")

        # 2) Сбор итоговой карты phone10 -> chat_id (берём самую позднюю запись по created_at).
        phone10_to_chat: Dict[str, str] = {}
        phone10_to_created: Dict[str, str] = {}
        bad_phone_list: List[str] = []

        for row in rows:
            chat_id = str(row["user_id"]).strip()
            raw_phone = row["phone"]

            phone11 = norm_phone(raw_phone)
            if not phone11:
                bad_phone_list.append("" if raw_phone is None else str(raw_phone))
                continue

            phone10 = phone10_from_digits(phone11)
            if not phone10:
                bad_phone_list.append("" if raw_phone is None else str(raw_phone))
                continue

            created_at = str(row["created_at"] or "")
            previous = phone10_to_created.get(phone10)
            if (previous is None) or (created_at and created_at >= previous):
                phone10_to_chat[phone10] = chat_id
                phone10_to_created[phone10] = created_at

        self.stdout.write(f"Valid phones (phone10): {len(phone10_to_chat)}; invalid phones: {len(bad_phone_list)}")

        # 3) Загружаем карту гостей.
        guest_phone10_map = load_guest_phone10_map()
        self.stdout.write(f"Guests with phone in Postgres (phone10 map size): {len(guest_phone10_map)}")

        # 4) Сопоставляем телефоны и гостей.
        desired: List[Tuple[int, int, str]] = []
        not_found_list: List[str] = []

        for phone10, chat_id in phone10_to_chat.items():
            guest_id = guest_phone10_map.get(phone10)
            if not guest_id:
                not_found_list.append(phone10)
                continue
            desired.append((guest_id, bot.id, chat_id))

        self.stdout.write(f"Guests matched by phone10: {len(desired)}")
        self.stdout.write(f"Phones not found in guests: {len(not_found_list)}")

        # 5) Сохраняем технические отчёты.
        invalid_path = dump_dir / "invalid_phones.txt"
        not_found_path = dump_dir / "not_found_phones.txt"

        with invalid_path.open("w", encoding="utf-8") as file_obj:
            for value in bad_phone_list:
                file_obj.write(value + "\n")

        with not_found_path.open("w", encoding="utf-8") as file_obj:
            for value in not_found_list:
                file_obj.write(value + "\n")

        self.stdout.write(self.style.WARNING(f"Saved invalid phones -> {invalid_path}"))
        self.stdout.write(self.style.WARNING(f"Saved not found phones -> {not_found_path}"))

        if bad_phone_list:
            self.stdout.write("\n--- INVALID PHONES (raw) ---")
            for value in bad_phone_list:
                self.stdout.write(value)

        if not_found_list:
            self.stdout.write("\n--- NOT FOUND IN GUESTS (phone10) ---")
            for value in not_found_list:
                self.stdout.write(value)

        if dry_run:
            self.stdout.write(self.style.SUCCESS("\nDRY RUN: no DB writes. Sample matched rows:"))
            for item in desired[:10]:
                self.stdout.write(f"  guest_id={item[0]} bot_id={item[1]} chat_id={item[2]}")
            return

        # 6) Пишем привязки в guest_bot_bindings.
        now = timezone.now()
        guest_ids = [guest_id for (guest_id, _, _) in desired]

        existing_qs = GuestBotBinding.objects.filter(
            guest_id__in=guest_ids,
            bot_id=bot.id,
        )
        existing_map = {(row.guest_id, row.bot_id): row for row in existing_qs}

        # Чтобы при создании не нарушить ограничение "один primary на гостя",
        # запоминаем гостей, у которых primary уже есть.
        primary_guest_ids = set(
            GuestBotBinding.objects.filter(guest_id__in=guest_ids, is_primary=True).values_list("guest_id", flat=True)
        )

        to_create: List[GuestBotBinding] = []
        to_update: List[GuestBotBinding] = []

        for guest_id, bot_id, chat_id in desired:
            key = (guest_id, bot_id)
            binding = existing_map.get(key)

            if binding is None:
                is_primary = guest_id not in primary_guest_ids
                to_create.append(
                    GuestBotBinding(
                        guest_id=guest_id,
                        bot_id=bot_id,
                        external_chat_id=str(chat_id),
                        is_opt_in=True,
                        is_active=True,
                        is_stop_sending=False,
                        is_primary=is_primary,
                    )
                )
                if is_primary:
                    primary_guest_ids.add(guest_id)
                continue

            if only_missing_chat and str(binding.external_chat_id or "").strip():
                continue

            changed = False
            if str(binding.external_chat_id or "") != str(chat_id):
                binding.external_chat_id = str(chat_id)
                changed = True
            if binding.is_opt_in is not True:
                binding.is_opt_in = True
                changed = True
            if binding.is_active is not True:
                binding.is_active = True
                changed = True
            if binding.is_stop_sending is not False:
                binding.is_stop_sending = False
                changed = True

            if changed:
                binding.updated_at = now
                to_update.append(binding)

        with transaction.atomic():
            if to_create:
                GuestBotBinding.objects.bulk_create(to_create, batch_size=2000, ignore_conflicts=True)
            if to_update:
                GuestBotBinding.objects.bulk_update(
                    to_update,
                    ["external_chat_id", "is_opt_in", "is_active", "is_stop_sending", "updated_at"],
                    batch_size=2000,
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. created={len(to_create)} updated={len(to_update)} "
                f"invalid={len(bad_phone_list)} not_found={len(not_found_list)}"
            )
        )
