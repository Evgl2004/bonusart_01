# Сообщение для vtelemax: что нужно изменить для batch-контракта купонов SAGUR

Коллеги, добрый день.

Со стороны SAGUR мы переводим доставку купонных событий на batch-формат. Причина: в реальных рассылочных кампаниях может быть сотни и тысячи гостей, и делать отдельный HTTP-запрос на каждое назначение купона нерационально по нагрузке, логам, retry и времени обработки.

Просим скорректировать вашу сторону интеграции под новый транспортный контракт.

Важно: учет внутри SAGUR остается поштучным. Один назначенный купон конкретному гостю в рамках кампании = один `assignment_id` и один `event_id`. Меняется только внешний HTTP-транспорт: вместо одного события в одном запросе SAGUR отправляет пачку `items[]`.

Обновление по итогам обратной связи vtelemax:

- в `assignments` item добавлено поле `valid_until` — машиночитаемый срок действия купона;
- batch size 100 поддерживается;
- текущий лимит тела запроса на стороне vtelemax: 512 KB;
- логи по `request_id`/`event_id` и item-level `results[]` на стороне vtelemax предусмотрены.

## 1. Что именно меняется

Было условно:

```json
{
  "event_id": "event-uuid",
  "direction": "assignments",
  "sent_at": "2026-05-15T10:00:00Z",
  "payload": {
    "campaign_id": 55,
    "assignment_id": 101,
    "coupon_series": "TEST",
    "coupon_code": "TST-A001"
  }
}
```

Должно стать:

```json
{
  "request_id": "request-uuid",
  "direction": "assignments",
  "sent_at": "2026-05-15T10:00:00Z",
  "items": [
    {
      "event_id": "event-uuid-1",
      "campaign_id": 55,
      "assignment_id": 101,
      "coupon_series": "TEST",
      "coupon_code": "TST-A001",
      "valid_until": "2026-05-18T23:59:59+05:00"
    },
    {
      "event_id": "event-uuid-2",
      "campaign_id": 55,
      "assignment_id": 102,
      "coupon_series": "TEST",
      "coupon_code": "TST-A002",
      "valid_until": "2026-05-18T23:59:59+05:00"
    }
  ]
}
```

То есть:

- на уровне HTTP один запрос содержит несколько items;
- `request_id` идентифицирует HTTP-пачку;
- `event_id` идентифицирует конкретный item внутри пачки;
- идемпотентность должна быть именно по `event_id`, не по `request_id`;
- результат обработки нужно вернуть по каждому `event_id` отдельно.

## 2. Endpoint

Endpoint остается прежним:

```text
POST /internal/integration/v1/sagur/coupons/events
```

Тело запроса всегда JSON в UTF-8.

Рекомендуемый размер пачки со стороны SAGUR: до 100 items.

Согласованный текущий лимит тела запроса на стороне vtelemax: 512 KB.

В одной пачке SAGUR не смешивает разные направления: один запрос содержит либо только `assignments`, либо только `status_update`.

## 3. Заголовки и HMAC

SAGUR отправляет заголовки:

```text
Content-Type: application/json
X-Sagur-Timestamp: <unix_timestamp>
X-Sagur-Signature: <hmac_sha256>
X-Sagur-Request-Id: <request_id>
```

`X-Sagur-Event-Id` больше не используется для batch-запроса, потому что в одном запросе несколько событий. Конкретные `event_id` находятся в `items[]`.

HMAC считается по прежней схеме:

```text
METHOD
PATH
TIMESTAMP
SHA256(BODY)
```

Где:

- `METHOD` = `POST`;
- `PATH` = `/internal/integration/v1/sagur/coupons/events`;
- `TIMESTAMP` = значение `X-Sagur-Timestamp`;
- `BODY` = точное UTF-8 тело запроса.

## 4. Request: общий формат

```json
{
  "request_id": "6c0d7c33-9f3f-4ed0-b8a7-0877ab9f5c2a",
  "direction": "assignments",
  "sent_at": "2026-05-15T10:00:00Z",
  "items": []
}
```

Поля верхнего уровня:

- `request_id` — UUID HTTP-пачки. Нужен для логов, трассировки и заголовка `X-Sagur-Request-Id`.
- `direction` — тип пачки: `assignments` или `status_update`.
- `sent_at` — время отправки пачки в UTC, ISO 8601.
- `items` — массив item-событий. Каждый item обязан иметь свой `event_id`.

## 5. Direction `assignments`

`assignments` означает: SAGUR зарезервировал купон за гостем и просит vtelemax привязать/показать этот купон у гостя до старта рассылки.

На стороне SAGUR синхронизационный шлюз не даст отправить рассылку, пока vtelemax не вернет подтверждение по каждому нужному assignment item.

Пример:

```json
{
  "request_id": "6c0d7c33-9f3f-4ed0-b8a7-0877ab9f5c2a",
  "direction": "assignments",
  "sent_at": "2026-05-15T10:00:00Z",
  "items": [
    {
      "event_id": "6f8c2b8d-13d0-4b0f-8f72-2e2fda7fd001",
      "campaign_id": 55,
      "assignment_id": 101,
      "guest_id": 9001,
      "person_id": "b2a0f1de-dfd9-45ac-8b54-41a4d18fd001",
      "phone_e164": "+79991112233",
      "coupon_series": "TEST",
      "coupon_code": "TST-A001",
      "valid_until": "2026-05-18T23:59:59+05:00",
      "venue_code": "DEP_1",
      "venue_name": "Тестовое заведение",
      "promo_text": "Скидка 20%",
      "status": "reserved",
      "vtelemax_sync_status": "pending"
    }
  ]
}
```

Ожидаемое действие vtelemax:

- найти получателя по `person_id` или `phone_e164`;
- привязать к нему купон `coupon_series + coupon_code`;
- сохранить `valid_until` как отдельный срок действия купона для карточки гостя;
- сделать купон активным/видимым в интерфейсе vtelemax;
- вернуть подтверждение на уровне элемента по `event_id`.

Поле `valid_until` передается только в `assignments`, формируется из `CouponCampaignAssignment.lifetime_expires_at` и отправляется в ISO 8601 с timezone. В `status_update` его можно не передавать, если срок действия купона не менялся.

Если получатель не найден или купон невозможно привязать, не нужно отклонять всю пачку. Нужно вернуть ошибку только по конкретному item.

## 6. Direction `status_update`

`status_update` означает: SAGUR сообщает изменение статуса уже назначенного купона.

Поддерживаемые статусы:

- `used` — купон использован в период действия акции;
- `used_after_campaign` — купон использован после завершения акции, но факт применения подтверждён iikoCard/OLAP;
- `expired` — купон истек после завершения кампании;
- `canceled` — купон отменен и должен быть убран у гостя;
- `sent` может появиться как расширение, если согласуем отдельное уведомление о фактической отправке сообщения.

Пример `used`:

```json
{
  "request_id": "f5dd7c7a-9080-4b48-a40a-3af2f864a201",
  "direction": "status_update",
  "sent_at": "2026-05-15T10:10:00Z",
  "items": [
    {
      "event_id": "60b56824-2956-4895-b0dc-7b5c3ec7f001",
      "campaign_id": 55,
      "assignment_id": 101,
      "person_id": "b2a0f1de-dfd9-45ac-8b54-41a4d18fd001",
      "phone_e164": "+79991112233",
      "coupon_series": "TEST",
      "coupon_code": "TST-A001",
      "status": "used",
      "status_at": "2026-05-15T10:09:30Z",
      "meta": {
        "order_id": 123456789,
        "release_to_pool": false
      }
    }
  ]
}
```

Для `used_after_campaign` структура такая же, но `status="used_after_campaign"`, а в `meta` SAGUR передаёт `used_after_campaign=true` и `release_to_pool=false`.

Пример `canceled`:

```json
{
  "request_id": "0f91f972-a5b2-407b-bd70-f44249ec3a90",
  "direction": "status_update",
  "sent_at": "2026-05-15T10:05:00Z",
  "items": [
    {
      "event_id": "f66dd3dc-8f12-4798-aaf8-0f5e98f4d100",
      "campaign_id": 55,
      "assignment_id": 101,
      "person_id": "b2a0f1de-dfd9-45ac-8b54-41a4d18fd001",
      "phone_e164": "+79991112233",
      "coupon_series": "TEST",
      "coupon_code": "TST-A001",
      "status": "canceled",
      "status_at": "2026-05-15T10:05:00Z",
      "meta": {
        "release_to_pool": true,
        "remove_from_guest": true
      }
    }
  ]
}
```

Ожидаемая семантика:

- `canceled` означает release: убрать купон у гостя и разрешить повторное назначение того же `coupon_series + coupon_code`;
- повторный `canceled` по тому же `event_id` или по уже снятому купону должен быть безопасным и идемпотентным;
- `used`, `used_after_campaign` и `expired` убирают купон из активных у гостя, но не освобождают его для повторной выдачи;
- `used_after_campaign` должен отображаться/храниться как отдельный статус: это не ошибка и не обычный `used`, а позднее подтверждённое применение купона;
- SAGUR освобождает купон обратно в пул только после подтверждения конкретного `canceled` item с `meta.release_to_pool=true`.

## 7. Response: обязательный item-level результат

Просим возвращать результат по каждому `event_id`.

Пример полного успеха:

```json
{
  "request_id": "6c0d7c33-9f3f-4ed0-b8a7-0877ab9f5c2a",
  "status": "acked",
  "results": [
    {
      "event_id": "6f8c2b8d-13d0-4b0f-8f72-2e2fda7fd001",
      "status": "acked"
    },
    {
      "event_id": "d853d2a2-73c4-4a9a-a6b3-d68f72b0d002",
      "status": "acked"
    }
  ]
}
```

Пример частичного успеха:

```json
{
  "request_id": "6c0d7c33-9f3f-4ed0-b8a7-0877ab9f5c2a",
  "status": "partial",
  "results": [
    {
      "event_id": "6f8c2b8d-13d0-4b0f-8f72-2e2fda7fd001",
      "status": "acked"
    },
    {
      "event_id": "d853d2a2-73c4-4a9a-a6b3-d68f72b0d002",
      "status": "rejected",
      "code": "recipient_not_found",
      "message": "Получатель не найден"
    }
  ]
}
```

SAGUR считает item успешным, если:

- `status` равен `acked`, `ok`, `success` или `accepted`;
- или у item есть `"ok": true`.

SAGUR считает item ошибочным, если:

- по `event_id` нет элемента в `results[]`;
- `status` равен `rejected`, `error`, `failed` или любому неизвестному неуспешному значению;
- есть `"ok": false`;
- batch-level ответ содержит `"ok": false`;
- HTTP-статус 4xx/5xx.

## 8. Почему нельзя отвечать только `{"ok": true}`

Для batch-формата общий `ok=true` недостаточен. SAGUR должен понимать результат по каждому назначению купона.

Если в пачке 100 items, из них 99 успешно обработаны, а 1 не найден у вас в базе, SAGUR должен:

- 99 items перевести в подтвержденные;
- 1 item оставить в ошибке/retry;
- не блокировать всю кампанию из-за уже подтвержденных items;
- показать оператору конкретную причину по проблемному гостю.

Поэтому `results[]` обязателен.

## 9. Идемпотентность и повторная доставка

Ключ идемпотентности item: `event_id`.

Что требуется:

- повторная доставка того же `event_id` не должна создавать дубликаты купона у гостя;
- если item уже обработан успешно, повторный запрос должен вернуть успешный результат по тому же `event_id`;
- если item был отклонен по постоянной бизнес-причине, повторный запрос может вернуть тот же `rejected` с тем же `code`;
- `request_id` не должен использоваться как ключ идемпотентности бизнес-события, потому что при retry пачка может получить новый `request_id`, но items сохранят свои `event_id`.

## 10. Ошибки

Рекомендуемые item-level `code`:

