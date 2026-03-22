# Исторический Прогон Webhook В OLAP Журнал

## Назначение
Команда `run_olap_webhook_backfill` переносит исторические webhook-события из внутреннего API
в `olap_check_sync_journal` для последующей дозагрузки чеков из OLAP.

Цепочка выглядит так:
1. `run_olap_webhook_backfill` читает webhook по страницам;
2. для `notificationType=1` ставит задачи в `olap_check_sync_journal`;
3. `run_olap_sync_worker` дозагружает строки чека в `olap_sales_raw_line`;
4. далее запускаются пересчёты витрин (`sync_olap_catalogs`, `sync_order_fact`, `sync_daily_category_fact`, `sync_window_metrics`).

## Переменные окружения
Обязательные для доступа к внутреннему webhook API:
1. `SAGUR_BASE_URL`
2. `SAGUR_USERNAME`
3. `SAGUR_PASSWORD`

Флаги и лимиты backfill:
1. `OLAP_BACKFILL_ENABLE` — глобальный флаг разрешения запуска.
2. `OLAP_BACKFILL_DRY_RUN` — режим без записи в БД.
3. `OLAP_BACKFILL_DATE_FROM` / `OLAP_BACKFILL_DATE_TO` — диапазон дат.
4. `OLAP_BACKFILL_PAGE_SIZE` — размер страницы API.
5. `OLAP_BACKFILL_MAX_PAGES_PER_CYCLE` — лимит страниц за цикл.
6. `OLAP_BACKFILL_SLEEP_BETWEEN_PAGES_SECONDS` — пауза между страницами.
7. `OLAP_BACKFILL_SLEEP_BETWEEN_CYCLES_SECONDS` — пауза между циклами в loop-режиме.
8. `OLAP_BACKFILL_PAUSE_QUEUE_GT` — верхний порог backpressure.
9. `OLAP_BACKFILL_RESUME_QUEUE_LT` — порог снятия backpressure.
10. `OLAP_BACKFILL_AUTH_TIMEOUT_SECONDS` / `OLAP_BACKFILL_REQUEST_TIMEOUT_SECONDS` — таймауты HTTP.

## Безопасный запуск (рекомендуется)
Первый прогон всегда в `dry-run`, чтобы проверить статистику без записи:

```powershell
python manage.py run_olap_webhook_backfill ^
  --once ^
  --force-run ^
  --date-from=2025-12-01T00:00:00Z ^
  --date-to=2026-03-19T23:59:59Z ^
  --page-size=100 ^
  --max-pages-per-cycle=5 ^
  --notification-type=1 ^
  --dry-run
```

После проверки статистики включаем запись:

```powershell
python manage.py run_olap_webhook_backfill ^
  --once ^
  --force-run ^
  --date-from=2025-12-01T00:00:00Z ^
  --date-to=2026-03-19T23:59:59Z ^
  --notification-type=1 ^
  --write
```

## Фильтры
Команда поддерживает фильтры внутреннего API:
1. `--status` (можно указывать несколько раз)
2. `--business-status` (можно указывать несколько раз)
3. `--category-id-ext` (можно указывать несколько раз)
4. `--notification-type` (можно указывать несколько раз)

Пример:

```powershell
python manage.py run_olap_webhook_backfill ^
  --once ^
  --force-run ^
  --date-from=2026-01-01T00:00:00Z ^
  --status=complete ^
  --business-status=complete ^
  --category-id-ext=BSamfrT83o4Cw5ZG1m4RU7N4CtW6WR2M ^
  --notification-type=1 ^
  --write
```

## Backpressure
Команда не перегружает контур дозагрузки:
1. если в `olap_check_sync_journal` слишком много задач `new|retry` (выше `OLAP_BACKFILL_PAUSE_QUEUE_GT`), intake ставится на паузу;
2. intake возобновляется, когда глубина очереди опускается ниже `OLAP_BACKFILL_RESUME_QUEUE_LT`.

## Graceful shutdown
Команда корректно завершает цикл по `SIGINT/SIGTERM`:
1. текущая страница дочитывается;
2. процесс завершает цикл;
3. HTTP-сессия закрывается.

## Прозрачная проверка загрузки в OLAP (run_olap_sync_worker)
После того как backfill создал строки в `olap_check_sync_journal`, нужно запустить дозагрузку
в сырой слой OLAP и сразу получить читаемый отчёт по изменённым строкам.

Разовый запуск:

```powershell
python manage.py run_olap_sync_worker ^
  --once ^
  --claim-limit=200 ^
  --portion-size=50 ^
  --print-row-details-limit=50
```

Что важно:
1. Воркер печатает сводку итерации (`claimed/loaded/retry/failed/portions`).
2. Дополнительно печатаются изменённые строки журнала (`id/status/order/business_date/attempt/next_try_at/last_error`).
3. Воркер сразу запрашивает OLAP в строгом окне `business_date ± 1 день`.
4. Фильтр `Department.Id` обязателен и не снимается.
5. Если строк по чеку нет, задача переводится в `retry` с диагностикой по фильтрам (`Department.Id` и окно дат).
