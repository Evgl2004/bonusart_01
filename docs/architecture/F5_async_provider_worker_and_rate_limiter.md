# F5. Async provider-worker и централизованный Redis rate limiter

## Цель этапа
Этап F5 переводит фактическую отправку сообщений в отдельный асинхронный воркер провайдера:
1. Воркер читает `DispatchTask` из Redis lane-очередей (`high|normal|bulk`).
2. Применяет централизованный limiter на Redis (общий для всех воркеров).
3. Отправляет сообщения через единый async-интерфейс провайдера.
4. Корректно обновляет статус `DispatchTask` и поддерживает graceful shutdown.

## Что добавлено
1. Async command:
   - `python manage.py run_provider_worker --provider telegram|max|vk`
2. Новый воркер:
   - `guests/services/universal_queue/provider_worker.py`
3. Централизованный limiter:
   - `guests/services/universal_queue/rate_limiter.py`
4. Async клиенты провайдеров:
   - `guests/services/universal_queue/provider_clients.py`
5. Расширение Redis queue adapter:
   - `pop_from_lane(...)` для fair-policy.

## Fair-policy (анти-голодание bulk)
Используется квотная схема приоритетов (по умолчанию):
1. `high = 10`
2. `normal = 3`
3. `bulk = 1`

Это снижает риск бесконечного вытеснения `bulk` при постоянном потоке `high`.

## Централизованный limiter
Limiter хранит состояние в Redis namespace universal queue:
1. `...:rate:<provider>:next_ts_ms` — следующий допустимый слот.
2. `...:rate:<provider>:pause_until_ms` — глобальная пауза после `RetryAfter`.
3. `...:rate:<provider>:scope:<chat_or_peer>:pause_until_ms` — локальная пауза по конкретному получателю.

Атомарность резервирования слота обеспечивается Lua-скриптом.

## Обработка статусов DispatchTask
1. На старте обработки:
   - `queued -> in_progress`, `attempt += 1`
2. Успех:
   - `status=done`, `finished_at`, метаданные отправки в `payload`
3. Временная ошибка / rate-limit:
   - `status=pending`, `enqueued_at=NULL`, `queue_name=NULL`, `available_at=now+delay`
4. Невосстановимая ошибка:
   - `status=failed`, `last_error`
5. Для blocked:
   - дополнительно блокируется `GuestBotBinding` (`is_stop_sending=True`, `is_active=False`)

## Graceful shutdown
Воркер обрабатывает `SIGINT` и `SIGTERM`:
1. Выставляет флаг мягкой остановки.
2. Завершает текущую задачу.
3. Закрывает HTTP-клиент и соединение Redis.

## Новые настройки
1. `UNIVERSAL_PROVIDER_BLOCK_TIMEOUT_SECONDS`
2. `UNIVERSAL_PROVIDER_IDLE_SLEEP_SECONDS`
3. `UNIVERSAL_PROVIDER_RETRY_BASE_SECONDS`
4. `UNIVERSAL_PROVIDER_RETRY_MAX_SECONDS`
5. `UNIVERSAL_FAIR_HIGH`, `UNIVERSAL_FAIR_NORMAL`, `UNIVERSAL_FAIR_BULK`
6. `UNIVERSAL_RATE_LIMIT_TELEGRAM_PER_SECOND`
7. `UNIVERSAL_RATE_LIMIT_MAX_PER_SECOND`
8. `UNIVERSAL_RATE_LIMIT_VK_PER_SECOND`
9. `UNIVERSAL_PROVIDER_HTTP_TIMEOUT`
10. `TELEGRAM_API_BASE_URL`
11. `MAX_API_BASE_URL`
12. `MAX_API_AUTH_PREFIX`
13. `VK_API_BASE_URL`
14. `VK_API_VERSION`

## Токены провайдеров
Порядок разрешения токена:
1. `BotProfile.resolve_token()` (предпочтительно через `secret_ref` + env);
2. `payload.bot_token` / `payload.bot_token_ref` (интеграционный fallback);
3. env fallback:
   - `UNIVERSAL_QUEUE_TELEGRAM_FALLBACK_TOKEN`
   - `UNIVERSAL_QUEUE_MAX_FALLBACK_TOKEN`
   - `UNIVERSAL_QUEUE_VK_FALLBACK_TOKEN`