- `recipient_not_found` — получатель не найден по `person_id`/`phone_e164`;
- `recipient_channel_disabled` — получатель есть, но купон нельзя показать из-за состояния канала;
- `coupon_already_assigned` — купон уже привязан конфликтующим образом;
- `coupon_not_active` — купон невозможно показать;
- `invalid_payload` — некорректные поля item;
- `internal_error` — внутренняя ошибка обработки item.

Пример item-ошибки:

```json
{
  "event_id": "d853d2a2-73c4-4a9a-a6b3-d68f72b0d002",
  "status": "rejected",
  "code": "recipient_not_found",
  "message": "Получатель не найден"
}
```

Batch-level HTTP 4xx/5xx стоит использовать только если не удалось обработать запрос целиком: неверная подпись, невалидный JSON, недоступность сервиса, системная ошибка до разбора items.

## 11. Требования к логированию на стороне vtelemax

Просим логировать:

- `request_id`;
- `direction`;
- количество `items`;
- количество `acked`;
- количество `rejected/error`;
- список проблемных `event_id` с `code`;
- время обработки пачки;
- результат проверки HMAC.

Для расследования инцидентов нам критично иметь возможность сопоставить SAGUR queue event по `event_id` с логом vtelemax.

## 12. Проверочные сценарии

Просим покрыть у себя минимум такие сценарии:

1. `assignments`: пачка из 2 items, оба успешно обработаны, оба вернулись в `results[]` как `acked`.
2. `assignments`: пачка из 2 items, один `acked`, второй `rejected recipient_not_found`.
3. `assignments`: повторная доставка того же `event_id` не создает дубль купона.
4. `status_update:used`: купон скрывается/помечается использованным, но не освобождается для повторной выдачи.
5. `status_update:used_after_campaign`: купон скрывается/помечается использованным после завершения акции, но не освобождается для повторной выдачи.
6. `status_update:expired`: купон скрывается/помечается истекшим, но не освобождается для повторной выдачи.
7. `status_update:canceled` с `meta.release_to_pool=true`: купон убирается у гостя, затем SAGUR после подтверждения может назначить тот же `series+code` другому гостю.
8. Повторный `status_update:canceled` по уже снятому купону безопасен и возвращает успешный item-level результат.
9. Ответ без `results[]` или без результата по конкретному `event_id` считается некорректным для SAGUR.

## 13. E2E-сценарий, который будем совместно проверять

1. SAGUR генерирует/проверяет пул купонов.
2. SAGUR создает кампанию на несколько тестовых гостей.
3. SAGUR отправляет `direction=assignments` пачкой.
4. vtelemax возвращает `results[]` по каждому item.
5. SAGUR пропускает sync-gate только для ACKed назначений.
6. SAGUR отправляет рассылку.
7. По одному гостю имитируем `used`, SAGUR отправляет `status_update:used`, vtelemax скрывает активный купон.
8. По истёкшему купону имитируем позднее применение, SAGUR отправляет `status_update:used_after_campaign`, vtelemax хранит тот же статус и скрывает купон.
9. По другому гостю отменяем reserved-назначение, SAGUR отправляет `status_update:canceled` с `release_to_pool=true`.
10. Только после подтверждения от vtelemax SAGUR освобождает купон обратно в пул.
11. SAGUR назначает тот же `coupon_series + coupon_code` другому гостю.

Главный контрольный кейс: `canceled -> release -> reassign`.

## 14. Что нужно переделать у вас

Просим внести следующие изменения:

- принимать верхнеуровневый batch-payload с `request_id`, `direction`, `sent_at`, `items[]`;
- перестать ожидать один `event_id` на верхнем уровне запроса;
- читать `event_id` внутри каждого item;
- обрабатывать каждый item независимо;
- возвращать `results[]` с результатом по каждому `event_id`;
- обеспечить идемпотентность по `event_id`;
- поддержать частичный успех пачки;
- сохранить согласованную семантику `canceled`, `used`, `used_after_campaign`, `expired`;
- убедиться, что `canceled` удаляет купон у гостя и разрешает повторное назначение того же `series+code`;
- обеспечить логи по `request_id` и `event_id`.

Со стороны SAGUR batch-отправка уже реализована в тестовом контуре и покрыта регрессионными тестами.
