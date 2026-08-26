# Совместный E2E-чек-лист SAGUR ↔ vtelemax: купонный batch-контур

Дата подготовки: 2026-05-15.

Цель проверки: подтвердить полный купонный сценарий от генерации/проверки пула в SAGUR до отображения купонов в vtelemax, обработки `used/used_after_campaign/expired/canceled` и контрольного кейса `canceled -> release -> reassign`.

Связанный контракт: `docs/operations/VTELEMAX_COUPON_BATCH_CONTRACT_MESSAGE_2026-05-15.md`.

## 1. Участники и роли

- SAGUR: оператор/разработчик, который управляет пулом, кампанией, очередью и проверяет БД/UI.
- vtelemax: разработчик/дежурный интеграции, который проверяет входящие batch-запросы, логи, UI и item-level ответы.
- iikoCard/iiko: оператор или ответственный, который подтверждает загрузку тестового пула купонов и доступность API проверки.

## 2. Фиксация тестового окружения

Перед стартом заполнить:

| Поле | Значение |
|---|---|
| Дата/время E2E | |
| Контур SAGUR | |
| Контур vtelemax | |
| Контур iikoCard | |
| Версия/commit SAGUR | |
| Версия/commit vtelemax | |
| `VTELEMAX_COUPON_SYNC_BASE_URL` | |
| `VTELEMAX_COUPON_SYNC_ENDPOINT` | |
| `VTELEMAX_COUPON_SYNC_BATCH_SIZE` | |
| `IIKO_AUTH_MODE` | |
| `IIKO_API_BASE_URL` | |
| Ответственный SAGUR | |
| Ответственный vtelemax | |

## 3. Предусловия

- [ ] vtelemax принимает batch payload с `request_id`, `direction`, `sent_at`, `items[]`.
- [ ] vtelemax возвращает `results[]` по каждому `event_id`.
- [ ] vtelemax ведёт логи по `request_id` и `event_id`.
- [ ] HMAC-секрет и endpoint совпадают на обеих сторонах.
- [ ] В SAGUR включен `VTELEMAX_COUPON_SYNC_ENABLED=True`.
- [ ] В SAGUR настроен `VTELEMAX_COUPON_SYNC_BATCH_SIZE=100` или согласованное значение.
- [ ] В SAGUR явно задан `IIKO_AUTH_MODE=legacy|v2`, настроены `IIKO_API_BASE_URL` и `IIKO_ORGANIZATION_ID`. Один ключ iikoWeb передан как `IIKO_LEGACY_API_LOGIN` для `legacy` и как `IIKO_API_KEY` для `v2`; для `v2` дополнительно заданы `IIKO_APP_ID` и `IIKO_CLIENT_SECRET`.
- [ ] Есть минимум 3 тестовых гостя с валидными `phone_e164` и vtelemax-каналами.
- [ ] Для reassign-сценария подготовлен отдельный гость D: гости A/B/C используются в ветках `used/expired/canceled`.
- [ ] Перед стартом нет неизвестных старых pending/error событий по тестовой серии.
- [ ] Negative test с ответом без `results[]` будет проводиться через mock/stub endpoint, не через production vtelemax.
- [ ] На стороне vtelemax подтвержден лимит тела запроса 512 KB; batch size 100 поддерживается.

## 4. Тестовые данные

Рекомендуется использовать новую уникальную серию на прогон:

| Сущность | Значение |
|---|---|
| `coupon_series` | |
| `venue_code` | |
| `venue_name` | |
| `campaign_id` | |
| Гость A / phone / person_id | |
| Гость B / phone / person_id | |
| Гость C / phone / person_id | |
| Гость D / phone / person_id | |
| Купон A | |
| Купон B | |
| Купон C | |
| Купон D, если нужен отдельный reassign-пул | |
| Тестовый `order_id` для `used` | |

## 5. Быстрый технический smoke перед E2E

