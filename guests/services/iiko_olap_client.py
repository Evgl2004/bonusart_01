"""
Клиент интеграции с iiko OLAP API.

Назначение:
1. Авторизация в `/resto/api/auth` с кэшированием ключа.
2. Выполнение запросов в `/resto/api/v2/reports/olap`.
3. Загрузка данных порциями (по списку `OrderNum`) с повторными попытками.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import logging
import time
from typing import Any, Iterable, Sequence

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class IikoOlapError(RuntimeError):
    """Базовая ошибка клиента iiko OLAP."""


class IikoOlapAuthError(IikoOlapError):
    """Ошибка авторизации в iiko OLAP."""


class IikoOlapRequestError(IikoOlapError):
    """Ошибка запроса в iiko OLAP API."""


@dataclass
class OlapPortionLoadStats:
    """
    Метрики загрузки OLAP-строк порциями.
    """

    requested_portions: int = 0
    successful_portions: int = 0
    failed_portions: int = 0
    total_data_rows: int = 0
    total_summary_rows: int = 0
    failed_order_number_portions: list[list[int]] = field(default_factory=list)


def _normalize_date(value: date | str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _split_chunks(values: Sequence[int], chunk_size: int) -> Iterable[list[int]]:
    safe_chunk_size = max(1, int(chunk_size))
    for idx in range(0, len(values), safe_chunk_size):
        yield list(values[idx: idx + safe_chunk_size])


class IikoOlapClient:
    """
    Клиент iiko OLAP API с кэшированием ключа и retry-механизмом.
    """

    TRANSIENT_STATUSES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        base_url: str,
        login: str,
        pass_hash: str,
        auth_timeout: float = 10.0,
        request_timeout: float = 30.0,
        key_ttl_seconds: int = 240,
        max_retries: int = 3,
        retry_base_seconds: float = 1.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.login = str(login or "").strip()
        self.pass_hash = str(pass_hash or "").strip()
        self.auth_timeout = max(1.0, float(auth_timeout))
        self.request_timeout = max(1.0, float(request_timeout))
        self.key_ttl_seconds = max(30, int(key_ttl_seconds))
        self.max_retries = max(0, int(max_retries))
        self.retry_base_seconds = max(0.1, float(retry_base_seconds))

        self._session = session or requests.Session()
        self._auth_key: str | None = None
        self._auth_key_expires_at: float = 0.0

    @property
    def auth_url(self) -> str:
        return f"{self.base_url}/auth"

    @property
    def report_url(self) -> str:
        return f"{self.base_url}/v2/reports/olap"

    def close(self) -> None:
        """
        Закрывает HTTP-сессию клиента.
        """
        self._session.close()

    def _is_key_alive(self) -> bool:
        return bool(self._auth_key and time.time() < self._auth_key_expires_at)

    @staticmethod
    def _extract_key_from_auth_response(response: requests.Response) -> str:
        """
        Возвращает ключ авторизации из ответа `/resto/api/auth`.

        Поддерживаемые форматы:
        1. plain text: `<key>`;
        2. JSON: `{\"key\": \"...\"}` или `{\"token\": \"...\"}`.
        """
        text_body = str(response.text or "").strip()
        if text_body and not text_body.startswith("{"):
            return text_body

        try:
            data = response.json()
        except Exception as err:
            raise IikoOlapAuthError(
                f"Не удалось распарсить ответ авторизации как JSON: {err}"
            ) from err

        key = str(data.get("key") or data.get("token") or "").strip()
        if not key:
            raise IikoOlapAuthError(
                f"В ответе авторизации нет ключа (body={str(data)[:300]})"
            )
        return key

    def get_auth_key(self, *, force_refresh: bool = False) -> str:
        """
        Возвращает действующий auth-ключ для OLAP API.
        """
        if not force_refresh and self._is_key_alive():
            return str(self._auth_key)

        if not self.base_url or not self.login or not self.pass_hash:
            raise IikoOlapAuthError(
                "Не заданы параметры IIKO_OLAP_BASE_URL/IIKO_OLAP_LOGIN/IIKO_OLAP_PASS_HASH."
            )

        try:
            response = self._session.post(
                self.auth_url,
                data={"login": self.login, "pass": self.pass_hash},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.auth_timeout,
            )
        except requests.RequestException as err:
            raise IikoOlapAuthError(f"Сетевая ошибка авторизации в iiko OLAP: {err}") from err

        if response.status_code >= 400:
            raise IikoOlapAuthError(
                f"Ошибка авторизации iiko OLAP: status={response.status_code}, body={response.text[:300]}"
            )

        key = self._extract_key_from_auth_response(response)
        self._auth_key = key
        self._auth_key_expires_at = time.time() + self.key_ttl_seconds
        logger.info("iiko OLAP: получен новый auth key, ttl=%s сек.", self.key_ttl_seconds)
        return key

    def _request_olap_once(self, payload: dict[str, Any], *, auth_key: str) -> requests.Response:
        return self._session.post(
            self.report_url,
            params={"key": auth_key},
            json=payload,
            timeout=self.request_timeout,
        )

    def query_olap(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Выполняет один запрос в OLAP API с retry-логикой.
        """
        last_error: Exception | None = None
        key_refreshed = False

        for attempt in range(self.max_retries + 1):
            try:
                key = self.get_auth_key(force_refresh=False)
                response = self._request_olap_once(payload=payload, auth_key=key)

                if response.status_code == 401:
                    # Ключ истёк или отклонён — один раз форсируем обновление ключа
                    # и повторяем без задержки.
                    if not key_refreshed:
                        key_refreshed = True
                        self.get_auth_key(force_refresh=True)
                        logger.warning("iiko OLAP: 401, ключ обновлён, повторяем запрос.")
                        continue
                    raise IikoOlapRequestError(
                        f"OLAP вернул 401 после обновления ключа (body={response.text[:200]})"
                    )

                if response.status_code in self.TRANSIENT_STATUSES:
                    raise IikoOlapRequestError(
                        f"Временная ошибка OLAP: status={response.status_code}, body={response.text[:200]}"
                    )

                if response.status_code >= 400:
                    raise IikoOlapRequestError(
                        f"Ошибка OLAP: status={response.status_code}, body={response.text[:300]}"
                    )

                try:
                    data = response.json()
                except Exception as err:
                    raise IikoOlapRequestError(
                        f"Некорректный JSON-ответ OLAP: {err}; body={response.text[:300]}"
                    ) from err

                if not isinstance(data, dict):
                    raise IikoOlapRequestError(
                        f"Неожиданный тип ответа OLAP: {type(data).__name__}"
                    )
                return data

            except (requests.RequestException, IikoOlapError) as err:
                last_error = err if isinstance(err, Exception) else Exception(str(err))
                is_last_try = attempt >= self.max_retries
                if is_last_try:
                    break

                delay = self.retry_base_seconds * (2 ** attempt)
                logger.warning(
                    "iiko OLAP: попытка %s/%s завершилась ошибкой: %s. Повтор через %.2f сек.",
                    attempt + 1,
                    self.max_retries + 1,
                    err,
                    delay,
                )
                time.sleep(delay)

        raise IikoOlapRequestError(f"OLAP-запрос не выполнен после retries: {last_error}")

    def build_sales_payload(
        self,
        *,
        date_from: date | str,
        date_to: date | str,
        order_numbers: Sequence[int],
        department_ids: Sequence[str] | None = None,
        aggregate_fields: Sequence[str] | None = None,
        group_by_row_fields: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """
        Формирует тело OLAP-запроса для отчёта SALES.
        """
        normalized_orders = [int(order) for order in order_numbers]
        if not normalized_orders:
            raise ValueError("order_numbers пустой, нечего запрашивать в OLAP.")

        agg_fields = list(aggregate_fields or ["DishSumInt", "UniqOrderId"])
        group_fields = list(
            group_by_row_fields
            or [
                "Department.Id",
                "Department.Code",
                "Department",
                "RestaurantSection.Id",
                "RestorauntGroup.Id",
                "RestorauntGroup",
                "OpenDate.Typed",
                "OrderNum",
                "UniqOrderId.Id",
                "ItemSaleEvent.Id",
                "DishCode",
                "DishName",
                "DishCategory.Id",
                "DishCategory",
                "DishGroup.Id",
                "DishGroup",
                "CouponInfo.Series",
                "CouponInfo.Number",
            ]
        )

        filters: dict[str, Any] = {
            "OpenDate.Typed": {
                "filterType": "DateRange",
                "periodType": "CUSTOM",
                "from": _normalize_date(date_from),
                "to": _normalize_date(date_to),
                "includeHigh": True,
            },
            "OrderNum": {
                "filterType": "IncludeValues",
                "values": normalized_orders,
            },
        }

        if department_ids:
            cleaned_department_ids = [str(item).strip() for item in department_ids if str(item).strip()]
            if cleaned_department_ids:
                filters["Department.Id"] = {
                    "filterType": "IncludeValues",
                    "values": cleaned_department_ids,
                }

        return {
            "reportType": "SALES",
            "aggregateFields": agg_fields,
            "groupByRowFields": group_fields,
            "filters": filters,
        }

    def fetch_sales_in_portions(
        self,
        *,
        date_from: date | str,
        date_to: date | str,
        order_numbers: Sequence[int],
        department_ids: Sequence[str] | None = None,
        portion_size: int = 200,
        aggregate_fields: Sequence[str] | None = None,
        group_by_row_fields: Sequence[str] | None = None,
        fail_fast: bool = True,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], OlapPortionLoadStats]:
        """
        Загружает OLAP-строки по списку чеков порциями.
        """
        rows: list[dict[str, Any]] = []
        summary_rows: list[dict[str, Any]] = []
        stats = OlapPortionLoadStats()

        normalized_orders = [int(order) for order in order_numbers]
        for portion in _split_chunks(normalized_orders, max(1, int(portion_size))):
            stats.requested_portions += 1
            payload = self.build_sales_payload(
                date_from=date_from,
                date_to=date_to,
                order_numbers=portion,
                department_ids=department_ids,
                aggregate_fields=aggregate_fields,
                group_by_row_fields=group_by_row_fields,
            )
            try:
                response = self.query_olap(payload)
                data_rows = response.get("data") if isinstance(response.get("data"), list) else []
                summary = response.get("summary") if isinstance(response.get("summary"), list) else []

                rows.extend(data_rows)
                summary_rows.extend(summary)
                stats.successful_portions += 1
                stats.total_data_rows += len(data_rows)
                stats.total_summary_rows += len(summary)
            except Exception as err:
                stats.failed_portions += 1
                stats.failed_order_number_portions.append(list(portion))
                logger.exception(
                    "iiko OLAP: ошибка загрузки порции order_numbers=%s: %s",
                    portion,
                    err,
                )
                if fail_fast:
                    raise

        return rows, summary_rows, stats


def build_iiko_olap_client_from_settings() -> IikoOlapClient:
    """
    Конструирует клиент OLAP на основе переменных Django settings.
    """
    return IikoOlapClient(
        base_url=str(getattr(settings, "IIKO_OLAP_BASE_URL", "") or "").strip(),
        login=str(getattr(settings, "IIKO_OLAP_LOGIN", "") or "").strip(),
        pass_hash=str(getattr(settings, "IIKO_OLAP_PASS_HASH", "") or "").strip(),
        auth_timeout=float(getattr(settings, "IIKO_OLAP_AUTH_TIMEOUT_SECONDS", 10.0)),
        request_timeout=float(getattr(settings, "IIKO_OLAP_REQUEST_TIMEOUT_SECONDS", 30.0)),
        key_ttl_seconds=int(getattr(settings, "IIKO_OLAP_KEY_TTL_SECONDS", 240)),
        max_retries=int(getattr(settings, "IIKO_OLAP_MAX_RETRIES", 3)),
        retry_base_seconds=float(getattr(settings, "IIKO_OLAP_RETRY_BASE_SECONDS", 1.0)),
    )
