import sqlite3
import re
from pathlib import Path
from typing import Optional, Tuple, List, Dict

from django.core.management.base import BaseCommand
from django.db import transaction, connection
from django.utils import timezone

from guests.models import GuestChannelLink, MailingChannel


def norm_phone(raw: str) -> Optional[str]:
    """
    Нормализуем телефон из SQLite: оставляем цифры и приводим к 11-значному виду 7XXXXXXXXXX.
    """
    if raw is None:
        return None
    digits = re.sub(r"\D+", "", str(raw))
    if not digits:
        return None

    # 8XXXXXXXXXX -> 7XXXXXXXXXX
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]

    # +7XXXXXXXXXX после чистки уже будет 7XXXXXXXXXX
    if len(digits) == 11 and digits.startswith("7"):
        return digits

    # Иногда хранят 10 цифр без кода страны
    if len(digits) == 10:
        return "7" + digits

    return None


def phone10_from_digits(digits: str) -> Optional[str]:
    """
    Берём последние 10 цифр.
    """
    if not digits:
        return None
    d = re.sub(r"\D+", "", str(digits))
    if len(d) < 10:
        return None
    return d[-10:]


def load_guest_phone10_map() -> Dict[str, int]:
    """
    Загружаем соответствие phone10 -> guest_id из Postgres.
    phone10 = последние 10 цифр телефона, очищенного от всех нецифр.
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
        out: Dict[str, int] = {}
        for guest_id, phone10 in cur.fetchall():
            if phone10:
                out[str(phone10)] = int(guest_id)
        return out


class Command(BaseCommand):
    help = "Import phone<->tg_user_id mappings from bot SQLite (user_phones) into guest_channel_links."

    def add_arguments(self, parser):
        parser.add_argument("--sqlite", required=True, help="Path to SQLite db file (e.g. bot_requests.db)")
        parser.add_argument("--channel-id", type=int, required=True, help="MailingChannel.id to link guests to")
        parser.add_argument("--dry-run", action="store_true", help="Do not write to Postgres, only show stats")
        parser.add_argument("--limit", type=int, default=0, help="Optional limit rows from SQLite (0 = no limit)")
        parser.add_argument(
            "--only-missing-chat",
            action="store_true",
            help="Update only links where external_chat_id is empty/null",
        )
        parser.add_argument(
            "--dump-dir",
            default=".",
            help="Directory to save invalid_phones.txt and not_found_phones.txt (default: current dir)",
        )

    def handle(self, *args, **opts):
        sqlite_path: str = opts["sqlite"]
        channel_id: int = opts["channel_id"]
        dry_run: bool = opts["dry_run"]
        limit: int = opts["limit"]
        only_missing_chat: bool = opts["only_missing_chat"]
        dump_dir = Path(opts["dump_dir"]).resolve()

        dump_dir.mkdir(parents=True, exist_ok=True)

        # Проверим, что канал существует
        try:
            channel = MailingChannel.objects.get(id=channel_id)
        except MailingChannel.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"MailingChannel id={channel_id} not found"))
            return

        self.stdout.write(f"SQLite: {sqlite_path}")
        self.stdout.write(f"Target channel: id={channel.id} kind={channel.channel_kind}")
        self.stdout.write(f"dry_run={dry_run} limit={limit or 'ALL'} only_missing_chat={only_missing_chat}")
        self.stdout.write(f"dump_dir={dump_dir}")

        # 1) Читаем SQLite
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        sql = "SELECT user_id, phone, created_at FROM user_phones"
        if limit and limit > 0:
            sql += f" LIMIT {int(limit)}"

        rows = cur.execute(sql).fetchall()
        conn.close()

        self.stdout.write(f"Read rows from SQLite: {len(rows)}")

        # 2) Сбор:
        # - phone10 -> chat_id (берём последнее по created_at)
        # - bad_phone_list: все невалидные (как есть)
        phone10_to_chat: Dict[str, str] = {}
        phone10_to_created: Dict[str, str] = {}
        bad_phone_list: List[str] = []

        for r in rows:
            chat_id = str(r["user_id"]).strip()
            raw_phone = r["phone"]

            phone11 = norm_phone(raw_phone)
            if not phone11:
                bad_phone_list.append("" if raw_phone is None else str(raw_phone))
                continue

            p10 = phone10_from_digits(phone11)
            if not p10:
                bad_phone_list.append("" if raw_phone is None else str(raw_phone))
                continue

            created = str(r["created_at"] or "")
            prev = phone10_to_created.get(p10)
            if (prev is None) or (created and created >= prev):
                phone10_to_chat[p10] = chat_id
                phone10_to_created[p10] = created

        self.stdout.write(f"Valid phones (phone10): {len(phone10_to_chat)}; invalid phones: {len(bad_phone_list)}")

        # 3) Загружаем карту гостей phone10 -> guest_id
        guest_phone10_map = load_guest_phone10_map()
        self.stdout.write(f"Guests with phone in Postgres (phone10 map size): {len(guest_phone10_map)}")

        # 4) Сопоставляем + собираем not_found
        desired: List[Tuple[int, int, str]] = []
        not_found_list: List[str] = []  # phone10

        for p10, chat_id in phone10_to_chat.items():
            guest_id = guest_phone10_map.get(p10)
            if not guest_id:
                not_found_list.append(p10)
                continue
            desired.append((guest_id, channel_id, chat_id))

        self.stdout.write(f"Guests matched by phone10: {len(desired)}")
        self.stdout.write(f"Phones not found in guests: {len(not_found_list)}")

        # 5) Печать в консоль (как ты просишь) + сохранение в файлы
        invalid_path = dump_dir / "invalid_phones.txt"
        not_found_path = dump_dir / "not_found_phones.txt"

        # Сохраняем всегда (даже если dry-run), чтобы списки не потерялись
        with invalid_path.open("w", encoding="utf-8") as f:
            for p in bad_phone_list:
                f.write(p + "\n")

        with not_found_path.open("w", encoding="utf-8") as f:
            for p in not_found_list:
                f.write(p + "\n")

        self.stdout.write(self.style.WARNING(f"Saved invalid phones -> {invalid_path}"))
        self.stdout.write(self.style.WARNING(f"Saved not found phones -> {not_found_path}"))

        # Печать всех в консоль (может быть много строк!)
        if bad_phone_list:
            self.stdout.write("\n--- INVALID PHONES (raw) ---")
            for p in bad_phone_list:
                self.stdout.write(p)

        if not_found_list:
            self.stdout.write("\n--- NOT FOUND IN GUESTS (phone10) ---")
            for p10 in not_found_list:
                self.stdout.write(p10)

        if dry_run:
            self.stdout.write(self.style.SUCCESS("\nDRY RUN: no DB writes. Sample matched rows:"))
            for item in desired[:10]:
                self.stdout.write(f"  guest_id={item[0]} channel_id={item[1]} chat_id={item[2]}")
            return

        # 6) Запись в Postgres
        now = timezone.now()

        guest_ids = [g for (g, _, _) in desired]

        existing_qs = GuestChannelLink.objects.filter(
            guest_id__in=guest_ids,
            channel_id=channel_id,
        )
        existing = {(l.guest_id, l.channel_id): l for l in existing_qs}

        to_create: List[GuestChannelLink] = []
        to_update: List[GuestChannelLink] = []

        for guest_id, ch_id, chat_id in desired:
            key = (guest_id, ch_id)
            link = existing.get(key)

            if link is None:
                to_create.append(
                    GuestChannelLink(
                        guest_id=guest_id,
                        channel_id=ch_id,
                        external_chat_id=str(chat_id),
                        is_opt_in=True,
                        is_active=True,
                        is_stop_sending=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
                continue

            if only_missing_chat and (link.external_chat_id is not None) and str(link.external_chat_id).strip() != "":
                continue

            changed = False
            if str(link.external_chat_id or "") != str(chat_id):
                link.external_chat_id = str(chat_id)
                changed = True
            if link.is_opt_in is not True:
                link.is_opt_in = True
                changed = True
            if link.is_active is not True:
                link.is_active = True
                changed = True
            if getattr(link, "is_stop_sending", False) is not False:
                link.is_stop_sending = False
                changed = True

            if changed:
                link.updated_at = now
                to_update.append(link)

        with transaction.atomic():
            if to_create:
                GuestChannelLink.objects.bulk_create(to_create, batch_size=2000, ignore_conflicts=True)
            if to_update:
                GuestChannelLink.objects.bulk_update(
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