На стороне SAGUR:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_pytest.ps1 guests/tests/test_vtelemax_coupon_sync_service.py guests/tests/test_coupon_campaign_lifecycle_service.py guests/tests/test_coupon_campaign_gate_service.py
```

Ожидаемо:

- [ ] тесты зелёные;
- [ ] нет ошибок импорта/settings;
- [ ] актуальный код содержит batch-отправку vtelemax.

Проверка health воркера:

```bash
python manage.py run_coupon_vtelemax_sync_worker --health-check
```

Ожидаемо:

- [ ] `status=healthy`;
- [ ] endpoint и HMAC-конфигурация валидны.

Read-only preflight купонного контура:

```bash
python manage.py audit_coupon_release_readiness
```

Ожидаемо:

- [ ] итог `status=ready` или только согласованные `warning`;
- [ ] нет `blocked` по конфигурации vtelemax;
- [ ] нет событий очереди, исчерпавших retry;
- [ ] нет release-событий с подтверждением без фактического возврата купона в пул;
- [ ] синк получателей vtelemax свежий для sync-gate.

## 6. Сценарий 1: генерация и проверка пула

На стороне SAGUR:

```bash
python manage.py generate_coupon_pool \
  --series <SERIES> \
  --venue-code <VENUE_CODE> \
  --venue-name "<VENUE_NAME>" \
  --prefix E2E- \
  --count 5 \
  --random-length 8 \
  --generated-by e2e \
  --export-path docs/operations/templates/iikocard_coupon_import_<SERIES>_e2e.csv
