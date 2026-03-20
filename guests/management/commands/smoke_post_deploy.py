from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from redis import from_url as redis_from_url

from guests.models import BotProfile, NotificationScenario
from guests.services.notification_registry import (
    SCENARIO_CODE_BALANCE_CHANGED,
    get_registered_notification_scenario_codes,
)
from guests.services.universal_queue.maintenance import UniversalQueueMaintenanceService
from guests.services.universal_queue.redis_lanes import ProviderLaneQueue


@dataclass(frozen=True)
class SmokeSummary:
    """
    Сводка результатов smoke-проверки после деплоя.
    """

    checks_run: int
    warnings_count: int
    errors_count: int


class Command(BaseCommand):
    """
    Быстрая неразрушающая smoke-проверка продового окружения.

    Команда помогает сразу после деплоя проверить:
    1. Django system checks;
    2. доступность БД и отсутствие неприменённых миграций;
    3. доступность Redis для webhook и universal queue;
    4. базовую целостность настроек авто-сценариев;
    5. наличие токенов у активных BotProfile.
    """

    help = "Неразрушающая post-deploy smoke-проверка окружения (DB/Redis/миграции/боты)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-django-check",
            action="store_true",
            help="Пропустить django check.",
        )
        parser.add_argument(
            "--skip-db",
            action="store_true",
            help="Пропустить проверку подключений к БД.",
        )
        parser.add_argument(
            "--skip-migrations",
            action="store_true",
            help="Пропустить проверку неприменённых миграций.",
        )
        parser.add_argument(
            "--skip-redis",
            action="store_true",
            help="Пропустить проверку Redis.",
        )
        parser.add_argument(
            "--skip-scenarios",
            action="store_true",
            help="Пропустить проверку системных NotificationScenario.",
        )
        parser.add_argument(
            "--skip-bot-tokens",
            action="store_true",
            help="Пропустить проверку токенов у активных BotProfile.",
        )

    @staticmethod
    def _configured_db_aliases() -> list[str]:
        """
        Возвращает алиасы БД, где реально задано имя базы.
        """
        aliases: list[str] = []
        for alias, cfg in settings.DATABASES.items():
            if not isinstance(cfg, dict):
                continue
            if not cfg.get("ENGINE"):
                continue
            if not cfg.get("NAME"):
                continue
            aliases.append(alias)
        return aliases

    def _check_django_system(self, errors: list[str]) -> None:
        """
        Запускает встроенный `manage.py check`.
        """
        try:
            call_command("check", verbosity=0)
            self.stdout.write(self.style.SUCCESS("[ok] django check"))
        except Exception as err:
            errors.append(f"[fail] django check: {err}")

    def _check_databases(self, errors: list[str]) -> None:
        """
        Проверяет подключение к каждой настроенной БД (SELECT 1).
        """
        for alias in self._configured_db_aliases():
            try:
                connection = connections[alias]
                connection.ensure_connection()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                self.stdout.write(self.style.SUCCESS(f"[ok] db:{alias} connection"))
            except Exception as err:
                errors.append(f"[fail] db:{alias} connection: {err}")

    def _check_migrations(self, errors: list[str]) -> None:
        """
        Проверяет отсутствие неприменённых миграций на всех активных БД.
        """
        for alias in self._configured_db_aliases():
            try:
                connection = connections[alias]
                executor = MigrationExecutor(connection)
                targets = executor.loader.graph.leaf_nodes()
                plan = executor.migration_plan(targets)
                if plan:
                    errors.append(
                        f"[fail] db:{alias} unapplied migrations: {len(plan)}"
                    )
                else:
                    self.stdout.write(self.style.SUCCESS(f"[ok] db:{alias} migrations"))
            except Exception as err:
                errors.append(f"[fail] db:{alias} migrations check: {err}")

    def _check_redis(self, errors: list[str], warnings: list[str]) -> None:
        """
        Проверяет webhook Redis и universal queue Redis.
        """
        webhook_redis_url = str(getattr(settings, "REDIS_QUEUE_URL", "") or "").strip()
        uq_redis_url = str(
            getattr(settings, "UNIVERSAL_QUEUE_REDIS_URL", webhook_redis_url) or ""
        ).strip()
        namespace = str(getattr(settings, "UNIVERSAL_QUEUE_NAMESPACE", "uq:v1") or "uq:v1").strip()
        webhook_queue_name = str(getattr(settings, "REDIS_QUEUE_NAME", "webhook_queue"))
        webhook_dlq_name = str(getattr(settings, "REDIS_DLQ_NAME", "webhook_queue_dlq"))

        if not webhook_redis_url and not uq_redis_url:
            errors.append("[fail] redis urls are empty")
            return

        checked_urls: set[str] = set()

        def _ping_url(label: str, redis_url: str) -> None:
            if not redis_url:
                errors.append(f"[fail] {label} redis url is empty")
                return
            if redis_url in checked_urls:
                self.stdout.write(self.style.SUCCESS(f"[ok] {label} redis ping (shared instance)"))
                return
            client = None
            try:
                client = redis_from_url(
                    redis_url,
                    decode_responses=False,
                    socket_connect_timeout=5,
                    socket_timeout=5,
                )
                client.ping()
                checked_urls.add(redis_url)
                self.stdout.write(self.style.SUCCESS(f"[ok] {label} redis ping"))
            except Exception as err:
                errors.append(f"[fail] {label} redis ping: {err}")
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

        _ping_url("webhook", webhook_redis_url)
        _ping_url("universal", uq_redis_url)

        # Проверка длин webhook-очередей (не ошибка, а диагностическая метрика).
        webhook_client = None
        try:
            webhook_client = redis_from_url(
                webhook_redis_url,
                decode_responses=False,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            webhook_len = int(webhook_client.llen(webhook_queue_name))
            webhook_dlq_len = int(webhook_client.llen(webhook_dlq_name))
            self.stdout.write(
                self.style.SUCCESS(
                    f"[ok] webhook queues: {webhook_queue_name}={webhook_len}, {webhook_dlq_name}={webhook_dlq_len}"
                )
            )
            if webhook_dlq_len > 0:
                warnings.append(
                    f"[warn] webhook DLQ is not empty: {webhook_dlq_name}={webhook_dlq_len}"
                )
        except Exception as err:
            warnings.append(f"[warn] webhook queue lengths unavailable: {err}")
        finally:
            try:
                webhook_client.close()
            except Exception:
                pass

        # Проверка lane-метрик universal queue.
        lane_queue = None
        try:
            lane_queue = ProviderLaneQueue(redis_url=uq_redis_url, namespace=namespace)
            lane_queue.ping()
            maintenance = UniversalQueueMaintenanceService(lane_queue=lane_queue)
            snapshots = maintenance.collect_health_snapshots()
            for provider, snapshot in snapshots.items():
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[ok] uq lanes {provider}: {snapshot.redis_lane_lengths} / db={snapshot.db_status_counts}"
                    )
                )
        except Exception as err:
            errors.append(f"[fail] universal queue snapshot: {err}")
        finally:
            if lane_queue is not None:
                try:
                    lane_queue.close()
                except Exception:
                    pass

    def _check_notification_scenarios(self, warnings: list[str]) -> None:
        """
        Проверяет наличие зарегистрированных системных сценариев.
        """
        expected_codes = set(get_registered_notification_scenario_codes())
        existing_codes = set(
            NotificationScenario.objects.filter(code__in=expected_codes).values_list(
                "code", flat=True
            )
        )
        missing_codes = sorted(expected_codes - existing_codes)
        if missing_codes:
            warnings.append(
                "[warn] missing NotificationScenario codes: "
                + ", ".join(missing_codes)
            )
        else:
            self.stdout.write(self.style.SUCCESS("[ok] notification scenarios exist"))

        balance = NotificationScenario.objects.filter(
            code=SCENARIO_CODE_BALANCE_CHANGED
        ).first()
        if balance is None:
            warnings.append("[warn] balance_changed scenario not found")
        elif not balance.is_active:
            warnings.append("[warn] balance_changed scenario is inactive")
        else:
            self.stdout.write(self.style.SUCCESS("[ok] balance_changed scenario active"))

    def _check_active_bot_tokens(self, errors: list[str], warnings: list[str]) -> None:
        """
        Проверяет, что у активных ботов реально разрешается токен.
        """
        active_bots: Iterable[BotProfile] = BotProfile.objects.filter(
            is_active=True
        ).order_by("code")
        active_bots = list(active_bots)
        if not active_bots:
            warnings.append("[warn] no active BotProfile records")
            return

        resolved = 0
        for bot in active_bots:
            try:
                token = bot.resolve_token()
            except Exception as err:
                errors.append(f"[fail] bot token resolve error: code={bot.code} err={err}")
                continue
            if not token:
                errors.append(
                    f"[fail] bot token is empty: code={bot.code} provider={bot.provider_type}"
                )
                continue
            resolved += 1

        self.stdout.write(
            self.style.SUCCESS(f"[ok] bot tokens resolved: {resolved}/{len(active_bots)}")
        )

    def _print_warnings(self, warnings: list[str]) -> None:
        for item in warnings:
            self.stdout.write(self.style.WARNING(item))

    def _print_errors(self, errors: list[str]) -> None:
        for item in errors:
            self.stderr.write(self.style.ERROR(item))

    def handle(self, *args, **options):
        warnings: list[str] = []
        errors: list[str] = []
        checks_run = 0

        if not options["skip_django_check"]:
            checks_run += 1
            self._check_django_system(errors)

        if not options["skip_db"]:
            checks_run += 1
            self._check_databases(errors)

        if not options["skip_migrations"]:
            checks_run += 1
            self._check_migrations(errors)

        if not options["skip_redis"]:
            checks_run += 1
            self._check_redis(errors, warnings)

        if not options["skip_scenarios"]:
            checks_run += 1
            self._check_notification_scenarios(warnings)

        if not options["skip_bot_tokens"]:
            checks_run += 1
            self._check_active_bot_tokens(errors, warnings)

        self._print_warnings(warnings)
        self._print_errors(errors)

        summary = SmokeSummary(
            checks_run=checks_run,
            warnings_count=len(warnings),
            errors_count=len(errors),
        )
        self.stdout.write(
            f"Smoke summary: checks={summary.checks_run}, warnings={summary.warnings_count}, errors={summary.errors_count}"
        )

        if errors:
            raise CommandError("Post-deploy smoke-check завершился с ошибками.")

        self.stdout.write(self.style.SUCCESS("Post-deploy smoke-check успешно завершён."))
