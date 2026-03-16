# F3: Веб-хук -> универсальная очередь (high priority)

## Цель этапа

Подключить постановку задач из входящих веб-хуков в новую универсальную очередь, не ломая текущий контур обработки iiko.

## Что реализовано

1. Добавлен `webhook_producer`:
   `guests/services/universal_queue/webhook_producer.py`
2. В `handle_api_webhook` добавлен безопасный вызов producer-а:
   ошибки enqueue не прерывают основную бизнес-обработку веб-хука.
3. Добавлены feature flags в settings:
   - `UNIVERSAL_QUEUE_ENABLE_WEBHOOK_ENQUEUE`
   - `UNIVERSAL_QUEUE_WEBHOOK_NOTIFY_TYPES`
   - `UNIVERSAL_QUEUE_WEBHOOK_PRIORITY`
   - `UNIVERSAL_QUEUE_WEBHOOK_PRIMARY_ONLY`
   - `UNIVERSAL_QUEUE_FALLBACK_OLD_TG_LINKS`

## Логика producer-а

1. Проверяет feature flag и отбор событий.
2. Находит гостя по `phone` или `customerId`.
3. Находит каналы доставки:
   - сначала через новую модель `GuestBotBinding`;
   - если нет, использует fallback `GuestChannelLink` (Telegram).
4. Создаёт `DispatchTask` с:
   - `source_type=webhook`
   - `priority=high` (или из настройки)
   - `status=pending`
   - `idempotency_key` для дедупликации

## Совместимость

Текущая обработка веб-хуков (категории, визиты, статусы webhook) продолжает работать в прежнем режиме.
Новый producer подключается независимо и не ломает старый контур даже при ошибках Redis/БД enqueue.

## Рекомендуемое включение

1. Сначала оставить `UNIVERSAL_QUEUE_ENABLE_WEBHOOK_ENQUEUE=False`.
2. На тесте включить `True` и проверить создание `DispatchTask`.
3. Настроить `UNIVERSAL_QUEUE_WEBHOOK_NOTIFY_TYPES` под нужные типы событий баланса.
4. После проверки запустить `dispatch_universal_tasks` и контролировать backlog lane-очередей.
