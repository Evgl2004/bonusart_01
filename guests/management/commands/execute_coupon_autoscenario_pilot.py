from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from guests.services.coupon_autoscenarios import (
    CouponAutoscenarioExecutionResult,
    CouponAutoscenarioPreviewError,
    execute_coupon_autoscenario_pilot,
    format_coupon_autoscenario_execution_mode,
)
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON


class Command(BaseCommand):
    """
    Безопасный пробный запуск купонного автосценария.

    По умолчанию команда ничего не меняет в базе. Фактическое резервирование
    купонов и постановка событий в очередь vtelemax выполняются только с
    явным флагом --confirm.
    """

    help = (
        "Безопасный пробный запуск купонного автосценария: строит план, "
        "а с --confirm резервирует купоны и создаёт события vtelemax без отправки сообщений гостям."
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
            help="Лимит гостей для прохода. Если не задан, используется max_recipients_per_run.",
        )
        parser.add_argument(
            "--scan-limit",
            type=int,
            default=None,
            help="Сколько подходящих по условию гостей просмотреть перед применением лимита прохода.",
        )
        parser.add_argument(
            "--sample-limit",
            type=int,
            default=20,
            help="Сколько запланированных пар гость/купон вывести в консоль.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Подтвердить фактическое резервирование купонов и создание событий vtelemax.",
        )

    def handle(self, *args, **options):
        try:
            result = execute_coupon_autoscenario_pilot(
                scenario_code=options["scenario_code"],
                limit=options["limit"],
                scan_limit=options["scan_limit"],
                confirm=options["confirm"],
            )
        except CouponAutoscenarioPreviewError as exc:
            raise CommandError(str(exc)) from exc

        self._print_result(result=result, sample_limit=options["sample_limit"])

    def _print_result(
        self,
        *,
        result: CouponAutoscenarioExecutionResult,
        sample_limit: int,
    ) -> None:
        plan = result.plan
        self.stdout.write("=== Пробный запуск купонного автосценария ===")
        self.stdout.write(f"dry_run={result.dry_run}")
        self.stdout.write(f"confirmed={result.confirmed}")
        self.stdout.write(f"run_id={result.run_id or '-'}")
        self.stdout.write(f"scenario_id={plan.scenario_id}")
        self.stdout.write(f"scenario_code={plan.scenario_code}")
        self.stdout.write(
            "Состояние автосценария="
            f"{format_coupon_autoscenario_execution_mode(plan.execution_mode)}"
        )
        self.stdout.write(f"can_execute={plan.can_execute}")
        self.stdout.write(f"coupon_series={plan.coupon_series or '-'}")
        self.stdout.write(f"venue_code={plan.venue_code or '-'}")
        self.stdout.write(f"venue_name={plan.venue_name or '-'}")
        self.stdout.write(f"inactive_days_threshold={plan.inactive_days_threshold}")
        self.stdout.write(f"birthday_preparation_window_days={plan.birthday_preparation_window_days}")
        self.stdout.write(f"max_recipients_per_run={plan.max_recipients_per_run}")
        self.stdout.write(f"scan_limit={plan.scan_limit}")

        self.stdout.write("")
        self.stdout.write("=== Аудитория ===")
        self.stdout.write(f"scanned_guests={plan.scanned_guests}")
        self.stdout.write(f"matched_guests={plan.matched_guests}")
        self.stdout.write(f"bot_bound_guests={plan.bot_bound_guests}")
        self.stdout.write(f"blocked_without_bot_binding={plan.blocked_without_bot_binding}")
        self.stdout.write(f"sendable_guests={plan.sendable_guests}")
        self.stdout.write(f"blocked_without_channel={plan.blocked_without_channel}")
        self.stdout.write(f"message_target_guests={plan.message_target_guests}")
        self.stdout.write(f"blocked_without_message_target={plan.blocked_without_message_target}")
        self.stdout.write(f"blocked_without_message_permission={plan.blocked_without_message_permission}")
        self.stdout.write(f"blocked_existing_active_coupon={plan.blocked_existing_active_coupon}")
        self.stdout.write(f"blocked_existing_trigger={plan.blocked_existing_trigger}")
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
        self.stdout.write(f"pilot_forced_guests={plan.pilot_forced_guests}")
        self.stdout.write(f"eligible_guests={plan.eligible_guests}")

        self.stdout.write("")
        self.stdout.write("=== Купоны и очередь ===")
        self.stdout.write(f"available_coupons={plan.available_coupons}")
        self.stdout.write(f"planned_assignments={plan.planned_assignments}")
        self.stdout.write(f"coupon_shortage={plan.coupon_shortage}")
        self.stdout.write(f"created_assignments={result.created_assignments}")
        self.stdout.write(f"queue_events_created={result.queue_events_created}")
        self.stdout.write("guest_messages_created=0")

        if plan.blockers:
            self.stdout.write("")
            self.stdout.write("=== Блокировки ===")
            for blocker in plan.blockers:
                self.stdout.write(f"- {blocker}")

        if plan.warnings:
            self.stdout.write("")
            self.stdout.write("=== Предупреждения ===")
            for warning in plan.warnings:
                self.stdout.write(f"- {warning}")

        self.stdout.write("")
        if result.dry_run:
            self.stdout.write(
                "Режим: сухой прогон. База не изменена. "
                "Для фактического резервирования добавьте --confirm."
            )
        else:
            self.stdout.write(
                "Режим: подтверждённый пробный запуск. "
                "Купоны зарезервированы, события ждут ACK vtelemax, сообщения гостям не созданы."
            )

        safe_sample_limit = max(0, int(sample_limit))
        self.stdout.write("")
        self.stdout.write("=== Плановые пары гость/купон ===")
        if not plan.plan_items:
            self.stdout.write("Нет плановых пар.")
            return

        for item in plan.plan_items[:safe_sample_limit]:
            channels = ", ".join(item.sendable_channels) if item.sendable_channels else "-"
            self.stdout.write(
                f"guest_id={item.guest_id} phone={item.phone or '-'} "
                f"coupon={item.coupon_series}:{item.coupon_code} "
                f"venue={item.venue_name or item.venue_code or '-'} "
                f"birthday_date={item.birthday_date.isoformat() if item.birthday_date else '-'} "
                f"days_until_birthday={item.days_until_birthday if item.days_until_birthday is not None else '-'} "
                f"trigger_key={item.trigger_key or '-'} "
                f"valid_until={item.valid_until.isoformat()} channels={channels}"
            )
