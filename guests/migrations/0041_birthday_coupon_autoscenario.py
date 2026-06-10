from datetime import time

from django.db import migrations, models


SCENARIO_CODE_BIRTHDAY_COUPON = "birthday_coupon"
TEMPLATE_NAME_BIRTHDAY_COUPON = "SYSTEM_BIRTHDAY_COUPON_TEMPLATE"
DEFAULT_BIRTHDAY_PREPARATION_WINDOW_DAYS = 7

TEMPLATE_TEXT_BIRTHDAY_COUPON = (
    "{{ first_name }}, скоро ваш день рождения. "
    "Дарим персональный купон {coupon_code}. "
    "До праздника осталось {{ days_until_birthday }} дн."
)


def seed_birthday_coupon_autoscenario(apps, schema_editor):
    MessageTemplate = apps.get_model("guests", "MessageTemplate")
    NotificationScenario = apps.get_model("guests", "NotificationScenario")
    CouponAutomationConfig = apps.get_model("guests", "CouponAutomationConfig")

    template, _ = MessageTemplate.objects.get_or_create(
        name=TEMPLATE_NAME_BIRTHDAY_COUPON,
        defaults={
            "description": "Системный шаблон купонного автосценария ко дню рождения.",
            "message_text": TEMPLATE_TEXT_BIRTHDAY_COUPON,
            "created_by": "system",
            "is_active": True,
        },
    )

    scenario, _ = NotificationScenario.objects.get_or_create(
        code=SCENARIO_CODE_BIRTHDAY_COUPON,
        defaults={
            "name": "Системный сценарий: день рождения + купон",
            "description": (
                "Плановый купонный автосценарий для гостей, чей день рождения "
                "попадает в окно подготовки."
            ),
            "is_active": False,
            "is_system": True,
            "trigger_type": "schedule",
            "priority": "normal",
            "target_mode": "primary_only",
            "distribution_mode": "uniform",
            "send_window_begin": time(9, 0),
            "send_window_end": time(21, 0),
            "timezone": "Asia/Yekaterinburg",
            "cooldown_minutes": 60,
            "max_per_day_per_guest": 1,
            "settings": {
                "coupon_required": True,
                "birthday_preparation_window_days": DEFAULT_BIRTHDAY_PREPARATION_WINDOW_DAYS,
                "coupon_payload": {},
            },
            "template_id": template.id,
        },
    )

    CouponAutomationConfig.objects.get_or_create(
        scenario_id=scenario.id,
        defaults={
            "execution_mode": "report_only",
            "coupon_series": "",
            "venue_code": "",
            "venue_name": "",
            "coupon_validity_days": 14,
            "coupon_promo_text_template": "",
            "max_recipients_per_run": 100,
            "max_active_coupons_per_guest": 1,
            "cooldown_days": 365,
            "settings": {
                "birthday_preparation_window_days": DEFAULT_BIRTHDAY_PREPARATION_WINDOW_DAYS,
            },
        },
    )


def noop_reverse(apps, schema_editor):
    """
    Обратная миграция не удаляет эксплуатационные настройки.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0040_coupon_automation_rules"),
    ]

    operations = [
        migrations.AddField(
            model_name="couponautoscenariorun",
            name="blocked_existing_trigger",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="couponautoscenarioassignment",
            name="trigger_key",
            field=models.CharField(blank=True, db_index=True, max_length=120, null=True),
        ),
        migrations.AddField(
            model_name="couponautoscenarioassignment",
            name="trigger_date",
            field=models.DateField(blank=True, db_index=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="couponautoscenarioassignment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("trigger_key__isnull", False)) & ~models.Q(("status", "canceled")),
                fields=("scenario", "guest", "trigger_key"),
                name="cautoass_scen_guest_trigger_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="couponautoscenarioassignment",
            index=models.Index(fields=["scenario", "trigger_key"], name="cautoass_scen_trigger_idx"),
        ),
        migrations.RunPython(seed_birthday_coupon_autoscenario, noop_reverse),
    ]
