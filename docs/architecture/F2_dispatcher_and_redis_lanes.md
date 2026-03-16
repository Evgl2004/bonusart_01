# F2: Диспетчер и Redis lane-очереди

## Что сделано

На этом этапе добавлен отдельный контур универсальной очереди, не затрагивающий текущий consumer веб-хуков iiko.

Введены компоненты:

1. `ProviderLaneQueue` — адаптер Redis-очередей.
2. `UniversalTaskDispatcher` — диспетчер постановки задач из БД в Redis.
3. Команда `dispatch_universal_tasks` — фоновый процесс диспетчеризации.

## Физическая схема ключей Redis

Используется namespace `uq:v1` и lane-ключи:

1. `uq:v1:telegram:high`, `uq:v1:telegram:normal`, `uq:v1:telegram:bulk`
2. `uq:v1:max:high`, `uq:v1:max:normal`, `uq:v1:max:bulk`
3. `uq:v1:vk:high`, `uq:v1:vk:normal`, `uq:v1:vk:bulk`

Итог: 9 физических ключей, при этом логика остаётся "3 очереди провайдеров с внутренними приоритетами".

## Расширение модели `DispatchTask`

Добавлено в миграции `0007_dispatchtask_queue_tracking`:

1. Новый статус `queued`.
2. `enqueued_at` — время постановки в Redis.
3. `queue_name` — фактический lane-ключ маршрутизации.

Это позволяет:

1. безопасно фильтровать задачи, которые уже поставлены в Redis;
2. проводить аудит и отладку маршрутизации;
3. избегать повторной постановки одной и той же задачи в normal-цикле.

## Алгоритм диспетчера

1. Выбирает `pending` задачи с `enqueued_at IS NULL` и `available_at <= now`.
2. Сортирует по приоритету `high -> normal -> bulk`.
3. Атомарно "захватывает" пачку (`select_for_update(skip_locked)`), переводит в `queued`.
4. Публикует каждую задачу в lane Redis.
5. При ошибке публикации возвращает задачу обратно в `pending` и пишет `last_error`.

## Запуск

Одна итерация:

```powershell
python manage.py dispatch_universal_tasks --once
```

Фоновый цикл:

```powershell
python manage.py dispatch_universal_tasks --batch-size 200 --sleep-seconds 2
```

## Важное замечание по совместимости

Текущий контур обработки входящих веб-хуков iiko не изменён.
Новый диспетчер запускается отдельно и пока не перехватывает существующие Redis-ключи веб-хуков.
