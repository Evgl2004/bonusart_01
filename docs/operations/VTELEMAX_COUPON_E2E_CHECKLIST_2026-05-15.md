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
- [ ] В SAGUR настроены `IIKO_API_KEY`, `IIKO_API_BASE_URL`, `IIKO_ORGANIZATION_ID`.
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
- [ ] нет release-событий с ACK без фактического возврата купона в пул;
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
- [ ] до ACK от vtelemax sync-gate не должен пропустить фактическую отправку рассылки.

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

Проверить до ACK:

- [ ] assignment C имеет `status=canceled`;
- [ ] создан `status_update:canceled`;
- [ ] `meta.release_to_pool=true`;
- [ ] купон C еще НЕ доступен для повторной выдачи до ACK vtelemax.

Отправить событие:

```bash
python manage.py run_coupon_vtelemax_sync_worker --once --batch-size 100 --force-run
```

Ожидаемо в vtelemax:

- [ ] купон C удален/скрыт у гостя C;
- [ ] повторный `canceled` безопасен;
- [ ] response содержит ACK по конкретному `event_id`.

Ожидаемо в SAGUR после ACK:

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

## 13. Негативный сценарий: partial ACK

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

- [ ] нет зависших release после ACK;
- [ ] нет активных купонов у старых гостей после `used/used_after_campaign/expired/canceled`;
- [ ] нет повторно выданных купонов без предварительного ACK `canceled`;
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
- [ ] sync-gate в SAGUR работает строго по ACK;
- [ ] `used` скрывает купон и не освобождает его;
- [ ] `used_after_campaign` скрывает купон, не освобождает его и совпадает по статусу в SAGUR/vtelemax;
- [ ] `expired` скрывает купон и не освобождает его;
- [ ] `canceled` скрывает купон и освобождает его только после ACK;
- [ ] тот же `coupon_series + coupon_code` успешно переназначен другому гостю после release;
- [ ] partial ACK корректно разделяет успешные и ошибочные items;
- [ ] ответ без `results[]` не принимается как ACK;
- [ ] SAGUR и vtelemax могут сопоставить логи по `request_id` и `event_id`.

## 17. Что фиксируем в итоговом протоколе

| Проверка | Результат | Комментарий |
|---|---|---|
| Pool generate/export/verify | | |
| `assignments` batch | | |
| item-level ACK | | |
| sync-gate | | |
| dispatch после ACK | | |
| `used` | | |
| `used_after_campaign` | | |
| `expired` | | |
| `canceled -> release` | | |
| `reassign` | | |
| partial ACK | | |
| no `results[]` negative test | | |
| audit clean | | |

Итоговое решение:

- [ ] E2E пройден, контур готов к релизной проверке.
- [ ] E2E пройден с замечаниями, нужен список исправлений.
- [ ] E2E не пройден, релизный контур блокируется.
