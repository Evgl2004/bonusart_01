from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0050_coupon_display_titles"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="couponautomationrule",
            name="cautorule_validity_days_gte_1",
        ),
        migrations.RemoveConstraint(
            model_name="couponautomationrule",
            name="cautorule_min_order_gte_0",
        ),
        migrations.AlterField(
            model_name="couponautomationconfig",
            name="execution_mode",
            field=models.CharField(
                choices=[
                    ("report_only", "Черновик"),
                    ("pilot", "Пилот"),
                    ("automatic", "Активен"),
                    ("paused", "Пауза"),
                ],
                db_index=True,
                default="report_only",
                help_text="Режим работы купонного автосценария.",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="couponautomationrule",
            name="venue_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text="Department.Id для правила по заведению; для правила Вся сеть (global) хранится __global__.",
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name="couponautomationrule",
            name="min_order_amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Справочная минимальная сумма заказа, настроенная в iikoCard для этого правила.",
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="couponautomationrule",
            name="coupon_promo_text_template",
            field=models.TextField(
                blank=True,
                help_text="Если задано, переопределяет описание купона из общей настройки.",
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="couponautomationrule",
            constraint=models.CheckConstraint(
                condition=models.Q(("coupon_validity_days__isnull", True))
                | models.Q(("coupon_validity_days__gte", 1)),
                name="cautorule_validity_null_or_gte_1",
            ),
        ),
        migrations.AddConstraint(
            model_name="couponautomationrule",
            constraint=models.CheckConstraint(
                condition=models.Q(("min_order_amount__isnull", True))
                | models.Q(("min_order_amount__gte", 0)),
                name="cautorule_min_order_null_or_gte_0",
            ),
        ),
    ]
