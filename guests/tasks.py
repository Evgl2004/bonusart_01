"""
Фоновые задачи проекта для запуска через Django Q.
"""

import logging
from datetime import datetime, timedelta, time as dt_time

from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from .services.notification_handler_registry import run_registered_schedule_scenarios
from .services.iiko_olap_client import build_iiko_olap_client_from_settings
from .services.iiko_customer_category_sync import IikoCustomerCategorySyncService
from .services.olap_check_sync import OlapCheckSyncWorkerService
from .services.olap_control_pull import OlapControlPullOptions, OlapControlPullService
from .services.olap_live_pipeline import OlapLivePipelineService
from .services.vtelemax_coupon_sync import VtelemaxCouponSyncService
from .services.webhooks import process_recent_webhooks

logger = logging.getLogger(__name__)


def fetch_pending_webhooks() -> int:
    """
    Периодическая задача: обработка pending webhook из внешнего сервиса.
    """
    logger.info("Запуск периодической обработки веб-хуков")

    try:
        processed = int(process_recent_webhooks(period_minutes=10))
        if processed > 0:
            logger.info("Периодическая обработка веб-хуков завершена: обработано=%s", processed)
        else:
            logger.info("Периодическая обработка веб-хуков: новых событий нет")
        return processed
    except Exception as err:
        logger.exception("Ошибка периодической обработки веб-хуков: %s", err)
        return 0


def run_scheduled_notification_scenarios_task() -> int:
    """
    Периодическая задача: запуск авто-сценариев через реестр handlers.

    Возвращает суммарное количество созданных DispatchTask.
    """
    logger.info("Запуск периодического скана авто-сценариев уведомлений")
    try:
        scenario_stats = run_registered_schedule_scenarios()
        total_created_tasks = sum(int(stat.created_tasks) for stat in scenario_stats.values())
        logger.info(
            "Авто-сценарии завершены: сценариев=%s, создано задач=%s",
            len(scenario_stats),
            total_created_tasks,
        )
        return total_created_tasks
    except Exception as err:
        logger.exception("Ошибка выполнения авто-сценариев уведомлений: %s", err)
        return 0


def run_vtelemax_recipients_delta_task() -> int:
    """
    Плановый delta-синк каналов получателей из vtelemax.
    """
    if not bool(getattr(settings, "VTELEMAX_SYNC_ENABLED", False)):
        logger.info("Vtelemax sync (schedule): disabled by VTELEMAX_SYNC_ENABLED.")
        return 0
    if not bool(getattr(settings, "VTELEMAX_SYNC_SCHEDULE_ENABLED", False)):
        logger.info("Vtelemax sync (schedule): disabled by VTELEMAX_SYNC_SCHEDULE_ENABLED.")
        return 0

    try:
        call_command("sync_vtelemax_recipients", mode="delta")
        logger.info("Vtelemax sync (schedule): delta cycle completed successfully.")
        return 1
    except Exception as err:
        logger.exception("Vtelemax sync (schedule): failed: %s", err)
        return 0


def run_vtelemax_coupon_sync_queue_task() -> int:
    """
    Плановая обработка очереди отправки купонов SAGUR -> vtelemax.

    Назначение:
    1. брать из БД пачку pending/error/sent событий;
    2. доставлять события в vtelemax c HMAC-подписью;
    3. подтверждать или переводить события в retry.
    """
    if not bool(getattr(settings, "VTELEMAX_COUPON_SYNC_ENABLED", False)):
        logger.info("Vtelemax coupon sync (schedule): disabled by VTELEMAX_COUPON_SYNC_ENABLED.")
        return 0
    if not bool(getattr(settings, "VTELEMAX_COUPON_SYNC_SCHEDULE_ENABLED", False)):
        logger.info(
            "Vtelemax coupon sync (schedule): disabled by VTELEMAX_COUPON_SYNC_SCHEDULE_ENABLED."
        )
        return 0

    batch_size = max(1, int(getattr(settings, "VTELEMAX_COUPON_SYNC_BATCH_SIZE", 100) or 100))
    try:
        service = VtelemaxCouponSyncService.from_settings()
        stats = service.process_batch(limit=batch_size)
        payload = stats.to_dict()
        logger.info(
            (
                "Vtelemax coupon sync (schedule): scanned=%s processed=%s acked=%s failed=%s "
                "skipped_max_attempts=%s assignments_acked=%s status_updates_acked=%s"
            ),
            payload["scanned"],
            payload["processed"],
            payload["acked"],
            payload["failed"],
            payload["skipped_max_attempts"],
            payload["assignments_acked"],
            payload["status_updates_acked"],
        )
        return int(payload["processed"])
    except Exception as err:
        logger.exception("Vtelemax coupon sync (schedule): failed: %s", err)
        return 0


