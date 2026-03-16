# F4. Перевод массовой рассылки на универсальную очередь `DispatchTask`

## Цель этапа
На этом этапе `mailing_worker` перестаёт быть местом фактической доставки сообщений
и становится producer-компонентом, который:
1. Берёт пачку строк `MailingGuest` со статусом `planned`.
2. Переводит их в `in_progress`.
3. Ставит задачи в `DispatchTask` для дальнейшей отправки провайдерными воркерами.

Текущий direct-send путь сохранён как fallback и отключается/включается feature-flag.

## Что изменено
1. Добавлен сервис `guests/services/universal_queue/mailing_producer.py`.
2. В `mailing_worker` добавлено условное ветвление:
   - `UNIVERSAL_QUEUE_ENABLE_MAILING_DISPATCH=False` (по умолчанию): старый direct-send путь.
   - `UNIVERSAL_QUEUE_ENABLE_MAILING_DISPATCH=True`: новый путь постановки задач в `DispatchTask`.
3. В `settings.py` добавлены параметры маршрутизации массовой рассылки.

## Новые настройки
1. `UNIVERSAL_QUEUE_ENABLE_MAILING_DISPATCH` — включает F4-режим producer для `mailing_worker`.
2. `UNIVERSAL_QUEUE_MAILING_TARGET_MODE`:
   - `primary_only` — отправка в основной бот гостя;
   - `all_bots` — отправка во все активные привязки гостя.
3. `UNIVERSAL_QUEUE_MAILING_PRIORITY` — приоритет задач (`high|normal|bulk`), по умолчанию `bulk`.
4. `UNIVERSAL_QUEUE_MAILING_FALLBACK_OLD_TG_LINKS` — fallback на `GuestChannelLink` (legacy Telegram).

## Логика постановки задач
Для каждой строки `MailingGuest`:
1. Выбираются цели отправки из `GuestBotBinding` (новая модель).
2. При отсутствии целей и включённом fallback используются legacy `GuestChannelLink`.
3. Для каждой цели создаётся `DispatchTask`:
   - `source_type=mailing`;
   - `provider_type` по типу бота (`telegram|max|vk`);
   - `priority` из настройки;
   - `idempotency_key` в формате `mailing:<id>:row:<id>:provider:<provider>:chat:<chat_id>`.

## Статусы `MailingGuest` в F4
1. Успешно поставлено в очередь (`created` или `duplicate`):  
   `status=done`, `delivery_status=queued_to_dispatch`.
2. Нет доступных каналов:  
   `status=error`, `delivery_status=dispatch_no_targets`.
3. Ошибка постановки задач:  
   `status=error`, `delivery_status=dispatch_enqueue_error` или `dispatch_enqueue_exception`.

## Почему это безопасно для поэтапного включения
1. F4 выключен по умолчанию.
2. Старый путь отправки не удалён.
3. Добавлена дедупликация через `idempotency_key`.
4. В логах фиксируются агрегаты постановки задач (`rows_total/rows_queued/rows_failed`).

