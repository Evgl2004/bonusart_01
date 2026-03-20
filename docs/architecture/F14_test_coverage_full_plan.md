# F14. Полный план увеличения покрытия тестами по всему проекту

## Статус документа
Черновик, подготовленный для согласования.
Дата фиксации baseline: 18 марта 2026.

## Цель
Системно увеличить тестовое покрытие по всему проекту (не только критические участки),
сохранить устойчивость текущего функционала и снизить риск регрессий перед дальнейшими
доработками основного бизнес-функционала.

## Baseline (снят 18.03.2026)
Команда запуска:

```powershell
pytest --create-db --cov=guests --cov=loyalty_viewer --cov-report=term
```

Результат:
1. Всего тестов: `220 passed`.
2. Общее покрытие statements: `72%`.
3. Общее покрытие branches: `62%`.

Агрегация по подсистемам:
1. `guests/views*`: `0.00%` (`478` строк).
2. `loyalty_viewer`: `66.67%` (`153` строк).
3. `guests/misc`: `74.71%` (`680` строк).
4. `services/other`: `82.70%` (`1214` строк).
5. `management/commands`: `86.74%` (`656` строк).
6. `services/universal_queue`: `91.46%` (`913` строк).

## Принципы выполнения
1. Движемся этапами, каждый этап завершается зелёным прогоном тестов.
2. На каждом этапе добавляем как позитивные, так и bug-seeking сценарии.
3. Докстринги и комментарии в новых тестах и вспомогательном коде пишем на русском языке.
4. Фиксируем работу в логических коммитах без крупных «свалок» изменений.
5. Сначала закрываем «нулевые» и низкие зоны, затем добиваем branch coverage.

## Крупные зоны с низким покрытием
### 0% покрытия
1. `guests/views.py`
2. `guests/views_mailings_import.py`
3. `guests/forms.py`
4. `guests/views_mailings_logs.py`
5. `guests/tasks.py`
6. `guests/urls.py`
7. `loyalty_viewer/asgi.py`
8. `loyalty_viewer/wsgi.py`
9. `loyalty_viewer/urls.py`

### Ниже 80%
1. `guests/services/universal_queue/webhook_producer.py` (`57%`)
2. `guests/management/commands/mailing_worker.py` (`63%`)
3. `guests/admin.py` (`65%`)
4. `guests/services/webhook_worker.py` (`69%`)
5. `loyalty_viewer/settings.py` (`72%`)
6. `guests/services/notification_events.py` (`73%`)
7. `guests/services/universal_queue/provider_clients.py` (`78%`)
8. `guests/management/commands/run_webhook_worker.py` (`79%`)

## Пошаговый план (T0-T10)
### T0. Фиксация baseline и целевых KPI
1. Зафиксировать baseline-метрики в документации.
2. Подтвердить целевые пороги:
   1. statements >= `85%`;
   2. branches >= `75%`;
   3. отсутствие файлов с покрытием `0%`.
3. Подготовить единый шаблон отчёта прогресса по этапам.

### T1. Инфраструктурные и «тонкие» 0%-файлы
1. Покрыть `guests/urls.py`, `loyalty_viewer/urls.py`, `loyalty_viewer/asgi.py`, `loyalty_viewer/wsgi.py`.
2. Добавить smoke-тесты импорта и базовой маршрутизации.
3. Покрыть `guests/tasks.py` через unit-тесты задач и мок-сервисы.

### T2. Формы и валидация
1. Полностью покрыть `guests/forms.py`.
2. Добавить тесты валидации: плохие входные данные, пустые поля, некорректные типы.
3. Добавить негативные кейсы на граничные значения.

### T3. Импорт рассылок через web-интерфейс
1. Покрыть `guests/views_mailings_import.py`.
2. Тесты:
   1. happy-path загрузки;
   2. пустой/битый файл;
   3. невалидные номера;
   4. dry-run и ошибки сохранения.

### T4. Логи рассылок через web-интерфейс
1. Покрыть `guests/views_mailings_logs.py`.
2. Тесты:
   1. список с фильтрами;
   2. скачивание логов;
   3. пустые выборки;
   4. обработка исключений.

### T5. Основные представления приложения
1. Основной фокус: `guests/views.py`.
2. Построить сценарии через `Django TestClient`:
   1. доступы и права;
   2. фильтрация и пагинация;
   3. корректные и некорректные входы;
   4. ветки ошибок и редкие пограничные случаи.

### T6. Админ-панель
1. Покрыть `guests/admin.py`:
   1. actions;
   2. readonly-ограничения;
   3. системные сценарии;
   4. queryset-аннотации и вычисляемые поля.
2. Добавить bug-seeking тесты на защиту от некорректных действий в админке.

### T7. Воркеры webhook и устойчивость цикла
1. Расширить покрытие `guests/services/webhook_worker.py`.
2. Закрыть ветки:
   1. reconnect-loop;
   2. DLQ-ветки;
   3. signal-stop в разных фазах цикла;
   4. ошибки обновления внешних статусов.

### T8. Notification и provider edge-cases
1. Расширить `guests/services/notification_events.py`.
2. Расширить `guests/services/universal_queue/provider_clients.py`.
3. Добавить bug-seeking сценарии:
   1. плохие payload;
   2. нестабильные ответы API;
   3. retry/fallback/timeout-ветки.

### T9. Команды management и операционные ветки
1. Добить ветвления в:
   1. `guests/management/commands/mailing_worker.py`
   2. `guests/management/commands/run_webhook_worker.py`
   3. `guests/management/commands/dispatch_universal_tasks.py`
   4. `guests/management/commands/run_universal_queue_monitor.py`
2. Добавить тесты корректного graceful shutdown и cleanup ресурсов.

### T10. Финализация и контроль в CI
1. Полный прогон `pytest --create-db --cov`.
2. Сформировать итоговый отчёт «baseline -> факт».
3. Ввести контроль порогов покрытия в CI.
4. Зафиксировать правило: новый код без тестов не принимается.

## Целевые контрольные точки
1. После T3: statements >= `76%`.
2. После T5: statements >= `80%`.
3. После T8: statements >= `84%`, branches >= `70%`.
4. После T10: statements >= `85%`, branches >= `75%`.

## Definition of Done для программы F14
1. Нет файлов с покрытием `0%` среди прикладного кода проекта.
2. Общее покрытие statements достигает согласованного порога.
3. Общее покрытие branches достигает согласованного порога.
4. На критичных ветках есть bug-seeking тесты (ошибки, таймауты, ретраи, некорректные входы).
5. Документация и отчёт по покрытию актуализированы.
