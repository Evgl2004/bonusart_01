from django.db import migrations, models


def fill_scenario_type(apps, schema_editor):
    CouponAutomationConfig = apps.get_model("guests", "CouponAutomationConfig")

    code_to_type = {
        "inactive_30d_coupon": "inactive_days_coupon",
        "birthday_coupon": "birthday_coupon",
        "fill_birthday_coupon": "birthdate_filled_coupon",
    }

    for scenario_code, scenario_type in code_to_type.items():
        CouponAutomationConfig.objects.filter(scenario__code=scenario_code).update(
            scenario_type=scenario_type
        )


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0048_coupon_autoscenario_audience_venue_filter"),
    ]

    operations = [
        migrations.AddField(
            model_name="couponautomationconfig",
            name="scenario_type",
            field=models.CharField(
                choices=[
                    ("inactive_days_coupon", "Гость не был N дней + купон"),
                    ("birthday_coupon", "День рождения + купон"),
                    ("birthdate_filled_coupon", "Дата рождения заполнена + купон"),
                ],
                db_index=True,
                default="inactive_days_coupon",
                help_text="Тип купонного автосценария: какая логика отбора гостей используется.",
                max_length=40,
            ),
        ),
        migrations.RunPython(fill_scenario_type, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="couponautomationconfig",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "scenario_type__in",
                        [
                            "inactive_days_coupon",
                            "birthday_coupon",
                            "birthdate_filled_coupon",
                        ],
                    )
                ),
                name="cauto_scenario_type_chk",
            ),
        ),
        migrations.AddIndex(
            model_name="couponautomationconfig",
            index=models.Index(fields=["scenario_type"], name="cauto_scenario_type_idx"),
        ),
    ]
