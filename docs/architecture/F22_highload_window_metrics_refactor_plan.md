# F22. План высоконагруженной стабилизации метрик Workbench (общий и категорийный контуры)

## Статус документа
Рабочий технический план на реализацию.

Дата фиксации: 2026-04-22

## Контекст и проблема
После реализации F21 категорийный режим в `guests/workbench` работает корректно, но в общем режиме (без выбранной категории) сохраняется архитектурный дефект смысла метрик:

1. `dashboard` считает выручку/заказы/средний чек по полным чекам (`OrderFact`).
2. `guests/workbench` в режиме "Общие метрики по окну" использует `GuestRestaurantWindowMetrics`, который исторически собирается через `GuestRestaurantDailyCategoryFact`.
3. `GuestRestaurantDailyCategoryFact` является категорийным дневным агрегатом (по позициям, связанным с целевыми категориями), поэтому не является источником полной кассовой выручки.
4. В результате одинаковый период (`30` дней) даёт разные цифры между `dashboard` и `workbench`.

Дополнительный риск high-load:

1. В F21 category-window пересчёт опирается на регулярный скан `OlapSalesRawLine` с фильтром `dish_code + date`.
2. На больших объёмах это даёт рост времени пересчёта и нагрузку на БД.

## Цель
Сделать архитектуру метрик корректной по бизнес-смыслу и устойчивой под high-load.

## Целевые принципы (best practices)
1. Один бизнес-показатель = один контракт смысла.
2. Разделение общего и категорийного контуров по источникам и назначению.
3. Сырые OLAP-данные (`OlapSalesRawLine`) использовать как ingest/audit-слой, а не как частый источник оконных пересчётов.
4. ETL только idempotent, инкрементальный, со scoped stale-cleanup.
5. Feature-flag rollout + dual-run + контрольный аудит до финального переключения.

## Что не делаем
1. Не ломаем и не переиспользуем `GuestRestaurantDailyCategoryFact` под "общую кассу".
2. Не смешиваем общий и категорийный смыслы в одной таблице-агрегаторе.
3. Не меняем UI-потоки рассылок в рамках этого этапа.

## Целевая архитектура данных

### Контур A. Общие метрики (без выбранной категории)
Источник смысла: полные чеки `OrderFact`.

Новый дневной слой:
`GuestRestaurantDailyOrderFact` (`guest_restaurant_daily_order_fact`)

1. Зерно строки: `business_date + guest_id + department_id`.
2. Поля:
1. `business_date`
2. `guest` (FK)
3. `department_id`
4. `orders_count`
5. `sum_net`
6. `bonus_in_sum`
7. `bonus_out_sum`
8. `updated_at`
3. Уникальность:
`(business_date, guest, department_id)`.
4. Индексы:
1. `(guest, department_id, business_date)`
2. `(department_id, business_date)`
3. `(business_date, department_id)`

Оконный слой:
`GuestRestaurantWindowMetrics` остаётся существующим, но пересчёт перевести на источник `GuestRestaurantDailyOrderFact`.

### Контур B. Категорийные метрики (при выбранной категории)
Источник смысла: "заказ содержит категорию" + "полная сумма заказа".

Новый order-level мост:
`GuestOrderFocusFact` (`guest_order_focus_fact`)

1. Зерно строки: `order + focus_category`.
2. Поля:
1. `business_date`
2. `guest` (FK, nullable если гость не определён)
3. `department_id`
4. `order_number`
5. `uniq_order_id`
6. `focus_category` (FK)
7. `sum_focus_net` (опциональная диагностическая сумма по позициям категории)
8. `items_count`
9. `updated_at`
3. Уникальность:
`(business_date, department_id, order_number, uniq_order_id, focus_category)`.
4. Индексы:
1. `(focus_category, business_date, department_id)`
2. `(business_date, department_id, focus_category)`
3. `(guest, business_date, focus_category)`
4. `(business_date, department_id, order_number, uniq_order_id)` для join к `OrderFact`

