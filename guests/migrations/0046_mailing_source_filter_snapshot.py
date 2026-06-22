from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0045_guest_workbench_venue_selection_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="mailing",
            name="source_filter_snapshot",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Снимок фильтров рабочего экрана гостей, по которым создана аудитория кампании.",
            ),
        ),
    ]
