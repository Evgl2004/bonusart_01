from datetime import time

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


SCENARIO_CODE_FILL_BIRTHDAY_REQUEST = "fill_birthday_request"
SCENARIO_CODE_FILL_BIRTHDAY_COUPON = "fill_birthday_coupon"

TEMPLATE_NAME_FILL_BIRTHDAY_REQUEST = "SYSTEM_FILL_BIRTHDAY_REQUEST_TEMPLATE"
TEMPLATE_NAME_FILL_BIRTHDAY_COUPON = "SYSTEM_FILL_BIRTHDAY_COUPON_TEMPLATE"

TEMPLATE_TEXT_FILL_BIRTHDAY_REQUEST = (
    "Укажите дату рождения в боте, чтобы мы могли подготовить персональный подарок."
)
TEMPLATE_TEXT_FILL_BIRTHDAY_COUPON = (
    "Спасибо, что указали дату рождения. Дарим персональный купон {coupon_code}."
)


def seed_fill_birthday_autoscenario(apps, schema_editor):
    MessageTemplate = apps.get_model("guests", "MessageTemplate")
    NotificationScenario = apps.get_model("guests", "NotificationScenario")
    CouponAutomationConfig = apps.get_model("guests", "CouponAutomationConfig")
    BotProfile = apps.get_model("guests", "BotProfile")

    request_template, _ = MessageTemplate.objects.get_or_create(
        name=TEMPLATE_NAME_FILL_BIRTHDAY_REQUEST,
        defaults={
            "description": "Системный шаблон просьбы заполнить дату рождения.",
            "message_text": TEMPLATE_TEXT_FILL_BIRTHDAY_REQUEST,
            "created_by": "system",
            "is_active": True,
        },
    )
    coupon_template, _ = MessageTemplate.objects.get_or_create(
        name=TEMPLATE_NAME_FILL_BIRTHDAY_COUPON,
        defaults={
            "description": "Системный шаблон купона за заполнение даты рождения.",
            "message_text": TEMPLATE_TEXT_FILL_BIRTHDAY_COUPON,
            "created_by": "system",
            "is_active": True,
        },
    )

    request_scenario, _ = NotificationScenario.objects.get_or_create(
        code=SCENARIO_CODE_FILL_BIRTHDAY_REQUEST,
        defaults={
            "name": "Системный сценарий: заполнить дату рождения",
            "description": (
                "Плановое уведомление гостям из новых ботов, у которых ещё не заполнена дата рождения."
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
                "request_repeat_days": 30,
            },
            "template_id": request_template.id,
        },
    )

    coupon_scenario, _ = NotificationScenario.objects.get_or_create(
        code=SCENARIO_CODE_FILL_BIRTHDAY_COUPON,
        defaults={
            "name": "Системный сценарий: дата рождения заполнена + купон",
            "description": (
                "Купонная награда гостю после появления даты рождения в профиле."
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
                "profile_event_type": "birthdate_filled",
            },
            "template_id": coupon_template.id,
        },
    )

    active_bots = list(
        BotProfile.objects.filter(
            is_active=True,
            provider_type__in=["telegram", "max", "vk"],
        ).order_by("id")
    )
    if active_bots:
        request_scenario.bot_profiles.add(*active_bots)
        coupon_scenario.bot_profiles.add(*active_bots)

    CouponAutomationConfig.objects.get_or_create(
        scenario_id=coupon_scenario.id,
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
                "profile_event_type": "birthdate_filled",
            },
        },
    )


def noop_reverse(apps, schema_editor):
    """
    Обратная миграция не удаляет эксплуатационные настройки.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0042_coupon_automation_venue_selection_mode"),
    ]

    operations = [
        migrations.CreateModel(
            name="GuestProfileCompletionEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "event_type",
                    models.CharField(
                        choices=[("birthdate_filled", "Дата рождения заполнена")],
                        db_index=True,
                        max_length=32,
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("vtelemax", "vtelemax"),
                            ("iiko", "iiko"),
                            ("manual", "Ручное изменение"),
                        ],
                        db_index=True,
                        default="vtelemax",
                        max_length=32,
                    ),
                ),
                ("source_ref", models.CharField(blank=True, db_index=True, max_length=180, null=True)),
                ("detected_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("profile_value", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("new", "Ожидает обработки"),
                            ("coupon_reserved", "Купон зарезервирован"),
                            ("skipped", "Пропущено"),
                            ("error", "Ошибка"),
                        ],
                        db_index=True,
                        default="new",
                        max_length=24,
                    ),
                ),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("error_text", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "coupon_assignment",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="profile_completion_event",
                        to="guests.couponautoscenarioassignment",
                    ),
                ),
                (
                    "guest",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="profile_completion_events",
                        to="guests.guest",
                    ),
                ),
                (
                    "request_notification_event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="profile_completion_request_events",
                        to="guests.notificationevent",
                    ),
                ),
            ],
            options={
                "verbose_name": "Событие заполнения профиля гостя",
                "verbose_name_plural": "События заполнения профилей гостей",
                "db_table": "guest_profile_completion_events",
            },
        ),
        migrations.AddConstraint(
            model_name="guestprofilecompletionevent",
            constraint=models.UniqueConstraint(
                fields=("guest", "event_type"),
                name="gprofile_event_guest_type_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="guestprofilecompletionevent",
            index=models.Index(fields=["event_type", "status", "detected_at"], name="gprofile_event_flow_idx"),
        ),
        migrations.AddIndex(
            model_name="guestprofilecompletionevent",
            index=models.Index(fields=["source", "source_ref"], name="gprofile_event_source_idx"),
        ),
        migrations.RunPython(seed_fill_birthday_autoscenario, noop_reverse),
    ]
