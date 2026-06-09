# Эксплуатация NotificationScenario (F13)

## Назначение
Документ описывает запуск и сопровождение автоматических сценариев уведомлений:
1. `balance_changed` (транзакционный сценарий из webhook);
2. `inactive_7d` (плановое напоминание неактивным гостям);
3. `inactive_30d_coupon` (купонный автосценарий "гость не был 30 дней + купон").

## Что уже создано миграциями
После применения миграций `0017` и `0018` в БД есть:
1. `balance_changed` — активный системный сценарий (`trigger_type=webhook`);
2. `inactive_7d` — системный сценарий, создан как `is_active=False`;
3. `inactive_30d_coupon` — системный сценарий, создан как `is_active=False`.

Важно:
1. `inactive_7d` по умолчанию выключен, чтобы не запустить рассылку до бизнес-подтверждения;
2. `inactive_30d_coupon` использует отдельный купонный контур автосценариев. Даже если `NotificationScenario.is_active=False`, пробный купонный запуск может выполняться явно через UI автосценариев или management-команду пилота;
3. старый планировщик `run_notification_scenarios` не должен использоваться как способ массовой выдачи купонов `inactive_30d_coupon`.

## Фоновый запуск плановых сценариев
Есть два штатных способа:
1. Через Django Q: задача `guests.tasks.run_scheduled_notification_scenarios_task` добавлена в `Q_CLUSTER.schedule` (каждые 30 минут).
2. Через отдельный воркер:

```bash
python manage.py run_notification_scenarios
```

Полезные опции:
1. `--once` — один проход без цикла;
2. `--scenario-code inactive_7d` — запуск только выбранного сценария;
3. `--limit-per-scenario 1000` — лимит гостей за проход;
4. `--sleep-seconds 300` — пауза между проходами в цикле.

Для купонных автосценариев это не основной путь запуска. Их фактическая выдача идет через отдельный исполнитель автосценария:

```bash
python manage.py plan_coupon_autoscenario --scenario-code inactive_30d_coupon
python manage.py execute_coupon_autoscenario_pilot --scenario-code inactive_30d_coupon --confirm
```

Первый вызов только считает план и не меняет базу. Второй создает техническую волну, резервирует купон и ставит assignment-событие во vtelemax; сообщение гостю создается только после ACK vtelemax.

## Мониторинг и диагностика
Техническая админка:
1. `NotificationScenario` — активность сценариев, приоритет, режим доставки, окно отправки;
2. `NotificationEvent` — дедупликация (`dedupe_key`, `duplicate_hits`), статусы обработки;
3. `DispatchTask` — фактическая доставка, ошибки провайдера, попытки и очередь.

Ключевая трассировка:
1. `DispatchTask.notification_scenario` и `DispatchTask.notification_event` связывают доставку с первопричиной события;
2. для маркетинговых кампаний по-прежнему используется `DispatchTask.mailing_guest`.

## Как работает webhook-поток сейчас
Центральная точка: `guests.services.webhooks.handle_api_webhook`.

Маршрутизация:
1. `category_id_ext == BSamfrT83o4Cw5ZG1m4RU7N4CtW6WR2M` (`balance_changed`) ->
   `run_webhook_scenario_by_code("balance_changed", ...)` ->
   `NotificationEvent -> DispatchTask`.
2. `notificationType=1` -> обновление `VisitHistory` (без постановки задач отправки).
3. `notificationType=5` -> назначение категории гостю (без постановки задач отправки).
4. остальные типы -> диагностический лог, спец-обработка не выполняется.

Параметр `send_balance_notification=False`:
1. отключает только enqueue balance-уведомления;
2. не отключает общую бизнес-обработку webhook.

## Явный route-override при создании события
Для бизнес-методов доступен фасад `create_notification_event(...)`:
1. базовые настройки берутся из `NotificationScenario` (БД);
2. параметры `route_priority`, `route_target_mode`, `route_allowed_bot_profile_ids`
   (если переданы и валидны) переопределяют сценарий только для текущего вызова.

Это позволяет явно управлять маршрутизацией из бизнес-кода, не полагаясь
исключительно на настройки карточки сценария.

## Купонный автосценарий `inactive_30d_coupon`

На 2026-06-09 это уже не каркас старого `coupon_resolver`, а отдельный купонный контур:

1. настройки хранятся в `CouponAutomationConfig`;
2. правила выбора купонной серии хранятся в `CouponAutomationRule`;
3. техническая волна фиксируется в `CouponAutoscenarioRun`;
4. назначение купона гостю фиксируется в `CouponAutoscenarioAssignment`;
5. `CouponVtelemaxSyncQueue` отправляет `assignments` и `status_update` во vtelemax с привязкой к автосценарному назначению;
6. `NotificationEvent` и `DispatchTask` создаются только после ACK vtelemax по assignment-событию.

Логика выбора купона:

1. предпросмотр и исполнитель одним пакетным запросом берут последнее заведение гостя из `OrderFact`;
2. если есть активное правило для последнего заведения и свободный купон, используется оно;
3. если правила или купона нет, используется резервное правило `Вся сеть (global)`;
4. если подходящего купона нет, гость пропускается и попадает в дефицит купонов;
5. за один проход одному гостю выдается не больше одного купона.

Проверенный серверный путь:

1. предпросмотр аудитории без изменения базы;
2. создание пробной волны в состоянии `Пилот`;
3. ACK vtelemax по assignment;
4. создание и доставка сообщения в Telegram;
5. cleanup пилота через `status_update:canceled` с `release_to_pool=true`;
6. возврат купона в пул после ACK vtelemax.

Открытый долг: полный E2E применения купона через реальный заказ iiko -> OLAP -> `order_fact` -> `sync_coupon_redemptions` -> `status_update:used` во vtelemax.

Пользовательский отчет:

1. отчет доступен в меню `Отчеты -> Автосценарии`;
2. боевые KPI считаются только по реальным запускам состояния `Активен`;
3. пилотные проверки вынесены в отдельный журнал и не влияют на маркетинговую конверсию, выручку, графики и выводы;
4. если за период были только пилоты, основной отчет должен честно показывать, что боевых данных нет;
5. дневная динамика показывает аудиторию, достижимых гостей, выдачи и применения по дням; выходные дни выделяются, при наведении на график показывается детализация дня;
6. применения купонов появляются после реального заказа, загрузки OLAP/order_fact и синхронизации `sync_coupon_redemptions`.

Текущий следующий эксплуатационный шаг: первый контролируемый боевой запуск малым лимитом на отдельной купонной серии, затем сверка отчета, очередей vtelemax, доставки и последующего применения через OLAP.

## Интеграционные проверки
Добавлены интеграционные тесты цепочки `Scenario -> Event -> Task`:
1. создание `NotificationEvent` и `DispatchTask` через `enqueue_notification_event_from_scenario`;
2. дедупликация по `(scenario, dedupe_key)`;
3. плановый сценарий неактивности через `run_scheduled_inactive_scenarios`.
4. запуск плановых сценариев через реестр обработчиков `code -> handler`
   (`run_registered_schedule_scenarios`).
5. запуск webhook-сценария `balance_changed` через реестр обработчиков
   (`run_webhook_scenario_by_code`).
6. проверка границы `handle_api_webhook`:
   - `notificationType=1` и `notificationType=5` не создают `DispatchTask`;
   - эти типы выполняют только бизнес-обновления данных (`VisitHistory`/категории).

Запуск локальных тестов выполняется только через проектный PowerShell-скрипт:

```bash
powershell -ExecutionPolicy Bypass -File scripts/run_pytest.ps1 guests/tests/test_notification_integration.py
```

Для полного блока купонных автосценариев:

```bash
powershell -ExecutionPolicy Bypass -File scripts/run_pytest.ps1 guests/tests/test_coupon_autoscenario_preview.py guests/tests/test_vtelemax_coupon_sync_service.py guests/tests/test_coupon_redemption_sync_service.py guests/tests/test_coupon_reports_views.py guests/tests/test_mailings_v2_views.py
```

## Лимиты отправки по сценарию
В `NotificationScenario` поддерживаются защитные лимиты для одного гостя:
1. `cooldown_minutes` — минимальный интервал между успешными отправками по этому сценарию;
2. `max_per_day_per_guest` — максимальное число отправок в сутки (в timezone сценария).

Поведение при срабатывании лимита:
1. `NotificationEvent` создаётся для аудита;
2. событие не пропускается, а переносится вперёд по времени;
3. `planned_send_at` сдвигается на допустимый момент;
4. `DispatchTask` всё равно создаётся с `available_at = planned_send_at`;
5. в `NotificationEvent.payload.deferred` сохраняется причина и время переноса.

Диагностика:
1. в админке смотрите `NotificationEvent.planned_send_at` и `payload`;
2. в `payload.deferred.reason` ищите признаки `cooldown` и `max_per_day`.
