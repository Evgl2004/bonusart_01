# Контрольная точка OLAP-контура — 23.03.2026

## Период
- Март 2026: с `2026-03-01` по `2026-03-23` (включительно)

## Загрузка webhook -> OLAP journal/raw
- Журнал марта:
  - `march_total = 1779`
  - `march_by_status = loaded: 1779`
- По логу backfill за март:
  - `seen_total = 2880`
  - `created_total = 1779`
  - `errors_total = 0`
  - `skipped_no_order_total = 0`
- По логу sync worker:
  - `claimed_total = 1779`
  - `loaded_total = 1779`
  - `retry_total = 0`
  - `failed_total = 0`
  - `raw_created_total = 19021`
  - `raw_duplicates_total = 153`
  - `portions_failed_total = 0`

## Пересчёт витрин за март
- `sync_olap_catalogs`: выполнен успешно
- `sync_order_fact`: выполнен успешно
- `sync_daily_category_fact`: выполнен успешно
- `sync_window_metrics`: выполнен успешно (`as_of_date = 2026-03-23`)

## Контрольные сверки
- `raw_distinct_orders = 1952`
- `order_fact_rows = 1952`
- `raw_net = 6327221.50`
- `order_fact_net = 6327221.50`
- `raw_gross = 6327221.50`
- `order_fact_gross = 6327221.50`
- `daily_rows = 493`
- `window_rows_as_of_2026-03-23 = 2876`

## Вывод
- Контур за март отработал корректно:
  - webhook -> journal -> OLAP raw -> order_fact -> daily -> window
- Критичных расхождений по контрольным суммам и количествам не выявлено.
