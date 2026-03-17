# F4. Перевод массовой рассылки на универсальную очередь `DispatchTask`

## Цель этапа
На этом этапе `mailing_worker` перестаёт быть местом фактической доставки сообщений
и становится producer-компонентом, который:
1. Берёт пачку строк `MailingGuest` со статусом `planned`.
2. Переводит их в `in_progress`.
3. Ставит задачи в `DispatchTask` для дальнейшей отправки провайдерными воркерами.

Legacy direct-send путь удалён: `mailing_worker` работает только в dispatch-only режиме.

## Что изменено
1. Добавлен сервис `guests/services/universal_queue/mailing_producer.py`.
2. `mailing_worker` переведён в целевой режим:
   - только постановка задач в `DispatchTask`;
   - без прямой отправки в провайдеров.
3. В модель `Mailing` добавлены поля маршрутизации:
   - `target_mode` (`primary_only|all_bots`);
   - `queue_priority` (`high|normal|bulk`).
4. В форме создания/редактирования рассылки добавлены элементы управления
   режимом получателей и приоритетом очереди.
5. Добавлена связь рассылки с конкретными ботами:
   - `Mailing.bot_profiles` через таблицу `MailingBotProfileLink`.

## Параметры рассылки в модели
1. `Mailing.target_mode`:
   - `primary_only` — отправка в основной бот гостя;
   - `all_bots` — отправка во все активные привязки гостя.
2. `Mailing.queue_priority` — приоритет задач (`high|normal|bulk`), применяется
   при создании `DispatchTask` для строк конкретной рассылки.
3. `Mailing.bot_profiles` — список конкретных активных ботов, через которые
   должна отправляться эта рассылка.

## Логика постановки задач
Для каждой строки `MailingGuest`:
1. Выбираются цели отправки из `GuestBotBinding` только для ботов,
   указанных в `mailing.bot_profiles`.
2. Для каждой цели создаётся `DispatchTask`:
   - `source_type=mailing`;
   - `provider_type` по типу бота (`telegram|max|vk`);
   - `priority` из `mailing.queue_priority`;
   - `idempotency_key` в формате `mailing:<id>:row:<id>:provider:<provider>:chat:<chat_id>`.

## Статусы `MailingGuest` в F4
1. Успешно поставлено в очередь (`created` или `duplicate`):  
   `status=done`, `delivery_status=queued_to_dispatch`.
2. Нет доступных каналов:  
   `status=error`, `delivery_status=dispatch_no_targets`.
3. В рассылке не выбраны активные боты:  
   `status=error`, `delivery_status=dispatch_no_bot_profiles`.
4. Ошибка постановки задач:  
   `status=error`, `delivery_status=dispatch_enqueue_error` или `dispatch_enqueue_exception`.

## Почему это безопасно после cutover
1. Добавлена дедупликация через `idempotency_key`.
2. В логах фиксируются агрегаты постановки задач (`rows_total/rows_queued/rows_failed`).
3. Единый путь отправки снижает риск рассинхронизации бизнес-логики.
