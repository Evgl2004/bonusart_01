from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.models import Count

from guests.models import CouponAutoscenarioAssignment, DispatchTask, NotificationEvent
from guests.services.coupon_autoscenarios import (
    COUPON_AUTOSCENARIO_STATUS_REASON_DELIVERY_FAILED,
    cancel_coupon_autoscenario_assignments_after_delivery_failure,
)


class Command(BaseCommand):
    """
    Контроль полной недоставки купонных автосценариев.
    """

    help = (
        "Проверяет назначения купонов автосценариев: если все задачи доставки "
        "финально упали и успешной доставки нет, ставит безопасную отмену."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=int(getattr(settings, "COUPON_AUTOSCENARIO_DELIVERY_GUARD_BATCH_SIZE", 100) or 100),
            help="Сколько назначений-кандидатов проверить за один проход.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Показать, что было бы отменено, без изменения базы.",
        )
        parser.add_argument(
            "--force-run",
            action="store_true",
            help="Разрешить запуск даже при COUPON_AUTOSCENARIO_DELIVERY_GUARD_ENABLED=False.",
        )
        parser.add_argument(
            "--health-check",
            action="store_true",
            help="Проверить состояние контура без обработки.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Показать подробный человекочитаемый отчёт для health-check.",
        )

    @staticmethod
    def _candidate_event_count() -> int:
        return int(
            NotificationEvent.objects.filter(
                source_ref__startswith="coupon_autoscenario_assignment:",
                dispatch_tasks__status=DispatchTask.Status.FAILED,
            )
            .values("source_ref")
            .distinct()
            .count()
        )

    @staticmethod
    def _failed_task_counts() -> dict[str, int]:
        rows = (
            DispatchTask.objects.filter(
                notification_event__source_ref__startswith="coupon_autoscenario_assignment:",
                status=DispatchTask.Status.FAILED,
            )
            .values("provider_type")
            .annotate(total=Count("id"))
            .order_by("provider_type")
        )
        return {str(row["provider_type"] or "-"): int(row["total"]) for row in rows}

    def _run_health_check(self, *, verbose: bool) -> None:
        connection = connections["default"]
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

        enabled = bool(getattr(settings, "COUPON_AUTOSCENARIO_DELIVERY_GUARD_ENABLED", False))
        schedule_enabled = bool(
            getattr(settings, "COUPON_AUTOSCENARIO_DELIVERY_GUARD_SCHEDULE_ENABLED", False)
        )
        candidate_events = self._candidate_event_count()
        canceled_by_guard = CouponAutoscenarioAssignment.objects.filter(
            status=CouponAutoscenarioAssignment.Status.CANCELED,
            status_reason=COUPON_AUTOSCENARIO_STATUS_REASON_DELIVERY_FAILED,
        ).count()
        self.stdout.write(
            self.style.SUCCESS("[health] status=healthy component=run_coupon_autoscenario_delivery_guard")
        )
        self.stdout.write(
            "[health] delivery_guard_enabled=%s schedule_enabled=%s candidate_events=%s canceled_by_guard=%s"
            % (enabled, schedule_enabled, candidate_events, canceled_by_guard)
        )
        if verbose:
            self.stdout.write("Статус: здоров (status=healthy)")
            self.stdout.write(
                "Компонент: контроль недоставки купонных автосценариев "
                "(component=run_coupon_autoscenario_delivery_guard)"
            )
            self.stdout.write("База данных: доступна (db=ok)")
            self.stdout.write(
                "Контур включён: %s (delivery_guard_enabled=%s)"
                % ("да" if enabled else "нет", enabled)
            )
            self.stdout.write(
                "Плановый запуск включён: %s (schedule_enabled=%s)"
                % ("да" if schedule_enabled else "нет", schedule_enabled)
            )
            self.stdout.write(f"Событий-кандидатов с failed-задачами: {candidate_events}")
            self.stdout.write(f"Уже отменено этим контуром: {canceled_by_guard}")
            failed_by_provider = self._failed_task_counts()
            if failed_by_provider:
                self.stdout.write("Ошибки доставки по провайдерам:")
                for provider_type, total in failed_by_provider.items():
                    self.stdout.write(f"  {provider_type}: {total}")
            else:
                self.stdout.write("Ошибок доставки по купонным автосценариям нет.")

    def handle(self, *args, **options):
        force_run = bool(options.get("force_run"))
        health_check = bool(options.get("health_check"))
        verbose = bool(options.get("verbose"))
        dry_run = bool(options.get("dry_run"))
        limit = max(1, int(options.get("limit") or 100))

        if health_check:
            return self._run_health_check(verbose=verbose)

        if not force_run and not bool(getattr(settings, "COUPON_AUTOSCENARIO_DELIVERY_GUARD_ENABLED", False)):
            raise CommandError(
                "Контроль недоставки купонных автосценариев выключен: "
                "COUPON_AUTOSCENARIO_DELIVERY_GUARD_ENABLED=False."
            )

        stats = cancel_coupon_autoscenario_assignments_after_delivery_failure(
            limit=limit,
            dry_run=dry_run,
        )
        summary = stats.as_dict()
        self.stdout.write("Контроль недоставки купонных автосценариев")
        self.stdout.write(f"Режим: {'dry-run' if dry_run else 'боевой'} (dry_run={dry_run})")
        self.stdout.write(f"Лимит: {limit}")
        self.stdout.write(
            "Итог: candidate_events_scanned={candidate_events_scanned} "
            "assignments_scanned={assignments_scanned} "
            "assignments_delivered={assignments_delivered} "
            "assignments_waiting={assignments_waiting} "
            "assignments_without_tasks={assignments_without_tasks} "
            "assignments_not_final_failed={assignments_not_final_failed} "
            "assignments_canceled={assignments_canceled} "
            "queue_events_created={queue_events_created} "
            "queue_events_updated={queue_events_updated} "
            "iiko_remove_events_created={iiko_remove_events_created}".format(**summary)
        )
