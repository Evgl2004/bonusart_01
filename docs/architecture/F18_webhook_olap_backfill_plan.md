# F18. План безопасного исторического прогона webhook -> OLAP (с дросселированием)

## Статус документа
Утверждаемый рабочий план перед реализацией исторического прогона и live-моста.

## Цель
1. Добавить автоматический мост `webhook -> olap_check_sync_journal`.
2. Выполнить исторический прогон (декабрь 2025 -> текущая дата) без перегрузки:
   1. сервиса получения webhook;
   2. сервера iiko OLAP;
   3. нашей БД и воркеров.
3. Сохранить управляемый запуск через `env`-флаги (по умолчанию всё выключено).

## Ключевые принципы безопасности
1. Любая новая интеграция включается только флагом.
2. Исторический прогон и live-контур разделены.
3. Работа только порциями с паузами и backpressure.
4. Идемпотентность на каждом шаге (без дублей).
5. В любой момент можно нажать «пауза/стоп» без потери уже обработанных данных.

## Рекомендуемые стартовые лимиты (консервативные)

### 1) Получение исторических webhook из внутреннего API
1. `page_size = 100` записей.
2. `max_pages_per_cycle = 5` (до 500 webhook за цикл).
3. `sleep_between_pages = 1.0` сек.
4. `sleep_between_cycles = 20` сек.
5. Параллелизм: `1` процесс загрузки истории.

Итог:
1. Мягкая нагрузка на источник webhook.
2. Простой контроль и предсказуемая скорость прогона.

### 2) Дозагрузка чеков из iiko OLAP
1. `orders_per_request = 50` чеков на один OLAP-запрос.
2. `max_requests_per_minute = 20`.
3. `concurrency = 1` (на старте).
4. Повторы: `retry_base = 2s`, `retry_max = 120s`, `max_retries = 5`.

Итог:
1. Риск перегрузки iiko минимален.
2. При ошибках поток автоматически замедляется.

### 3) Backpressure (обязательная защита от лавины)
1. Если в `olap_check_sync_journal` накопилось `new/retry > 5000`:
   1. исторический загрузчик webhook ставится на паузу;
   2. продолжается только «разбор хвоста».
2. Возобновление, когда очередь упала ниже `2000`.

## Этапы работ

### F18.1. Кодировка и текстовая безопасность
1. Единый стандарт: `UTF-8` для всех JSON/файлов.
2. Запрет «текстового перекодирования» при копировании входных JSON.
3. Тесты на кириллицу в:
   1. сырых OLAP-строках;
   2. справочниках;
   3. дашборде.

### F18.2. Live-мост webhook -> olap_check_sync_journal
1. В `handle_api_webhook` для `notificationType=1` добавляется постановка задачи в журнал OLAP.
2. Добавляется `env`-флаг включения:
   1. в проде сначала `False`;
   2. включение только после подтверждения.

### F18.3. Исторический загрузчик webhook (backfill)
1. Отдельная management-команда для чтения внутренних webhook API по страницам.
2. Фильтры по датам/статусам/категории.
3. Режим `dry-run`.
4. Идемпотентная запись в `olap_check_sync_journal`.

### F18.4. Оркестрация контура OLAP
1. Пайплайн:
   1. `olap_check_sync_journal -> olap_sales_raw_line`;
   2. `sync_olap_catalogs`;
   3. `rebuild_focus_category_nomenclature_resolved`;
   4. `sync_order_fact`;
   5. `sync_daily_category_fact`;
   6. `sync_window_metrics`.
2. Отдельные процессы для:
   1. intake истории;
   2. OLAP-дозагрузки;
   3. ночных/периодических пересчётов.

### F18.5. Обновление docker-compose (prod)
1. Добавить сервисы:
   1. `webhook-backfill-worker` (исторический прогон);
   2. `olap-sync-worker` (дозагрузка из журнала);
   3. `analytics-rebuild-worker` (пересчёты агрегатов).
2. Добавить healthcheck и restart policy.
3. Чётко разделить профили запуска: `live`, `backfill`, `rebuild`.

### F18.6. Контролируемый ввод в эксплуатацию
1. Фаза A: `dry-run` на диапазоне 1 день.
2. Фаза B: 7 дней истории.
3. Фаза C: полный диапазон с декабря 2025.
4. Фаза D: включение live-моста.

## Переменные окружения (новые)
1. `OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE=false`
2. `OLAP_BRIDGE_ALLOWED_NOTIFICATION_TYPES=1`
3. `OLAP_BACKFILL_ENABLE=false`
4. `OLAP_BACKFILL_DRY_RUN=true`
5. `OLAP_BACKFILL_DATE_FROM=2025-12-01T00:00:00Z`
6. `OLAP_BACKFILL_DATE_TO=`
7. `OLAP_BACKFILL_PAGE_SIZE=100`
8. `OLAP_BACKFILL_MAX_PAGES_PER_CYCLE=5`
9. `OLAP_BACKFILL_SLEEP_BETWEEN_PAGES_SECONDS=1`
10. `OLAP_BACKFILL_SLEEP_BETWEEN_CYCLES_SECONDS=20`
11. `OLAP_BACKFILL_PAUSE_QUEUE_GT=5000`
12. `OLAP_BACKFILL_RESUME_QUEUE_LT=2000`
13. `IIKO_OLAP_ORDERS_PER_REQUEST=50`
14. `IIKO_OLAP_MAX_REQUESTS_PER_MINUTE=20`
15. `IIKO_OLAP_CONCURRENCY=1`

## Метрики контроля
1. Скорость intake webhook (записей/мин).
2. Глубина `olap_check_sync_journal` по статусам.
3. OLAP success/error/retry rate.
4. Время построения `order_fact`, `daily`, `window`.
5. Доля строк с проблемной кодировкой (должна быть 0).

## Definition of Done
1. Live-мост реализован и управляется флагом.
2. Исторический прогон выполняется порциями без перегрузок.
3. Кириллица в сырых данных/дашборде отображается корректно.
4. Пайплайн агрегатов проходит end-to-end.
5. Есть подтверждённый runbook запуска/паузы/возобновления.
