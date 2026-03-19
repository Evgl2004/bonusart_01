# Профили запуска OLAP-контура в docker-compose (F18.5)

## Назначение
Этот документ описывает, как включать сервисы OLAP-контура по профилям в внешнем `docker-compose.prod.yaml` без поломки действующего контура.

Патч для внешнего файла:
`docs/operations/patches/webhook_03/docker-compose.prod.olap-f18.5.patch`

## Что добавлено в патче
1. Для существующих сервисов универсальной очереди добавлен профиль `live`:
   1. `mailing-bonus`
   2. `dispatch-bonus`
   3. `sender-telegram-bonus`
   4. `sender-max-bonus`
   5. `sender-vk-bonus`
   6. `uq-monitor-bonus`
2. Добавлены новые сервисы OLAP-контура:
   1. `olap-sync-bonus` (профили `live`, `backfill`)
   2. `webhook-backfill-bonus` (профиль `backfill`)
   3. `analytics-rebuild-bonus` (профиль `rebuild`)

## Режимы запуска
1. Рабочий контур (без исторического прогона):
```bash
docker compose -f docker-compose.prod.yaml --profile live up -d
```
2. Исторический прогон webhook + OLAP:
```bash
docker compose -f docker-compose.prod.yaml --profile backfill up -d
```
3. Периодический пересчет аналитики:
```bash
docker compose -f docker-compose.prod.yaml --profile rebuild up -d
```

## Проверка состояния
```bash
docker compose -f docker-compose.prod.yaml ps
```

Проверить журналы конкретного сервиса:
```bash
docker compose -f docker-compose.prod.yaml logs -f olap-sync-bonus
docker compose -f docker-compose.prod.yaml logs -f webhook-backfill-bonus
docker compose -f docker-compose.prod.yaml logs -f analytics-rebuild-bonus
```

## Остановка по профилю
1. Остановить live-контур:
```bash
docker compose -f docker-compose.prod.yaml --profile live down
```
2. Остановить backfill-контур:
```bash
docker compose -f docker-compose.prod.yaml --profile backfill down
```
3. Остановить rebuild-контур:
```bash
docker compose -f docker-compose.prod.yaml --profile rebuild down
```

## Важные примечания
1. Профили запускаются независимо: можно включать только нужный режим.
2. Все новые сервисы настроены на `restart: unless-stopped` и имеют healthcheck через `pgrep`.
3. Для корректного завершения в контейнерах задан `stop_grace_period` (`45s` для OLAP-сервисов).
