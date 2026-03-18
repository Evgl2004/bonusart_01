# Эксплуатация NotificationScenario (F13)

## Назначение
Документ описывает запуск и сопровождение автоматических сценариев уведомлений:
1. `balance_changed` (транзакционный сценарий из webhook);
2. `inactive_7d` (плановое напоминание неактивным гостям);
3. `inactive_30d_coupon` (плановый сценарий с купоном, каркас).

## Что уже создано миграциями
После применения миграций `0017` и `0018` в БД есть:
1. `balance_changed` — активный системный сценарий (`trigger_type=webhook`);
2. `inactive_7d` — системный сценарий, создан как `is_active=False`;
3. `inactive_30d_coupon` — системный сценарий, создан как `is_active=False`.

Важно:
1. `inactive_*` по умолчанию выключены, чтобы не запустить рассылку до бизнес-подтверждения;
2. активация выполняется через Django Admin в разделе `NotificationScenario`.

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

## Мониторинг и диагностика
Техническая админка:
1. `NotificationScenario` — активность сценариев, приоритет, режим доставки, окно отправки;
2. `NotificationEvent` — дедупликация (`dedupe_key`, `duplicate_hits`), статусы обработки;
3. `DispatchTask` — фактическая доставка, ошибки провайдера, попытки и очередь.

Ключевая трассировка:
1. `DispatchTask.notification_scenario` и `DispatchTask.notification_event` связывают доставку с первопричиной события;
2. для маркетинговых кампаний по-прежнему используется `DispatchTask.mailing_guest`.

## Ограничение текущего каркаса
Сценарий `inactive_30d_coupon` пока не интегрирован с реальной выдачей купонов iiko:
1. если `coupon_required=True` и купон не получен, событие пропускается;
2. точка расширения предусмотрена через `coupon_resolver` в `guests/services/notification_scenarios.py`.

## Интеграционные проверки
Добавлены интеграционные тесты цепочки `Scenario -> Event -> Task`:
1. создание `NotificationEvent` и `DispatchTask` через `enqueue_notification_event_from_scenario`;
2. дедупликация по `(scenario, dedupe_key)`;
3. плановый сценарий неактивности через `run_scheduled_inactive_scenarios`.
4. запуск плановых сценариев через реестр обработчиков `code -> handler`
   (`run_registered_schedule_scenarios`).
5. запуск webhook-сценария `balance_changed` через реестр обработчиков
   (`run_webhook_scenario_by_code`).

Запуск:

```bash
python manage.py test guests.tests.NotificationScenarioIntegrationTests
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
