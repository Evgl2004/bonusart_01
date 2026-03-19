# Профили запуска OLAP-контура в docker-compose (F18.5)

## Назначение
Документ описывает рабочую схему без лишних фоновых контейнеров:
1. Плановые `OLAP sync` и `OLAP rebuild` запускаются через `Django Q` (сервис `task-bonus`).
2. В `docker-compose` отдельным профилем остаётся только сервис исторического прогона.

Патч для внешнего файла:
`docs/operations/patches/webhook_03/docker-compose.prod.olap-f18.5.patch`

## Архитектура запуска (best practice для текущего контура)
1. Постоянно работают основные сервисы приложения + `task-bonus` (qcluster).
2. `task-bonus` по расписанию запускает one-shot задачи:
   1. `guests.tasks.run_olap_sync_scheduled_task` — дозагрузка OLAP в рабочее окно.
   2. `guests.tasks.run_olap_rebuild_scheduled_task` — ночной пересчет витрин.
3. Историческая загрузка выполняется отдельным сервисом `webhook-backfill-bonus` по профилю `backfill`.

## Что добавлено в compose-патче
1. Только один сервис:
   1. `webhook-backfill-bonus` с профилем `backfill`.
2. Для штатного контура никаких дополнительных `live/rebuild` профилей не требуется.

## Как запускать
1. Обычный прод-запуск:
```bash
docker compose -f docker-compose.prod.yaml up -d
```
2. Запустить backfill:
```bash
docker compose -f docker-compose.prod.yaml --profile backfill up -d webhook-backfill-bonus
```
3. Остановить backfill:
```bash
docker compose -f docker-compose.prod.yaml stop webhook-backfill-bonus
```

## Проверка
```bash
docker compose -f docker-compose.prod.yaml ps
docker compose -f docker-compose.prod.yaml logs -f task-bonus
docker compose -f docker-compose.prod.yaml logs -f webhook-backfill-bonus
```

## Важные примечания
1. Расписание OLAP-задач управляется env-переменными в `.env` (`OLAP_SYNC_*`, `OLAP_REBUILD_*`), а не через вечные циклы контейнеров.
2. `webhook-backfill-bonus` нужен только на период исторического прогона.
3. Такая схема упрощает эксплуатацию: меньше процессов, прозрачный контроль времени запусков и нагрузки на OLAP.
