from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand

from guests.models import Guest
from guests.services.guest_resolution import normalize_phone10


class Command(BaseCommand):
    help = (
        "Аудит потенциальных дублей гостей по двум осям: "
        "phone10 (последние 10 цифр телефона) и iiko_id."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Сколько групп дублей показывать в консоли для каждого типа.",
        )
        parser.add_argument(
            "--show-groups",
            action="store_true",
            help="Показать примеры групп дублей в консоли.",
        )
        parser.add_argument(
            "--output-json",
            type=str,
            default="",
            help="Путь для сохранения полного JSON-отчета.",
        )

    def handle(self, *args, **options):
        limit = max(1, int(options["limit"]))
        show_groups = bool(options["show_groups"])
        output_json = str(options["output_json"] or "").strip()

        by_phone10: dict[str, list[int]] = defaultdict(list)
        by_iiko_id: dict[str, list[int]] = defaultdict(list)

        total_guests = 0
        queryset = Guest.objects.only("id", "phone", "iiko_id").order_by("id")
        for guest in queryset.iterator(chunk_size=2000):
            total_guests += 1

            phone10 = normalize_phone10(guest.phone)
            if phone10:
                by_phone10[phone10].append(guest.id)

            iiko_id = (guest.iiko_id or "").strip()
            if iiko_id:
                by_iiko_id[iiko_id].append(guest.id)

        phone_dups = {key: ids for key, ids in by_phone10.items() if len(ids) > 1}
        iiko_dups = {key: ids for key, ids in by_iiko_id.items() if len(ids) > 1}

        phone_dup_rows = sum(len(ids) - 1 for ids in phone_dups.values())
        iiko_dup_rows = sum(len(ids) - 1 for ids in iiko_dups.values())
        impacted_guest_ids = {
            guest_id for ids in phone_dups.values() for guest_id in ids
        } | {
            guest_id for ids in iiko_dups.values() for guest_id in ids
        }

        summary = {
            "total_guests": total_guests,
            "phone10_duplicate_groups": len(phone_dups),
            "phone10_duplicate_rows": phone_dup_rows,
            "iiko_id_duplicate_groups": len(iiko_dups),
            "iiko_id_duplicate_rows": iiko_dup_rows,
            "impacted_guests": len(impacted_guest_ids),
        }

        self.stdout.write("=== Guest Duplicates Audit ===")
        self.stdout.write(f"total_guests: {summary['total_guests']}")
        self.stdout.write(f"phone10_duplicate_groups: {summary['phone10_duplicate_groups']}")
        self.stdout.write(f"phone10_duplicate_rows: {summary['phone10_duplicate_rows']}")
        self.stdout.write(f"iiko_id_duplicate_groups: {summary['iiko_id_duplicate_groups']}")
        self.stdout.write(f"iiko_id_duplicate_rows: {summary['iiko_id_duplicate_rows']}")
        self.stdout.write(f"impacted_guests: {summary['impacted_guests']}")

        if show_groups:
            self.stdout.write("")
            self.stdout.write(f"--- phone10 groups (top {limit}) ---")
            for key, ids in sorted(phone_dups.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]:
                self.stdout.write(f"phone10={key} guests={ids}")

            self.stdout.write("")
            self.stdout.write(f"--- iiko_id groups (top {limit}) ---")
            for key, ids in sorted(iiko_dups.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]:
                self.stdout.write(f"iiko_id={key} guests={ids}")

        if output_json:
            report = self._build_json_report(
                summary=summary,
                phone_dups=phone_dups,
                iiko_dups=iiko_dups,
            )
            output_path = Path(output_json)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.stdout.write("")
            self.stdout.write(f"JSON report saved: {output_path}")

    @staticmethod
    def _build_json_report(
        *,
        summary: dict[str, Any],
        phone_dups: dict[str, list[int]],
        iiko_dups: dict[str, list[int]],
    ) -> dict[str, Any]:
        return {
            "summary": summary,
            "phone10_duplicate_groups": [
                {"phone10": phone10, "guest_ids": guest_ids}
                for phone10, guest_ids in sorted(phone_dups.items(), key=lambda item: (-len(item[1]), item[0]))
            ],
            "iiko_id_duplicate_groups": [
                {"iiko_id": iiko_id, "guest_ids": guest_ids}
                for iiko_id, guest_ids in sorted(iiko_dups.items(), key=lambda item: (-len(item[1]), item[0]))
            ],
        }