```

Действия:

- [ ] SAGUR создал `CouponPoolBatch`.
- [ ] SAGUR создал 5 записей `CouponRegistryEntry`.
- [ ] CSV передан/загружен в iikoCard.
- [ ] iikoCard подтвердил загрузку купонов.

Проверка SAGUR ↔ iiko:

```bash
python manage.py verify_coupon_pool_iiko --series <SERIES> --sample-info-check-limit 5
```

Ожидаемо:

- [ ] все тестовые купоны имеют `iiko_check_status=found`;
- [ ] batch имеет статус полной или ожидаемо частичной загрузки;
- [ ] в UI реестра видны серия, batch, заведение, статус iiko.

## 7. Сценарий 2: создание кампании и reserve назначений

На стороне SAGUR через UI:

- [ ] создать тестовую кампанию на гостей A/B/C;
- [ ] указать `coupon_series=<SERIES>`;
- [ ] указать `venue_code=<VENUE_CODE>`;
- [ ] указать promo text;
- [ ] в шаблоне использовать купонные переменные, например код купона.

Ожидаемо после подготовки/старта рассылочного контура:

- [ ] на каждого гостя создан `CouponCampaignAssignment`;
- [ ] каждому назначению выдан уникальный `coupon_code`;
- [ ] купоны переведены из доступных в assigned;
- [ ] созданы события `CouponVtelemaxSyncQueue` с `direction=assignments`;
- [ ] до подтверждения от vtelemax синхронизационный шлюз не должен пропустить фактическую отправку рассылки.

Фиксируем:

| Гость | `assignment_id` | `event_id` assignments | `coupon_series` | `coupon_code` |
|---|---:|---|---|---|
| A | | | | |
| B | | | | |
| C | | | | |

## 8. Сценарий 3: batch `assignments` в vtelemax

На стороне SAGUR:

```bash
python manage.py run_coupon_vtelemax_sync_worker --once --batch-size 100 --force-run
```

Ожидаемый HTTP request в vtelemax:

- [ ] один request с `direction=assignments`;
- [ ] верхний уровень содержит `request_id`, `direction`, `sent_at`, `items[]`;
- [ ] нет верхнеуровневого `payload`;
- [ ] каждый item содержит `event_id`;
- [ ] каждый `assignments` item содержит `valid_until` в ISO 8601 с timezone;
- [ ] количество items соответствует числу тестовых назначений;
- [ ] HMAC успешно проверен.

Ожидаемый response vtelemax:

```json
{
  "request_id": "<same-or-logged-request-id>",
  "status": "acked",
  "results": [
    {"event_id": "<event_id_A>", "status": "acked"},
    {"event_id": "<event_id_B>", "status": "acked"},
    {"event_id": "<event_id_C>", "status": "acked"}
  ]
}
```

Ожидаемо в SAGUR:

- [ ] все assignment-события перешли в `acked`;
- [ ] у назначений `vtelemax_sync_status=ok`;
- [ ] заполнен `vtelemax_synced_at`;
- [ ] в реестре купонов виден статус vtelemax sync.

## 9. Сценарий 4: sync-gate и фактическая отправка кампании

На стороне SAGUR:

- [ ] повторно запустить/продолжить рассылочный worker или действие UI, которое отправляет готовые строки;
- [ ] убедиться, что sync-gate больше не блокирует ACKed гостей.

Ожидаемо:

- [ ] строки кампании уходят в dispatch;
- [ ] назначения переходят `reserved -> sent`;
- [ ] купонный текст содержит корректный `coupon_code`;
- [ ] vtelemax продолжает показывать активные купоны у гостей.

## 10. Сценарий 5: `status_update:used`

На стороне SAGUR подготовить факт использования одного купона, например гостя A, через OLAP/order fact или согласованный тестовый способ.

Затем:

```bash
python manage.py sync_coupon_redemptions --limit 100
python manage.py run_coupon_vtelemax_sync_worker --once --batch-size 100 --force-run
```

Ожидаемый request:

- [ ] `direction=status_update`;
- [ ] item по гостю A содержит `status=used`;
- [ ] item содержит `coupon_series`, `coupon_code`, `assignment_id`;
- [ ] `meta.release_to_pool=false` или release отсутствует/ложный.

Ожидаемо в vtelemax:

- [ ] купон гостя A скрыт из активных или помечен использованным;
- [ ] купон не доступен для повторной выдачи.

Ожидаемо в SAGUR:

- [ ] assignment A имеет `status=used`;
- [ ] заполнены `used_at` и, если доступно, `used_order_id`;
- [ ] registry entry имеет статус used;
- [ ] status_update event ACKed.

## 11. Сценарий 6: `status_update:expired`

Для гостя B оставить назначение неиспользованным и закрыть кампанию после окончания окна.

На стороне SAGUR:

```bash
python manage.py close_coupon_campaigns --campaign-id <CAMPAIGN_ID> --limit 10
python manage.py run_coupon_vtelemax_sync_worker --once --batch-size 100 --force-run
```

Ожидаемо:

- [ ] для sent-назначения B создан `status_update` со `status=expired`;
- [ ] vtelemax скрывает/помечает купон B истекшим;
- [ ] SAGUR не возвращает купон B в доступный пул;
- [ ] повторное назначение этого `series+code` запрещено.

### 11.1. Проверка позднего использования после `expired`

После сценария `expired` имитировать в iiko/OLAP факт применения этого же купона уже после завершения окна кампании.

На стороне SAGUR:

```bash
python manage.py sync_coupon_redemptions --limit 100
python manage.py run_coupon_vtelemax_sync_worker --once --batch-size 100 --force-run
```

Ожидаемо:

- [ ] assignment B переходит из `expired` в `used_after_campaign`;
- [ ] registry entry B переходит в `used_after_campaign`;
- [ ] создан `status_update` со `status=used_after_campaign`;
- [ ] `meta.release_to_pool=false`;
- [ ] vtelemax хранит/показывает статус `used_after_campaign` в том же смысле, что SAGUR;
- [ ] купон B не возвращается в пул и не доступен для повторной выдачи;
- [ ] отчёт кампании учитывает купон в общем `assignments_used` и отдельно в `assignments_used_after_campaign`.

## 12. Сценарий 7: `canceled -> release -> reassign`

Это главный контрольный сценарий.

Подготовка:

- [ ] создать отдельную короткую тестовую кампанию или использовать гостя C до фактической отправки;
- [ ] добиться состояния assignment C = `reserved`;
- [ ] убедиться, что купон C назначен гостю C и не доступен в пуле.

На стороне SAGUR отменить кампанию/назначение так, чтобы reserved ушел в canceled:

```bash
python manage.py close_coupon_campaigns --campaign-id <CAMPAIGN_ID> --limit 10
```

Или использовать согласованное UI-действие отмены кампании.

Проверить до подтверждения:

- [ ] assignment C имеет `status=canceled`;
- [ ] создан `status_update:canceled`;
- [ ] `meta.release_to_pool=true`;
- [ ] купон C еще НЕ доступен для повторной выдачи до подтверждения vtelemax.

Отправить событие:

```bash
python manage.py run_coupon_vtelemax_sync_worker --once --batch-size 100 --force-run
```

Ожидаемо в vtelemax:

- [ ] купон C удален/скрыт у гостя C;
- [ ] повторный `canceled` безопасен;
- [ ] ответ содержит подтверждение по конкретному `event_id`.

Ожидаемо в SAGUR после подтверждения:

- [ ] event `status_update:canceled` имеет `acked`;
- [ ] купон C вернулся в `verified_loaded`;
- [ ] `is_active=True`;
- [ ] `assigned_at=None`;
- [ ] тот же `coupon_series + coupon_code` можно назначить другому гостю.

Reassign:

- [ ] использовать отдельного гостя D, не участвующего в ветках A/B/C;
- [ ] создать новую тестовую кампанию/строку на гостя D;
- [ ] подтвердить, что SAGUR может назначить освобожденный `coupon_series + coupon_code`;
- [ ] отправить новый `assignments` batch;
- [ ] vtelemax показывает тот же код уже у гостя D, а не у гостя C.

Фиксация:

| Шаг | Старый гость C | Новый гость D |
|---|---|---|
| `coupon_series` | | |
| `coupon_code` | | |
| old `assignment_id` | | |
| cancel `event_id` | | |
| new `assignment_id` | | |
| new assignment `event_id` | | |

## 13. Негативный сценарий: частичное подтверждение

Просим vtelemax на тестовой пачке вернуть:

- item A: `acked`;
- item B: `rejected`, `code=recipient_not_found`;
- item C: `acked`.

Ожидаемо в SAGUR:

- [ ] A/C события переходят в `acked`;
- [ ] B событие переходит в `error`;
- [ ] у B заполнен `last_error`;
- [ ] у assignment B `vtelemax_sync_status=error`;
- [ ] sync-gate блокирует только проблемную строку/причину, а успешные ACKed назначения не теряются.

## 14. Негативный сценарий: нет `results[]`

Важно: этот сценарий проводим только через mock/stub endpoint, не через production vtelemax.

Просим vtelemax на отдельном тестовом запросе вернуть только:

```json
{"ok": true}
```

Ожидаемо в SAGUR:

- [ ] событие не считается ACKed;
- [ ] event переходит в `error`;
- [ ] ошибка содержит смысл: нет item-level `results[]`;
- [ ] retry возможен после исправления ответа vtelemax.

## 15. Аудит после E2E

На стороне SAGUR:

```bash
python manage.py audit_coupon_release_sync --show-rows --limit 50
python manage.py report_coupon_campaign_performance --campaign-id <CAMPAIGN_ID> --as-json
```

Ожидаемо:

- [ ] нет зависших release после подтверждения;
- [ ] нет активных купонов у старых гостей после `used/used_after_campaign/expired/canceled`;
- [ ] нет повторно выданных купонов без предварительного подтверждения `canceled`;
- [ ] отчет кампании показывает корректные назначения, used/used_after_campaign/expired/canceled.

На стороне vtelemax:

- [ ] по всем `request_id` есть логи приема;
- [ ] по всем `event_id` есть item-level результат;
- [ ] нет дублей активных купонов у одного гостя;
- [ ] нет активного купона у гостя C после release/reassign;
- [ ] у гостя D отображается переназначенный купон.

## 16. Критерии успешного E2E

E2E считается пройденным, если:

- [ ] `assignments` batch принят и подтвержден item-level `results[]`;
- [ ] синхронизационный шлюз в SAGUR работает строго по подтверждению;
- [ ] `used` скрывает купон и не освобождает его;
- [ ] `used_after_campaign` скрывает купон, не освобождает его и совпадает по статусу в SAGUR/vtelemax;
- [ ] `expired` скрывает купон и не освобождает его;
- [ ] `canceled` скрывает купон и освобождает его только после подтверждения;
- [ ] тот же `coupon_series + coupon_code` успешно переназначен другому гостю после release;
- [ ] частичное подтверждение корректно разделяет успешные и ошибочные items;
- [ ] ответ без `results[]` не принимается как подтверждение;
- [ ] SAGUR и vtelemax могут сопоставить логи по `request_id` и `event_id`.

## 17. Что фиксируем в итоговом протоколе

| Проверка | Результат | Комментарий |
|---|---|---|
| Pool generate/export/verify | | |
| `assignments` batch | | |
| подтверждение на уровне элемента | | |
| sync-gate | | |
| dispatch после подтверждения | | |
| `used` | | |
| `used_after_campaign` | Отложено | Отдельная проверка зафиксирована как технический долг: нужен контролируемый сценарий применения после окончания окна кампании. |
| `expired` | OK | Кампания `#4`: `status_update:expired` ACKed во vtelemax, 3 купона скрыты из активного меню гостей. Повторять сейчас не требуется, можно оставить как регрессионную проверку после включения автоматизации. |
| `canceled -> release` | OK | Кампания `#6`: `status_update:canceled` с `meta.release_to_pool=true` ACKed, купон возвращён в пул SAGUR и удалён у гостя C во vtelemax. |
| `reassign` | OK | Кампания `#7`: тот же `coupon_series+coupon_code` повторно назначен гостю D, подтверждён во vtelemax как активный/видимый, затем очищен через cleanup cancel. |
| частичное подтверждение | | |
| no `results[]` negative test | | |
| audit clean | OK | После cleanup: активных тестовых кампаний нет, открытой купонной очереди нет, pending/error назначений нет, открытых dispatch-задач нет. |

