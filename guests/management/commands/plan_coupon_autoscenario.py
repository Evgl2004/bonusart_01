from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from guests.services.coupon_autoscenarios import (
    CouponAutoscenarioExecutionPlan,
    CouponAutoscenarioPreviewError,
    build_coupon_autoscenario_execution_plan,
)
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON


class Command(BaseCommand):
    """
    Safe execution plan for a coupon autoscenario.

    The command deliberately has no write mode yet: it does not reserve coupons,
    create vtelemax queue events, or send guest messages.
    """

    help = (
        "Builds a safe execution plan for a coupon autoscenario. "
        "No database side effects are performed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--scenario-code",
            default=SCENARIO_CODE_INACTIVE_30D_COUPON,
            help="NotificationScenario code.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Run recipient limit. Defaults to CouponAutomationConfig.max_recipients_per_run.",
        )
        parser.add_argument(
            "--scan-limit",
            type=int,
            default=None,
            help="How many candidate guests to inspect before run limit/coupon pairing.",
        )
        parser.add_argument(
            "--sample-limit",
            type=int,
            default=20,
            help="How many planned pairs to print.",
        )

    def handle(self, *args, **options):
        try:
            plan = build_coupon_autoscenario_execution_plan(
                scenario_code=options["scenario_code"],
                limit=options["limit"],
                scan_limit=options["scan_limit"],
            )
        except CouponAutoscenarioPreviewError as exc:
            raise CommandError(str(exc)) from exc

        self._print_plan(plan=plan, sample_limit=options["sample_limit"])

    def _print_plan(self, *, plan: CouponAutoscenarioExecutionPlan, sample_limit: int) -> None:
        self.stdout.write("=== Coupon autoscenario execution plan ===")
        self.stdout.write(f"scenario_id={plan.scenario_id}")
        self.stdout.write(f"scenario_code={plan.scenario_code}")
        self.stdout.write(f"execution_mode={plan.execution_mode}")
        self.stdout.write(f"can_execute={plan.can_execute}")
        self.stdout.write(f"coupon_series={plan.coupon_series or '-'}")
        self.stdout.write(f"venue_code={plan.venue_code or '-'}")
        self.stdout.write(f"venue_name={plan.venue_name or '-'}")
        self.stdout.write(f"inactive_days_threshold={plan.inactive_days_threshold}")
        self.stdout.write(f"max_recipients_per_run={plan.max_recipients_per_run}")
        self.stdout.write(f"scan_limit={plan.scan_limit}")

        self.stdout.write("")
        self.stdout.write("=== Audience filters ===")
        self.stdout.write(f"scanned_guests={plan.scanned_guests}")
        self.stdout.write(f"matched_guests={plan.matched_guests}")
        self.stdout.write(f"sendable_guests={plan.sendable_guests}")
        self.stdout.write(f"blocked_without_channel={plan.blocked_without_channel}")
        self.stdout.write(f"blocked_existing_active_coupon={plan.blocked_existing_active_coupon}")
        self.stdout.write(f"blocked_by_cooldown={plan.blocked_by_cooldown}")
        self.stdout.write(f"blocked_by_pilot_filter={plan.blocked_by_pilot_filter}")
        self.stdout.write(
            "pilot_phone_filters="
            f"{', '.join(plan.pilot_phone_filters) if plan.pilot_phone_filters else '-'}"
        )
        self.stdout.write(
            "pilot_guest_id_filters="
            f"{', '.join(str(value) for value in plan.pilot_guest_id_filters) if plan.pilot_guest_id_filters else '-'}"
        )
        self.stdout.write(f"used_default_pilot_phone={plan.used_default_pilot_phone}")
        self.stdout.write(f"eligible_guests={plan.eligible_guests}")

        self.stdout.write("")
        self.stdout.write("=== Coupon pairing ===")
        self.stdout.write(f"available_coupons={plan.available_coupons}")
        self.stdout.write(f"planned_assignments={plan.planned_assignments}")
        self.stdout.write(f"coupon_shortage={plan.coupon_shortage}")

        if plan.blockers:
            self.stdout.write("")
            self.stdout.write("=== Blockers ===")
            for blocker in plan.blockers:
                self.stdout.write(f"- {blocker}")

        if plan.warnings:
            self.stdout.write("")
            self.stdout.write("=== Warnings ===")
            for warning in plan.warnings:
                self.stdout.write(f"- {warning}")

        safe_sample_limit = max(0, int(sample_limit))
        self.stdout.write("")
        self.stdout.write("=== Planned guest/coupon pairs ===")
        if not plan.plan_items:
            self.stdout.write("No planned pairs.")
            return

        for item in plan.plan_items[:safe_sample_limit]:
            channels = ", ".join(item.sendable_channels) if item.sendable_channels else "-"
            self.stdout.write(
                f"guest_id={item.guest_id} phone={item.phone or '-'} "
                f"coupon={item.coupon_series}:{item.coupon_code} "
                f"venue={item.venue_name or item.venue_code or '-'} "
                f"valid_until={item.valid_until.isoformat()} channels={channels}"
            )
