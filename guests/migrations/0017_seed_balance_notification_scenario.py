"""
Data-миграция: базовая инициализация системного сценария balance_changed.

Назначение:
1. Создать технический шаблон для системного уведомления об изменении баланса;
2. Создать системный NotificationScenario (code=balance_changed),
   чтобы новый контур NotificationEvent -> DispatchTask был готов сразу после migrate.
"""

from django.db import migrations


BALANCE_SCENARIO_CODE = "balance_changed"
BALANCE_TEMPLATE_NAME = "SYSTEM_BALANCE_CHANGED_TEMPLATE"
BALANCE_TEMPLATE_TEXT = "{message_text}"


def seed_balance_scenario(apps, schema_editor):
    MessageTemplate = apps.get_model("guests", "MessageTemplate")
    NotificationScenario = apps.get_model("guests", "NotificationScenario")

    template, _ = MessageTemplate.objects.get_or_create(
        name=BALANCE_TEMPLATE_NAME,
        defaults={
            "description": "Системный шаблон уведомления об изменении баланса.",
            "message_text": BALANCE_TEMPLATE_TEXT,
            "created_by": "system",
            "is_active": True,
        },
    )

    NotificationScenario.objects.get_or_create(
        code=BALANCE_SCENARIO_CODE,
        defaults={
            "name": "Системный сценарий: изменение баланса",
            "description": "Транзакционное уведомление из webhook-события изменения баланса.",
            "is_active": True,
            "is_system": True,
            "trigger_type": "webhook",
            "priority": "high",
            "target_mode": "primary_only",
            "distribution_mode": "immediate",
            "timezone": "Asia/Yekaterinburg",
            "template_id": template.id,
        },
    )


def unseed_balance_scenario(apps, schema_editor):
    """
    Обратная миграция не удаляет данные, чтобы не потерять эксплуатационные настройки.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0016_notificationevent_dispatchtask_notification_event_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_balance_scenario, unseed_balance_scenario),
    ]
