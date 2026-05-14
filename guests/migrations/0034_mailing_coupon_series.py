from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0033_coupon_registry_stage_c"),
    ]

    operations = [
        migrations.AddField(
            model_name="mailing",
            name="coupon_series",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text=(
                    "Опциональная серия купонов iikoCard для кампании. "
                    "Если заполнено, перед отправкой включается купонный sync-gate."
                ),
                max_length=120,
                null=True,
            ),
        ),
    ]
