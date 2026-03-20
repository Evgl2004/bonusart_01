# F8. Унифицированный producer уведомлений для бизнес-событий

## Цель этапа
Убрать дублирование логики постановки задач и дать единый API для создания `DispatchTask` из любых бизнес-сценариев.

## Что добавлено
1. Общий модуль:
   - `guests/services/universal_queue/notification_producer.py`
2. Общий метод:
   - `enqueue_guest_notification_tasks(...)`

## Что изменено
1. Основной webhook-сценарий (изменение баланса) теперь вызывает общий producer напрямую из бизнес-логики:
   - `guests/services/webhooks.py -> enqueue_balance_notification_from_webhook(...)`.
2. Параметры маршрутизации для balance задаются явно кодом:
   - `priority=high`
   - `primary_only=True`
3. Legacy-функция `enqueue_high_priority_webhook_tasks(...)` оставлена только как совместимый адаптер.

## Логика общего producer-а
1. Принимает гостя, текст, приоритет, `source_type`, `source_key`, payload.
2. Собирает цели доставки:
   - только из `GuestBotBinding`.
3. Создаёт `DispatchTask` по каждой цели:
   - с дедупликацией через `idempotency_key`, когда передан `source_key`.

## Практический эффект
1. Новые источники уведомлений подключаются единообразно.
2. Снижается риск расхождения логики между webhook, рассылкой и другими триггерами.
3. Упрощается сопровождение и расширение бизнес-уведомлений.
