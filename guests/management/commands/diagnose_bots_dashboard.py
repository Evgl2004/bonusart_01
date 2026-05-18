from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from guests.services.bots_dashboard import build_bots_dashboard_payload, normalize_bots_period_days


def _parse_iso_date(raw_value: str | None, *, option_name: str) -> date | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise CommandError(f"Некорректная дата `{raw_value}` для {option_name}. Ожидается YYYY-MM-DD.") from exc


class Command(BaseCommand):
    help = "Показывает фактический период и последние точки payload дашборда ботов."

    def add_arguments(self, parser):
        parser.add_argument(
            "--period-days",
            default=30,
            help="Размер окна: 7, 14 или 30 дней. Некорректное значение будет заменено на 30.",
        )
        parser.add_argument(
            "--date-to",
            default="",
            help="Последний день периода YYYY-MM-DD. Если не указан, берётся локальное вчера.",
        )
        parser.add_argument(
            "--tail",
            type=int,
            default=5,
            help="Сколько последних строк ряда вывести.",
        )

    def handle(self, *args, **options):
        period_days = normalize_bots_period_days(options.get("period_days"))
        date_to = _parse_iso_date(options.get("date_to"), option_name="--date-to")
        if date_to is None:
            date_to = timezone.localdate() - timedelta(days=1)
        date_from = date_to - timedelta(days=period_days - 1)
        tail = max(1, int(options.get("tail") or 5))

        payload = build_bots_dashboard_payload(
            date_from=date_from,
            date_to=date_to,
            period_days=period_days,
        )
        rows = payload["rows"]
        last_rows = rows[-tail:]
        growth = payload["yesterday_growth"]

        self.stdout.write("=== Диагностика дашборда ботов (bots_dashboard) ===")
        self.stdout.write(
            "Период backend (date_window): "
            f"date_from={payload['filters']['date_from']} "
            f"date_to={payload['filters']['date_to']} "
            f"period_days={payload['filters']['period_days']}"
        )
        self.stdout.write(f"Последние точки рядов (series_tail): tail={len(last_rows)}")
        self.stdout.write(
            "day | channels_delta | optin_delta | unique_delta | unique_optin_delta | "
            "channels_total | unique_total"
        )
        for row in last_rows:
            self.stdout.write(
                f"{row['day']} | "
                f"{_channels_total_delta(row)} | "
                f"{_channels_optin_delta(row)} | "
                f"{row['unique_persons_total_delta']} | "
                f"{row['unique_persons_registered_optin_delta']} | "
                f"{_channels_total(row)} | "
                f"{row['unique_persons_total']}"
            )

        self.stdout.write(
            'Карточка "Прирост за вчера" (yesterday_growth): '
            f"date={growth['date']} "
            f"channels={growth['channels_total_delta_display']} "
            f"optin={growth['channels_registered_optin_delta_display']} "
            f"unique={growth['unique_persons_total_delta_display']} "
            f"unique_optin={growth['unique_persons_registered_optin_delta_display']}"
        )
        ai_report = {
            "date_from": payload["filters"]["date_from"],
            "date_to": payload["filters"]["date_to"],
            "period_days": payload["filters"]["period_days"],
            "last_row": rows[-1] if rows else None,
            "yesterday_growth": growth,
        }
        self.stdout.write(f"ИИ-отчёт (ai_report): {json.dumps(ai_report, ensure_ascii=False, sort_keys=True)}")


def _channels_total(row: dict[str, Any]) -> int:
    return int(row["channels_total_telegram"]) + int(row["channels_total_vk"]) + int(row["channels_total_max"])


def _channels_total_delta(row: dict[str, Any]) -> int:
    return (
        int(row["channels_total_telegram_delta"])
        + int(row["channels_total_vk_delta"])
        + int(row["channels_total_max_delta"])
    )


def _channels_optin_delta(row: dict[str, Any]) -> int:
    return (
        int(row["channels_registered_optin_telegram_delta"])
        + int(row["channels_registered_optin_vk_delta"])
        + int(row["channels_registered_optin_max_delta"])
    )
