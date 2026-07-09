from django.db import migrations


SCENARIO_CODE_WELCOME_COUPON = "welcome_coupon"
TEMPLATE_NAME_WELCOME_COUPON = "SYSTEM_WELCOME_COUPON_TEMPLATE"

TEMPLATE_TEXT_WELCOME_COUPON = (
    "{{ first_name }}, добро пожаловать в программу лояльности SAGUR. "
    "Ваш приветственный купон: {coupon_code}."
)


def seed_welcome_coupon_autoscenario(apps, schema_editor):
    """
    Создаёт системный черновик welcome-автосценария.

    Купонные правила здесь не создаются: их настраивают через рабочий интерфейс
    автосценариев после выбора серии купонов и условий акции.
    """

    MessageTemplate = apps.get_model("guests", "MessageTemplate")
    NotificationScenario = apps.get_model("guests", "NotificationScenario")
    CouponAutomationConfig = apps.get_model("guests", "CouponAutomationConfig")
    BotProfile = apps.get_model("guests", "BotProfile")

    template, _ = MessageTemplate.objects.get_or_create(
        name=TEMPLATE_NAME_WELCOME_COUPON,
        defaults={
            "description": "Системный шаблон приветственного купонного автосценария.",
            "message_text": TEMPLATE_TEXT_WELCOME_COUPON,
            "created_by": "system",
            "is_active": True,
        },
    )

    scenario, created = NotificationScenario.objects.get_or_create(
        code=SCENARIO_CODE_WELCOME_COUPON,
        defaults={
            "name": "Системный сценарий: регистрация гостя + приветственный купон",
            "description": (
                "Событийный купонный автосценарий для гостей, зарегистрированных "
                "в Telegram, VK или MAX через vtelemax."
            ),
            "is_active": False,
            "is_system": True,
            "trigger_type": "schedule",
            "priority": "high",
            "target_mode": "primary_only",
            "distribution_mode": "immediate",
            "send_window_begin": None,
            "send_window_end": None,
            "timezone": "Asia/Yekaterinburg",
            "cooldown_minutes": 0,
            "max_per_day_per_guest": None,
            "settings": {
                "coupon_required": True,
                "registration_event_source": "vtelemax",
            },
            "template_id": template.id,
        },
    )

    if created:
        active_bots = list(
            BotProfile.objects.filter(
                is_active=True,
                provider_type__in=["telegram", "max", "vk"],
            ).order_by("id")
        )
        if active_bots:
            scenario.bot_profiles.add(*active_bots)

    CouponAutomationConfig.objects.get_or_create(
        scenario_id=scenario.id,
        defaults={
            "scenario_type": "welcome_registration_coupon",
            "execution_mode": "report_only",
            "venue_selection_mode": "last_order",
            "audience_venue_filter_mode": "disabled",
            "audience_venue_code": None,
            "audience_venue_name": None,
            "coupon_series": None,
            "venue_code": None,
            "venue_name": None,
            "coupon_validity_days": 14,
            "coupon_title_template": None,
            "coupon_promo_text_template": None,
            "min_order_amount": None,
            "iikocard_action_note": None,
            "max_recipients_per_run": 100,
            "max_active_coupons_per_guest": 1,
            "cooldown_days": 3650,
            "settings": {
                "registration_event_source": "vtelemax",
            },
        },
    )


def noop_reverse(apps, schema_editor):
    """
    Обратная миграция не удаляет эксплуатационные настройки.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0058_welcome_coupon_scenario_type"),
    ]

    operations = [
        migrations.RunPython(seed_welcome_coupon_autoscenario, noop_reverse),
    ]
