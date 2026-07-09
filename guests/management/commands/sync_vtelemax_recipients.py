from __future__ import annotations

from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone as dj_timezone

from guests.models import VtelemaxSyncState
from guests.services.vtelemax_integration_client import (
    VtelemaxApiError,
    VtelemaxRecipientsApiClient,
)
from guests.services.vtelemax_recipients_sync import VtelemaxApplyStats, VtelemaxRecipientsApplyService


def _parse_rfc3339_utc(raw_value: str | None) -> datetime | None:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return None
    if raw_text.endswith("Z"):
        raw_text = raw_text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw_text)
    except ValueError as exc:
        raise CommandError(f"Некорректный формат даты `{raw_value}`. Ожидается RFC3339 UTC.") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Command(BaseCommand):
    help = (
        "Синхронизирует каналы получателей из vtelemax API "
        "(snapshot/delta) и обновляет GuestBotBinding."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            choices=["snapshot", "delta"],
            default="delta",
            help="Режим синхронизации: snapshot или delta.",
        )
        parser.add_argument(
            "--since",
            default="",
            help="Переопределение since для delta (RFC3339 UTC).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Лимит строк на страницу (0 = значение из settings).",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=0,
            help="Ограничение по числу страниц (0 = без ограничения).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Не писать изменения в БД, только вывести статистику.",
        )

    def handle(self, *args, **options):
        mode = str(options.get("mode") or "delta").strip().lower()
        dry_run = bool(options.get("dry_run", False))
        limit_value = int(options.get("limit") or 0)
        max_pages = max(0, int(options.get("max_pages") or 0))
        since_override = _parse_rfc3339_utc(str(options.get("since") or "").strip())

        if not bool(getattr(settings, "VTELEMAX_SYNC_ENABLED", False)):
            raise CommandError("Синхронизация отключена: VTELEMAX_SYNC_ENABLED=false.")

        base_url = str(getattr(settings, "VTELEMAX_SYNC_BASE_URL", "") or "").strip()
        hmac_secret = str(getattr(settings, "VTELEMAX_SYNC_HMAC_SECRET", "") or "").strip()
        if not base_url or not hmac_secret:
            raise CommandError("Не заполнены VTELEMAX_SYNC_BASE_URL и/или VTELEMAX_SYNC_HMAC_SECRET.")
        require_https = bool(getattr(settings, "VTELEMAX_SYNC_REQUIRE_HTTPS", True))
        if require_https and not base_url.lower().startswith("https://"):
            raise CommandError(
                "Небезопасный VTELEMAX_SYNC_BASE_URL: требуется HTTPS "
                "(или установите VTELEMAX_SYNC_REQUIRE_HTTPS=False для доверенного внутреннего контура)."
            )

        default_limit = max(1, int(getattr(settings, "VTELEMAX_SYNC_DEFAULT_LIMIT", 1000)))
        max_limit = max(default_limit, int(getattr(settings, "VTELEMAX_SYNC_MAX_LIMIT", 5000)))
        limit = default_limit if limit_value <= 0 else max(1, min(limit_value, max_limit))

        state, _ = VtelemaxSyncState.objects.get_or_create(key="vtelemax_recipients")
        since_value = since_override
        if mode == "delta" and since_value is None:
            since_value = state.watermark
        if mode == "delta" and since_value is None:
            raise CommandError(
                "Для первого delta-прогона задайте --since=<RFC3339> или предварительно установите watermark."
            )

        client = VtelemaxRecipientsApiClient(
            base_url=base_url,
            hmac_secret=hmac_secret,
            timeout_seconds=float(getattr(settings, "VTELEMAX_SYNC_HTTP_TIMEOUT_SECONDS", 20.0)),
        )
        apply_service = VtelemaxRecipientsApplyService(
            bot_code_telegram=str(getattr(settings, "VTELEMAX_SYNC_BOT_CODE_TELEGRAM", "") or "").strip(),
            bot_code_max=str(getattr(settings, "VTELEMAX_SYNC_BOT_CODE_MAX", "") or "").strip(),
            bot_code_vk=str(getattr(settings, "VTELEMAX_SYNC_BOT_CODE_VK", "") or "").strip(),
            create_missing_guests=bool(getattr(settings, "VTELEMAX_SYNC_CREATE_MISSING_GUESTS", False)),
        )

        started_at = dj_timezone.now()
        if not dry_run:
            state.last_status = VtelemaxSyncState.Status.RUNNING
            state.last_mode = mode
            state.last_started_at = started_at
            state.last_finished_at = None
            state.last_error = None
            state.save(
                update_fields=[
                    "last_status",
                    "last_mode",
                    "last_started_at",
                    "last_finished_at",
                    "last_error",
                    "updated_at",
                ]
            )

        cursor: str | None = None
        pages = 0
        totals = VtelemaxApplyStats()
        max_seen_updated_at: datetime | None = None

        try:
            while True:
                if mode == "snapshot":
                    page = client.fetch_snapshot_page(limit=limit, cursor=cursor)
                else:
                    page = client.fetch_delta_page(since=since_value, limit=limit, cursor=cursor)

                page_stats = apply_service.apply_items(items=page.items, dry_run=dry_run)
                totals.rows_total += page_stats.rows_total
                totals.rows_created += page_stats.rows_created
                totals.rows_updated += page_stats.rows_updated
                totals.rows_skipped_stale += page_stats.rows_skipped_stale
                totals.rows_skipped_invalid += page_stats.rows_skipped_invalid
                totals.rows_not_eligible_for_guest_create += page_stats.rows_not_eligible_for_guest_create
                totals.rows_guest_unresolved += page_stats.rows_guest_unresolved
                totals.rows_binding_created += page_stats.rows_binding_created
                totals.rows_binding_updated += page_stats.rows_binding_updated
                totals.rows_binding_disabled += page_stats.rows_binding_disabled
                totals.rows_birthdate_events_created += page_stats.rows_birthdate_events_created

                if page.max_seen_updated_at and (
                    max_seen_updated_at is None or page.max_seen_updated_at > max_seen_updated_at
                ):
                    max_seen_updated_at = page.max_seen_updated_at

                pages += 1
                cursor = page.next_cursor
                if not cursor:
                    break
                if max_pages and pages >= max_pages:
                    break

            finished_at = dj_timezone.now()
            if not dry_run:
                state.last_status = VtelemaxSyncState.Status.SUCCESS
                state.last_rows = totals.rows_total
                state.last_pages = pages
                state.last_finished_at = finished_at
                state.last_success_at = finished_at
                state.last_error = None
                if mode == "delta" and max_seen_updated_at is not None:
                    state.watermark = max_seen_updated_at
                state.save(
                    update_fields=[
                        "last_status",
                        "last_rows",
                        "last_pages",
                        "last_finished_at",
                        "last_success_at",
                        "last_error",
                        "watermark",
                        "updated_at",
                    ]
                )
        except (VtelemaxApiError, CommandError, Exception) as exc:
            if not dry_run:
                state.last_status = VtelemaxSyncState.Status.ERROR
                state.last_finished_at = dj_timezone.now()
                state.last_error = str(exc)[:4000]
                state.last_rows = totals.rows_total
                state.last_pages = pages
                state.save(
                    update_fields=[
                        "last_status",
                        "last_finished_at",
                        "last_error",
                        "last_rows",
                        "last_pages",
                        "updated_at",
                    ]
                )
            raise CommandError(f"Синхронизация vtelemax завершилась с ошибкой: {exc}") from exc

        self.stdout.write("sync_vtelemax_recipients done")
        self.stdout.write(
            (
                f"mode={mode} dry_run={dry_run} pages={pages} rows_total={totals.rows_total} "
                f"created={totals.rows_created} updated={totals.rows_updated} "
                f"skipped_stale={totals.rows_skipped_stale} "
                f"invalid={totals.rows_skipped_invalid} "
                f"not_eligible_for_guest_create={totals.rows_not_eligible_for_guest_create} "
                f"guest_unresolved={totals.rows_guest_unresolved} "
                f"binding_created={totals.rows_binding_created} "
                f"binding_updated={totals.rows_binding_updated} "
                f"binding_disabled={totals.rows_binding_disabled} "
                f"birthdate_events_created={totals.rows_birthdate_events_created}"
            )
        )
        if mode == "delta":
            self.stdout.write(f"since={since_value.isoformat() if since_value else 'None'}")
            self.stdout.write(
                f"max_seen_updated_at={max_seen_updated_at.isoformat() if max_seen_updated_at else 'None'}"
            )
        self.stdout.write(f"watermark={state.watermark.isoformat() if state.watermark else 'None'}")
