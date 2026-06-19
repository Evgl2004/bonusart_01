from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0043_fill_birthday_profile_autoscenario"),
    ]

    operations = [
        migrations.AddField(
            model_name="guestworkbenchfilterpreset",
            name="audience_channel_group",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="all",
                help_text="Тип аудитории по доступности канала для рассылки.",
                max_length=32,
            ),
        ),
    ]