Оконный слой:
`GuestRestaurantWindowCategoryMetrics` остаётся существующим, но пересчёт переводится с raw-скана на `GuestOrderFocusFact + OrderFact`.

## Потоки расчёта после рефакторинга

### Общий режим (`WORKBENCH_CATEGORY_WINDOW_METRICS_V2=False` или категория не выбрана)
1. `OrderFact` -> `GuestRestaurantDailyOrderFact`.
2. `GuestRestaurantDailyOrderFact` -> `GuestRestaurantWindowMetrics`.
3. `workbench` берёт карточки/таблицу/фильтры из `GuestRestaurantWindowMetrics`.

### Категорийный режим (`WORKBENCH_CATEGORY_WINDOW_METRICS_V2=True` и категория выбрана)
1. `OlapSalesRawLine + FocusCategoryNomenclatureResolved` -> `GuestOrderFocusFact`.
2. `GuestOrderFocusFact + OrderFact` -> `GuestRestaurantWindowCategoryMetrics`.
3. `workbench` берёт карточки/таблицу/фильтры из `GuestRestaurantWindowCategoryMetrics`.

## ETL-команды (новые и изменяемые)

### Новые команды
1. `sync_daily_order_fact`
1. `--once`
2. `--business-date-from`
3. `--business-date-to`
4. `--department-id`
5. `--batch-size`
2. `sync_order_focus_fact`
1. `--once`
2. `--business-date-from`
3. `--business-date-to`
4. `--department-id`
5. `--batch-size`

### Изменяемые команды
1. `sync_window_metrics`:
перевести источник с `GuestRestaurantDailyCategoryFact` на `GuestRestaurantDailyOrderFact`.
2. `sync_window_category_metrics`:
перевести источник отбора заказов с `OlapSalesRawLine` на `GuestOrderFocusFact`, сохранив расчёт полных сумм через `OrderFact`.

### Обязательные свойства ETL
1. idempotent upsert.
2. scoped stale-delete внутри точного scope.
3. поддержка backfill по периоду.
4. одинаковая сигнатура параметров, как в существующих OLAP-командах.

## Индексация и производительность

### Что усилить дополнительно
1. Для `OlapSalesRawLine` добавить индекс под загрузку `GuestOrderFocusFact`:
`(business_date, department_id, dish_code)`.
2. Проверить покрытие join `OrderFact` по ключу заказа:
уникальность `(business_date, department_id, order_number, uniq_order_id)` уже есть, использовать её в join-стратегии.
3. Для новых таблиц добавить индексы сразу в миграциях (не отдельным этапом).

### Требования к нагрузочному поведению
1. Пересчёт окон не должен выполнять полный скан `OlapSalesRawLine` по каждому `window_days`.
2. category-window должен брать уже подготовленную order-level связь.
3. Команды должны работать chunked/batch и не держать в памяти полный объём периода.

## Schedule и ENV

### Новые ENV (общий контур)
1. `OLAP_DAILY_ORDER_FACT_SCHEDULE_ENABLED`
2. `OLAP_DAILY_ORDER_FACT_SCHEDULE_MINUTES`
3. `OLAP_DAILY_ORDER_FACT_SCHEDULE_TAIL_DAYS`
4. `OLAP_DAILY_ORDER_FACT_SCHEDULE_END_LAG_DAYS`
5. `OLAP_DAILY_ORDER_FACT_SCHEDULE_BATCH_SIZE`
6. `OLAP_DAILY_ORDER_FACT_SCHEDULE_DEPARTMENT_ID`

### Новые ENV (категорийный order-level мост)
1. `OLAP_ORDER_FOCUS_FACT_SCHEDULE_ENABLED`
2. `OLAP_ORDER_FOCUS_FACT_SCHEDULE_MINUTES`
3. `OLAP_ORDER_FOCUS_FACT_SCHEDULE_TAIL_DAYS`
4. `OLAP_ORDER_FOCUS_FACT_SCHEDULE_END_LAG_DAYS`
5. `OLAP_ORDER_FOCUS_FACT_SCHEDULE_BATCH_SIZE`
6. `OLAP_ORDER_FOCUS_FACT_SCHEDULE_DEPARTMENT_ID`

