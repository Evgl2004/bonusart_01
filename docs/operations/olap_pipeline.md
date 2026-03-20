# Оркестратор Полного OLAP-Контура

## Назначение
Команда `run_olap_pipeline` выполняет полный цикл переработки данных после backfill:
1. `olap_sync` (журнал -> сырой слой, опционально);
2. `catalog_sync` (справочники OLAP);
3. `resolved_rebuild` (плоский состав фокусных категорий);
4. `order_fact`;
5. `daily_category_fact`;
6. `window_metrics`.

Рекомендуемый production-режим:
1. запускать команду в формате `--once` через планировщик (`Django Q`);
2. не держать отдельный бесконечный контейнер только для пересчетов.

## Базовый запуск
Один проход:

```powershell
python manage.py run_olap_pipeline --once
```

Циклический режим:

```powershell
python manage.py run_olap_pipeline --sleep-seconds=900
```

## Частичный запуск
Если нужно прогнать только витрины без запроса OLAP:

```powershell
python manage.py run_olap_pipeline ^
  --once ^
  --skip-olap-sync
```

Если нужно исключить отдельные шаги:
1. `--skip-catalog-sync`
2. `--skip-resolved-rebuild`
3. `--skip-order-fact`
4. `--skip-daily-fact`
5. `--skip-window-metrics`

## Диапазоны и фильтры
Для пересчёта только нужного окна:

```powershell
python manage.py run_olap_pipeline ^
  --once ^
  --skip-olap-sync ^
  --raw-line-id-from=100000 ^
  --raw-line-id-to=120000 ^
  --business-date-from=2026-03-01 ^
  --business-date-to=2026-03-31 ^
  --focus-code=meat_focus ^
  --window-days=7 ^
  --window-days=30 ^
  --as-of-date=2026-03-31 ^
  --department-id=3c9e75bc-24f3-41ad-8fc0-0af8fa8e09d6
```

## Устойчивость
Флаг `--continue-on-step-error` позволяет продолжать конвейер после ошибки отдельного шага:

```powershell
python manage.py run_olap_pipeline --once --continue-on-step-error
```

По умолчанию (без этого флага) команда завершится при первой ошибке.

## Graceful shutdown
Команда корректно обрабатывает `SIGINT/SIGTERM`:
1. завершается текущий проход;
2. закрывается клиент OLAP;
3. процесс выходит без «обрыва» середины шага.
