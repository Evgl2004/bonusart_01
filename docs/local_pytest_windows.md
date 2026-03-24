# Локальный запуск pytest в проблемном окружении Windows

## Почему обычный `python` падает
В этом окружении бинарник `.venv\\Scripts\\python.exe` не запускается,
потому что системный Python из `C:\\Users\\admin_eas\\AppData\\Local\\Programs\\Python\\Python313\\python.exe`
возвращает `Access Denied`.

Из-за этого обычные команды:
- `python -m pytest ...`
- `.venv\\Scripts\\pytest.exe ...`

неработоспособны.

## Рабочий способ (фиксированный)
Используем стабильный интерпретатор pgAdmin Python и подмешиваем зависимости
из `.venv\\Lib\\site-packages`.

Команда:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_pytest.ps1 guests/tests/test_focus_categories_workbench_view.py guests/tests/test_navigation_menu.py
```

Если аргументы не переданы, скрипт запускает smoke-тест файла:
`guests/tests/test_focus_categories_workbench_view.py`.

## Важно
Скрипт отключает `pytest` cacheprovider (`-p no:cacheprovider`),
чтобы не падать на правах записи в `.pytest_cache`.
