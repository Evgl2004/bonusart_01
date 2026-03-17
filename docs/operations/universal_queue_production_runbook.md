# Продовый запуск Universal Queue (Redis Shared)

## Цель документа
Этот файл показывает, какие сервисы нужно добавить в продовый `docker-compose.prod.yaml`, чтобы новая архитектура очередей работала полноценно:
1. `mailing_worker` ставит задачи в `DispatchTask`.
2. `dispatch_universal_tasks` перекладывает задачи в Redis lane-очереди.
3. `run_provider_worker` отправляет сообщения по провайдерам `telegram|max|vk`.
4. `run_universal_queue_monitor` восстанавливает stale-задачи и пишет health-метрики.

Важно: текущий входной контур webhook (`run_webhook_worker`) остаётся без изменений.

## Что оставить из текущего compose
Не удалять текущие сервисы:
1. `worker-bonus` (`python manage.py run_webhook_worker --verbose`) — входящая webhook-очередь.
2. `task-bonus` (`python manage.py qcluster`) — периодические фоновые задачи Django-Q.
3. `app-bonus` — веб-приложение.

## Блок сервисов для добавления в `docker-compose.prod.yaml`
Вставьте фрагмент ниже в секцию `services` внешнего продового compose (рядом с `worker-bonus` и `task-bonus`).

```yaml
  # producer массовых рассылок -> DispatchTask
  mailing-bonus:
    build: ./loyalty_service
    env_file:
      - ./loyalty_service/.env
    environment:
      - DJANGO_SETTINGS_MODULE=loyalty_viewer.settings
      - TZ=UTC
    command: python manage.py mailing_worker
    user: "1000:1000"
    volumes:
      - /var/log/loyalty_app/mailing:/app/logs
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      app-bonus:
        condition: service_started
    restart: unless-stopped
    stop_grace_period: 30s
    healthcheck:
      test: [ "CMD", "pgrep", "-f", "mailing_worker" ]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:
      - app_network

  # dispatcher: DispatchTask -> Redis lane queues
  dispatch-bonus:
    build: ./loyalty_service
    env_file:
      - ./loyalty_service/.env
    environment:
      - DJANGO_SETTINGS_MODULE=loyalty_viewer.settings
      - TZ=UTC
    command: python manage.py dispatch_universal_tasks
    user: "1000:1000"
    volumes:
      - /var/log/loyalty_app/dispatch:/app/logs
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      app-bonus:
        condition: service_started
    restart: unless-stopped
    stop_grace_period: 30s
    healthcheck:
      test: [ "CMD", "pgrep", "-f", "dispatch_universal_tasks" ]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:
      - app_network

  # Рекомендуемый прод-режим: отдельный dispatcher на каждый провайдер.
  # Пример команд:
  # - python manage.py dispatch_universal_tasks --provider telegram
  # - python manage.py dispatch_universal_tasks --provider max
  # - python manage.py dispatch_universal_tasks --provider vk
  #
  # Такой запуск предотвращает перекос общей порции задач в один провайдер
  # и позволяет стабильно загружать каждую провайдерную очередь независимо.

  # sender: Telegram provider worker
  sender-telegram-bonus:
    build: ./loyalty_service
    env_file:
      - ./loyalty_service/.env
    environment:
      - DJANGO_SETTINGS_MODULE=loyalty_viewer.settings
      - TZ=UTC
    command: python manage.py run_provider_worker --provider telegram
    user: "1000:1000"
    volumes:
      - /var/log/loyalty_app/sender_telegram:/app/logs
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      app-bonus:
        condition: service_started
    restart: unless-stopped
    stop_grace_period: 30s
    healthcheck:
      test: [ "CMD", "pgrep", "-f", "run_provider_worker" ]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:
      - app_network

  # sender: MAX provider worker
  sender-max-bonus:
    build: ./loyalty_service
    env_file:
      - ./loyalty_service/.env
    environment:
      - DJANGO_SETTINGS_MODULE=loyalty_viewer.settings
      - TZ=UTC
    command: python manage.py run_provider_worker --provider max
    user: "1000:1000"
    volumes:
      - /var/log/loyalty_app/sender_max:/app/logs
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      app-bonus:
        condition: service_started
    restart: unless-stopped
    stop_grace_period: 30s
    healthcheck:
      test: [ "CMD", "pgrep", "-f", "run_provider_worker" ]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:
      - app_network

  # sender: VK provider worker
  sender-vk-bonus:
    build: ./loyalty_service
    env_file:
      - ./loyalty_service/.env
    environment:
      - DJANGO_SETTINGS_MODULE=loyalty_viewer.settings
      - TZ=UTC
    command: python manage.py run_provider_worker --provider vk
    user: "1000:1000"
    volumes:
      - /var/log/loyalty_app/sender_vk:/app/logs
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      app-bonus:
        condition: service_started
    restart: unless-stopped
    stop_grace_period: 30s
    healthcheck:
      test: [ "CMD", "pgrep", "-f", "run_provider_worker" ]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:
      - app_network

  # monitor/recovery: stale queued/in_progress
  uq-monitor-bonus:
    build: ./loyalty_service
    env_file:
      - ./loyalty_service/.env
    environment:
      - DJANGO_SETTINGS_MODULE=loyalty_viewer.settings
      - TZ=UTC
    command: python manage.py run_universal_queue_monitor
    user: "1000:1000"
    volumes:
      - /var/log/loyalty_app/uq_monitor:/app/logs
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      app-bonus:
        condition: service_started
    restart: unless-stopped
    stop_grace_period: 30s
    healthcheck:
      test: [ "CMD", "pgrep", "-f", "run_universal_queue_monitor" ]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:
      - app_network
```

## Готовый unified diff
Готовый патч для внешнего файла уже подготовлен в репозитории:
`docs/operations/patches/webhook_03/docker-compose.prod.universal-queue.patch`

## Обязательные флаги в `.env` (для включения нового контура)
Минимум:
1. `UNIVERSAL_QUEUE_ENABLE_MAILING_DISPATCH=true`
2. `UNIVERSAL_QUEUE_ENABLE_WEBHOOK_ENQUEUE=true`
3. `UNIVERSAL_QUEUE_REDIS_URL=redis://redis:6379/1`
4. `UNIVERSAL_QUEUE_NAMESPACE=uq:v1`
5. `UNIVERSAL_QUEUE_MAILING_TARGET_MODE=primary_only` (или `all_bots`)
6. `UNIVERSAL_QUEUE_ENABLE_BALANCE_NOTIFICATION=false` (включайте `true` после проверки на тесте)

Полный пример переменных добавлен в файл `.env.sample` в корне репозитория.

## Почему Nginx менять не нужно
Новые сервисы являются фоновыми воркерами и не публикуют HTTP-порты.  
Поэтому маршрутизация в `nginx.prod.conf` для них не требуется.
