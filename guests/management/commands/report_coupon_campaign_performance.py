from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from guests.models import Mailing
from guests.services.coupon_campaign_reporting import build_coupon_campaign_performance_snapshot


class Command(BaseCommand):
    help = (
        "Строит KPI-отчёт по купонной кампании: отправка, использование, "
        "конверсия, возвращаемость и поздние применения."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign-id",
            type=int,
            required=True,
            help="ID кампании (mailing), по которой нужен отчёт.",
        )
        parser.add_argument(
            "--returned-window-days",
            type=int,
            default=0,
            help=(
                "Окно в днях для метрики returned_guest_coupon. "
                "Если не задано (>0), используется длительность кампании."
            ),
        )
        parser.add_argument(
            "--late-rows-limit",
            type=int,
            default=20,
            help="Лимит строк для секции поздних применений купонов.",
        )
        parser.add_argument(
            "--as-json",
            action="store_true",
            help="Вывести отчёт JSON-структурой.",
        )

    def handle(self, *args, **options):
        campaign_id = int(options.get("campaign_id") or 0)
        returned_window_days = int(options.get("returned_window_days") or 0)
        late_rows_limit = max(0, int(options.get("late_rows_limit") or 0))
        as_json = bool(options.get("as_json", False))

        mailing = Mailing.objects.filter(pk=campaign_id).first()
        if mailing is None:
            raise CommandError(f"Кампания с id={campaign_id} не найдена.")

        snapshot = build_coupon_campaign_performance_snapshot(
            mailing=mailing,
            returned_window_days=(returned_window_days if returned_window_days > 0 else None),
            late_rows_limit=late_rows_limit,
        )
        payload = snapshot.to_dict()

        if as_json:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        self.stdout.write("=== Отчёт по купонной кампании ===")
        self.stdout.write(
            f"Кампания: #{mailing.id} {mailing.name} (campaign_id={mailing.id})"
        )
        self.stdout.write(
            f"Серия купонов: {payload['coupon_series'] or '-'} (coupon_series={payload['coupon_series'] or ''})"
        )
        self.stdout.write(
            f"Аудитория: {payload['recipients_total']} (audience_total={payload['recipients_total']})"
        )
        self.stdout.write(
            f"Назначено купонов: {payload['assignments_total']} (assignments_total={payload['assignments_total']})"
        )
        self.stdout.write(
            f"Отправлено купонов: {payload['coupons_sent_total']} (coupons_sent_total={payload['coupons_sent_total']})"
        )
        self.stdout.write(
            f"Использовано купонов: {payload['assignments_used']} (coupons_used_total={payload['assignments_used']})"
        )
        self.stdout.write(
            f"Конверсия купонов: {payload['usage_rate_percent']}% (coupon_usage_rate={payload['usage_rate_percent']})"
        )
        self.stdout.write(
            f"Вернувшиеся гости: {payload['returned_guest_coupon']} (returned_guests_total={payload['returned_guest_coupon']})"
        )
        self.stdout.write(
            f"Доля вернувшихся: {payload['returned_guests_rate_percent']}% "
            f"(returned_guests_rate={payload['returned_guests_rate_percent']})"
        )
        self.stdout.write(
            f"Выручка по купонным заказам: {payload['revenue_net_used']} "
            f"(coupon_orders_revenue={payload['revenue_net_used']})"
        )
        self.stdout.write(
            f"Средний чек по купонам: {payload['coupon_orders_avg_check']} "
            f"(coupon_orders_avg_check={payload['coupon_orders_avg_check']})"
        )
        self.stdout.write(
            f"Поздних применений: {payload['used_late_total']} (late_usage_total={payload['used_late_total']})"
        )
        self.stdout.write(
            f"Окно returned-метрики: {payload['returned_window_days']} дней "
            f"(returned_window_days={payload['returned_window_days']})"
        )

        if payload["late_usage_rows"]:
            self.stdout.write("--- Поздние применения (late_usage_rows) ---")
            for row in payload["late_usage_rows"]:
                self.stdout.write(
                    "assignment_id={assignment_id} guest_id={guest_id} coupon_code={coupon_code} "
                    "business_date={business_date} used_at={used_at} order_number={order_number}".format(
                        assignment_id=row.get("assignment_id"),
                        guest_id=row.get("guest_id"),
                        coupon_code=row.get("coupon_code"),
                        business_date=row.get("business_date"),
                        used_at=row.get("used_at"),
                        order_number=row.get("order_number"),
                    )
                )
