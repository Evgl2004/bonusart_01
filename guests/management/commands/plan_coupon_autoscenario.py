from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from guests.models import NotificationScenario
from guests.services.coupon_autoscenarios import (
    CouponAutoscenarioExecutionPlan,
    CouponAutoscenarioPreviewError,
    build_coupon_autoscenario_execution_plan,
    format_coupon_autoscenario_audience_venue_filter,
    format_coupon_autoscenario_execution_mode,
)
from guests.services.notification_registry import SCENARIO_CODE_INACTIVE_30D_COUPON


class Command(BaseCommand):
    """
    Безопасный план запуска купонного автосценария.

    Команда не резервирует купоны, не создаёт события vtelemax и не ставит
    сообщения гостям в очередь. Она нужна для диагностики связности и причин
    блокировок перед пилотом или боевым запуском.
    """

    help = (
        "Строит безопасный план купонного автосценария и показывает связность "
        "сценария уведомлений, купонной настройки, шаблона, ботов и расписания."
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
            help="Сколько подходящих по условию гостей просмотреть перед подбором купонов.",
        )
        parser.add_argument(
            "--sample-limit",
            type=int,
            default=20,
            help="Сколько запланированных пар гость/купон вывести в консоль.",
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
        scenario = (
            NotificationScenario.objects.select_related("template")
            .prefetch_related("bot_profiles")
            .filter(pk=plan.scenario_id)
            .first()
        )

        self.stdout.write("=== План запуска купонного автосценария ===")
        self.stdout.write(f"scenario_id={plan.scenario_id}")
        self.stdout.write(f"scenario_code={plan.scenario_code}")
        self.stdout.write(
            "Состояние автосценария="
            f"{format_coupon_autoscenario_execution_mode(plan.execution_mode)}"
        )
        self.stdout.write(f"can_execute={plan.can_execute}")

        if scenario is not None:
            self.stdout.write("")
            self.stdout.write("=== Связность сценария ===")
            self.stdout.write(f"notification_scenario_active={scenario.is_active}")
            self.stdout.write(f"trigger_type={scenario.get_trigger_type_display()}")
            self.stdout.write(f"template_id={scenario.template_id}")
            self.stdout.write(f"template_name={scenario.template.name}")
            active_bots = list(
                scenario.bot_profiles.filter(is_active=True).order_by("provider_type", "name", "id")
            )
            self.stdout.write(f"active_bot_profiles={len(active_bots)}")
            if active_bots:
                self.stdout.write(
                    "active_bot_profile_codes="
                    + ", ".join(f"{bot.code}:{bot.provider_type}" for bot in active_bots)
                )
            else:
                self.stdout.write("active_bot_profile_codes=-")

            schedule_codes = {
                str(code).strip()
                for code in (getattr(settings, "COUPON_AUTOSCENARIO_SCHEDULE_CODES", set()) or set())
                if str(code).strip()
            }
            self.stdout.write("")
            self.stdout.write("=== Расписание купонных автосценариев ===")
            self.stdout.write(
                "schedule_enabled="
                f"{bool(getattr(settings, 'COUPON_AUTOSCENARIO_SCHEDULE_ENABLED', False))}"
            )
            self.stdout.write(
                "schedule_cron="
                f"{getattr(settings, 'COUPON_AUTOSCENARIO_SCHEDULE_CRON', '-') or '-'}"
            )
            self.stdout.write(
                "schedule_codes="
                f"{', '.join(sorted(schedule_codes)) if schedule_codes else '-'}"
            )
            self.stdout.write(f"scenario_in_schedule_codes={plan.scenario_code in schedule_codes}")

        self.stdout.write("")
        self.stdout.write("=== Купонные настройки ===")
        self.stdout.write(f"coupon_series={plan.coupon_series or '-'}")
        self.stdout.write(f"venue_code={plan.venue_code or '-'}")
        self.stdout.write(f"venue_name={plan.venue_name or '-'}")
        self.stdout.write(
            "audience_venue_filter="
            f"{format_coupon_autoscenario_audience_venue_filter(plan.audience_venue_filter_mode)}"
        )
        self.stdout.write(f"audience_venue_code={plan.audience_venue_code or '-'}")
        self.stdout.write(f"audience_venue_name={plan.audience_venue_name or '-'}")
        self.stdout.write(f"inactive_days_threshold={plan.inactive_days_threshold}")
        self.stdout.write(f"birthday_preparation_window_days={plan.birthday_preparation_window_days}")
        self.stdout.write(f"max_recipients_per_run={plan.max_recipients_per_run}")
        self.stdout.write(f"scan_limit={plan.scan_limit}")

        self.stdout.write("")
        self.stdout.write("=== Аудитория и блокировки ===")
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
        self.stdout.write("=== Подбор купонов ===")
        self.stdout.write(f"available_coupons={plan.available_coupons}")
        self.stdout.write(f"planned_assignments={plan.planned_assignments}")
        self.stdout.write(f"coupon_shortage={plan.coupon_shortage}")

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

        safe_sample_limit = max(0, int(sample_limit))
        self.stdout.write("")
        self.stdout.write("=== Плановые пары гость/купон ===")
        if not plan.plan_items:
            self.stdout.write("Нет плановых пар.")
            return

        for item in plan.plan_items[:safe_sample_limit]:
            channels = ", ".join(item.sendable_channels) if item.sendable_channels else "-"
            last_order_venue = item.last_order_department_name or item.last_order_department_id or "-"
            coupon_rule = item.coupon_rule_label or "-"
            selection_source = item.coupon_selection_source or "-"
            self.stdout.write(
                f"guest_id={item.guest_id} phone={item.phone or '-'} "
                f"coupon={item.coupon_series}:{item.coupon_code} "
                f"venue={item.venue_name or item.venue_code or '-'} "
                f"rule={coupon_rule} selection={selection_source} "
                f"last_order_venue={last_order_venue} "
                f"birthday_date={item.birthday_date.isoformat() if item.birthday_date else '-'} "
                f"days_until_birthday={item.days_until_birthday if item.days_until_birthday is not None else '-'} "
                f"trigger_key={item.trigger_key or '-'} "
                f"valid_until={item.valid_until.isoformat()} channels={channels}"
            )
