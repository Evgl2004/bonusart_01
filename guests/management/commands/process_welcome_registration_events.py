from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone

from guests.models import GuestWelcomeRegistrationEvent
from guests.services.welcome_registration_events import WelcomeRegistrationEventProcessor


class Command(BaseCommand):
    """
    Обрабатывает очередь welcome-регистраций, принятых от vtelemax.
    """

    help = (
        "Применяет события регистрации vtelemax к гостям и резервирует "
        "приветственные купоны через штатный купонный контур."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Сколько событий обработать за запуск (0 = значение из settings).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Показать, сколько событий готово к обработке, без изменения базы.",
        )
        parser.add_argument(
            "--health-check",
            action="store_true",
            help="Показать диагностические счётчики очереди без обработки.",
        )
        parser.add_argument(
            "--force-run",
            action="store_true",
            help="Запустить обработку даже при WELCOME_COUPON_PROCESSING_ENABLED=false.",
        )

    def handle(self, *args, **options):
        if bool(options.get("health_check")):
            self._print_health()
            return

        if not bool(getattr(settings, "WELCOME_COUPON_PROCESSING_ENABLED", False)) and not bool(
            options.get("force_run")
        ):
            raise CommandError(
                "Обработка welcome-регистраций отключена: WELCOME_COUPON_PROCESSING_ENABLED=false."
            )

        configured_limit = int(getattr(settings, "WELCOME_COUPON_PROCESSING_BATCH_SIZE", 100) or 100)
        option_limit = int(options.get("limit") or 0)
        limit = configured_limit if option_limit <= 0 else option_limit

        processor = WelcomeRegistrationEventProcessor.from_settings()
        stats = processor.process_batch(
            limit=limit,
            now=timezone.now(),
            dry_run=bool(options.get("dry_run")),
        )

        self.stdout.write("=== Обработка welcome-регистраций vtelemax ===")
        self.stdout.write(f"dry_run={stats.dry_run}")
        self.stdout.write(f"limit={limit}")
        for key, value in stats.as_dict().items():
            self.stdout.write(f"{key}={value}")

    def _print_health(self) -> None:
        now = timezone.now()
        max_attempts = int(getattr(settings, "WELCOME_COUPON_PROCESSING_MAX_ATTEMPTS", 8) or 8)
        rows_by_status = {
            str(row["status"]): int(row["total"])
            for row in GuestWelcomeRegistrationEvent.objects.values("status")
            .annotate(total=Count("id"))
            .order_by("status")
        }
        due_count = GuestWelcomeRegistrationEvent.objects.filter(
            status__in=[
                GuestWelcomeRegistrationEvent.Status.NEW,
                GuestWelcomeRegistrationEvent.Status.CHANNEL_APPLIED,
                GuestWelcomeRegistrationEvent.Status.ERROR,
            ],
            attempts__lt=max_attempts,
            next_retry_at__lte=now,
        ).count()
        exhausted_count = GuestWelcomeRegistrationEvent.objects.filter(
            status__in=[
                GuestWelcomeRegistrationEvent.Status.NEW,
                GuestWelcomeRegistrationEvent.Status.CHANNEL_APPLIED,
                GuestWelcomeRegistrationEvent.Status.ERROR,
            ],
            attempts__gte=max_attempts,
        ).count()

        self.stdout.write("=== Диагностика welcome-регистраций vtelemax ===")
        self.stdout.write(
            "processing_enabled="
            f"{bool(getattr(settings, 'WELCOME_COUPON_PROCESSING_ENABLED', False))}"
        )
        self.stdout.write(
            "scenario_code="
            f"{str(getattr(settings, 'WELCOME_COUPON_SCENARIO_CODE', 'welcome_coupon') or 'welcome_coupon')}"
        )
        self.stdout.write(f"due_events={int(due_count)}")
        self.stdout.write(f"max_attempts_exhausted={int(exhausted_count)}")
        if not rows_by_status:
            self.stdout.write("status_total=0")
            return
        for status, total in rows_by_status.items():
            self.stdout.write(f"status.{status}={total}")
