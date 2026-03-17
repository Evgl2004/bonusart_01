# F10. Удаление Legacy Channel-Моделей

## Цель этапа
Окончательно убрать из кода и схемы БД старую модель каналов рассылки,
которая использовала таблицы `mailing_channels`, `mailing_channel_links`,
`guest_channel_links`.

## Что изменено
1. В модели `Mailing` удалено поле `channels`.
2. Удалены Django-модели:
   - `MailingChannel`
   - `MailingChannelLink`
   - `GuestChannelLink`
3. Сгенерирована миграция удаления legacy-структур:
   - `guests/migrations/0014_remove_mailingchannellink_channel_and_more.py`
4. Обновлены связанные участки кода:
   - `admin.py` (убрана регистрация `MailingChannel`);
   - `views_mailings_logs.py` (убраны неиспользуемые импорты);
   - `import_bot_user_phones` переведён на `GuestBotBinding` и `BotProfile`;
   - `init_schema` переведён в deprecated-режим без raw SQL.

## Новый стандарт
1. Маршрутизация массовых рассылок выполняется через:
   - `Mailing.bot_profiles`
   - `GuestBotBinding`
2. Отправка уведомлений и рассылок идёт только через универсальную очередь `DispatchTask`.
3. Любые изменения схемы БД выполняются только через Django migrations.
