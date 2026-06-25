from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0049_coupon_automation_scenario_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="mailing",
            name="coupon_title",
            field=models.CharField(
                blank=True,
                help_text="Название купона для кнопки и карточки гостя в vtelemax.",
                max_length=120,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponautomationconfig",
            name="coupon_title_template",
            field=models.CharField(
                blank=True,
                help_text="Название купона для кнопки и карточки vtelemax; поддерживает переменные шаблона.",
                max_length=120,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponautomationrule",
            name="coupon_title_template",
            field=models.CharField(
                blank=True,
                help_text="Если задано, переопределяет название купона из общей настройки.",
                max_length=120,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponcampaignassignment",
            name="coupon_title",
            field=models.CharField(
                blank=True,
                help_text="Снимок названия купона, отправленного гостю в vtelemax.",
                max_length=120,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponautoscenarioassignment",
            name="coupon_title",
            field=models.CharField(blank=True, max_length=120, null=True),
        ),
    ]
