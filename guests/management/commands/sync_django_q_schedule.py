"""
Команда синхронизации таблицы django_q_schedule из settings.Q_CLUSTER["schedule"].
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from guests.services.django_q_schedule_sync import sync_django_q_schedule_from_settings


class Command(BaseCommand):
    help = (
        "Синхронизирует записи django_q_schedule с settings.Q_CLUSTER['schedule'] "
        "(idempotent upsert + optional prune stale)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Только показать изменения без записи в БД.",
        )
        parser.add_argument(
            "--no-prune-stale",
            action="store_true",
            help="Не удалять stale-строки managed-расписания, отсутствующие в settings.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run", False))
        prune_stale = not bool(options.get("no_prune_stale", False))

        self.stdout.write("Запущен sync_django_q_schedule")
        self.stdout.write(f"dry_run={dry_run}")
        self.stdout.write(f"prune_stale={prune_stale}")

        stats = sync_django_q_schedule_from_settings(
            dry_run=dry_run,
            prune_stale=prune_stale,
        )

        self.stdout.write(
            "[django_q_schedule] "
            f"source={stats.source_entries} "
            f"created={stats.created} "
            f"updated={stats.updated} "
            f"unchanged={stats.unchanged} "
            f"deleted={stats.deleted} "
            f"renamed={stats.renamed_by_func} "
            f"skipped_invalid={stats.skipped_invalid}"
        )
