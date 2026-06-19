from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0044_guest_workbench_audience_channel_group"),
    ]

    operations = [
        migrations.AddField(
            model_name="guestworkbenchfilterpreset",
            name="venue_selection_mode",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="visited_once",
                help_text="Способ связи гостя с заведением для отбора.",
                max_length=32,
            ),
        ),
    ]
