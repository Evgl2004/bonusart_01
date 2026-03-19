# F15. Архитектурный Blueprint: аналитика чеков и категорий на базе OLAP

## Цель
Построить в проекте собственный аналитический слой по чекам гостей в разрезе заведений:
1. считать средний чек, частоту визитов, динамику по категориям номенклатуры;
2. формировать сегменты и автоматические рассылки по бизнес-правилам;
3. не перегружать iiko запросами на каждый входящий webhook;
4. не ломать текущий контур доставки сообщений (`NotificationEvent -> DispatchTask`).

## Термины (фиксируем единообразно)
1. `Журнал синхронизации чеков с OLAP` — очередь записей, какие чеки нужно дозагрузить из OLAP.
2. `Сырые строки OLAP` — неизменяемые строки ответа OLAP по позициям чеков.
3. `Порция` — ограниченное количество чеков, обрабатываемых за один запрос/цикл.
4. `Дневной слой` — агрегаты за конкретный день.
5. `Оконные метрики` — агрегаты по окнам `7/14/30/60/180` дней.

## Ключевые выводы по текущему состоянию
1. Текущая модель хранит визиты и назначения категорий, но не хранит полноценные факты чеков/позиций.
2. Для аналитики по среднему чеку и поведению гостя этого недостаточно.
3. По результатам проверки OLAP доступны поля, достаточные для построения собственной витрины данных.
4. Фильтрация по `Department.Id` должна быть подтверждена отдельным техническим шагом (см. F16-этап S1).

## Принципы проектирования
1. Источник истины для аналитики — собственная БД, а не текущие webhook-категории iiko Card.
2. В персонализированных таблицах связь с гостем обязательна там, где это технически возможно.
3. Нагрузка распределяется по времени:
   1. регулярная дозагрузка чеков из OLAP — короткими циклами;
   2. тяжёлые пересчёты оконных итогов — в ночных заданиях.
4. Сырые данные не удаляются сразу: они нужны для пересчёта новых метрик и аудита.
5. Все изменения схемы только через Django migrations.

## Целевая модель данных
Ниже указан минимальный состав таблиц для старта. Ранее обсуждавшиеся «4 таблицы» остаются ядром,
но дополняются обязательными справочниками категорий.

### 1) Журнал синхронизации чеков с OLAP (`olap_check_sync_journal`)
Назначение: хранить задания на дозагрузку чека из OLAP и статусы их исполнения.

Поля:
1. `id` (PK)
2. `guest_id` (FK -> `guests.id`, nullable только если гость не распознан)
3. `source_webhook_id` (id уведомления из внешнего контура)
4. `transaction_id` (идентификатор транзакции, если есть)
5. `order_num` (номер чека)
6. `uniq_order_id` (ID заказа из OLAP, если известен)
7. `organization_id`, `terminal_group_id`
8. `department_id` (торговое предприятие в OLAP, если определено)
9. `event_at` (время события из webhook)
10. `status` (`new|in_progress|loaded|retry|failed|skipped`)
11. `attempt_count`, `next_try_at`, `last_error`
12. `created_at`, `updated_at`, `loaded_at`

Индексы/ограничения:
1. индекс на `(status, next_try_at)`
2. индекс на `(order_num, event_at)`
3. уникальность на комбинацию источника, чтобы не плодить дубли заданий

### 2) Сырые строки OLAP по позициям чека (`olap_sales_raw_line`)
Назначение: сохранить «как пришло» из OLAP для аудита и повторных пересчётов.

Поля:
1. `id` (PK)
2. `sync_journal_id` (FK -> `olap_check_sync_journal.id`)
3. `guest_id` (FK -> `guests.id`, nullable допустим, но заполняется при наличии связи)
4. `business_date` (`OpenDate.Typed`)
5. `department_id`, `department_name`
6. `order_num`, `uniq_order_id`
7. `item_sale_event_id` (если доступен)
8. `dish_code`, `dish_name`
9. `dish_category_id`, `dish_category_name`
10. `dish_group_id`, `dish_group_name`
11. `dish_amount`
12. `dish_sum_before_discount` (`DishSumInt`)
13. `dish_sum_after_discount` (`DishDiscountSumInt`)
14. `discount_sum`, `bonus_sum`
15. `coupon_series`, `coupon_number`
16. `raw_json`
17. `created_at`

Индексы/ограничения:
1. индекс на `(guest_id, business_date)`
2. индекс на `(department_id, business_date)`
3. индекс на `(dish_category_id, business_date)`
4. уникальность строки позиции по устойчивому составному ключу заказа/позиции

