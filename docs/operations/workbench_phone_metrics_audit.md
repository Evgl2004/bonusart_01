# Команда аудита workbench по телефонам

## Назначение
`audit_workbench_phone_metrics` — сервисная команда для проверки корректности метрик в `guests/workbench` по конкретным телефонам.

Команда:
1. строит `payload` тем же сервисом, что использует UI (`build_guest_workbench_payload`);
2. определяет активный слой метрик (`window` или `category_window`);
3. сверяет строку гостя в `payload` со строкой активного слоя в БД;
4. возвращает детальный отчёт и итоговый статус `PASS/FAIL`.

Дополнительно есть режим глубокой трассировки (`--status-mode=full`) по слоям:
`raw -> order_fact -> daily_fact -> window/category_window`.

## Где находится
[audit_workbench_phone_metrics.py](/C:/Users/admin_eas/PycharmProjects/Program-Loyal_dev_cai/guests/management/commands/audit_workbench_phone_metrics.py)

## Обязательные аргументы
1. `--phone` (можно несколько раз).

Все остальные параметры опциональны.

## Опциональные аргументы
1. `--as-of-date` — дата среза `YYYY-MM-DD`.
2. `--window-days` — окно в днях (`7/14/30/60/180`).
3. `--department-id` — фильтр по `Department.Id`.
4. `--segment-code` — фильтр по сегменту workbench.
5. `--focus-category-code` — фильтр по фокусной категории.
6. `--cf-field` / `--cf-op` / `--cf-value` — сложные фильтры (длины списков должны совпадать).
7. `--selected-limit` — расширенный лимит `selected_guests` для payload (по умолчанию `5000`).
8. `--max-db-rows` — сколько диагностических строк из DB показывать на гостя.
9. `--status-mode=brief|full` — краткий или полный режим.
10. `--max-full-rows` — лимит sample-строк на слой в `full`.
11. `--output-format=pretty|json` — формат вывода.
12. `--output-file` — путь для сохранения полного JSON-отчёта.
13. `--strict` — завершать команду с ошибкой, если найден хотя бы один `FAIL`.

## Пример (краткий режим)
```bash
sudo docker compose exec app-bonus python manage.py audit_workbench_phone_metrics \
  --phone=79097376140 \
  --phone=79829334252 \
  --as-of-date=2026-04-22 \
  --window-days=180 \
  --department-id=a90230ee-9035-4916-8b93-e69ef29e4f48 \
  --segment-code=lost_60d_plus \
  --focus-category-code=sushi_rolls \
  --cf-field=orders_count --cf-op=gte --cf-value=2 \
  --cf-field=avg_check_net --cf-op=gte --cf-value=2000 \
  --output-format=pretty
```

## Пример (полный режим трассировки)
```bash
sudo docker compose exec app-bonus python manage.py audit_workbench_phone_metrics \
  --phone=79097376140 \
  --phone=79829334252 \
  --as-of-date=2026-04-22 \
  --window-days=180 \
  --department-id=a90230ee-9035-4916-8b93-e69ef29e4f48 \
  --segment-code=lost_60d_plus \
  --focus-category-code=sushi_rolls \
  --status-mode=full \
  --max-full-rows=100 \
  --output-format=json \
  --output-file=./scp_tmp/f21_phone_audit_full_20260422.json
```

## Что смотреть в отчёте
1. `payload.metrics_layer` — какой слой реально использован (`window`/`category_window`).
2. `phones[].status` — итог по каждому номеру.
3. `phones[].checks[]` — покомпонентная сверка `orders/visits/sum/avg/rating`.
4. В `full` режиме:
`phones[].full_trace.raw_scope`,
`phones[].full_trace.order_fact_scope`,
`phones[].full_trace.daily_fact_scope`,
`phones[].full_trace.focus_scope`.

## Выходные коды
1. `0` — команда отработала без исключений, а в режиме `--strict` также без `FAIL`.
2. `!=0` — ошибка в аргументах/выполнении или обнаружен `FAIL` при `--strict`.
