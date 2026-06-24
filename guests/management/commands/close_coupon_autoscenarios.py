from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from guests.services.coupon_autoscenarios import close_expired_coupon_autoscenario_assignments


class Command(BaseCommand):
    help = (
        "Close expired coupon autoscenario assignments and enqueue vtelemax status_update events."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum expired assignments to close in one pass.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Calculate statistics without writing to the database.",
        )

    def handle(self, *args, **options):
        limit = max(1, int(options.get("limit") or 100))
        dry_run = bool(options.get("dry_run", False))

        stats = close_expired_coupon_autoscenario_assignments(
            close_before=timezone.now(),
            limit=limit,
            dry_run=dry_run,
        ).as_dict()

        self.stdout.write("close_coupon_autoscenarios done")
        self.stdout.write(
            (
                f"dry_run={dry_run} "
                f"assignments_scanned={stats['assignments_scanned']} "
                f"assignments_expired={stats['assignments_expired']} "
                f"registry_marked_expired={stats['registry_marked_expired']} "
                f"queue_events_created={stats['queue_events_created']} "
                f"queue_events_updated={stats['queue_events_updated']}"
            )
        )
