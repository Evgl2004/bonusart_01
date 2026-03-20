# Post-Deploy Smoke Check (Прод)

## Назначение
Документ фиксирует стандартную процедуру короткой проверки после выкладки.
Цель — быстро убедиться, что приложение и воркеры работоспособны, а цепочка
доставки сообщений не сломана.

## Когда запускать
1. Сразу после `docker compose up -d` на проде.
2. После изменения `.env` с критичными переменными (DB/Redis/API токены).
3. После миграций и обновления образов воркеров.

## Быстрый чек-лист
1. Проверить, что контейнеры запущены:

```bash
docker compose -f docker-compose.prod.yaml ps
```

2. Проверить, что веб-приложение отвечает:

```bash
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py check
```

3. Запустить неразрушающий smoke-командой:

```bash
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py smoke_post_deploy
```

4. Проверить, что фоновые сервисы активны и не перезапускаются:
1. `worker-bonus` (webhook worker)
2. `dispatch-bonus` (dispatcher)
3. `sender-telegram-bonus`
4. `sender-max-bonus`
5. `sender-vk-bonus`
6. `uq-monitor-bonus`

5. Проверить логи без критических ошибок:

```bash
docker compose -f docker-compose.prod.yaml logs --tail=200 dispatch-bonus
docker compose -f docker-compose.prod.yaml logs --tail=200 sender-telegram-bonus
docker compose -f docker-compose.prod.yaml logs --tail=200 uq-monitor-bonus
docker compose -f docker-compose.prod.yaml logs --tail=200 worker-bonus
```

## Что проверяет `smoke_post_deploy`
1. `django check`.
2. Подключение к настроенным БД (`SELECT 1`).
3. Отсутствие неприменённых миграций.
4. Доступность Redis (webhook и universal queue).
5. Метрики lane-очередей universal queue.
6. Наличие системных NotificationScenario.
7. Разрешение токенов у активных `BotProfile`.

Важно:
1. Команда не отправляет сообщения пользователям.
2. Команда не создаёт тестовые задачи доставки.
3. При ошибках возвращает ненулевой код завершения.

## Полезные флаги
1. Пропустить проверку Redis:

```bash
python manage.py smoke_post_deploy --skip-redis
```

2. Пропустить проверку токенов ботов:

```bash
python manage.py smoke_post_deploy --skip-bot-tokens
```

3. Прогнать только БД+миграции:

```bash
python manage.py smoke_post_deploy --skip-django-check --skip-redis --skip-scenarios --skip-bot-tokens
```

## Критерии «деплой можно считать успешным»
1. `smoke_post_deploy` завершился без ошибок.
2. Все обязательные воркеры работают и не находятся в цикле рестартов.
3. В логах нет постоянных `CRITICAL`/`ERROR` по Redis/БД/API провайдеров.
4. Очереди не накапливаются аномально быстро (особенно DLQ).

## Рекомендация по graceful shutdown
Для сервисов-воркеров в `docker-compose.prod.yaml` держать `stop_grace_period`
не менее `30s`, чтобы процесс успевал корректно выйти после SIGTERM.
