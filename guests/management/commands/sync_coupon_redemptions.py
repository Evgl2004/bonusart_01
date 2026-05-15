from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from guests.services.coupon_redemption_sync import CouponRedemptionSyncService


def _parse_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise CommandError(f"Некорректная дата `{raw}`. Ожидается формат YYYY-MM-DD.") from exc


class Command(BaseCommand):
    help = (
        "Синхронизирует применение купонов из order_fact в купонный реестр и "
        "ставит status_update события в очередь синхронизации vtelemax."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--business-date-from",
            default="",
            help="Нижняя граница business_date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--business-date-to",
            default="",
            help="Верхняя граница business_date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--order-fact-id-from",
            type=int,
            default=0,
            help="Нижняя граница id в order_fact.",
        )
        parser.add_argument(
            "--order-fact-id-to",
            type=int,
            default=0,
            help="Верхняя граница id в order_fact.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Ограничить количество строк order_fact с купонами для прохода.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Проверочный прогон без записи изменений в БД.",
        )

    def handle(self, *args, **options):
        date_from = _parse_date(options.get("business_date_from"))
        date_to = _parse_date(options.get("business_date_to"))
        if date_from and date_to and date_from > date_to:
            raise CommandError("`business-date-from` не может быть позже `business-date-to`.")

        order_fact_id_from = int(options.get("order_fact_id_from") or 0) or None
        order_fact_id_to = int(options.get("order_fact_id_to") or 0) or None
        if order_fact_id_from and order_fact_id_to and order_fact_id_from > order_fact_id_to:
            raise CommandError("`order-fact-id-from` не может быть больше `order-fact-id-to`.")

        limit = max(0, int(options.get("limit") or 0))
        dry_run = bool(options.get("dry_run", False))

        service = CouponRedemptionSyncService()
        stats = service.sync_from_order_facts(
            business_date_from=date_from,
            business_date_to=date_to,
            order_fact_id_from=order_fact_id_from,
            order_fact_id_to=order_fact_id_to,
            limit=limit,
            dry_run=dry_run,
        )

        self.stdout.write("=== Синхронизация статусов купонов из OLAP ===")
        self.stdout.write(
            f"Режим: {'dry-run' if dry_run else 'боевой'} (dry_run={dry_run})"
        )
        self.stdout.write(
            f"Период business_date: {date_from or '-'} .. {date_to or '-'}"
        )
        self.stdout.write(
            f"Диапазон id order_fact: {order_fact_id_from or '-'} .. {order_fact_id_to or '-'}"
        )
        self.stdout.write(f"Лимит прохода: {limit or 'без лимита'}")

        metrics = stats.to_dict()
        self.stdout.write(
            "Итог: "
            + " ".join(
                [
                    f"order_facts_total={metrics['order_facts_total']}",
                    f"order_facts_with_coupon={metrics['order_facts_with_coupon']}",
                    f"assignments_matched={metrics['assignments_matched']}",
                    f"assignments_marked_used={metrics['assignments_marked_used']}",
                    "assignments_marked_used_after_campaign="
                    f"{metrics['assignments_marked_used_after_campaign']}",
                    f"assignments_already_used={metrics['assignments_already_used']}",
                    f"assignments_guest_mismatch={metrics['assignments_guest_mismatch']}",
                    f"assignments_missing={metrics['assignments_missing']}",
                    f"queue_events_created={metrics['queue_events_created']}",
                    f"queue_events_updated={metrics['queue_events_updated']}",
                    f"registry_marked_used={metrics['registry_marked_used']}",
                ]
            )
        )