### 17.1. Фактически зафиксированный результат: позитивный сценарий `assign -> подтверждение -> send -> visible QR -> used`

Дата фиксации: 2026-05-20.

Тестовая кампания SAGUR:

| Поле | Значение |
|---|---|
| `campaign_id` | `5` |
| Название | `E2E Сами Сусами Американо 19.05.2026 A-B-C-D (тест)` |
| `coupon_series` | `E2E_SAMI_20260519` |
| Заведение | `Сами Сусами` |
| `venue_code` | `c9a0df27-11dc-4bee-83a3-f0a5aa16c185` |
| Аудитория | 3 гостя |
| Batch size | 100 |

Фактически пройденная цепочка:

| Шаг | Результат | Подтверждение |
|---|---|---|
| Генерация/экспорт пула | OK | Пул создан в SAGUR, CSV выгружен и загружен в iikoCard. |
| Проверка iikoCard | OK | Купоны серии `E2E_SAMI_20260519` подтверждены как найденные в iikoCard. |
| Назначение купонов | OK | Для кампании `#5` созданы 3 назначения. |
| Batch `assignments` в vtelemax | OK | `run_coupon_vtelemax_sync_worker --once --batch-size 100`: `processed=3 acked=3 failed=0`. |
| Синхронизационный шлюз | OK | До подтверждения рассылка блокировалась; после подтверждения повторный `run-now` пропустил 3 строки. |
| Dispatch SAGUR | OK | Все 3 dispatch-задачи завершены `done`: Telegram, MAX, VK. |
| Отображение купона в vtelemax/боте | OK | Telegram подтверждён пользователем; MAX подтверждён коллегой, QR-код формируется; VK подтверждён пользователем, купон пришёл и отображается. |
| Реальное применение купона | OK | Купон `E2E-JA03FCBC` применён в заведении `Сами Сусами`, заказ `70`, оплаченная сумма заказа после скидок `350.00`. |
| OLAP -> order_fact -> coupon redemption | OK | SAGUR нашёл факт применения по OLAP/order_fact и перевёл назначение в `used`. |
| `status_update:used` в vtelemax | OK | `run_coupon_vtelemax_sync_worker --once --batch-size 100`: `processed=1 acked=1 failed=0`. |
| Скрытие использованного купона | OK | В vtelemax Telegram-боте купон `E2E-JA03FCBC` больше не отображается как активный. |
| Отчёт SAGUR | OK | Отчёт кампании `#5` показывает 3 отправленных купона, 1 использованный, конверсию 33,33%, оплаченную сумму `350.00`. |

