from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0036_coupon_used_after_campaign_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="CouponAutomationConfig",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "execution_mode",
                    models.CharField(
                        choices=[
                            ("report_only", "Только отчёт"),
                            ("pilot", "Пилот"),
                            ("automatic", "Автоматически"),
                            ("paused", "Пауза"),
                        ],
                        db_index=True,
                        default="report_only",
                        help_text="Режим работы купонного автосценария.",
                        max_length=24,
                    ),
                ),
                (
                    "coupon_series",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        help_text="Серия купонов, из которой автосценарий будет брать доступные купоны.",
                        max_length=120,
                        null=True,
                    ),
                ),
                (
                    "venue_code",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        help_text="Код заведения или __global__ для сетевой акции.",
                        max_length=64,
                        null=True,
                    ),
                ),
                (
                    "venue_name",
                    models.CharField(
                        blank=True,
                        help_text="Название заведения для отображения и payload vtelemax.",
                        max_length=255,
                        null=True,
                    ),
                ),
                (
                    "coupon_validity_days",
                    models.PositiveSmallIntegerField(
                        default=14,
                        help_text="Срок действия выдаваемого купона в днях.",
                    ),
                ),
                (
                    "coupon_promo_text_template",
                    models.TextField(
                        blank=True,
                        help_text=(
                            "Текст акции для карточки купона; поддержка переменных "
                            "добавляется на уровне executor."
                        ),
                        null=True,
                    ),
                ),
                (
                    "min_order_amount",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Справочная минимальная сумма заказа, настроенная в iikoCard.",
                        max_digits=12,
                        null=True,
                    ),
                ),
                (
                    "iikocard_action_note",
                    models.TextField(
                        blank=True,
                        help_text=(
                            "Что именно настроено в iikoCard: подарок, скидка, место продаж, "
                            "тип заказа."
                        ),
                        null=True,
                    ),
                ),
                (
                    "max_recipients_per_run",
                    models.PositiveIntegerField(
                        default=100,
                        help_text="Максимум гостей, которых сценарий может обработать за один проход.",
                    ),
                ),
                (
                    "max_active_coupons_per_guest",
                    models.PositiveSmallIntegerField(
                        default=1,
                        help_text="Защита от нескольких активных купонов одной акции у одного гостя.",
                    ),
                ),
                (
                    "cooldown_days",
                    models.PositiveIntegerField(
                        default=30,
                        help_text=(
                            "Минимальная пауза перед повторным попаданием гостя "
                            "в этот купонный сценарий."
                        ),
                    ),
                ),
                (
                    "settings",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text=(
                            "Резерв для дополнительных параметров сценария до появления "
                            "специализированных полей."
                        ),
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "scenario",
                    models.OneToOneField(
                        help_text=(
                            "Сценарий уведомлений, для которого настроена купонная автоматизация."
                        ),
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="coupon_automation_config",
                        to="guests.notificationscenario",
                    ),
                ),
            ],
            options={
                "verbose_name": "Купонная настройка автосценария",
                "verbose_name_plural": "Купонные настройки автосценариев",
                "db_table": "coupon_automation_configs",
                "indexes": [
                    models.Index(fields=["execution_mode"], name="cauto_mode_idx"),
                    models.Index(fields=["coupon_series"], name="cauto_series_idx"),
                    models.Index(fields=["venue_code"], name="cauto_venue_idx"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(execution_mode__in=["report_only", "pilot", "automatic", "paused"]),
                        name="cauto_execution_mode_chk",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(coupon_validity_days__gte=1),
                        name="cauto_validity_days_gte_1",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(max_recipients_per_run__gte=1),
                        name="cauto_max_recipients_gte_1",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(max_active_coupons_per_guest__gte=1),
                        name="cauto_max_active_gte_1",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(cooldown_days__gte=0),
                        name="cauto_cooldown_days_gte_0",
                    ),
                ],
            },
        ),
    ]
