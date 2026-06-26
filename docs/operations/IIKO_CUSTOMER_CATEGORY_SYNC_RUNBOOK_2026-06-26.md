# iikoCard: категория «Активный купон SAGUR»

Документ описывает операционный контур, который добавляет гостя в общую
категорию iikoCard перед рассылкой купона и удаляет категорию после закрытия
последнего живого купона гостя.

## Назначение

Категория iikoCard используется как дополнительный фильтр акции: купон должен
применяться только у авторизованного гостя, которому SAGUR выдал активный купон.
Поэтому при включённом gate сообщение с купоном не отправляется, пока iikoCard
не подтвердил `add` категории.

## Включение

Минимальные переменные окружения:

```env
IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=True
IIKO_ACTIVE_COUPON_CATEGORY_ID=<id категории iikoCard>
IIKO_CUSTOMER_CATEGORY_GATE_REQUIRE_ACK=True
```

Плановая обработка через Django Q включается отдельно:

```env
IIKO_CUSTOMER_CATEGORY_SYNC_SCHEDULE_ENABLED=True
IIKO_CUSTOMER_CATEGORY_SYNC_SCHEDULE_MINUTES=1
```

Если `IIKO_CUSTOMER_CATEGORY_SYNC_ENABLED=False`, старый поток рассылки остаётся
без iikoCard-gate.

## Поток

1. SAGUR создаёт назначение купона гостю (`reserved`).
2. SAGUR ставит событие в очередь vtelemax.
3. SAGUR ставит `add` в очередь `IikoCustomerCategorySyncEvent`.
4. Рассылка разрешается только после ACK vtelemax и ACK iikoCard.
5. При `used`, `expired`, `canceled` SAGUR ставит `remove`, но только если у
   гостя нет другого живого купона.

Живой купон: любое назначение гостя в статусе `reserved` или `sent` в ручной
купонной кампании или купонном автосценарии.

## Команды

Один проход очереди:

```powershell
python manage.py run_iiko_customer_category_sync_worker --once
```

Запуск даже при выключенном глобальном флаге, для аварийной диагностики:

```powershell
python manage.py run_iiko_customer_category_sync_worker --once --force-run
```

Health-check без отправки API-запросов:

```powershell
python manage.py run_iiko_customer_category_sync_worker --health-check --verbose
```

Диагностика очереди:

```powershell
python manage.py diagnose_iiko_customer_category_sync --limit=20
```

Диагностика по гостю:

```powershell
python manage.py diagnose_iiko_customer_category_sync --guest-id=<guest_id>
```

## Нагрузочные настройки

```env
IIKO_CUSTOMER_CATEGORY_SYNC_BATCH_SIZE=100
IIKO_CUSTOMER_CATEGORY_SYNC_REQUEST_INTERVAL_SECONDS=0
IIKO_CUSTOMER_CATEGORY_SYNC_MAX_ATTEMPTS=8
IIKO_CUSTOMER_CATEGORY_SYNC_RETRY_BASE_SECONDS=30
IIKO_CUSTOMER_CATEGORY_SYNC_RETRY_MAX_SECONDS=3600
```

Если iikoCard начнёт ограничивать частоту запросов, сначала уменьшите
`IIKO_CUSTOMER_CATEGORY_SYNC_BATCH_SIZE` и задайте небольшую паузу
`IIKO_CUSTOMER_CATEGORY_SYNC_REQUEST_INTERVAL_SECONDS`.

## Безопасность удаления

`remove` проверяет живые купоны дважды:

1. при постановке события в очередь;
2. прямо перед API-вызовом `customer_category/remove`.

Если между постановкой события и обработкой очереди гостю выдали новый купон,
событие `remove` завершится статусом `skipped`, а категория останется у гостя.

Если iikoCard на `customer_category/remove` возвращает код
`Customer_CustomerHasNoCategory`, событие тоже завершается статусом `skipped`.
Это означает, что целевое состояние уже достигнуто: у гостя нет категории
`Активный купон SAGUR`. Повторять запрос не нужно, но причина сохраняется в
очереди и видна в диагностике.
