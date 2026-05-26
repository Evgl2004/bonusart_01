from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from guests.services.coupon_autoscenarios import (
    CouponAutoscenarioPreview,
    CouponAutoscenarioPreviewError,
    preview_coupon_autoscenario_audience,
)
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON


class Command(BaseCommand):
    """
    Черновой расчёт аудитории купонного автосценария без выдачи купонов.
    """

    help = (
        "Считает потенциальную аудиторию купонного автосценария, доступные купоны "
        "и дефицит пула. Команда ничего не создаёт и ничего не отправляет."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--scenario-code",
            default=SCENARIO_CODE_INACTIVE_30D_COUPON,
            help="Код сценария NotificationScenario.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Лимит гостей для расчёта. Если не задан, используется max_recipients_per_run из настроек.",
        )
        parser.add_argument(
            "--sample-limit",
            type=int,
            default=20,
            help="Сколько строк примера аудитории вывести в консоль.",
        )

    def handle(self, *args, **options):
        try:
            preview = preview_coupon_autoscenario_audience(
                scenario_code=options["scenario_code"],
                limit=options["limit"],
                sample_limit=options["sample_limit"],
            )
        except CouponAutoscenarioPreviewError as exc:
            raise CommandError(str(exc)) from exc

        self._print_preview(preview)

    def _print_preview(self, preview: CouponAutoscenarioPreview) -> None:
        self.stdout.write("=== Черновой расчёт купонного автосценария ===")
        self.stdout.write(f"scenario_id={preview.scenario_id}")
        self.stdout.write(f"scenario_code={preview.scenario_code}")
        self.stdout.write(f"execution_mode={preview.execution_mode}")
        self.stdout.write(f"coupon_series={preview.coupon_series or '-'}")
        self.stdout.write(f"venue_code={preview.venue_code or '-'}")
        self.stdout.write(f"venue_name={preview.venue_name or '-'}")
        self.stdout.write(f"inactive_days_threshold={preview.inactive_days_threshold}")
        self.stdout.write(f"max_recipients_per_run={preview.max_recipients_per_run}")
        self.stdout.write("")
        self.stdout.write("=== Аудитория ===")
        self.stdout.write(f"scanned_guests={preview.scanned_guests}")
        self.stdout.write(f"matched_guests={preview.matched_guests}")
        self.stdout.write(f"sendable_guests={preview.sendable_guests}")
        self.stdout.write(f"blocked_without_channel={preview.blocked_without_channel}")
        self.stdout.write("")
        self.stdout.write("=== Купоны ===")
        self.stdout.write(f"available_coupons={preview.available_coupons}")
        self.stdout.write(f"coupon_shortage={preview.coupon_shortage}")

        if preview.warnings:
            self.stdout.write("")
            self.stdout.write("=== Предупреждения ===")
            for warning in preview.warnings:
                self.stdout.write(f"- {warning}")

        self.stdout.write("")
        self.stdout.write("=== Пример аудитории ===")
        if not preview.sample_rows:
            self.stdout.write("Нет строк для вывода.")
            return

        for row in preview.sample_rows:
            channels = ", ".join(row.sendable_channels) if row.sendable_channels else "-"
            last_visit = row.last_visit_at.isoformat() if row.last_visit_at else "-"
            self.stdout.write(
                f"guest_id={row.guest_id} phone={row.phone or '-'} "
                f"name={(row.first_name + ' ' + row.last_name).strip() or '-'} "
                f"last_visit_at={last_visit} channels={channels}"
            )
