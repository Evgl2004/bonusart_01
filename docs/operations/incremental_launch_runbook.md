# Инкрементальный запуск проекта (от минимального режима до полного)

## 1. Зачем этот runbook
Документ нужен для безопасного поэтапного ввода доработок в эксплуатацию:
1. сначала запускаем проект без рисков исходящей рассылки;
2. затем поэтапно включаем очередь и отправителей;
3. в конце включаем OLAP-контур и плановые задачи.

После каждого шага есть команды диагностики, чтобы вместе быстро понять текущее состояние.

## 2. Короткие ответы на ключевые вопросы
1. Если отключить `BALANCE_WEBHOOK_NOTIFY_ENABLED=False`, то уведомления о балансе в боты не отправляются, но обработка webhook продолжает работать.
2. Если справочник ботов пустой, веб-приложение и базовые воркеры запускаются. Ошибки начнутся только в момент реальной отправки задач в провайдеры.
3. Критично для старта приложения: корректные `DB`, `Redis`, `SECRET_KEY`, `ALLOWED_HOSTS`, доступ к webhook API (если запускается входной воркер).

## 3. Подготовка
1. Рабочая ветка: `codex/dev-ai`.
2. Файл окружения заполнен на базе `.env.sample`.
3. Команда ниже должна быть успешной:

```bash
docker compose -f docker-compose.prod.yaml config -q
```

4. Рекомендуемый безопасный стартовый набор флагов:

```env
BALANCE_WEBHOOK_NOTIFY_ENABLED=False
OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE=False
OLAP_SYNC_SCHEDULE_ENABLED=False
OLAP_REBUILD_SCHEDULE_ENABLED=False
OLAP_BACKFILL_ENABLE=False
```

## 4. Шаги запуска

### Шаг 0. Проверка кода и миграций (без поднятия сервисов)
```bash
.venv\\Scripts\\python.exe manage.py check
.venv\\Scripts\\python.exe manage.py makemigrations --check
.venv\\Scripts\\python.exe manage.py migrate --plan
```

Критерий перехода: нет ошибок.

---

### Шаг 1. Поднять только базовый контур приложения
Запускаем:
```bash
docker compose -f docker-compose.prod.yaml up -d app-bonus task-bonus
```

Проверяем:
```bash
docker compose -f docker-compose.prod.yaml ps
docker compose -f docker-compose.prod.yaml logs --tail=150 app-bonus
docker compose -f docker-compose.prod.yaml logs --tail=150 task-bonus
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py smoke_post_deploy --skip-redis --skip-bot-tokens
```

Критерий перехода: `app-bonus` и `task-bonus` стабильны, smoke без ошибок.

---

### Шаг 2. Поднять входной webhook-воркер без отправки в боты
Важно: `BALANCE_WEBHOOK_NOTIFY_ENABLED=False`.

Запускаем:
```bash
docker compose -f docker-compose.prod.yaml up -d worker-bonus
```

Проверяем:
```bash
docker compose -f docker-compose.prod.yaml logs --tail=200 worker-bonus
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py smoke_post_deploy --skip-bot-tokens
```

Критерий перехода: входные webhook обрабатываются, критических ошибок нет, исходящих задач по балансу не появляется.

---

### Шаг 3. Поднять диспетчер и монитор universal queue (без sender)
Запускаем:
```bash
docker compose -f docker-compose.prod.yaml up -d dispatch-bonus uq-monitor-bonus
```

Проверяем:
```bash
docker compose -f docker-compose.prod.yaml logs --tail=200 dispatch-bonus
docker compose -f docker-compose.prod.yaml logs --tail=200 uq-monitor-bonus
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py smoke_post_deploy --skip-bot-tokens
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py shell -c "from django.db.models import Count; from guests.models import DispatchTask; print(list(DispatchTask.objects.values('status').annotate(c=Count('id')).order_by('status')))"
```

Критерий перехода: диспетчер и монитор стабильны, очередь читается, ошибок подключения к Redis/БД нет.

---

### Шаг 4. Подключить sender Telegram (пилот)
Перед шагом:
1. создать/активировать минимум один `BotProfile` провайдера `telegram`;
2. убедиться, что токен разрешается через `secret_ref` или fallback env.

