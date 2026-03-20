# F18.6 Контролируемый ввод OLAP-контура в эксплуатацию

## Назначение
Документ фиксирует безопасный порядок запуска исторического прогона и перехода в live-режим без перегрузки:
1. источника webhook;
2. iiko OLAP;
3. нашей БД и воркеров.

## Общие правила перед стартом
1. Сделать резервную копию БД.
2. Убедиться, что базовый контур поднят (`app-bonus`, `worker-bonus`, `dispatch-bonus`, `sender-*`, `uq-monitor-bonus`, `task-bonus`).
3. На старте держать `OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE=False`.
4. На старте держать `OLAP_SYNC_SCHEDULE_ENABLED=False` и `OLAP_REBUILD_SCHEDULE_ENABLED=False`.

## Фаза A: dry-run на 1 день
Цель: проверить чтение исторических webhook без записи в журнал.

1. В `.env`:
```env
OLAP_BACKFILL_ENABLE=True
OLAP_BACKFILL_DRY_RUN=True
OLAP_BACKFILL_DATE_FROM=2026-03-18T00:00:00Z
OLAP_BACKFILL_DATE_TO=2026-03-19T00:00:00Z
```
2. Запуск one-shot:
```bash
docker compose -f docker-compose.prod.yaml --profile backfill run --rm \
  webhook-backfill-bonus \
  python manage.py run_olap_webhook_backfill --once --force-run --dry-run --notification-type=1
```
3. Проверка:
```bash
docker compose -f docker-compose.prod.yaml logs --tail=200 webhook-backfill-bonus
```

## Фаза B: запись за 7 дней
Цель: проверить запись в `olap_check_sync_journal` на ограниченном диапазоне.

1. В `.env`:
```env
OLAP_BACKFILL_DRY_RUN=False
OLAP_BACKFILL_DATE_FROM=2026-03-12T00:00:00Z
OLAP_BACKFILL_DATE_TO=2026-03-19T00:00:00Z
```
2. Запуск one-shot:
```bash
docker compose -f docker-compose.prod.yaml --profile backfill run --rm \
  webhook-backfill-bonus \
  python manage.py run_olap_webhook_backfill --once --force-run --write --notification-type=1
```
3. Проверка статусов журнала:
```bash
docker compose -f docker-compose.prod.yaml exec app-bonus \
  python manage.py shell -c "from guests.models import OlapCheckSyncJournal as J; print(J.objects.values('status').order_by('status').annotate(c=__import__('django.db.models').db.models.Count('id')))"
```

## Фаза C: полный исторический диапазон
Цель: догнать историю с декабря 2025 порциями и backpressure.

1. В `.env`:
```env
OLAP_BACKFILL_DRY_RUN=False
OLAP_BACKFILL_DATE_FROM=2025-12-01T00:00:00Z
OLAP_BACKFILL_DATE_TO=
OLAP_BACKFILL_PAGE_SIZE=100
OLAP_BACKFILL_MAX_PAGES_PER_CYCLE=5
OLAP_BACKFILL_SLEEP_BETWEEN_PAGES_SECONDS=1
OLAP_BACKFILL_SLEEP_BETWEEN_CYCLES_SECONDS=20
OLAP_BACKFILL_PAUSE_QUEUE_GT=5000
OLAP_BACKFILL_RESUME_QUEUE_LT=2000
```
2. Запуск отдельным временным контейнером:
```bash
docker compose -f docker-compose.prod.yaml --profile backfill up -d webhook-backfill-bonus
```
3. Контроль прогресса:
```bash
docker compose -f docker-compose.prod.yaml logs -f webhook-backfill-bonus
docker compose -f docker-compose.prod.yaml logs --tail=200 task-bonus
```
4. После завершения исторического прогона:
```bash
docker compose -f docker-compose.prod.yaml stop webhook-backfill-bonus
```

## Фаза D: переход в live-режим
Цель: включить постоянную обработку новых webhook и плановый пересчет витрин.

1. В `.env`:
```env
OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE=True
OLAP_BRIDGE_ALLOWED_NOTIFICATION_TYPES=1

OLAP_SYNC_SCHEDULE_ENABLED=True
OLAP_SYNC_SCHEDULE_MINUTES=30
OLAP_SYNC_WINDOW_START_LOCAL=12:00
OLAP_SYNC_WINDOW_END_LOCAL=01:00
OLAP_SYNC_SCHEDULE_CLAIM_LIMIT=100
OLAP_SYNC_SCHEDULE_PORTION_SIZE=50

OLAP_REBUILD_SCHEDULE_ENABLED=True
OLAP_REBUILD_SCHEDULE_CRON=30 2 * * *
OLAP_REBUILD_SCHEDULE_BATCH_SIZE=2000
OLAP_REBUILD_SCHEDULE_WINDOW_DAYS=7,14,30,60,180
```
2. Перезапуск сервисов, которые читают settings при старте:
```bash
docker compose -f docker-compose.prod.yaml restart worker-bonus task-bonus
```
3. Проверка:
```bash
docker compose -f docker-compose.prod.yaml logs --tail=200 worker-bonus
docker compose -f docker-compose.prod.yaml logs --tail=200 task-bonus
```

## Rollback (если что-то пошло не так)
1. В `.env` выключить:
```env
OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE=False
OLAP_SYNC_SCHEDULE_ENABLED=False
OLAP_REBUILD_SCHEDULE_ENABLED=False
```
2. Перезапустить:
```bash
docker compose -f docker-compose.prod.yaml restart worker-bonus task-bonus
docker compose -f docker-compose.prod.yaml stop webhook-backfill-bonus
```

## Smoke после каждой фазы
1. Запустить общий smoke:
```bash
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py smoke_post_deploy
```
2. Проверить, что нет штормов ошибок в логах `worker-bonus`, `task-bonus`, `dispatch-bonus`.
