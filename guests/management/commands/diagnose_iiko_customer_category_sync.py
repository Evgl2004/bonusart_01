from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from guests.models import IikoCustomerCategorySyncEvent
from guests.services.iiko_customer_category_sync import (
    get_iiko_active_coupon_category_id,
    get_iiko_active_coupon_category_name,
)


class Command(BaseCommand):
    """
    Диагностика очереди синхронизации категорий гостей iikoCard.
    """

    help = "Показывает состояние очереди iikoCard customer category без отправки событий в API."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Сколько последних проблемных событий вывести.",
        )
        parser.add_argument(
            "--guest-id",
            type=int,
            default=None,
            help="Ограничить диагностику конкретным guest_id.",
        )
        parser.add_argument(
            "--status",
            type=str,
            default="",
            help="Ограничить вывод конкретным статусом очереди.",
        )

    def handle(self, *args, **options):
        limit = max(1, int(options.get("limit") or 10))
        guest_id = options.get("guest_id")
        selected_status = str(options.get("status") or "").strip()
        now = timezone.now()
        max_attempts = max(1, int(getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_MAX_ATTEMPTS", 8) or 8))

        queryset = IikoCustomerCategorySyncEvent.objects.all()
        if guest_id:
            queryset = queryset.filter(guest_id=int(guest_id))
        if selected_status:
            queryset = queryset.filter(status=selected_status)

        due_count = queryset.filter(
            status__in=[
                IikoCustomerCategorySyncEvent.Status.PENDING,
                IikoCustomerCategorySyncEvent.Status.ERROR,
                IikoCustomerCategorySyncEvent.Status.SENT,
            ],
            attempts__lt=max_attempts,
            next_retry_at__lte=now,
        ).count()
        exhausted_count = queryset.filter(
            status__in=[
                IikoCustomerCategorySyncEvent.Status.PENDING,
                IikoCustomerCategorySyncEvent.Status.ERROR,
                IikoCustomerCategorySyncEvent.Status.SENT,
            ],
            attempts__gte=max_attempts,
        ).count()

        self.stdout.write("Диагностика очереди iikoCard категорий гостей")
        self.stdout.write(
            "Синк включён: %s (IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=%s)"
            % (
                "да" if bool(getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED", False)) else "нет",
                bool(getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED", False)),
            )
        )
        self.stdout.write(
            "Gate перед рассылкой: %s (IIKO_CUSTOMER_CATEGORY_GATE_REQUIRE_ACK=%s)"
            % (
                "да" if bool(getattr(settings, "IIKO_CUSTOMER_CATEGORY_GATE_REQUIRE_ACK", True)) else "нет",
                bool(getattr(settings, "IIKO_CUSTOMER_CATEGORY_GATE_REQUIRE_ACK", True)),
            )
        )
        self.stdout.write(
            f"Категория: {get_iiko_active_coupon_category_name()} "
            f"(category_id={get_iiko_active_coupon_category_id() or '-'})"
        )
        self.stdout.write(f"Готово к обработке: {due_count} (due={due_count})")
        self.stdout.write(f"Исчерпали попытки: {exhausted_count} (max_attempts={max_attempts})")

        self.stdout.write("")
        self.stdout.write("Срез по действиям и статусам:")
        rows = (
            queryset.values("action", "status")
            .annotate(total=Count("id"))
            .order_by("action", "status")
        )
        if not rows:
            self.stdout.write("  событий нет")
        for row in rows:
            self.stdout.write(
                "  action=%s status=%s total=%s"
                % (row["action"], row["status"], row["total"])
            )

        self.stdout.write("")
        self.stdout.write(f"Последние проблемные или пропущенные события (limit={limit}):")
        problem_events = (
            queryset.filter(
                status__in=[
                    IikoCustomerCategorySyncEvent.Status.ERROR,
                    IikoCustomerCategorySyncEvent.Status.SENT,
                    IikoCustomerCategorySyncEvent.Status.PENDING,
                    IikoCustomerCategorySyncEvent.Status.SKIPPED,
                ]
            )
            .select_related("guest", "campaign_assignment", "autoscenario_assignment")
            .order_by("-updated_at", "-id")[:limit]
        )
        if not problem_events:
            self.stdout.write("  проблемных или пропущенных событий нет")
        for event in problem_events:
            assignment_ref = "-"
            if event.campaign_assignment_id:
                assignment_ref = f"campaign_assignment:{event.campaign_assignment_id}"
            elif event.autoscenario_assignment_id:
                assignment_ref = f"autoscenario_assignment:{event.autoscenario_assignment_id}"
            self.stdout.write(
                "  id=%s event_id=%s action=%s status=%s guest_id=%s ref=%s attempts=%s next_retry_at=%s error=%s"
                % (
                    event.id,
                    event.event_id,
                    event.action,
                    event.status,
                    event.guest_id or "-",
                    assignment_ref,
                    event.attempts,
                    event.next_retry_at.isoformat() if event.next_retry_at else "-",
                    (event.last_error or "-")[:300],
                )
            )