Детализация назначений кампании `#5`:

| Канал | Телефон | assignment_id | Купон | `assignments` event_id | Dispatch | Итог |
|---|---|---:|---|---|---|---|
| Telegram | `+79129923438` | `4` | `E2E-JA03FCBC` | `6acc7680-e5cc-412a-a825-882061e48bb8` | `done` | Купон использован, `status_update:used` ACKed. |
| MAX | `+79220093686` | `5` | `E2E-NMI2ZS0S` | `ddfaf383-9b60-4df3-b77a-84d52b0f4e7d` | `done` | Получение сообщения и отображение QR подтверждены коллегой. |
| VK | `+79959343477` | `6` | `E2E-KFR2J6FL` | `63f104c6-d0dd-4e58-bc9c-9279ef3cb276` | `done` | Получение сообщения и отображение купона подтверждены пользователем. |

Событие использования:

| Поле | Значение |
|---|---|
| Купон | `E2E_SAMI_20260519:E2E-JA03FCBC` |
| `status_update` event_id | `d52c8366-b772-4c52-8847-54381179a729` |
| Итоговый статус SAGUR | `used` |
| `used_order_id` | `70` |
| Заведение | `Сами Сусами` |
| Оплаченная сумма по заказу после скидок | `350.00` |
| Подарочная позиция | `Американо`, до скидки `190.00`, оплачено `0.00` |

