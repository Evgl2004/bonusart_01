import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0039_coupon_autoscenario_used_business_date"),
    ]

    operations = [
        migrations.CreateModel(
            name="CouponAutomationRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                (
                    "scope_type",
                    models.CharField(
                        choices=[("venue", "Заведение"), ("global", "Вся сеть (global)")],
                        db_index=True,
                        default="venue",
                        max_length=16,
                    ),
                ),
                (
                    "coupon_series",
                    models.CharField(
                        db_index=True,
                        help_text="Серия купонов, из которой правило будет брать доступные купоны.",
                        max_length=120,
                    ),
                ),
                (
                    "venue_code",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        default="",
                        help_text="Department.Id для правила по заведению; для global хранится __global__.",
                        max_length=64,
                    ),
                ),
                ("venue_name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "coupon_validity_days",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        help_text="Если задано, переопределяет срок действия из общей настройки.",
                        null=True,
                    ),
                ),
                (
                    "priority",
                    models.PositiveSmallIntegerField(
                        db_index=True,
                        default=100,
                        help_text="Меньшее значение означает более высокий приоритет среди правил одного типа.",
                    ),
                ),
                (
                    "min_order_amount",
                    models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
                ),
                ("iikocard_action_note", models.TextField(blank=True, null=True)),
                ("coupon_promo_text_template", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "config",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="coupon_rules",
                        to="guests.couponautomationconfig",
                    ),
                ),
            ],
            options={
                "verbose_name": "Купонное правило автосценария",
                "verbose_name_plural": "Купонные правила автосценариев",
                "db_table": "coupon_automation_rules",
                "indexes": [
                    models.Index(fields=["config", "is_active"], name="cautorule_cfg_active_idx"),
                    models.Index(fields=["config", "scope_type", "priority"], name="cautorule_cfg_scope_pri"),
                    models.Index(fields=["coupon_series"], name="cautorule_series_idx"),
                    models.Index(fields=["venue_code"], name="cautorule_venue_idx"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("scope_type__in", ["venue", "global"])),
                        name="cautorule_scope_type_chk",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("coupon_validity_days__isnull", True))
                        | models.Q(("coupon_validity_days__gte", 1)),
                        name="cautorule_validity_days_gte_1",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("min_order_amount__isnull", True))
                        | models.Q(("min_order_amount__gte", 0)),
                        name="cautorule_min_order_gte_0",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="couponautoscenarioassignment",
            name="coupon_rule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assignments",
                to="guests.couponautomationrule",
            ),
        ),
        migrations.AddField(
            model_name="couponautoscenarioassignment",
            name="coupon_selection_source",
            field=models.CharField(blank=True, db_index=True, max_length=32, null=True),
        ),
    ]
