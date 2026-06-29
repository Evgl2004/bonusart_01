from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0054_olap_live_pipeline_queue"),
    ]

    operations = [
        migrations.AddField(
            model_name="couponautoscenarioassignment",
            name="status_reason",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Техническая причина текущего статуса назначения купона.",
                max_length=80,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponautoscenarioassignment",
            name="status_details",
            field=models.TextField(
                blank=True,
                help_text="Человекочитаемое пояснение к причине текущего статуса назначения купона.",
                null=True,
            ),
        ),
    ]
