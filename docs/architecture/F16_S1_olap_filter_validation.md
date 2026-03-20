# F16 / S1. Валидация фильтров OLAP по идентификаторам

## Дата проверки
18 марта 2026.

## Цель
Подтвердить рабочие фильтры для отбора данных по заведению в OLAP и исключить нестабильные варианты.

## Методика
1. Выполнена авторизация в iiko API через:
   1. `POST /resto/api/auth`;
   2. формат `x-www-form-urlencoded`.
2. В качестве контрольного чека использован `OrderNum = 698698`, дата `2026-03-18`.
3. Из базового ответа получены эталонные значения:
   1. `Department = Узбечка`;
   2. `Department.Id = b6e067a9-6f0f-4cf7-a349-0c235bf2232a`;
   3. `Department.Code = 9`;
   4. `RestaurantSection.Id = 336e1780-2a64-4f99-bafd-3b5e3cbc1a25`;
   5. `RestorauntGroup = Узбечка`;
   6. `RestorauntGroup.Id = 3c9e75bc-24f3-41ad-8fc0-0af8fa8e09d6`.
4. Далее выполнены серии тестов:
   1. строгий тест (дата + `OrderNum` + проверяемый фильтр);
   2. контрольный тест (только дата + проверяемый фильтр).

## Результаты (строгий тест: дата + OrderNum)
1. `Department` (по названию) -> `0` строк.
2. `Department.Id` -> `1` строка.
3. `Department.Code` -> `1` строка.
4. `RestaurantSection.Id` -> `1` строка.
5. `RestorauntGroup` (по названию) -> `0` строк.
6. `RestorauntGroup.Id` -> `1` строка.

## Результаты (контрольный тест: только дата)
1. `Department.Id` -> `190` строк.
2. `Department.Code` -> `190` строк.
3. `RestaurantSection.Id` -> `180` строк.
4. `RestorauntGroup.Id` -> `190` строк.

## Выводы
1. Фильтрация по текстовым названиям (`Department`, `RestorauntGroup`) ненадёжна для практического использования.
2. Рабочими и стабильными показали себя идентификаторные поля:
   1. `Department.Id` — рекомендуемый основной фильтр;
   2. `Department.Code` — рабочий резерв;
   3. `RestorauntGroup.Id` — рабочий альтернативный идентификатор;
   4. `RestaurantSection.Id` — рабочий, но по данным контрольного дня имеет иную гранулярность выборки.
3. Для проекта фиксируется приоритет:
   1. основной: `Department.Id`;
   2. резерв 1: `Department.Code`;
   3. резерв 2: `RestorauntGroup.Id`;
   4. специализированный резерв: `RestaurantSection.Id`.

## Практическая рекомендация для реализации
1. В таблицах аналитики хранить одновременно:
   1. `department_id`;
   2. `department_code`;
   3. `restoraunt_group_id`;
   4. `restaurant_section_id` (если приходит).
2. При формировании запросов в OLAP использовать `Department.Id` как первичный фильтр.
3. Логи синхронизации должны фиксировать, каким полем выполнялся фактический отбор.

## Верифицированное сопоставление из webhook-файла `iiko_card_balans_53110.txt`
Источник webhook:
1. `orderNumber = 698698`
2. `orderId = 51ed1890-ab9f-4626-b889-4df9a06e179e`
3. `organizationId = b5529c0c-420c-11e8-80e0-d8d38565926f`
4. `terminalGroupId = 3c9e75bc-24f3-41ad-8fc0-0af8fa8e09d6`
5. `changedOn = 2026-03-18T11:56:03.3940651+05:00` (дата для OLAP: `2026-03-18`)

Результат сопоставления с OLAP:
1. `department_name = Узбечка`
2. `department_id = b6e067a9-6f0f-4cf7-a349-0c235bf2232a`
3. `department_code = 9`
4. `restaurant_section_id = 336e1780-2a64-4f99-bafd-3b5e3cbc1a25`
5. `restoraunt_group_id = 3c9e75bc-24f3-41ad-8fc0-0af8fa8e09d6`
6. `uniq_order_id = 51ed1890-ab9f-4626-b889-4df9a06e179e`

Проверки целостности:
1. `terminalGroupId == RestorauntGroup.Id` -> `True`
2. `webhook.orderId == OLAP.UniqOrderId.Id` -> `True`

## Рекомендация для будущей таблицы сопоставления заведений
Минимальные поля:
1. `organization_id`
2. `terminal_group_id`
3. `restoraunt_group_id`
4. `department_id`
5. `department_code`
6. `department_name`
7. `restaurant_section_id`
8. `is_active`
9. `verified_at`
10. `source_order_num`
11. `source_business_date`

Ограничения:
1. уникальность на `(organization_id, terminal_group_id)`
2. индекс на `department_id`
3. индекс на `restoraunt_group_id`