### Feature flags переключения
1. `WORKBENCH_CATEGORY_WINDOW_METRICS_V2` (существует, сохраняется).
2. `WORKBENCH_GENERAL_WINDOW_METRICS_FROM_ORDER_FACT_V2` (новый флаг перевода общего режима на новый контур).

## План внедрения (по этапам)

### Этап 0. Подготовка и baseline
1. Зафиксировать текущие метрики/время ETL/lag.
2. Подготовить контрольные телефоны и контрольные фильтры.

### Этап 1. Схема БД
1. Миграция `GuestRestaurantDailyOrderFact`.
2. Миграция `GuestOrderFocusFact`.
3. Миграция дополнительных индексов `OlapSalesRawLine`.

### Этап 2. ETL-слои
1. Реализация `sync_daily_order_fact`.
2. Реализация `sync_order_focus_fact`.
3. Рефактор `sync_window_metrics` на новый дневной общий слой.
4. Рефактор `sync_window_category_metrics` на `GuestOrderFocusFact + OrderFact`.

### Этап 3. Schedule-интеграция
1. Django Q task wrappers для новых команд.
2. Регистрация schedule через `settings.py`.
3. Добавление ENV в `.env.sample` и docs.

### Этап 4. Dual-run
1. Запуск новых ETL параллельно со старыми без переключения UI.
2. Сравнение старого и нового выхода на контрольном диапазоне.
3. Устранение расхождений.

### Этап 5. Переключение общего режима
1. Включение `WORKBENCH_GENERAL_WINDOW_METRICS_FROM_ORDER_FACT_V2=True`.
2. Проверка консистентности `Dashboard vs Workbench` (без категории) на одинаковых фильтрах.

### Этап 6. Переключение категорийного пересчёта на order-level мост
1. Перевод `sync_window_category_metrics` на новый источник.
2. Верификация F21-кейсов и сложных фильтров.

### Этап 7. Стабилизация
1. Наблюдение за производительностью, lag и ошибками.
2. Отключение устаревших путей после выдержки стабильности.

## Проверки и тесты

### Unit
1. Построение `GuestRestaurantDailyOrderFact` из `OrderFact`.
2. Построение `GuestOrderFocusFact` из raw+mapping.
3. Корректность пересчёта `window_metrics` и `window_category_metrics`.
4. Idempotency/stale-delete.

### Integration
1. `workbench` без категории: совпадение смысла с `dashboard`.
2. `workbench` с категорией: совпадение с category-window ожиданием.
3. Сложные фильтры (логика И) на обоих активных слоях.

### Регрессия
1. `dashboard` не ломается.
2. `segments` и фокус-матрицы не ломаются.
3. текущие сценарии уведомлений не ломаются.

### Запуск тестов
Тесты запускать через проектный скрипт:
`scripts/run_pytest.ps1` (как стандарт команды проекта).

## Критерии приёмки
1. Без выбранной категории `Workbench` и `Dashboard` совпадают по смыслу показателей при одинаковых фильтрах и периоде.
2. С выбранной категорией метрики соответствуют order-level категории (заказ содержит категорию + полная сумма чека).
3. ETL работает инкрементально и стабильно на периодах backfill.
4. Время регламентного пересчёта укладывается в операционные окна.
5. Есть воспроизводимый аудит "UI vs DB" на контрольных телефонах.

## Риски и mitigation
1. Риск: неполное сопоставление номенклатуры -> категория.
Меры: отдельный аудит покрытия, мониторинг `without_mapping`.
2. Риск: рост индексов и write-amplification.
Меры: только целевые индексы, контроль bloat, регулярный analyze/vacuum policy.
3. Риск: расхождения в переходный dual-run период.
Меры: flags + staged rollout + быстрый rollback.

## Результат после реализации
1. Общий режим метрик становится кассово-корректным и прозрачным для бизнеса.
2. Категорийный режим остаётся точным и становится устойчивее по производительности.
3. Архитектура витрин разделена по ответственности и масштабируется под high-load.
