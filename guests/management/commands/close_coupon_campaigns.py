from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from guests.services.coupon_campaign_lifecycle import CouponCampaignLifecycleService


class Command(BaseCommand):
    help = (
        "Post-campaign закрытие купонов: переводит sent->expired, reserved->canceled "
        "для завершённых купонных кампаний и ставит status_update события в vtelemax-очередь."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign-id",
            action="append",
            type=int,
            help="Ограничить закрытие конкретной кампанией (можно передать несколько раз).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Лимит кампаний за один проход.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Только расчёт статистики без записи в БД.",
        )

    def handle(self, *args, **options):
        campaign_ids_raw = options.get("campaign_id")
        if campaign_ids_raw is None:
            campaign_ids = []
        elif isinstance(campaign_ids_raw, (list, tuple)):
            campaign_ids = [int(value) for value in campaign_ids_raw if int(value) > 0]
        else:
            campaign_ids = [int(campaign_ids_raw)] if int(campaign_ids_raw) > 0 else []
        limit = max(1, int(options.get("limit") or 100))
        dry_run = bool(options.get("dry_run", False))

        service = CouponCampaignLifecycleService()
        stats = service.close_finished_campaigns(
            close_before=timezone.now(),
            campaign_ids=campaign_ids,
            limit=limit,
            dry_run=dry_run,
        ).to_dict()

        self.stdout.write("close_coupon_campaigns done")
        self.stdout.write(
            (
                f"dry_run={dry_run} campaigns_scanned={stats['campaigns_scanned']} "
                f"campaigns_processed={stats['campaigns_processed']} "
                f"campaigns_deactivated={stats['campaigns_deactivated']} "
                f"rows_canceled={stats['rows_canceled']} "
                f"dispatch_tasks_canceled={stats['dispatch_tasks_canceled']} "
                f"assignments_scanned={stats['assignments_scanned']} "
                f"assignments_canceled={stats['assignments_canceled']} "
                f"assignments_expired={stats['assignments_expired']} "
                f"assignments_released_to_pool={stats['assignments_released_to_pool']} "
                f"queue_events_created={stats['queue_events_created']} "
                f"queue_events_updated={stats['queue_events_updated']}"
            )
        )
