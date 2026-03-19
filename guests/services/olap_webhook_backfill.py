"""
Сервис исторического прогона webhook -> olap_check_sync_journal.

Назначение:
1. Читать входящие webhook по страницам из внутреннего API;
2. Фильтровать события (по типу уведомления и бизнес-фильтрам запроса);
3. Идемпотентно ставить задачи в `OlapCheckSyncJournal`;
4. Поддерживать безопасный режим `dry-run` и backpressure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import time
from typing import Any, Callable, Iterable, Iterator, Optional

import requests

from guests.models import OlapCheckSyncJournal
from guests.services.olap_webhook_bridge import enqueue_olap_sync_from_webhook
from guests.services.webhooks import find_guest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OlapWebhookBackfillOptions:
    """
    Параметры одного цикла исторического прогона webhook.
    """

    dry_run: bool
    date_from: str
    date_to: str | None
    page_size: int
    max_pages_per_cycle: int
    sleep_between_pages_seconds: float
    pause_queue_gt: int
    resume_queue_lt: int
    statuses: list[str]
    business_statuses: list[str]
    category_external_ids: list[str]
    allowed_notification_types: set[int]


@dataclass
class OlapWebhookBackfillStats:
    """
    Статистика одного цикла исторического прогона.
    """

    queue_depth: int = 0
    paused_by_backpressure: bool = False
    pages_fetched: int = 0
    webhooks_seen: int = 0
    filtered_by_notification_type: int = 0
    skipped_without_order_number: int = 0
    would_enqueue: int = 0
    created_rows: int = 0
    duplicate_rows: int = 0
    other_skipped_rows: int = 0
    processing_errors: int = 0


class OlapWebhookBackfillService:
    """
    Сервис фонового/разового переноса webhook-событий в OLAP-журнал.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        auth_timeout_seconds: float = 10.0,
        request_timeout_seconds: float = 20.0,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.username = str(username or "").strip()
        self.password = str(password or "").strip()
        self.auth_timeout_seconds = max(1.0, float(auth_timeout_seconds))
        self.request_timeout_seconds = max(1.0, float(request_timeout_seconds))
        self._cached_access_token: str | None = None
        self._cached_access_expires_at: float = 0.0
        self._backpressure_active = False
        self._session = requests.Session()

    def close(self) -> None:
        """
        Закрывает HTTP-сессию сервиса.
        """
        self._session.close()

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _queue_depth() -> int:
        """
        Текущая глубина очереди дозагрузки OLAP (статусы `new|retry`).
        """
        return int(
            OlapCheckSyncJournal.objects.filter(
                status__in=[
                    OlapCheckSyncJournal.Status.NEW,
                    OlapCheckSyncJournal.Status.RETRY,
                ]
            ).count()
        )

    def _build_webhook_url(self) -> str:
        return f"{self.base_url}/api/internal/webhooks/"

    def _build_token_url(self) -> str:
        return f"{self.base_url}/api/token/"

    def _get_access_token(self, *, force_refresh: bool = False) -> str:
        now_ts = time.time()
        if (
            not force_refresh
            and self._cached_access_token
            and now_ts < self._cached_access_expires_at
        ):
            return self._cached_access_token

        response = self._session.post(
            self._build_token_url(),
            json={"username": self.username, "password": self.password},
            timeout=self.auth_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        access_token = str(payload.get("access") or "").strip()
        if not access_token:
            raise ValueError("SAGUR /api/token/ вернул ответ без access-токена.")

        # Access-токен SAGUR живет 15 минут. Делаем небольшой запас.
        self._cached_access_token = access_token
        self._cached_access_expires_at = now_ts + 14 * 60
        return access_token

    def _build_query_params(self, options: OlapWebhookBackfillOptions) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": max(1, int(options.page_size))}
        if options.date_from:
            params["date_from"] = options.date_from
        if options.date_to:
            params["date_to"] = options.date_to
        if options.statuses:
            params["status"] = list(options.statuses)
        if options.business_statuses:
            params["business_status"] = list(options.business_statuses)
        if options.category_external_ids:
            params["category_id_ext"] = list(options.category_external_ids)
        return params

    def _fetch_webhook_page(
        self,
        *,
        url: str,
        params: Optional[dict[str, Any]],
        access_token: str,
    ) -> tuple[list[dict[str, Any]], str | None, str]:
        headers = {"Authorization": f"Bearer {access_token}"}
        response = self._session.get(
            url,
            params=params,
            headers=headers,
            timeout=self.request_timeout_seconds,
        )

        if response.status_code == 401:
            refreshed_token = self._get_access_token(force_refresh=True)
            headers = {"Authorization": f"Bearer {refreshed_token}"}
            response = self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.request_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            return self._extract_items_from_page(payload), self._extract_next_url(payload), refreshed_token

        response.raise_for_status()
        payload = response.json()
        return self._extract_items_from_page(payload), self._extract_next_url(payload), access_token

    @staticmethod
    def _extract_items_from_page(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list):
                return [item for item in results if isinstance(item, dict)]
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_next_url(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        next_url = payload.get("next")
        if next_url is None:
            return None
        safe_next_url = str(next_url).strip()
        return safe_next_url or None

    def _iter_webhook_pages(
        self,
        *,
        options: OlapWebhookBackfillOptions,
        stop_requested: Callable[[], bool] | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        access_token = self._get_access_token()
        current_url = self._build_webhook_url()
        current_params: Optional[dict[str, Any]] = self._build_query_params(options)

        pages_left = max(1, int(options.max_pages_per_cycle))
        while current_url and pages_left > 0:
            if stop_requested is not None and stop_requested():
                return

            items, next_url, access_token = self._fetch_webhook_page(
                url=current_url,
                params=current_params,
                access_token=access_token,
            )
            yield items

            pages_left -= 1
            if pages_left <= 0:
                return

            if not next_url:
                return

            current_url = next_url
            current_params = None

            sleep_seconds = max(0.0, float(options.sleep_between_pages_seconds))
            if sleep_seconds > 0:
                remaining = sleep_seconds
                while remaining > 0:
                    if stop_requested is not None and stop_requested():
                        return
                    step = min(0.5, remaining)
                    time.sleep(step)
                    remaining -= step

    def _check_backpressure(self, *, options: OlapWebhookBackfillOptions) -> tuple[bool, int]:
        queue_depth = self._queue_depth()
        pause_threshold = max(1, int(options.pause_queue_gt))
        resume_threshold = max(0, int(options.resume_queue_lt))

        if self._backpressure_active:
            if queue_depth < resume_threshold:
                self._backpressure_active = False
                logger.info(
                    "OLAP backfill: backpressure снят, глубина очереди=%s < resume=%s",
                    queue_depth,
                    resume_threshold,
                )
            else:
                return True, queue_depth
        elif queue_depth > pause_threshold:
            self._backpressure_active = True
            logger.warning(
                "OLAP backfill: включен backpressure, глубина очереди=%s > pause=%s",
                queue_depth,
                pause_threshold,
            )
            return True, queue_depth

        return False, queue_depth

    def run_cycle(
        self,
        *,
        options: OlapWebhookBackfillOptions,
        stop_requested: Callable[[], bool] | None = None,
        pages_override: Iterable[list[dict[str, Any]]] | None = None,
    ) -> OlapWebhookBackfillStats:
        """
        Выполняет один цикл исторического прогона.
        """
        stats = OlapWebhookBackfillStats()
        paused, queue_depth = self._check_backpressure(options=options)
        stats.queue_depth = queue_depth
        stats.paused_by_backpressure = paused
        if paused:
            return stats

        page_iterable = (
            pages_override
            if pages_override is not None
            else self._iter_webhook_pages(options=options, stop_requested=stop_requested)
        )

        for page_items in page_iterable:
            if stop_requested is not None and stop_requested():
                break

            stats.pages_fetched += 1
            for webhook in page_items:
                if stop_requested is not None and stop_requested():
                    break

                stats.webhooks_seen += 1
                event = webhook.get("parsed_body") or {}
                if not isinstance(event, dict):
                    stats.processing_errors += 1
                    continue

                notification_type = self._to_int(event.get("notificationType"))
                if (
                    options.allowed_notification_types
                    and notification_type not in options.allowed_notification_types
                ):
                    stats.filtered_by_notification_type += 1
                    continue

                order_number = self._to_int(event.get("orderNumber"))
                if order_number is None:
                    stats.skipped_without_order_number += 1
                    continue

                if options.dry_run:
                    stats.would_enqueue += 1
                    continue

                try:
                    bridge_result = enqueue_olap_sync_from_webhook(
                        webhook=webhook,
                        guest=find_guest(event),
                    )
                except Exception:
                    logger.exception(
                        "OLAP backfill: ошибка постановки webhook id=%s в OlapCheckSyncJournal",
                        webhook.get("id"),
                    )
                    stats.processing_errors += 1
                    continue

                if bridge_result.created:
                    stats.created_rows += 1
                    continue

                reason_text = str(bridge_result.reason or "").lower()
                if "дубль" in reason_text:
                    stats.duplicate_rows += 1
                elif "ordernumber" in reason_text:
                    stats.skipped_without_order_number += 1
                else:
                    stats.other_skipped_rows += 1

        return stats
