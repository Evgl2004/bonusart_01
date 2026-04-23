# SPRINT 1 TASKBOARD: F21 (CATEGORY WINDOW METRICS)

Дата фиксации: 2026-04-22  
Статус: в работу в новом диалоге

## Обновление статуса (2026-04-23)

Текущий статус: основной блок F21/F22 по `guests/workbench` выполнен, проверен и принят в ручной проверке UI.

Что зафиксировано:
1. Введен category-window контур и подключение режима A/B.
2. Общий оконный слой переведен на расчет от полных чеков.
3. Настроен и проверен schedule-контур через Django Q sync.
4. Сверки по контрольным телефонам и smoke/idempotency прогоны дали PASS.
5. Синхронизирована логика карточек и таблицы на экране workbench.

Детальный отчет:
1. `docs/architecture/F22_workbench_execution_report_2026-04-23.md`.

Следующий этап:
1. F23: надежность ежедневной OLAP-цепочки (автодиагностика, автовосстановление пропусков, health-report).
2. F24: производительность workbench на боевом объеме (SQL-профиль и точечные индексы).
3. F23/UI: запуск нового каркаса рассылок `mailings-v2` (flow + bridge-экраны + поэтапный отказ от legacy UI).

Документ по новому UI рассылок:
1. `docs/architecture/F23_mailings_ui_blueprint_2026-04-23.md`.

## Цель спринта

Сделать корректный расчет метрик на экране `guests/workbench` при выбранной целевой категории, не ломая текущий общий оконный слой.

## Область работ

- Backend: новая модель + ETL + schedule + env.
- Backend/API: переключение источника метрик в workbench при выборе категории.
- QA: контрольные проверки и сверка на живых кейсах.

## Трек задач

1. Проектирование и модель данных
- [ ] Добавить модель `GuestRestaurantWindowCategoryMetrics`.
- [ ] Уникальный ключ: `as_of_date, guest_id, department_id, window_days, focus_category_id`.
- [ ] Добавить рабочие индексы под выборки workbench.
- [ ] Миграция и проверка применения.

Критерий готовности:
- Таблица создана, миграция проходит без ошибок.

2. ETL-команда пересчета
- [ ] Добавить management command `sync_window_category_metrics`.
- [ ] Реализовать режим `--once`.
- [ ] Реализовать backfill по периоду: `--business-date-from`, `--business-date-to`.
- [ ] Реализовать режим окна: `--as-of-date`, `--window-days`.
- [ ] Реализовать ограничение по заведению: `--department-id`.
- [ ] Реализовать idempotent upsert и удаление stale-строк в рамках scope.

Критерий готовности:
- Команда корректно пересчитывает данные на малом периоде и повторный запуск не ломает результат.

3. Расписание и настройки окружения
- [ ] Добавить ENV-переменные schedule для нового слоя.
- [ ] Добавить регистрацию schedule-задачи в `settings.py` (Django Q).
- [ ] Добавить task-wrapper в `guests/tasks.py`.
- [ ] Добавить значения и описания в `.env.sample`.

Рекомендуемые переменные:
- `OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_ENABLED`
- `OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_MINUTES`
- `OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_AS_OF_LAG_DAYS`
- `OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_BATCH_SIZE`
- `OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_WINDOW_DAYS`
- `OLAP_WINDOW_CATEGORY_METRICS_SCHEDULE_DEPARTMENT_ID`
- `WORKBENCH_CATEGORY_WINDOW_METRICS_V2`

Критерий готовности:
- Задача регистрируется/снимается по env-флагу как остальные OLAP schedule-задачи.

4. Подключение в workbench
- [ ] Ввести режим A/B:
- [ ] Режим A: без категории -> текущий `GuestRestaurantWindowMetrics`.
- [ ] Режим B: с категорией + `WORKBENCH_CATEGORY_WINDOW_METRICS_V2=True` -> `GuestRestaurantWindowCategoryMetrics`.
- [ ] Применить тот же режим ко всем блокам: сводка, таблица гостей, доп. условия.
- [ ] Обновить подсказки в UI, чтобы было ясно, какой слой сейчас активен.

Критерий готовности:
- При выбранной категории цифры соответствуют category-window слою, при пустой категории — старому слою.

5. Тесты и аудит
- [ ] Unit-тесты нового ETL.
- [ ] Интеграционный тест режима B в workbench.
- [ ] Проверка сложных условий при режиме B.
- [ ] Контрольные ручные кейсы по телефонам (из реальных проблемных примеров).
- [ ] Аудитный режим/команда для сравнения "ожидаемо vs UI".
- [ ] Запуск тестов через `scripts/run_pytest.ps1` и фиксация команд в отчете по этапу.

Критерий готовности:
- Нет расхождений на контрольных кейсах.

## Риски

- Некорректная интерпретация visits_count при category-window агрегации.
- Возможные дубляжи при неверном unique scope.
- Разъезд UI-логики между вкладками при переключении слоя.

## План внедрения в прод

1. Деплой кода и миграций с `WORKBENCH_CATEGORY_WINDOW_METRICS_V2=False`.
2. Ручной backfill на малом периоде.
3. Аудит и сверка.
4. Догрузка истории.
5. Включение флага V2.
6. Мониторинг и быстрый rollback флага при необходимости.
