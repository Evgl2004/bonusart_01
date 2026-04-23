# Django Q: синхронизация расписания из settings в БД

## Проблема
В проекте расписание описывается в `settings.Q_CLUSTER["schedule"]`, но `django-q2` запускает задачи из таблицы `django_q_schedule`.
Если строки не созданы в БД, плановые задачи не исполняются.

## Что реализовано
1. Автосинхронизация на старте `qcluster`:
   1. `upsert` по `name`;
   2. fallback-переименование legacy-строки по `func` (если совпадение одно);
   3. удаление stale managed-строк (если включено).
2. Ручная команда:
   1. `python manage.py sync_django_q_schedule`;
   2. `python manage.py sync_django_q_schedule --dry-run`;
   3. `python manage.py sync_django_q_schedule --no-prune-stale`.

## ENV-переменные
1. `DJANGO_Q_SCHEDULE_AUTOSYNC_ON_QCLUSTER_START` (`True`/`False`)  
Включает автосинхронизацию при запуске `manage.py qcluster`.
2. `DJANGO_Q_SCHEDULE_AUTOSYNC_PRUNE_STALE` (`True`/`False`)  
Удаляет stale managed-строки, которых нет в текущем settings map.
3. `DJANGO_Q_SCHEDULE_MANAGED_EXTRA_NAMES` (`CSV`)  
Дополнительные managed-ключи для stale-prune.

## Базовые проверки после деплоя
1. Проверить карту расписания из settings:
```bash
python manage.py shell -c "from django.conf import settings; print(sorted((settings.Q_CLUSTER.get('schedule') or {}).keys()))"
```
2. Проверить строки в БД:
```bash
python manage.py shell -c "from django_q.models import Schedule; print(Schedule.objects.count()); [print(s.name, s.func, s.schedule_type, s.minutes, s.cron) for s in Schedule.objects.order_by('id')]"
```
3. Проверить, что задачи начали исполняться:
```bash
python manage.py shell -c "from django_q.models import Task; from django.utils import timezone; from datetime import timedelta; since=timezone.now()-timedelta(hours=6); print(Task.objects.filter(started__gte=since).count())"
```
