# Развёртывание отслеживаемых ссылок интерактивных сообщений

- Дата: 2026-08-26.
- Область: SAGUR, отдельная служба `redirect-bonus`, Nginx и минимальная роль PostgreSQL.
- Режим: сначала развёртывание с выключенным формированием ссылок, затем ручная приёмка и отдельное разрешение боевого пилота.
- Проект Loyalty на сервере: `/var/www/sagur_project/loyalty_service`.
- Производственный Compose на сервере: `/var/www/sagur_project`.

## 1. Фиксация проверенных версий

Ранее перечисленные здесь промежуточные коммиты не являются основанием для развёртывания: после них были утверждены исправления полного аудита, а ошибочные коммиты ручного проекта `webhook_03` сняты из его ветки.

Перед переносом обязательно:

1. завершить исправления Б-1—Б-10 и документальные уточнения В-1—В-3;
2. проверить отсутствие незакоммиченных целевых изменений в `loyalty_service` и записать его фактические ветку и `HEAD` в рабочий журнал;
3. в ручном проекте `webhook_03` отдельно проверить итоговый diff производственных Docker Compose и Nginx;
4. зафиксировать инфраструктурные изменения вручную владельцем проекта и записать фактический итоговый коммит в рабочий журнал;
5. переносить только эти две зафиксированные итоговые версии.

Codex не создаёт и не изменяет историю коммитов `webhook_03`. До фиксации итоговых версий эта инструкция не является разрешением серверного переноса.

## 2. Обязательные реквизиты без раскрытия значений

В основном `/var/www/sagur_project/loyalty_service/.env` задаются:

```dotenv
MESSAGE_TRACKED_LINKS_ENABLED=False
MESSAGE_TRACKED_LINK_PUBLIC_BASE_URL=https://sagur.24vds.ru/r/v1/
MESSAGE_TRACKED_LINK_ALLOWED_HOSTS=rest.market,susami.rest.market,uzbechka.rest.market,gruzinka.rest.market,china.rest.market,gruzinka.restoplace.ws,susami.restoplace.ws,china.restoplace.ws,usbechka.restoplace.ws
```

Отдельно по шаблону `.env.redirect-bonus.sample` создаётся неотслеживаемый файл `/var/www/sagur_project/loyalty_service/.env.redirect-bonus`:

```dotenv
SECRET_KEY=<отдельный случайный секрет Django>
ALLOWED_HOSTS=sagur.24vds.ru
PG_NAME=<имя действующей базы SAGUR>
PG_USER=sagur_tracked_link
PG_PASSWORD=<отдельный случайный пароль PostgreSQL>
PG_HOST=db
PG_PORT=5432
```

Имя и пароль роли переходов не должны совпадать с `DATABASES_USER` и `DATABASES_PASSWORD`. `MESSAGE_TRACKED_LINK_ALLOWED_HOSTS` задаётся только в основном окружении управляющего приложения: публичная служба использует уже сохранённый неизменяемый снимок HTTPS-адреса. Секреты генерируются и переносятся вне чата и журналов команд. `docker compose config` без параметра `--quiet` не используется: его вывод может раскрыть подставленные значения.

Публичный контейнер читает только `.env.redirect-bonus` и не получает полный файл окружения Loyalty с токенами ботов, секретом vtelemax и настройками очередей.

## 3. Проверка исходного состояния

Из `/var/www/sagur_project`:

```bash
git -C loyalty_service status --short --branch
git -C loyalty_service rev-parse --short=12 HEAD
git status --short --branch
git rev-parse --short=12 HEAD
sudo docker compose config --quiet
sudo docker compose ps
```

Наличие пользовательских резервных файлов не является причиной их удаления. Перенос останавливается, если изменённые отслеживаемые файлы пересекаются с целевыми файлами и происхождение изменения не установлено.

Перед миграцией проверяется действующий резервный контур:

```bash
sudo docker compose exec -T barman barman check db
sudo docker compose exec -T barman barman list-backups db | head -n 5
```

## 4. Сборка и миграция при выключенном формировании

