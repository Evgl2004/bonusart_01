# F13. План доработки автоматизированных уведомлений (Scenario + Event)

## Цель
Добавить в проект управляемые из БД автоматизированные сценарии уведомлений, не ломая уже реализованную универсальную очередь (`DispatchTask -> dispatcher -> provider worker`).

## Почему это нужно
1. Настройки авто-уведомлений должны храниться в БД, а не в коде.
2. Нужен прозрачный аудит: почему было отправлено сообщение и откуда оно появилось.
3. Нужно разделить маркетинговые кампании (`Mailing/MailingGuest`) и системные/триггерные уведомления.

## План из 11 шагов
1. Ввести сущность `NotificationScenario` (правило автоматизации) с настройками приоритета, целевого режима, окна и режима распределения.
   Статус: `completed`
2. Ввести сущность `NotificationEvent` (факт срабатывания сценария) с полями дедупликации, планового времени отправки и контекстом события.
   Статус: `completed`
3. Добавить связь `DispatchTask` c `notification_scenario` и `notification_event` (оба nullable) для трассировки источника задач.
   Статус: `completed`
4. Реализовать дедупликацию через `dedupe_key` и уникальный индекс `(scenario, dedupe_key)`.
   Статус: `completed`
5. Реализовать расчёт `planned_send_at` и перенос в `DispatchTask.available_at`.
   Статус: `completed`
6. Поддержать два режима распределения:
   - `immediate` для транзакционных сценариев (например, баланс);
   - `uniform` для маркетинговых/авто-кампаний (равномерно в окне отправки).
   Статус: `completed`
7. Подключить первый рабочий сценарий `balance_changed` через новый контур `Scenario -> Event -> DispatchTask`.
   Статус: `completed` (подключён новый контур + добавлена data-миграция начальной инициализации сценария)
8. Подготовить каркас сценариев `inactive_7d` и `inactive_30d_coupon` (без ручного участия человека).
   Статус: `completed` (добавлены data-миграция `0018`, сервис планового запуска и management-команда `run_notification_scenarios`)
9. Расширить техническую админку для сопровождения сценариев/событий/доставки с фильтрами и диагностикой дублей/ошибок.
   Статус: `completed`
10. Зафиксировать эксплуатационную документацию и добавить интеграционные проверки на цепочку `Scenario -> Event -> Task`.
    Статус: `completed` (добавлен runbook эксплуатации и интеграционный test-suite `NotificationScenarioIntegrationTests`)
11. Добавить управляемый список кодов сценариев в `NotificationScenario`:
    - в админке поле `code` должно выбираться из выпадающего списка допустимых кодов;
    - при сохранении должна быть валидация, что `code` зарегистрирован в реестре обработчиков;
    - для невалидного `code` сохранение блокируется с понятной ошибкой.
    Статус: `completed` (добавлен реестр кодов, dropdown в админке и валидация `clean()` для модели)

## Модельная граница (важно)
1. `MailingGuest` используется только для маркетинговых кампаний.
2. Для задач из `MailingGuest`:
   - `DispatchTask.mailing_guest` заполнен;
   - `DispatchTask.notification_event` и `DispatchTask.notification_scenario` равны `NULL`.
3. Для автоматизированных/веб-хук задач:
   - `DispatchTask.mailing_guest` равен `NULL`;
   - заполняются `notification_event` и/или `notification_scenario`.

## Минимальные поля `NotificationScenario`
1. `code` (уникальный ключ сценария)
2. `name`
3. `is_active`, `is_system`
4. `trigger_type` (`webhook|schedule|manual`)
5. `template` (FK на `MessageTemplate`)
6. `priority` (`high|normal|bulk`)
7. `target_mode` (`primary_only|all_bots`)
8. `distribution_mode` (`immediate|uniform`)
9. `send_window_begin`, `send_window_end`, `timezone`
10. `created_at`, `updated_at`

## Минимальные поля `NotificationEvent`
1. `scenario` (FK)
2. `guest` (FK)
3. `source_type`, `source_ref`
4. `dedupe_key`
5. `status`
6. `event_at`
7. `planned_send_at`
8. `duplicate_hits`, `last_duplicate_at`
9. `payload`
10. `coupon_code`, `coupon_external_id`, `coupon_expires_at` (для купонных сценариев)
11. `created_at`, `updated_at`

## Принцип дедупликации
1. По каждому входящему событию строится `dedupe_key`.
2. Первый приход создаёт новый `NotificationEvent`.
3. Повтор обновляет `duplicate_hits` и `last_duplicate_at`, но не создаёт повторную отправку.

## Принцип планирования времени отправки
1. В момент создания `NotificationEvent` вычисляется `planned_send_at`.
2. Затем создаётся `DispatchTask` с `available_at = planned_send_at`.
3. Диспетчер забирает только задачи, у которых `available_at <= now()`.