### 3) Справочник категорий из OLAP (`olap_category_dict`)
Назначение: единый каталог категорий, обнаруженных в OLAP, с внешним идентификатором iiko.

Поля:
1. `id` (PK)
2. `iiko_category_external_id` (уникальный внешний ID категории из iiko)
3. `category_name`
4. `first_seen_at`, `last_seen_at`
5. `is_active`
6. `created_at`, `updated_at`

Индексы/ограничения:
1. уникальность на `iiko_category_external_id`
2. индекс на `category_name`

### 3.1) Справочник номенклатуры из OLAP (`olap_nomenclature_dict`)
Назначение: хранить блюда/позиции меню из OLAP и их связь с категорией.

Поля:
1. `id` (PK)
2. `iiko_nomenclature_external_id` (уникальный внешний ID номенклатуры)
3. `nomenclature_name`
4. `olap_category_id` (FK -> `olap_category_dict.id`)
5. `iiko_dish_group_external_id`, `dish_group_name`
6. `first_seen_at`, `last_seen_at`
7. `is_active`
8. `created_at`, `updated_at`

Индексы/ограничения:
1. уникальность на `iiko_nomenclature_external_id`
2. индекс на `(olap_category_id, is_active)`

### 4) Виртуальные категории (`virtual_category`)
Назначение: пользовательские категории маркетолога, которые можно собирать из номенклатур и/или категорий OLAP.

Поля:
1. `id` (PK)
2. `code` (уникальный технический код)
3. `name`
4. `description`
5. `is_active`
6. `created_at`, `updated_at`

Индексы/ограничения:
1. уникальность на `code`
2. индекс на `(is_active, name)`

### 4.1) Состав виртуальной категории по номенклатурам (`virtual_category_nomenclature_link`)
Назначение: перечислять конкретные номенклатуры, входящие в виртуальную категорию.

Поля:
1. `id` (PK)
2. `virtual_category_id` (FK -> `virtual_category.id`)
3. `nomenclature_id` (FK -> `olap_nomenclature_dict.id`)
4. `created_at`

Индексы/ограничения:
1. уникальность на `(virtual_category_id, nomenclature_id)`

### 4.2) Состав виртуальной категории по категориям OLAP (`virtual_category_olap_category_link`)
Назначение: включать в виртуальную категорию целые категории OLAP.

Поля:
1. `id` (PK)
2. `virtual_category_id` (FK -> `virtual_category.id`)
3. `olap_category_id` (FK -> `olap_category_dict.id`)
4. `created_at`

Индексы/ограничения:
1. уникальность на `(virtual_category_id, olap_category_id)`

### 4.3) Единый справочник категорий в фокусе (`focus_category`)
Назначение: единая точка аналитического отбора; категория может быть либо прямой OLAP-категорией, либо виртуальной категорией.

Поля:
1. `id` (PK)
2. `source_type` (`olap_direct|virtual`)
3. `code`, `name`
4. `olap_category_id` (FK -> `olap_category_dict.id`, nullable)
5. `virtual_category_id` (FK -> `virtual_category.id`, nullable)
6. `is_enabled`
7. `priority_weight`, `tag_code`, `comment`
8. `created_at`, `updated_at`

Индексы/ограничения:
1. уникальность на `code`
2. `CHECK` по `source_type` (заполнено ровно одно из полей `olap_category_id` / `virtual_category_id`)
3. индекс на `(is_enabled, source_type, tag_code)`

### 4.4) Предрассчитанный состав фокуса по номенклатурам (`focus_category_nomenclature_resolved`)
Назначение: таблица ускорения ночных и оконных расчётов; хранит плоские пары `фокусная категория -> номенклатура`.

Поля:
1. `id` (PK)
2. `focus_category_id` (FK -> `focus_category.id`)
3. `nomenclature_id` (FK -> `olap_nomenclature_dict.id`)
4. `source_reason` (`direct_olap|virtual_nomenclature|virtual_olap_category`)
5. `created_at`, `updated_at`

Индексы/ограничения:
1. уникальность на `(focus_category_id, nomenclature_id)`
2. индекс на `(focus_category_id, source_reason)`

### 5) Факт чека (`order_fact`)
Назначение: одна запись на чек для быстрых метрик среднего чека и частоты.

