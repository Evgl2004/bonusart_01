FROM python:3.13-slim

WORKDIR /app

# 1. Установка системных зависимостей
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# 2. Создаем не-root пользователя с фиксированным UID
RUN groupadd -r app -g 1000 && \
    useradd -r -u 1000 -g app -s /bin/bash app

# 3. ДО смены пользователя для кэширования?, копирование списка используемых пакетов
# и установка зависимостей Python (отдельный слой для кэширования)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Копирование проекта
COPY --chown=app:app . .

# 5. Создаем папки для логов и статики с правильными правами
RUN mkdir -p /app/logs /app/static /app/media && \
    chown -R app:app /app && \
    chmod 755 /app/logs

# 6. Переключаемся на не-root пользователя
USER app

# 7. Сборка статических файлов на этапе build
RUN python manage.py collectstatic --no-input

# 8. Команда по умолчанию (будет переопределена в docker-compose)
CMD ["gunicorn", "loyalty_viewer.wsgi:application", "--bind", "0.0.0.0:8001"]
