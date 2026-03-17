
Program-Loyal

Как запустить проект локально

1. Клонировать репозиторий

Скопируйте проект с GitHub и перейдите в папку проекта:

git clone [https://github.com/Janneryli/Program-Loyal.git](https://github.com/Janneryli/Program-Loyal.git)
cd Program-Loyal

---

2. Создать и активировать виртуальное окружение

Создайте виртуальное окружение:

python -m venv .venv

Активируйте его.

Для Windows:
.venv\Scripts\activate

Для macOS / Linux:
source .venv/bin/activate

---

3. Установить зависимости

Установите все зависимости проекта:

pip install -r requirements.txt

---

4. Создать файл .env

В корне проекта необходимо создать файл .env.
Этот файл не хранится в репозитории и содержит настройки окружения и секреты.

Пример содержимого .env:

DEBUG=True
SECRET_KEY=your-secret-key

PG_NAME=programm_loyalty
PG_USER=postgres
PG_PASSWORD=your_password
PG_HOST=localhost
PG_PORT=5432

IIKO_API_KEY=your_iiko_api_key
IIKO_API_BASE_URL=
IIKO_ORGANIZATION_ID=your_organization_id

SAGUR_BASE_URL=
SAGUR_USERNAME=business_service
SAGUR_PASSWORD=your_password

---

5. Применить миграции базы данных

Перед первым запуском нужно применить миграции:

python manage.py migrate

При необходимости можно создать администратора:

python manage.py createsuperuser

---

Запуск проекта

Проект запускается двумя процессами в разных терминалах.

Терминал 1 — запуск Django-сервера:

python manage.py runserver

После этого приложение будет доступно в браузере по адресу:
[http://127.0.0.1:8000/](http://127.0.0.1:8000/)

Терминал 2 — запуск обработчика фоновых задач (Django Q):

python manage.py qcluster

Без запуска qcluster фоновые задачи и обработка вебхуков работать не будут.
---
Запуск фоновой рассылки

python manage.py mailing_worker  

---
Добавление таблиц в БД 

python manage.py migrate

Команда `init_schema` оставлена только как совместимый alias и больше не применяет raw SQL:

python manage.py init_schema
python manage.py init_schema --apply

---
Добавление chat_id  в таблицу гостей 

 1) python manage.py import_bot_user_phones --sqlite guests/management/commands/bot_requests.db --bot-profile-id 1 --dry-run  ( посмотреть как отработает и не применять изменение)
    
 2) python manage.py import_bot_user_phones --sqlite guests/management/commands/bot_requests.db --bot-profile-id 1  (запуск и применением изменений)