Поля:
1. `id` (PK)
2. `guest_id` (FK -> `guests.id`)
3. `business_date`
4. `department_id`, `department_name`
5. `order_num`, `uniq_order_id`
6. `gross_sum`, `net_sum`, `discount_sum`, `bonus_sum`
7. `items_count`, `categories_count`
8. `coupon_used` (bool), `coupon_series`, `coupon_number`
9. `order_type`, `is_delivery`
10. `first_seen_at`, `updated_at`

Индексы/ограничения:
1. индекс на `(guest_id, business_date)`
2. индекс на `(department_id, business_date)`
3. уникальность заказа по устойчивому ключу

### 6) Дневные итоги по категориям (`guest_restaurant_daily_category_fact`)
Назначение: базовый слой для окон `7/14/30/60/180` без тяжёлых запросов к сырым данным.

Поля:
1. `id` (PK)
2. `business_date`
3. `guest_id` (FK -> `guests.id`)
4. `department_id`
5. `dish_category_id`
6. `orders_count`
7. `items_count`
8. `sum_gross`
9. `sum_net`
10. `bonus_sum`
11. `updated_at`

Индексы/ограничения:
1. уникальность на `(business_date, guest_id, department_id, dish_category_id)`
2. индекс на `(guest_id, department_id, business_date)`

### 7) Оконные метрики гостя по заведению (`guest_restaurant_window_metrics`)
Назначение: быстрый источник для дашбордов, сегментов и рейтингов.

Поля:
1. `id` (PK)
2. `as_of_date`
3. `guest_id` (FK -> `guests.id`)
4. `department_id`
5. `window_days` (`7|14|30|60|180`)
6. `orders_count`
7. `visits_count`
8. `avg_check_net`
9. `sum_net`
10. `bonus_in_sum`, `bonus_out_sum`
11. `last_visit_at`
12. `rating_score`
13. `updated_at`

Индексы/ограничения:
1. уникальность на `(as_of_date, guest_id, department_id, window_days)`
2. индекс на `(department_id, window_days, rating_score)`

## Поток данных
1. Входящий webhook (баланс/транзакция) создаёт запись в `Журнал синхронизации чеков с OLAP`.
2. Фоновый процесс раз в 10-15 минут выбирает новые записи порциями.
3. Для каждой порции выполняются запросы в OLAP и сохраняются строки в `olap_sales_raw_line`.
4. На основе сырых строк обновляется `order_fact`.
5. Ночным заданием пересчитываются:
   1. `guest_restaurant_daily_category_fact`;
   2. `guest_restaurant_window_metrics`.
6. Сценарии уведомлений и дашборды читают агрегированные таблицы, не нагружая OLAP.

## Что делать с ежедневной актуализацией
1. Дневной слой обновляется инкрементально в течение дня.
2. Ночной пересчёт перепроверяет последние N дней (например, 210), чтобы корректно учесть опоздавшие данные.
3. Оконные метрики `7/14/30/60/180` считаются только из дневного слоя.

## Правила привязки к гостю
1. Во всех аналитических таблицах, где возможна персонализация, хранится `guest_id`.
2. Если в момент загрузки гость не найден, запись допустимо сохранить с `guest_id = NULL`, но с обязательной задачей дозаполнения.
3. Для пользовательских дашбордов и сегментов используются только записи с валидным `guest_id`.

## Дашборд (минимальный состав)
1. Топ гостей по заведению:
   1. средний чек;
   2. количество визитов;
   3. рейтинг.
2. Топ категорий/блюд по заведению:
   1. количество заказов;
   2. выручка;
   3. динамика за 7/30 дней.
3. Контроль качества синхронизации:
   1. сколько записей в журнале в статусах `new/retry/failed`;
   2. задержка дозагрузки.

## Риски и меры
1. Риск: дубли строк OLAP.
   Мера: уникальные ключи и идемпотентная загрузка.
2. Риск: лимиты API iiko.
   Мера: обработка порциями, паузы между запросами, повторные попытки с отложенным запуском.
3. Риск: разрастание таблицы сырых строк.
   Мера: индексы, партиционирование по дате (этап F16), регламент хранения.
4. Риск: изменение полей OLAP.
   Мера: хранение `raw_json` и версия схемы загрузчика.

## Definition of Done для F15
1. Утверждены термины и структура таблиц.
2. Зафиксированы обязательные индексы и ключи.
3. Подтверждена необходимость единого справочника «категорий в фокусе» (OLAP + виртуальные) и таблиц его связей.
4. Зафиксировано правило обязательной связи с гостем там, где это возможно.
5. Подготовлен план внедрения F16 с разбивкой по логическим коммитам.
