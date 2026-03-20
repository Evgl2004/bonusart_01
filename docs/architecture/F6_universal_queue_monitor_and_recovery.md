# F6. Monitor и восстановление зависших задач universal queue

## Цель этапа
Добавить эксплуатационный контур для устойчивости очереди:
1. Находить stale задачи в статусах `queued` и `in_progress`.
2. Автоматически переводить их в корректное состояние (`pending` или `failed`).
3. Логировать health-снимки Redis lane и статусов `DispatchTask` в БД.

## Что добавлено
1. Сервис обслуживания:
   - `guests/services/universal_queue/maintenance.py`
2. Management command monitor:
   - `python manage.py run_universal_queue_monitor`
   - файл: `guests/management/commands/run_universal_queue_monitor.py`
3. Новые настройки:
   - `UNIVERSAL_MONITOR_INTERVAL_SECONDS`
   - `UNIVERSAL_STALE_QUEUED_SECONDS`
   - `UNIVERSAL_STALE_IN_PROGRESS_SECONDS`

## Логика восстановления
1. `stale queued`:
   - условие: `status=queued` и `enqueued_at < now - stale_queued`
   - действие: `pending`, `enqueued_at=NULL`, `queue_name=NULL`, `available_at=now`
2. `stale in_progress`:
   - условие: `status=in_progress` и `started_at < now - stale_in_progress`
   - действие:
     - если `attempt < max_attempts` -> `pending`
     - если `attempt >= max_attempts` -> `failed`

## Health snapshot
Monitor выводит для каждого провайдера:
1. Размеры lane-очередей Redis (`high|normal|bulk`).
2. Агрегированные статусы задач `DispatchTask` в БД.

## Зачем это нужно
1. Компенсирует аварийные падения между чтением из Redis и фиксацией результата.
2. Уменьшает риск «застревания» задач без ручного вмешательства.
3. Даёт быстрый операционный обзор состояния очереди.

