"""
Data-миграция: каркас плановых сценариев для неактивных гостей.

Что создаём:
1. Шаблон и системный сценарий `inactive_7d`;
2. Шаблон и системный сценарий `inactive_30d_coupon`.

Важно:
1. Оба сценария создаются в неактивном состоянии (`is_active=False`);
2. Это безопасная инициализация структуры, без немедленного запуска отправок.
"""

from datetime import time
from django.db import migrations

SCENARIO_CODE_INACTIVE_7D = "inactive_7d"
SCENARIO_CODE_INACTIVE_30D_COUPON = "inactive_30d_coupon"

TEMPLATE_NAME_INACTIVE_7D = "SYSTEM_INACTIVE_7D_TEMPLATE"
TEMPLATE_NAME_INACTIVE_30D_COUPON = "SYSTEM_INACTIVE_30D_COUPON_TEMPLATE"

TEMPLATE_TEXT_INACTIVE_7D = (
    "Мы соскучились по вам. Вы не были у нас {days_without_visits} дней. "
    "Будем рады видеть вас снова."
)
TEMPLATE_TEXT_INACTIVE_30D_COUPON = (
    "Мы давно не виделись ({days_without_visits} дней). "
    "Для вас персональный купон: {coupon_code}"
)


def seed_inactive_scenarios(apps, schema_editor):
    MessageTemplate = apps.get_model("guests", "MessageTemplate")
    NotificationScenario = apps.get_model("guests", "NotificationScenario")

    template_7d, _ = MessageTemplate.objects.get_or_create(
        name=TEMPLATE_NAME_INACTIVE_7D,
        defaults={
            "description": "Системный шаблон авто-уведомления для неактивных гостей (7 дней).",
            "message_text": TEMPLATE_TEXT_INACTIVE_7D,
            "created_by": "system",
            "is_active": True,
        },
    )
    template_30d_coupon, _ = MessageTemplate.objects.get_or_create(
        name=TEMPLATE_NAME_INACTIVE_30D_COUPON,
        defaults={
            "description": "Системный шаблон авто-уведомления для неактивных гостей с купоном (30 дней).",
            "message_text": TEMPLATE_TEXT_INACTIVE_30D_COUPON,
            "created_by": "system",
            "is_active": True,
        },
    )

    NotificationScenario.objects.get_or_create(
        code=SCENARIO_CODE_INACTIVE_7D,
        defaults={
            "name": "Системный сценарий: гость не был 7 дней",
            "description": "Плановый сценарий напоминания для гостей без визита в течение 7 дней.",
            "is_active": False,
            "is_system": True,
            "trigger_type": "schedule",
            "priority": "normal",
            "target_mode": "primary_only",
            "distribution_mode": "uniform",
            "send_window_begin": time(8, 0),
            "send_window_end": time(17, 0),
            "timezone": "Asia/Yekaterinburg",
            "cooldown_minutes": 60,
            "max_per_day_per_guest": 1,
            "settings": {
                "inactive_days": 7,
                "coupon_required": False,
            },
            "template_id": template_7d.id,
        },
    )

    NotificationScenario.objects.get_or_create(
        code=SCENARIO_CODE_INACTIVE_30D_COUPON,
        defaults={
            "name": "Системный сценарий: гость не был 30 дней + купон",
            "description": (
                "Плановый сценарий возврата гостей без визита в течение 30 дней. "
                "Требует подключённой интеграции генерации купонов."
            ),
            "is_active": False,
            "is_system": True,
            "trigger_type": "schedule",
            "priority": "normal",
            "target_mode": "primary_only",
            "distribution_mode": "uniform",
            "send_window_begin": time(8, 0),
            "send_window_end": time(17, 0),
            "timezone": "Asia/Yekaterinburg",
            "cooldown_minutes": 60,
            "max_per_day_per_guest": 1,
            "settings": {
                "inactive_days": 30,
                "coupon_required": True,
                "coupon_payload": {},
            },
            "template_id": template_30d_coupon.id,
        },
    )


def unseed_inactive_scenarios(apps, schema_editor):
    """
    Обратная миграция оставлена пустой, чтобы не удалять эксплуатационные настройки.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0017_seed_balance_notification_scenario"),
    ]

    operations = [
        migrations.RunPython(seed_inactive_scenarios, unseed_inactive_scenarios),
    ]