```bash
sudo docker compose build migrate-bonus app-bonus mailing-bonus dispatch-bonus worker-bonus task-bonus uq-monitor-bonus sender-telegram-bonus sender-vk-bonus sender-max-bonus redirect-bonus
sudo docker compose run --rm --no-deps migrate-bonus python manage.py check
sudo docker compose run --rm --no-deps migrate-bonus python manage.py showmigrations guests
sudo docker compose run --rm --no-deps migrate-bonus python manage.py migrate guests 0061 --plan
```

План должен содержать только `guests.0061_message_interaction_tracked_links`. После проверки:

```bash
date -Is
SECONDS=0
sudo docker compose run --rm --no-deps migrate-bonus python manage.py migrate guests 0061 --verbosity 2
result=$?
echo "Код завершения: $result"
echo "Продолжительность: ${SECONDS} секунд"
date -Is
```

После успеха повторно выполняются:

```bash
sudo docker compose run --rm --no-deps migrate-bonus python manage.py migrate --plan
sudo docker compose run --rm --no-deps migrate-bonus
```

Ожидаются отсутствие оставшихся операций и успешный идемпотентный запуск штатного контейнера миграций.

## 5. Подготовка минимальной роли PostgreSQL

После миграции администратор вручную создаёт или приводит к требуемому состоянию отдельную роль PostgreSQL. Автоматической службы `provision-tracked-link-db` в производственном Compose нет и добавлять её не требуется. Имя и пароль роли берутся из `PG_USER` и `PG_PASSWORD` файла `.env.redirect-bonus`; секрет не выводится в командную строку, журнал или документ.

Итоговые права роли ограничиваются следующим перечнем:

- чтение `interaction_id`, `public_token`, `target_url`, `disabled_at` таблицы `message_interaction_tracked_links`;
- вставка `tracked_link_id`, `received_at` и чтение `id` таблицы `message_interaction_link_transitions`;
- использование последовательности `message_interaction_link_transitions_id_seq`;
- подключение к базе и использование схемы `public`.

Роль не должна владеть объектами базы, состоять в других ролях, иметь специальные атрибуты PostgreSQL, изменять либо удалять ссылки и переходы или читать остальные таблицы SAGUR.

После ручной выдачи прав и до запуска публичного маршрута выполняется отдельная немутирующая команда через окружение `redirect-bonus` и эту роль:

```bash
sudo docker compose run --rm --no-deps redirect-bonus python manage.py audit_tracked_link_redirect_permissions --settings=loyalty_viewer.settings_redirect --as-json
```

Команда должна завершиться с кодом 0 и итогом `ready`. Она проверяет наличие таблиц и последовательности, подключение к базе, использование схемы, точные столбцовые права, права последовательности, отсутствие специальных атрибутов, членства в других ролях, владения объектами и прав на остальные таблицы. Команда не вставляет тестовые строки. Любая блокировка запрещает запуск публичного маршрута.

## 6. Перенос конфигурации Nginx

Compose монтирует `/var/www/sagur_project/nginx/nginx.conf`, тогда как контролируемый образец хранится в `nginx.prod.conf`. Полная слепая замена запрещена: рабочий файл уже содержит входящую точку vtelemax и может содержать другие подтверждённые серверные изменения.

Перед изменением:

```bash
sudo cp -a --update=none nginx/nginx.conf nginx/nginx.conf.bak.before_tracked_links_20260826_01
sudo diff -u nginx/nginx.conf nginx.prod.conf || true
```

Из `nginx.prod.conf` переносятся только три части, помеченные комментариями об отслеживаемых ссылках:

1. зоны ограничения частоты, вычисление десятисимвольного маркера и безопасный формат журнала внутри `http`;
2. точный маршрут `/r/v1/<32-символьный токен>`, проверку исходного `$request_uri` и внутренний обработчик 405;
3. расположенный после рабочего выражения запасной маршрут `^/r(?:/|$)` и внутренняя единая заглушка 410.

После слияния проверяется разница с резервной копией. В ней должны быть только эти части, а существующая точка `/internal/integration/v1/vtelemax/message-interactions/events` должна сохраниться.