def run_iiko_customer_category_sync_queue_task() -> int:
    """
    Плановая обработка очереди категорий гостей SAGUR -> iikoCard.

    Контур нужен для купонов: гость получает сообщение только после того,
    как iikoCard подтвердил добавление категории «Активный купон SAGUR».
    """
    if not bool(getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED", False)):
        logger.info(
            "iikoCard category sync (schedule): disabled by IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED."
        )
        return 0
    if not bool(getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_SCHEDULE_ENABLED", False)):
        logger.info(
            "iikoCard category sync (schedule): disabled by IIKO_CUSTOMER_CATEGORY_SYNC_SCHEDULE_ENABLED."
        )
        return 0

    batch_size = max(1, int(getattr(settings, "IIKO_CUSTOMER_CATEGORY_SYNC_BATCH_SIZE", 100) or 100))
    try:
        service = IikoCustomerCategorySyncService.from_settings()
        stats = service.process_batch(limit=batch_size)
        payload = stats.to_dict()
        logger.info(
            (
                "iikoCard category sync (schedule): scanned=%s processed=%s acked=%s failed=%s "
                "skipped=%s skipped_max_attempts=%s add_acked=%s remove_acked=%s"
            ),
            payload["scanned"],
            payload["processed"],
            payload["acked"],
            payload["failed"],
            payload["skipped"],
            payload["skipped_max_attempts"],
            payload["add_acked"],
            payload["remove_acked"],
        )
        return int(payload["processed"])
    except Exception as err:
        logger.exception("iikoCard category sync (schedule): failed: %s", err)
        return 0


def run_coupon_autoscenarios_task() -> int:
    """
    Плановый запуск купонных автосценариев через отдельный купонный контур.

    Задача не использует старый планировщик уведомлений. Каждый сценарий
    выполнится только если он включён и его купонная настройка находится
    в состоянии «Активен».
    """
    if not bool(getattr(settings, "COUPON_AUTOSCENARIO_SCHEDULE_ENABLED", False)):
        logger.info("Coupon autoscenarios schedule: disabled by COUPON_AUTOSCENARIO_SCHEDULE_ENABLED.")
        return 0

    from .services.coupon_autoscenarios import (
        CouponAutoscenarioPreviewError,
        execute_coupon_autoscenario_automatic,
    )
    from .services.notification_registry import (
        SCENARIO_CODE_BIRTHDAY_COUPON,
        SCENARIO_CODE_FILL_BIRTHDAY_COUPON,
        SCENARIO_CODE_INACTIVE_30D_COUPON,
    )

    configured_codes = {
        str(code).strip()
        for code in (getattr(settings, "COUPON_AUTOSCENARIO_SCHEDULE_CODES", set()) or set())
        if str(code).strip()
    }
    preferred_order = (
        SCENARIO_CODE_BIRTHDAY_COUPON,
        SCENARIO_CODE_FILL_BIRTHDAY_COUPON,
        SCENARIO_CODE_INACTIVE_30D_COUPON,
    )
    scenario_codes = [
        code for code in preferred_order if code in configured_codes
    ] + sorted(configured_codes - set(preferred_order))
    if not scenario_codes:
        logger.info("Coupon autoscenarios schedule: no scenario codes configured.")
        return 0

    raw_limit = int(getattr(settings, "COUPON_AUTOSCENARIO_SCHEDULE_LIMIT", 0) or 0)
    raw_scan_limit = int(getattr(settings, "COUPON_AUTOSCENARIO_SCHEDULE_SCAN_LIMIT", 0) or 0)
    limit = raw_limit if raw_limit > 0 else None
    scan_limit = raw_scan_limit if raw_scan_limit > 0 else None

    total_created_assignments = 0
    total_queue_events = 0
    for scenario_code in scenario_codes:
        try:
            result = execute_coupon_autoscenario_automatic(
                scenario_code=scenario_code,
                limit=limit,
                scan_limit=scan_limit,
                confirm=True,
            )
        except CouponAutoscenarioPreviewError as err:
            logger.info(
                "Coupon autoscenario %s skipped: %s",
                scenario_code,
                err,
            )
            continue
        except Exception as err:
            logger.exception(
                "Coupon autoscenario %s failed unexpectedly: %s",
                scenario_code,
                err,
            )
            continue

        total_created_assignments += int(result.created_assignments)
        total_queue_events += int(result.queue_events_created)
        logger.info(
            (
                "Coupon autoscenario %s completed: run_id=%s created_assignments=%s "
                "queue_events_created=%s scanned=%s matched=%s eligible=%s"
            ),
            scenario_code,
            result.run_id,
            result.created_assignments,
            result.queue_events_created,
            result.plan.scanned_guests,
            result.plan.matched_guests,
            result.plan.eligible_guests,
        )

    logger.info(
        "Coupon autoscenarios schedule completed: scenarios=%s created_assignments=%s queue_events_created=%s",
        len(scenario_codes),
        total_created_assignments,
        total_queue_events,
    )
    return total_created_assignments


def run_coupon_campaign_close_task() -> int:
    """
    Плановое post-campaign закрытие купонов.

    Логика:
    1. Берёт завершённые купонные кампании;
    2. Переводит `sent -> expired`, `reserved -> canceled`;
    3. Ставит status_update события в sync-очередь vtelemax.
    """
    if not bool(getattr(settings, "COUPON_CAMPAIGN_CLOSE_ENABLED", True)):
        logger.info("Coupon campaign close (schedule): disabled by COUPON_CAMPAIGN_CLOSE_ENABLED.")
        return 0
    if not bool(getattr(settings, "COUPON_CAMPAIGN_CLOSE_SCHEDULE_ENABLED", False)):
        logger.info(
            "Coupon campaign close (schedule): disabled by COUPON_CAMPAIGN_CLOSE_SCHEDULE_ENABLED."
        )
        return 0

    limit = max(1, int(getattr(settings, "COUPON_CAMPAIGN_CLOSE_LIMIT", 100) or 100))
    try:
        call_command("close_coupon_campaigns", limit=limit)
        logger.info("Coupon campaign close (schedule): completed (limit=%s).", limit)
        return 1
    except Exception as err:
        logger.exception("Coupon campaign close (schedule): failed: %s", err)
        return 0


def run_coupon_autoscenario_close_task() -> int:
    if not bool(getattr(settings, "COUPON_AUTOSCENARIO_CLOSE_ENABLED", True)):
        logger.info(
            "Coupon autoscenario close (schedule): disabled by COUPON_AUTOSCENARIO_CLOSE_ENABLED."
        )
        return 0
    if not bool(getattr(settings, "COUPON_AUTOSCENARIO_CLOSE_SCHEDULE_ENABLED", False)):
        logger.info(
            "Coupon autoscenario close (schedule): disabled by COUPON_AUTOSCENARIO_CLOSE_SCHEDULE_ENABLED."
        )
        return 0

    limit = max(1, int(getattr(settings, "COUPON_AUTOSCENARIO_CLOSE_LIMIT", 100) or 100))
    try:
        call_command("close_coupon_autoscenarios", limit=limit)
        logger.info("Coupon autoscenario close (schedule): completed (limit=%s).", limit)
        return 1
    except Exception as err:
        logger.exception("Coupon autoscenario close (schedule): failed: %s", err)
        return 0


def run_coupon_autoscenario_delivery_guard_task() -> int:
    """
    Плановый контроль недоставленных купонных автосценариев.

    Если у назначения купона все задачи доставки завершились финальной ошибкой
    и ни один канал не доставил сообщение, назначение отменяется через штатные
    очереди vtelemax и iikoCard.
    """
    if not bool(getattr(settings, "COUPON_AUTOSCENARIO_DELIVERY_GUARD_ENABLED", False)):
        logger.info(
            "Контроль недоставки купонных автосценариев: выключен флагом "
            "COUPON_AUTOSCENARIO_DELIVERY_GUARD_ENABLED."
        )
        return 0
    if not bool(getattr(settings, "COUPON_AUTOSCENARIO_DELIVERY_GUARD_SCHEDULE_ENABLED", False)):
        logger.info(
            "Контроль недоставки купонных автосценариев: плановый запуск выключен флагом "
            "COUPON_AUTOSCENARIO_DELIVERY_GUARD_SCHEDULE_ENABLED."
        )
        return 0

    from .services.coupon_autoscenarios import cancel_coupon_autoscenario_assignments_after_delivery_failure

    limit = max(1, int(getattr(settings, "COUPON_AUTOSCENARIO_DELIVERY_GUARD_BATCH_SIZE", 100) or 100))
    try:
        stats = cancel_coupon_autoscenario_assignments_after_delivery_failure(
            limit=limit,
            dry_run=False,
        )
        summary = stats.as_dict()
        logger.info(
            (
                "Контроль недоставки купонных автосценариев: candidates=%s scanned=%s "
                "canceled=%s waiting=%s delivered=%s queue_created=%s queue_updated=%s"
            ),
            summary["candidate_events_scanned"],
            summary["assignments_scanned"],
            summary["assignments_canceled"],
            summary["assignments_waiting"],
            summary["assignments_delivered"],
            summary["queue_events_created"],
            summary["queue_events_updated"],
        )
        return int(summary["assignments_scanned"])
    except Exception as err:
        logger.exception("Контроль недоставки купонных автосценариев: ошибка прохода: %s", err)
        return 0


def _parse_hhmm(value: str, *, default: dt_time) -> dt_time:
    """
    Возвращает время в формате HH:MM.

    Если формат некорректный, возвращает `default`, чтобы не падать
    на ошибке конфигурации расписания.
    """
    raw_value = str(value or "").strip()
    try:
        parsed = datetime.strptime(raw_value, "%H:%M")
    except ValueError:
        return default
    return parsed.time()


def _is_time_in_window(
    *,
    now_value: dt_time,
    start_value: dt_time,
    end_value: dt_time,
) -> bool:
    """
    Проверяет, попадает ли текущее локальное время в рабочее окно.

    Поддерживает переход через полночь:
    1. `12:00 -> 23:00` — обычное окно;
    2. `12:00 -> 01:00` — окно с переходом через 00:00.
    """
    if start_value <= end_value:
        return start_value <= now_value <= end_value
    return now_value >= start_value or now_value <= end_value


def _parse_int_csv(raw_value: str) -> list[int]:
    """
    Читает CSV-список целых чисел, убирает дубликаты и невалидные токены.
    """
    values: list[int] = []
    for item in str(raw_value or "").split(","):
        token = item.strip()
        if not token:
            continue
        try:
            parsed = int(token)
        except ValueError:
            continue
        if parsed <= 0 or parsed in values:
            continue
        values.append(parsed)
    return values


def _parse_text_csv(raw_value: str) -> list[str]:
    """
    Читает CSV-список строк, убирает пустые значения и дубликаты с сохранением порядка.
    """
    values: list[str] = []
    for item in str(raw_value or "").split(","):
        token = str(item).strip()
        if not token or token in values:
            continue
        values.append(token)
    return values


def run_olap_sync_scheduled_task() -> int:
    """
    Плановая задача дозагрузки OLAP.

    Логика:
    1. Проверяет включение флагом и рабочее окно времени;
    2. Делает ровно один проход (`one-shot`);
    3. Возвращает число обработанных записей журнала.
    """
    if not bool(getattr(settings, "OLAP_SYNC_SCHEDULE_ENABLED", False)):
        logger.info("OLAP sync (schedule): выключено флагом OLAP_SYNC_SCHEDULE_ENABLED.")
        return 0

    now_local = timezone.localtime()
    start_time = _parse_hhmm(
        str(getattr(settings, "OLAP_SYNC_WINDOW_START_LOCAL", "12:00")),
        default=dt_time(12, 0),
    )
    end_time = _parse_hhmm(
        str(getattr(settings, "OLAP_SYNC_WINDOW_END_LOCAL", "01:00")),
        default=dt_time(1, 0),
    )

    if not _is_time_in_window(
        now_value=now_local.time(),
        start_value=start_time,
        end_value=end_time,
    ):
        logger.info(
            "OLAP sync (schedule): вне рабочего окна, now=%s window=%s-%s.",
            now_local.strftime("%H:%M"),
            start_time.strftime("%H:%M"),
            end_time.strftime("%H:%M"),
        )
        return 0

    client = build_iiko_olap_client_from_settings()
    worker_service = OlapCheckSyncWorkerService(
        client=client,
        claim_limit=max(1, int(getattr(settings, "OLAP_SYNC_SCHEDULE_CLAIM_LIMIT", 100))),
        portion_size=max(1, int(getattr(settings, "OLAP_SYNC_SCHEDULE_PORTION_SIZE", 50))),
        max_attempts=max(1, int(getattr(settings, "OLAP_SYNC_SCHEDULE_MAX_ATTEMPTS", 5))),
        retry_base_seconds=max(
            1,
            int(getattr(settings, "OLAP_SYNC_SCHEDULE_RETRY_BASE_SECONDS", 120)),
        ),
        lock_timeout_seconds=max(
            60,
            int(getattr(settings, "OLAP_SYNC_SCHEDULE_LOCK_TIMEOUT_SECONDS", 900)),
        ),
    )
    try:
        stats = worker_service.run_iteration()
        logger.info(
            (
                "OLAP sync (schedule): claimed=%s loaded=%s retry=%s failed=%s skipped=%s "
                "raw_created=%s raw_duplicates=%s portions_ok=%s portions_fail=%s"
            ),
            stats.claimed_rows,
            stats.loaded_rows,
            stats.retry_rows,
            stats.failed_rows,
            stats.skipped_rows,
            stats.raw_rows_created,
            stats.raw_rows_duplicates,
            stats.successful_portions,
            stats.failed_portions,
        )
        return int(stats.claimed_rows)
    except Exception as err:
        logger.exception("OLAP sync (schedule): ошибка one-shot прохода: %s", err)
        return 0
    finally:
        client.close()


def run_olap_live_pipeline_queue_task() -> int:
    """
    Плановая задача оперативного OLAP-конвейера.

    Обрабатывает только свежую очередь `olap_live_pipeline_queue`, не запускает
    полный пересчёт аналитических витрин и общий хвост `order_fact`.
    """
    if not bool(getattr(settings, "OLAP_LIVE_PIPELINE_ENABLED", False)):
        logger.info("Оперативный OLAP-конвейер: выключен флагом OLAP_LIVE_PIPELINE_ENABLED.")
        return 0
    if not bool(getattr(settings, "OLAP_LIVE_PIPELINE_SCHEDULE_ENABLED", False)):
        logger.info("Оперативный OLAP-конвейер: плановый запуск выключен флагом OLAP_LIVE_PIPELINE_SCHEDULE_ENABLED.")
        return 0

    client = build_iiko_olap_client_from_settings()
    try:
        service = OlapLivePipelineService.from_settings(client=client)
        stats = service.process_batch()
        summary = stats.to_dict()
        logger.info(
            (
                "Оперативный OLAP-конвейер: claimed=%s processed=%s waiting_olap=%s "
                "facts_built=%s coupon_synced=%s done=%s retry=%s failed=%s"
            ),
            summary["claimed"],
            summary["processed"],
            summary["waiting_olap"],
            summary["facts_built"],
            summary["coupon_synced"],
            summary["done"],
            summary["retried"],
            summary["failed"],
        )
        return int(summary["processed"])
    except Exception as err:  # noqa: BLE001
        logger.exception("Оперативный OLAP-конвейер: ошибка планового прохода: %s", err)
        return 0
    finally:
        client.close()


def run_olap_rebuild_scheduled_task() -> int:
    """
    Плановый пересчёт аналитических витрин OLAP (one-shot).

    Для стабильной эксплуатации используется `run_olap_pipeline --once`
    с отключённым шагом OLAP sync (`--skip-olap-sync`), так как дозагрузка
    выполняется отдельной плановой задачей.
    """
    if not bool(getattr(settings, "OLAP_REBUILD_SCHEDULE_ENABLED", False)):
        logger.info("OLAP rebuild (schedule): выключено флагом OLAP_REBUILD_SCHEDULE_ENABLED.")
        return 0

    call_options = {
        "once": True,
        "skip_olap_sync": True,
        "continue_on_step_error": bool(
            getattr(settings, "OLAP_REBUILD_SCHEDULE_CONTINUE_ON_STEP_ERROR", True)
        ),
        "batch_size": max(100, int(getattr(settings, "OLAP_REBUILD_SCHEDULE_BATCH_SIZE", 2000))),
    }

    department_id = str(getattr(settings, "OLAP_REBUILD_SCHEDULE_DEPARTMENT_ID", "") or "").strip()
    if department_id:
        call_options["department_id"] = department_id

    window_days = _parse_int_csv(
        str(getattr(settings, "OLAP_REBUILD_SCHEDULE_WINDOW_DAYS", "7,14,30,60,180"))
    )
    if window_days:
        call_options["window_days"] = [str(value) for value in window_days]

    if bool(getattr(settings, "OLAP_REBUILD_SCHEDULE_USE_TODAY_AS_OF_DATE", True)):
        call_options["as_of_date"] = timezone.localdate().isoformat()

    try:
        call_command("run_olap_pipeline", **call_options)
        logger.info("OLAP rebuild (schedule): витрины пересчитаны успешно.")
        return 1
    except Exception as err:
        logger.exception("OLAP rebuild (schedule): ошибка пересчёта витрин: %s", err)
        return 0


def _build_tail_window_dates(*, tail_days: int, end_lag_days: int):
    """
    Build date window [date_from, date_to] for incremental recalculation.
    """
    safe_tail_days = max(1, int(tail_days))
    safe_end_lag_days = max(0, int(end_lag_days))

    date_to = timezone.localdate() - timedelta(days=safe_end_lag_days)
    date_from = date_to - timedelta(days=safe_tail_days - 1)
    return date_from, date_to


def run_order_fact_scheduled_task() -> int:
    """
    Scheduled one-shot order_fact rebuild for the latest N-day tail.
    """
    if not bool(getattr(settings, "OLAP_ORDER_FACT_SCHEDULE_ENABLED", False)):
        logger.info("Order fact (schedule): disabled by OLAP_ORDER_FACT_SCHEDULE_ENABLED.")
        return 0

    tail_days = max(1, int(getattr(settings, "OLAP_ORDER_FACT_SCHEDULE_TAIL_DAYS", 3)))
    end_lag_days = max(0, int(getattr(settings, "OLAP_ORDER_FACT_SCHEDULE_END_LAG_DAYS", 0)))
    batch_size = max(100, int(getattr(settings, "OLAP_ORDER_FACT_SCHEDULE_BATCH_SIZE", 2000)))
    date_from, date_to = _build_tail_window_dates(tail_days=tail_days, end_lag_days=end_lag_days)

    try:
        call_command(
            "sync_order_fact",
            once=True,
            business_date_from=date_from.isoformat(),
            business_date_to=date_to.isoformat(),
            batch_size=batch_size,
        )
        if bool(getattr(settings, "COUPON_REDEMPTION_SYNC_ENABLED", True)):
            coupon_sync_limit = max(
                0,
                int(getattr(settings, "COUPON_REDEMPTION_SYNC_LIMIT", 0)),
            )
            coupon_call_options = {
                "business_date_from": date_from.isoformat(),
                "business_date_to": date_to.isoformat(),
            }
            if coupon_sync_limit > 0:
                coupon_call_options["limit"] = coupon_sync_limit
            call_command("sync_coupon_redemptions", **coupon_call_options)
            logger.info(
                "Coupon redemption sync (schedule): completed for range %s..%s (limit=%s).",
                date_from.isoformat(),
                date_to.isoformat(),
                coupon_sync_limit or "no-limit",
            )
        logger.info(
            "Order fact (schedule): completed for range %s..%s (tail_days=%s, end_lag_days=%s).",
            date_from.isoformat(),
            date_to.isoformat(),
            tail_days,
            end_lag_days,
        )
        return 1
    except Exception as err:
        logger.exception(
            "Order fact (schedule): failed for range %s..%s: %s",
            date_from.isoformat(),
            date_to.isoformat(),
            err,
        )
        return 0


def run_daily_fact_scheduled_task() -> int:
    """
    Scheduled one-shot daily category fact rebuild for the latest N-day tail.
    """
    if not bool(getattr(settings, "OLAP_DAILY_FACT_SCHEDULE_ENABLED", False)):
        logger.info("Daily fact (schedule): disabled by OLAP_DAILY_FACT_SCHEDULE_ENABLED.")
        return 0

    tail_days = max(1, int(getattr(settings, "OLAP_DAILY_FACT_SCHEDULE_TAIL_DAYS", 3)))
    end_lag_days = max(0, int(getattr(settings, "OLAP_DAILY_FACT_SCHEDULE_END_LAG_DAYS", 0)))
    batch_size = max(100, int(getattr(settings, "OLAP_DAILY_FACT_SCHEDULE_BATCH_SIZE", 2000)))
    date_from, date_to = _build_tail_window_dates(tail_days=tail_days, end_lag_days=end_lag_days)

    try:
        call_command(
            "sync_daily_category_fact",
            once=True,
            business_date_from=date_from.isoformat(),
            business_date_to=date_to.isoformat(),
            batch_size=batch_size,
        )
        logger.info(
            "Daily fact (schedule): completed for range %s..%s (tail_days=%s, end_lag_days=%s).",
            date_from.isoformat(),
            date_to.isoformat(),
            tail_days,
            end_lag_days,
        )
        return 1
    except Exception as err:
        logger.exception(
            "Daily fact (schedule): failed for range %s..%s: %s",
            date_from.isoformat(),
            date_to.isoformat(),
            err,
        )
        return 0


def run_daily_order_fact_scheduled_task() -> int:
    """
    Scheduled one-shot daily_order_fact rebuild for the latest N-day tail.
    """
    if not bool(getattr(settings, "OLAP_DAILY_ORDER_FACT_SCHEDULE_ENABLED", False)):
        logger.info("Daily order fact (schedule): disabled by OLAP_DAILY_ORDER_FACT_SCHEDULE_ENABLED.")
        return 0

    tail_days = max(1, int(getattr(settings, "OLAP_DAILY_ORDER_FACT_SCHEDULE_TAIL_DAYS", 3)))
    end_lag_days = max(0, int(getattr(settings, "OLAP_DAILY_ORDER_FACT_SCHEDULE_END_LAG_DAYS", 0)))
    batch_size = max(100, int(getattr(settings, "OLAP_DAILY_ORDER_FACT_SCHEDULE_BATCH_SIZE", 2000)))
    date_from, date_to = _build_tail_window_dates(tail_days=tail_days, end_lag_days=end_lag_days)

    call_options = {
        "once": True,
        "business_date_from": date_from.isoformat(),
        "business_date_to": date_to.isoformat(),
        "batch_size": batch_size,
    }
    department_id = str(
        getattr(settings, "OLAP_DAILY_ORDER_FACT_SCHEDULE_DEPARTMENT_ID", "") or ""
    ).strip()
    if department_id:
        call_options["department_id"] = department_id

    try:
        call_command("sync_daily_order_fact", **call_options)
        logger.info(
            "Daily order fact (schedule): completed for range %s..%s (tail_days=%s, end_lag_days=%s).",
            date_from.isoformat(),
            date_to.isoformat(),
            tail_days,
            end_lag_days,
        )
        return 1
    except Exception as err:
        logger.exception(
            "Daily order fact (schedule): failed for range %s..%s: %s",
            date_from.isoformat(),
            date_to.isoformat(),
            err,
        )
        return 0


def run_order_focus_fact_scheduled_task() -> int:
    """
    Scheduled one-shot order_focus_fact rebuild for the latest N-day tail.
    """
    if not bool(getattr(settings, "OLAP_ORDER_FOCUS_FACT_SCHEDULE_ENABLED", False)):
        logger.info("Order focus fact (schedule): disabled by OLAP_ORDER_FOCUS_FACT_SCHEDULE_ENABLED.")
        return 0

    tail_days = max(1, int(getattr(settings, "OLAP_ORDER_FOCUS_FACT_SCHEDULE_TAIL_DAYS", 3)))
    end_lag_days = max(0, int(getattr(settings, "OLAP_ORDER_FOCUS_FACT_SCHEDULE_END_LAG_DAYS", 0)))
    batch_size = max(100, int(getattr(settings, "OLAP_ORDER_FOCUS_FACT_SCHEDULE_BATCH_SIZE", 2000)))
    date_from, date_to = _build_tail_window_dates(tail_days=tail_days, end_lag_days=end_lag_days)

    call_options = {
        "once": True,
        "business_date_from": date_from.isoformat(),
        "business_date_to": date_to.isoformat(),
        "batch_size": batch_size,
    }
    department_id = str(
        getattr(settings, "OLAP_ORDER_FOCUS_FACT_SCHEDULE_DEPARTMENT_ID", "") or ""
    ).strip()
    if department_id:
        call_options["department_id"] = department_id

    try:
        call_command("sync_order_focus_fact", **call_options)
        logger.info(
            "Order focus fact (schedule): completed for range %s..%s (tail_days=%s, end_lag_days=%s).",
            date_from.isoformat(),
            date_to.isoformat(),
            tail_days,
            end_lag_days,
        )
        return 1
    except Exception as err:
        logger.exception(
            "Order focus fact (schedule): failed for range %s..%s: %s",
            date_from.isoformat(),
            date_to.isoformat(),
            err,
        )
        return 0


def run_window_metrics_scheduled_task() -> int:
    """
    Scheduled one-shot rebuild of rolling window metrics.
    """
    if not bool(getattr(settings, "OLAP_WINDOW_METRICS_SCHEDULE_ENABLED", False)):
        logger.info("Window metrics (schedule): disabled by OLAP_WINDOW_METRICS_SCHEDULE_ENABLED.")
        return 0

    as_of_lag_days = max(
        0,
        int(getattr(settings, "OLAP_WINDOW_METRICS_SCHEDULE_AS_OF_LAG_DAYS", 0)),
    )
    as_of_date = timezone.localdate() - timedelta(days=as_of_lag_days)
    batch_size = max(
        100,
        int(getattr(settings, "OLAP_WINDOW_METRICS_SCHEDULE_BATCH_SIZE", 2000)),
    )

    window_days = _parse_int_csv(
        str(getattr(settings, "OLAP_WINDOW_METRICS_SCHEDULE_WINDOW_DAYS", "7,14,30,60,180"))
    )
    if not window_days:
        window_days = [7, 14, 30, 60, 180]

    call_options = {
        "once": True,
        "as_of_date": as_of_date.isoformat(),
        "window_days": [str(value) for value in window_days],
        "batch_size": batch_size,
    }

    department_id = str(
        getattr(settings, "OLAP_WINDOW_METRICS_SCHEDULE_DEPARTMENT_ID", "") or ""
    ).strip()
    if department_id:
        call_options["department_id"] = department_id

    try:
        call_command("sync_window_metrics", **call_options)
        logger.info(
            "Window metrics (schedule): completed as_of=%s windows=%s.",
            as_of_date.isoformat(),
            ",".join(str(value) for value in window_days),
        )
        return 1
    except Exception as err:
        logger.exception(
            "Window metrics (schedule): failed as_of=%s: %s",
            as_of_date.isoformat(),
            err,
        )
        return 0


def run_window_category_metrics_scheduled_task() -> int:
    """
    Scheduled one-shot rebuild of category-window metrics.
    """
    if not bool(getattr(settings, "OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_ENABLED", False)):
        logger.info(
            "Window category metrics (schedule): disabled by OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_ENABLED."
        )
        return 0

    as_of_lag_days = max(
        0,
        int(getattr(settings, "OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_AS_OF_LAG_DAYS", 0)),
    )
    as_of_date = timezone.localdate() - timedelta(days=as_of_lag_days)
    batch_size = max(
        100,
        int(getattr(settings, "OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_BATCH_SIZE", 2000)),
    )

    window_days = _parse_int_csv(
        str(getattr(settings, "OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_WINDOW_DAYS", "7,14,30,60,180"))
    )
    if not window_days:
        window_days = [7, 14, 30, 60, 180]

    call_options = {
        "once": True,
        "as_of_date": as_of_date.isoformat(),
        "window_days": [str(value) for value in window_days],
        "batch_size": batch_size,
    }

    department_id = str(
        getattr(settings, "OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_DEPARTMENT_ID", "") or ""
    ).strip()
    if department_id:
        call_options["department_id"] = department_id

    try:
        call_command("sync_window_category_metrics", **call_options)
        logger.info(
            "Window category metrics (schedule): completed as_of=%s windows=%s.",
            as_of_date.isoformat(),
            ",".join(str(value) for value in window_days),
        )
        return 1
    except Exception as err:
        logger.exception(
            "Window category metrics (schedule): failed as_of=%s: %s",
            as_of_date.isoformat(),
            err,
        )
        return 0


def run_olap_control_pull_scheduled_task() -> int:
    """
    Плановая контрольная дозагрузка OLAP-журнала по прямому OLAP-срезу.

    Логика:
    1. Берёт активные Department.Id из mapping (или фильтр из env);
    2. Загружает заказы за tail-окно последних N дней;
    3. Ставит недостающие задачи в `olap_check_sync_journal`.
    """
    if not bool(getattr(settings, "OLAP_CONTROL_PULL_SCHEDULE_ENABLED", False)):
        logger.info("OLAP control pull (schedule): disabled by OLAP_CONTROL_PULL_SCHEDULE_ENABLED.")
        return 0

    tail_days = max(1, int(getattr(settings, "OLAP_CONTROL_PULL_SCHEDULE_TAIL_DAYS", 2)))
    date_to = timezone.localdate()
    date_from = date_to - timedelta(days=tail_days - 1)
    dry_run = bool(getattr(settings, "OLAP_CONTROL_PULL_SCHEDULE_DRY_RUN", False))
    department_ids = set(
        _parse_text_csv(str(getattr(settings, "OLAP_CONTROL_PULL_SCHEDULE_DEPARTMENT_IDS", "") or ""))
    )

    client = build_iiko_olap_client_from_settings()
    service = OlapControlPullService(
        client=client,
        phone_denylist=set(getattr(settings, "OLAP_CONTROL_PULL_PHONE_DENYLIST", set()) or set()),
    )
    try:
        stats = service.run_cycle(
            options=OlapControlPullOptions(
                business_date_from=date_from,
                business_date_to=date_to,
                department_ids=(department_ids or None),
                dry_run=dry_run,
            )
        )
        logger.info(
            (
                "OLAP control pull (schedule): range=%s..%s departments=%s failed=%s rows=%s orders=%s "
                "would_create=%s created=%s duplicates=%s skipped_invalid=%s skipped_deleted=%s "
                "skipped_blacklist=%s dry_run=%s"
            ),
            date_from.isoformat(),
            date_to.isoformat(),
            stats.departments_scanned,
            stats.departments_failed,
            stats.olap_rows_seen,
            stats.distinct_order_keys_seen,
            stats.would_create_journal_rows,
            stats.created_journal_rows,
            stats.duplicate_journal_rows,
            stats.skipped_invalid_rows,
            stats.olap_rows_deleted_with_writeoff,
            stats.olap_rows_blacklisted_phone,
            dry_run,
        )
        return int(stats.created_journal_rows if not dry_run else stats.would_create_journal_rows)
    except Exception as err:
        logger.exception(
            "OLAP control pull (schedule): failed for range %s..%s: %s",
            date_from.isoformat(),
            date_to.isoformat(),
            err,
        )
        return 0
    finally:
        client.close()