Запускаем:
```bash
docker compose -f docker-compose.prod.yaml up -d sender-telegram-bonus
```

Проверяем:
```bash
docker compose -f docker-compose.prod.yaml logs --tail=200 sender-telegram-bonus
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py smoke_post_deploy
```

Критерий перехода: sender Telegram стартует без циклических ошибок авторизации/429/5xx.

---

### Шаг 5. Подключить sender MAX и sender VK
Запускаем:
```bash
docker compose -f docker-compose.prod.yaml up -d sender-max-bonus sender-vk-bonus
```

Проверяем:
```bash
docker compose -f docker-compose.prod.yaml logs --tail=200 sender-max-bonus
docker compose -f docker-compose.prod.yaml logs --tail=200 sender-vk-bonus
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py smoke_post_deploy
```

Критерий перехода: оба sender стабильны, нет постоянных `ERROR` по токенам/сети.

---

### Шаг 6. Включить уведомления баланса в очереди
Меняем `.env`:
```env
BALANCE_WEBHOOK_NOTIFY_ENABLED=True
```

Применяем:
```bash
docker compose -f docker-compose.prod.yaml restart worker-bonus
```

Проверяем:
```bash
docker compose -f docker-compose.prod.yaml logs --tail=200 worker-bonus
docker compose -f docker-compose.prod.yaml logs --tail=200 dispatch-bonus
docker compose -f docker-compose.prod.yaml logs --tail=200 sender-telegram-bonus
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py shell -c "from django.db.models import Count; from guests.models import DispatchTask; print(list(DispatchTask.objects.values('status').annotate(c=Count('id')).order_by('status')))"
```

Критерий перехода: balance webhook порождает задачи, задачи проходят цепочку `DispatchTask -> dispatcher -> sender`.

---

### Шаг 7. Включить OLAP-контур (контролируемо)
Используем по фазам из документа:
`docs/operations/olap_f18_6_controlled_rollout.md`.

Минимум для live-фазы:
```env
OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE=True
OLAP_SYNC_SCHEDULE_ENABLED=True
OLAP_REBUILD_SCHEDULE_ENABLED=True
```

После изменения:
```bash
docker compose -f docker-compose.prod.yaml restart worker-bonus task-bonus
```

Проверяем:
```bash
docker compose -f docker-compose.prod.yaml logs --tail=200 worker-bonus
docker compose -f docker-compose.prod.yaml logs --tail=200 task-bonus
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py smoke_post_deploy
```

Критерий перехода: журнал OLAP пополняется, плановые one-shot задачи запускаются по расписанию без деградации контура.

## 5. Диагностический пакет после каждого шага (для совместного разбора)
После завершения шага присылайте вывод этих команд:

```bash
docker compose -f docker-compose.prod.yaml ps
docker compose -f docker-compose.prod.yaml logs --tail=120 app-bonus
docker compose -f docker-compose.prod.yaml logs --tail=120 worker-bonus
docker compose -f docker-compose.prod.yaml logs --tail=120 dispatch-bonus
docker compose -f docker-compose.prod.yaml logs --tail=120 sender-telegram-bonus
docker compose -f docker-compose.prod.yaml logs --tail=120 sender-max-bonus
docker compose -f docker-compose.prod.yaml logs --tail=120 sender-vk-bonus
docker compose -f docker-compose.prod.yaml logs --tail=120 uq-monitor-bonus
docker compose -f docker-compose.prod.yaml logs --tail=120 task-bonus
docker compose -f docker-compose.prod.yaml exec app-bonus python manage.py smoke_post_deploy
```

Если часть сервисов на шаге не запущена, соответствующую команду `logs` можно пропустить.

## 6. Быстрый откат
1. Отключить флаги:
```env
BALANCE_WEBHOOK_NOTIFY_ENABLED=False
OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE=False
OLAP_SYNC_SCHEDULE_ENABLED=False
OLAP_REBUILD_SCHEDULE_ENABLED=False
```
2. Перезапустить:
```bash
docker compose -f docker-compose.prod.yaml restart worker-bonus task-bonus dispatch-bonus uq-monitor-bonus
```
3. При необходимости временно остановить sender:
```bash
docker compose -f docker-compose.prod.yaml stop sender-telegram-bonus sender-max-bonus sender-vk-bonus
```