```bash
sudo diff -u nginx/nginx.conf.bak.before_tracked_links_20260826_01 nginx/nginx.conf
sudo docker compose exec -T nginx nginx -t
```

Перезагрузка запрещена при любой ошибке `nginx -t`.

## 7. Запуск контейнеров и первичная проверка

Сначала запускается публичная служба, затем проверяется её внутреннее состояние:

```bash
sudo docker compose up -d --no-deps redirect-bonus
sudo docker compose ps redirect-bonus
sudo docker compose exec -T nginx wget -qO- --header='Host: sagur.24vds.ru' --header='X-Forwarded-Proto: https' http://redirect-bonus:8002/internal/health
```

Ожидается `ok`. Затем Nginx безопасно перечитывает проверенную конфигурацию:

```bash
sudo docker compose exec -T nginx nginx -s reload
sudo docker compose ps nginx redirect-bonus
```

Без действующей ссылки проверяются только отрицательные и не изменяющие базу варианты:

```bash
curl --silent --show-error --output /dev/null --write-out 'неверная_форма HTTP=%{http_code}\n' https://sagur.24vds.ru/r/v1/short
curl --silent --show-error --output /dev/null --write-out 'корень_маршрута HTTP=%{http_code}\n' https://sagur.24vds.ru/r
curl --silent --show-error --output /dev/null --write-out 'неизвестный_токен HTTP=%{http_code}\n' https://sagur.24vds.ru/r/v1/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
curl --path-as-is --silent --show-error --output /dev/null --write-out 'параметры HTTP=%{http_code}\n' 'https://sagur.24vds.ru/r/v1/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA?probe=1'
curl --path-as-is --silent --show-error --output /dev/null --write-out 'двойная_косая HTTP=%{http_code}\n' https://sagur.24vds.ru/r//v1/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
curl --path-as-is --silent --show-error --output /dev/null --write-out 'сегмент_пути HTTP=%{http_code}\n' https://sagur.24vds.ru/r/v1/../v1/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
curl --path-as-is --silent --show-error --output /dev/null --write-out 'кодирование HTTP=%{http_code}\n' https://sagur.24vds.ru/r/v1/%41AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
curl --head --silent --show-error --output /dev/null --write-out 'HEAD HTTP=%{http_code}\n' https://sagur.24vds.ru/r/v1/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
curl --request POST --silent --show-error --output /dev/null --write-out 'POST HTTP=%{http_code}\n' https://sagur.24vds.ru/r/v1/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
```

Все варианты от `неверная_форма` до `кодирование` должны получить 410; HEAD — 204, POST — 405. Обычный ответ POST должен содержать `Allow: GET, HEAD` при отдельном просмотре заголовков. Запрещённый метод с телом сверх инфраструктурного предела может получить 413; это также подтверждает блокировку до приложения и не требует преобразования в 405. Ответы 410 должны содержать одинаковый нейтральный текст `Ссылка недоступна.`. При превышении любого из двух ограничителей Nginx должен возвращать 429, а не стандартный 503.

В журналах запрещены полный путь и 32-символьный токен:

```bash
sudo docker compose logs --since=10m --no-color nginx | grep 'scope=tracked_link' | tail -n 20
sudo docker compose logs --since=10m --no-color redirect-bonus
```

Безопасные записи Nginx выводятся в stdout и ротируются действующей настройкой журналов Docker. Они содержат десятисимвольный `link_marker`, время, адрес источника, метод, статус, длительность и состояния ограничителей, но не содержат URI, аргументы запроса или полный токен. Для канонического 32-символьного токена и того же токена с параметрами ожидается одинаковый десятисимвольный маркер; в журнале не должно быть оставшихся 22 символов. Локальный `error_log` для публичных ссылочных маршрутов отключён, потому что стандартный формат Nginx способен записать полный запрос; ошибки приложения проверяются в журнале `redirect-bonus`.

## 8. Перезапуск прикладных служб и аудит

С выключенным `MESSAGE_TRACKED_LINKS_ENABLED=False` пересоздаются службы, содержащие новый код:

```bash
sudo docker compose up -d --no-deps --force-recreate app-bonus mailing-bonus dispatch-bonus worker-bonus task-bonus uq-monitor-bonus sender-telegram-bonus sender-vk-bonus sender-max-bonus
sudo docker compose ps app-bonus mailing-bonus dispatch-bonus worker-bonus task-bonus uq-monitor-bonus sender-telegram-bonus sender-vk-bonus sender-max-bonus redirect-bonus nginx
sudo docker compose exec -T app-bonus python manage.py check
sudo docker compose exec -T app-bonus python manage.py smoke_post_deploy
sudo docker compose exec -T app-bonus python manage.py audit_message_interactions_readiness --fail-on-blocked --as-json
```

На этом этапе предупреждение о выключенном формировании ссылок ожидаемо. Ошибки схемы, справочника, доменов или публичного префикса недопустимы.

## 9. Нагрузочная приёмка и планы запросов

До массового включения проверяются планы PostgreSQL для:

- поиска снимка по `public_token`;
- вставки перехода;
- отчёта одной рассылки;
- отчёта автосценария за период.

Полный просмотр таблиц `dispatch_tasks` и `message_interaction_link_transitions` для ограниченного отчёта считается блокирующим результатом.

Нагрузочная проверка В-4 выполняется на сервере с выключенным `MESSAGE_TRACKED_LINKS_ENABLED=False`, только на заранее созданных тестовых ссылках и после согласования окна: 200 допустимых запросов в секунду в течение пяти минут и пик 500 запросов в секунду в течение десяти секунд. Критерии: отсутствие потерянных принятых переходов и ошибок базы, 95-й процентиль не более 200 мс в штатном профиле, ожидаемые ответы 429 сверх пределов и отсутствие ухудшения `app-bonus` и отправителей. До её успеха включать формирование ссылок в рабочей среде нельзя.

## 10. Управляемое включение и пилот

Только после успешных предыдущих этапов владелец вручную меняет:

```dotenv
MESSAGE_TRACKED_LINKS_ENABLED=True
```

Затем пересоздаются `app-bonus`, производители задач и три отправителя. До реальной отправки выполняется сухой пилот с конкретным существующим гостем, ботом и активным кодом назначения:

```bash
sudo docker compose exec -T app-bonus python manage.py pilot_message_interaction --guest-id <ID_ГОСТЯ> --bot-code <КОД_БОТА> --button-set rating_menu_link --tracked-link-destination-code delivery_main --message-text 'ТЕСТ SAGUR — отслеживаемая ссылка.' --as-json
```

Команда без `--confirm` не создаёт задачу. Реальная отправка выполняется только после проверки результата и отдельного разрешения, с уникальным `--run-id` и `--confirm`.

Для Telegram, VK и MAX по отдельности проверяются:

1. три ряда `l+d / ссылка / m`;
2. один переход по ссылке и появление одной строки перехода в SAGUR;
3. отсутствие события ссылки в vtelemax;
4. удаление только `l/d` после оценки;
5. сохранение ссылки и многократная работа `m`;
6. показатели отчёта без двойного учёта одного сообщения;
7. отсутствие полного токена и конечного адреса в журналах.

## 11. Откат

Безопасный первый откат не удаляет миграцию и историю:

1. установить `MESSAGE_TRACKED_LINKS_ENABLED=False`;
2. пересоздать производители и отправители, чтобы новые ссылки больше не формировались;
3. при отказе публичного контура убрать только маршруты `/r/` из рабочего Nginx и перезагрузить его после `nginx -t`;
4. сохранить таблицы ссылок и переходов для расследования и уже отправленных сообщений;
5. не удалять роль, строки справочника и миграцию `0061` без отдельного плана восстановления старых ссылок.

Отключение записи справочника назначения (`is_active=False`) прекращает создание новых задач и снимков ссылок с этим назначением, включая уже настроенные кампании. Оно не отменяет созданные задачи и не ломает отправленные ссылки. Для аварийного отключения конкретной отправленной ссылки используется её отдельное поле `disabled_at`.

Откат кода без отката схемы допустим только после проверки совместимости выбранного коммита. Обратная миграция и физическое удаление таблиц не являются штатным аварийным действием.
