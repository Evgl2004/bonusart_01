from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


class VtelemaxApiError(Exception):
    """Ошибка запроса к read-only API интеграции vtelemax."""


def _to_rfc3339_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_rfc3339_utc(raw_value: str | None) -> datetime | None:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return None
    if raw_text.endswith("Z"):
        raw_text = raw_text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw_text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(slots=True)
class VtelemaxRecipientsPage:
    items: list[dict[str, Any]]
    next_cursor: str | None
    max_seen_updated_at: datetime | None
    generated_at: datetime | None


class VtelemaxRecipientsApiClient:
    """
    Клиент read-only API vtelemax для загрузки каналов получателей.
    """

    SNAPSHOT_PATH = "/internal/integration/v1/sagur/recipients/snapshot"
    DELTA_PATH = "/internal/integration/v1/sagur/recipients/delta"

    def __init__(
        self,
        *,
        base_url: str,
        hmac_secret: str,
        timeout_seconds: float = 20.0,
    ):
        normalized_base_url = str(base_url or "").strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError("base_url is required")

        normalized_secret = str(hmac_secret or "").strip()
        if not normalized_secret:
            raise ValueError("hmac_secret is required")

        self.base_url = normalized_base_url
        self.hmac_secret = normalized_secret
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def fetch_snapshot_page(self, *, limit: int, cursor: str | None = None) -> VtelemaxRecipientsPage:
        params: dict[str, str] = {"limit": str(int(limit))}
        if cursor:
            params["cursor"] = str(cursor)
        payload = self._get_json(path=self.SNAPSHOT_PATH, params=params)
        return self._parse_page_payload(payload)

    def fetch_delta_page(
        self,
        *,
        since: datetime,
        limit: int,
        cursor: str | None = None,
    ) -> VtelemaxRecipientsPage:
        since_rfc3339 = _to_rfc3339_utc(since)
        if not since_rfc3339:
            raise ValueError("since is required for delta")

        params: dict[str, str] = {
            "since": since_rfc3339,
            "limit": str(int(limit)),
        }
        if cursor:
            params["cursor"] = str(cursor)
        payload = self._get_json(path=self.DELTA_PATH, params=params)
        return self._parse_page_payload(payload)

    def _get_json(self, *, path: str, params: dict[str, str]) -> dict[str, Any]:
        query = str(httpx.QueryParams(params))
        path_with_query = f"{path}?{query}" if query else path

        timestamp = str(int(datetime.now(tz=timezone.utc).timestamp()))
        canonical_payload = "\n".join(["GET", path_with_query, timestamp])
        signature = hmac.new(
            self.hmac_secret.encode("utf-8"),
            canonical_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "X-Sagur-Timestamp": timestamp,
            "X-Sagur-Signature": signature,
        }

        url = f"{self.base_url}{path_with_query}"
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise VtelemaxApiError(f"HTTP request failed: {exc}") from exc

        response_payload = self._safe_json_dict(response)
        if response.status_code >= 400:
            error_message = str(response_payload.get("message") or response.text or "HTTP error").strip()
            raise VtelemaxApiError(
                f"vtelemax API error status={response.status_code}: {error_message[:500]}"
            )
        return response_payload

    @staticmethod
    def _safe_json_dict(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except Exception:
            return {"message": response.text[:1000]}
        if isinstance(parsed, dict):
            return parsed
        return {"message": str(parsed)}

    def _parse_page_payload(self, payload: dict[str, Any]) -> VtelemaxRecipientsPage:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raw_items = []
        items = [row for row in raw_items if isinstance(row, dict)]

        next_cursor = payload.get("next_cursor")
        next_cursor_text = str(next_cursor).strip() if next_cursor is not None else ""
        if not next_cursor_text:
            next_cursor_text = None

        max_seen = _parse_rfc3339_utc(str(payload.get("max_seen_updated_at") or "").strip())
        generated_at = _parse_rfc3339_utc(str(payload.get("generated_at") or "").strip())
        return VtelemaxRecipientsPage(
            items=items,
            next_cursor=next_cursor_text,
            max_seen_updated_at=max_seen,
            generated_at=generated_at,
        )

