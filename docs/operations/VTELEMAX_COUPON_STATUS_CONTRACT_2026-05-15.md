# Контракт статусов купонов SAGUR ↔ vtelemax

Дата фиксации: 2026-05-15  
Источник: согласование с командой vtelemax.

## Подтверждённые правила на стороне vtelemax
1. `status_update: canceled` трактуется как `release`:
   1. купон исчезает из активного меню гостя;
   2. связка `купон ↔ гость` снимается;
   3. тот же `coupon_series + coupon_code` может быть назначен повторно будущим `assignments` событием.
2. Повторный `canceled` (по тому же `event_id` или по уже освобождённому купону) обрабатывается идемпотентно.
3. `status_update: used` и `status_update: expired`:
   1. убирают купон из активного списка;
   2. не освобождают купон для повторного назначения.
4. Текущий payload считается достаточным:
   1. `event_id`;
   2. `direction=status_update`;
   3. `campaign_id`;
   4. `assignment_id`;
   5. `person_id` / `phone_e164`;
   6. `coupon_series`;
   7. `coupon_code`;
   8. `status`;
   9. `status_at`;
   10. `meta`.
5. При отмене кампании SAGUR отправляет release только для `reserved` купонов.
   1. `sent/used/expired/canceled` автоматически в пул не возвращаются.

## Критерий приёмки
1. Кампания отменена.
2. Купон исчез у исходного гостя.
3. Тот же купон повторно назначен другому гостю.
4. Купон появился у нового гостя.

## Привязка к реализации SAGUR
1. `cancel_campaign` переводит только `reserved -> canceled`, выставляет `meta.release_to_pool=true`, но не освобождает купон мгновенно.
2. Освобождение купона в пул (`verified_loaded`, `is_active=true`) выполняется только после подтверждения `status_update(canceled)`.
3. Для `used/expired` выставляется `meta.release_to_pool=false`, купон повторно не используется.
