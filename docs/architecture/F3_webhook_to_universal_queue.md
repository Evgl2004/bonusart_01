# F3: Веб-хук -> универсальная очередь (баланс, high priority)

## Цель этапа
Подключить постановку задач уведомления об изменении баланса из входящих веб-хуков в универсальную очередь, не ломая текущий контур обработки iiko.

## Что реализовано
1. В `guests/services/webhooks.py` добавлен явный бизнес-метод:
   - `enqueue_balance_notification_from_webhook(webhook)`.
2. В `handle_api_webhook` добавлен отдельный маршрут balance-события:
   - webhook относится к балансу, если `category_id_ext == "BSamfrT83o4Cw5ZG1m4RU7N4CtW6WR2M"`.
3. В `handle_api_webhook(...)` добавлен явный параметр бизнес-вызова:
   - `send_balance_notification=True|False`
   - при `False` отключается только отправка в очередь, остальная обработка webhook сохраняется.
4. Маршрутизация задаётся явно из кода:
   - `priority=high`
   - `primary_only=True`

## Логика постановки balance-уведомления
1. Проверяется, что webhook относится к балансу:
   - `category_id_ext` должен совпасть с фиксированным ID категории баланса.
2. Проверяется параметр вызова `send_balance_notification`.
3. Определяется гость:
   - сначала из локальной БД;
   - при наличии телефона используется fallback `get_or_create_guest_from_iiko`.
4. Формируется текст сообщения.
5. Через `enqueue_guest_notification_tasks(...)` создаются `DispatchTask` с:
   - `source_type=webhook`
   - `priority=high`
   - `status=pending`
   - payload с деталями события.

## Совместимость
1. Текущая бизнес-обработка веб-хуков (категории, визиты, статусы webhook) сохраняется без изменений.
2. `send_balance_notification=False` отключает только отправку уведомлений в боты.
3. Временный legacy-адаптер `enqueue_high_priority_webhook_tasks(...)` оставлен для совместимости импортов и перенаправляет вызов в новый метод.

## Рекомендуемое включение
1. На тесте сначала вызывать `handle_api_webhook(..., send_balance_notification=False)`.
2. После проверки маршрутизации включить `send_balance_notification=True`.
3. Проверить создание `DispatchTask` и доставку через `dispatch_universal_tasks` и `run_provider_worker`.