Итог по позитивному сценарию: **пройден**. Оставшиеся отдельные проверки полного контракта: `used_after_campaign`; повторный `expired` можно выполнить после завершения активных тестовых купонов кампании `#5`, если потребуется дополнительное подтверждение.

### 17.2. Фактически зафиксированный результат: `canceled -> release -> reassign`

Дата фиксации: 2026-05-21.

Цель проверки: подтвердить, что неотправленный `reserved`-купон можно безопасно отменить, освободить только после подтверждения vtelemax и повторно назначить другому гостю тем же `coupon_series + coupon_code`.

Тестовые данные:

| Поле | Значение |
|---|---|
| `coupon_series` | `E2E_CANCEL_RELEASE_20260521` |
| `coupon_code` | `REL-DBEXB604` |
| Заведение | `Сами Сусами` |
| `venue_code` | `c9a0df27-11dc-4bee-83a3-f0a5aa16c185` |
| Гость C | `+79129923438`, `person_id=c93cb561-f002-42de-935d-eb79dbaad0ea` |
| Гость D | `+79995487851`, `person_id=3765e30a-8fce-4620-a5b3-3ea65c1d41ac` |

Фактически пройденная цепочка:

| Шаг | Результат | Подтверждение |
|---|---|---|
| Генерация и проверка пула | OK | Создана серия из 1 купона, CSV загружен в iikoCard, партия подтверждена как `loaded`. |
| Назначение гостю C | OK | Кампания `#6`, `assignment_id=7`, купон `REL-DBEXB604`, статус `reserved`. |
| Batch `assignments` для C | OK | `event_id=7c351c55-fcf5-4fa2-8d3f-e758986c4363`, ACKed в SAGUR и подтверждён во vtelemax. |
| Отправка сообщения C | OK | Сообщение не отправлялось: `dispatch_tasks=[]`, кампания не активировалась для фактической доставки. |
| Pre-state vtelemax перед отменой | OK | vtelemax подтвердил активный/видимый купон у C и отсутствие этого купона у других гостей. |
| Safe cancel кампании `#6` | OK | Строка аудитории отменена, assignment переведён в `canceled`, создано `status_update:canceled`. |
| Release-событие | OK | `event_id=8972983a-6545-4ba1-bddf-a81905ee0288`, `meta.release_to_pool=true`, `meta.remove_from_guest=true`. |
| Подтверждение release от vtelemax | OK | `processed=1 acked=1 failed=0 status_updates_acked=1`; во vtelemax купон удалён у C, active_visible_count=0. |
| Возврат купона в пул SAGUR | OK | Купон `REL-DBEXB604`: `pool_status=verified_loaded`, `is_active=true`, `assigned_at=None`. |
| Pre-state vtelemax перед reassign | OK | vtelemax подтвердил: у C купона нет, у D купона нет, активной занятой связки по `series+code` нет. |
| Reassign гостю D | OK | Кампания `#7`, `assignment_id=8`, тот же `coupon_id=15`, тот же `coupon_code=REL-DBEXB604`, статус `reserved`. |
| Batch `assignments` для D | OK | `event_id=74519f3f-8d40-4393-b652-1d93c703c8b7`, ACKed в SAGUR. |
| Post-state vtelemax после reassign | OK | vtelemax подтвердил активный/видимый купон у D, отсутствие купона у C, ровно одну активную связку по `coupon_series+coupon_code`. |
| Cleanup кампании `#7` | OK | Кампания `#7` остановлена, строка аудитории отменена, создано финальное `status_update:canceled` с `meta.release_to_pool=true`. |
| Подтверждение очистки release | OK | `event_id=c4c55aba-7484-4189-a7be-54848a7648e1`, `processed=1 acked=1 failed=0 status_updates_acked=1`. |
| Финальный post-cleanup SAGUR | OK | Assignment `#8`: `status=canceled`, `vtelemax_sync_status=ok`; купон `REL-DBEXB604`: `pool_status=verified_loaded`, `is_active=true`, `assigned_at=None`; `dispatch_tasks=[]`. |
| Финальный post-cleanup vtelemax | OK | Купон отсутствует у C и D; `active_visible_occupied_rows=0`, `any_rows_for_coupon=0`, rejected=0, problems=[]. |

