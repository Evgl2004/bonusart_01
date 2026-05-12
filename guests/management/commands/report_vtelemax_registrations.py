from __future__ import annotations

from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from guests.models import VtelemaxRecipientChannel


def _parse_date(raw_value: str | None) -> date | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise CommandError(f"Некорректная дата `{raw_value}`. Ожидается YYYY-MM-DD.") from exc


class Command(BaseCommand):
    help = (
        "Показывает динамику регистраций гостей из vtelemax "
        "по дням и платформам (telegram/max/vk)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Период в днях назад от текущей даты (по умолчанию 30).",
        )
        parser.add_argument(
            "--date-from",
            default="",
            help="Нижняя граница периода YYYY-MM-DD (перекрывает --days).",
        )
        parser.add_argument(
            "--date-to",
            default="",
            help="Верхняя граница периода YYYY-MM-DD (включительно).",
        )
        parser.add_argument(
            "--platform",
            choices=["telegram", "max", "vk"],
            default="",
            help="Ограничить отчёт одной платформой.",
        )

    def handle(self, *args, **options):
        days = max(1, int(options.get("days") or 30))
        platform = str(options.get("platform") or "").strip().lower()
        date_from = _parse_date(options.get("date_from"))
        date_to = _parse_date(options.get("date_to"))

        today = timezone.localdate()
        if date_from is None:
            date_from = today - timedelta(days=days - 1)
        if date_to is None:
            date_to = today

        if date_from > date_to:
            raise CommandError("date-from не может быть позже date-to.")

        channels = (
            VtelemaxRecipientChannel.objects.filter(
                is_registered=True,
            )
            .annotate(registration_at=Coalesce("registered_at", "account_created_at"))
            .filter(
                registration_at__isnull=False,
                registration_at__date__gte=date_from,
                registration_at__date__lte=date_to,
            )
        )
        if platform:
            channels = channels.filter(platform=platform)

        rows = list(
            channels.annotate(day=TruncDate("registration_at"))
            .values("day", "platform")
            .annotate(total=Count("id"), persons=Count("person_id", distinct=True))
            .order_by("-day", "platform")
        )

        self.stdout.write("=== Динамика регистраций vtelemax ===")
        self.stdout.write(
            f"Период: {date_from.isoformat()} .. {date_to.isoformat()} "
            f"(platform={platform or 'all'})"
        )

        if not rows:
            self.stdout.write("Данные отсутствуют.")
            return

        self.stdout.write("day | platform | registrations | unique_persons")
        for row in rows:
            self.stdout.write(
                f"{row['day']} | {row['platform']} | {row['total']} | {row['persons']}"
            )