Итог по сценарию `canceled -> release -> reassign -> cleanup`: **пройден**.

Зафиксированная семантика:

- `canceled` с `meta.release_to_pool=true` удаляет купон у гостя во vtelemax;
- SAGUR возвращает купон в пул только после подтверждения на уровне элемента от vtelemax;
- тот же `coupon_series + coupon_code` может быть повторно назначен другому гостю;
- повторное назначение не создаёт дубль у старого гостя;
- в тесте reassign сообщения гостям не отправлялись, проверялся именно жизненный цикл купона и состояние vtelemax;
- после cleanup тестовый купон освобождён и не висит активным ни у одного гостя.

### 17.3. Финальная операционная чистота после E2E cleanup

Дата фиксации: 2026-05-21.

Проверка выполнялась после cleanup кампаний `#6` и `#7`.

Фактическое состояние SAGUR:

| Проверка | Результат |
|---|---|
| Активные рассылочные кампании | `active_mailings=[]` |
| Открытая очередь купонных событий | `open_coupon_queue=[]` |
| Назначения с `pending/error` синхронизацией vtelemax | `pending_or_error_assignments=[]` |
| Открытые dispatch-задачи | `dispatch_open=[]` |
| Кампания `#5` | `is_active=False`, позитивный сценарий `used` завершён |
| Кампания `#6` | `is_active=False`, cancel/release для гостя C завершён |
| Кампания `#7` | `is_active=False`, reassign и cleanup для гостя D завершены |
| Тестовый купон `REL-DBEXB604` | `pool_status=verified_loaded`, `is_active=True`, `assigned_at=None` |

Фактическое состояние аудита release:

| Метрика | Значение |
|---|---:|
| `canceled_total` | 2 |
| `canceled_release_requested_total` | 2 |
| `release_waiting_ack` | 0 |
| `release_acked_not_released` | 0 |
| `release_done` | 2 |
| `reserved_stale_total` | 0 |

Итог: после тестов нет зависших купонных событий, нет зависших release, нет активных тестовых кампаний и нет открытых dispatch-задач. Тестовый купон `E2E_CANCEL_RELEASE_20260521:REL-DBEXB604` снова находится в пуле SAGUR и не занят гостем.

### 17.4. Отложенные проверки и автоматизация

`used_after_campaign`:

- статус: технический долг;
- цель будущей проверки: подтвердить, что купон, применённый после окончания окна кампании, переводится в `used_after_campaign`, скрывается у гостя во vtelemax и не возвращается в пул;
- текущий E2E этот сценарий не закрывал.

`expired`:

- отдельный сценарий уже был пройден на кампании `#4`: `status_update:expired` дошёл до vtelemax, 3 купона переведены в `expired` и скрыты из активного меню;
- повторный `expired` сейчас не требуется для закрытия текущего ручного E2E;
- повторить `expired` имеет смысл как регрессионную проверку после включения автоматических расписаний закрытия кампаний.

Автоматизация:

- `VTELEMAX_COUPON_SYNC_SCHEDULE_ENABLED=False` — автоматическая плановая отправка купонной очереди во vtelemax пока выключена;
- `COUPON_CAMPAIGN_CLOSE_SCHEDULE_ENABLED=False` — автоматическое плановое закрытие завершённых купонных кампаний пока выключено;
- предупреждения audit по этим двум настройкам ожидаемые и не являются ошибкой текущего ручного E2E;
- перед включением нужно отдельно согласовать эксплуатационную схему: какие worker-процессы работают постоянно, какие интервалы запуска, какие health-check/alert считаются обязательными и как оператор видит зависшие очереди.

Итоговое решение:

- [ ] E2E пройден, контур готов к релизной проверке.
- [ ] E2E пройден с замечаниями, нужен список исправлений.
- [ ] E2E не пройден, релизный контур блокируется.
