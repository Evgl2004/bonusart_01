from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0031_vtelemaxsyncstate_vtelemaxrecipientchannel"),
    ]

    operations = [
        migrations.AddField(
            model_name="vtelemaxrecipientchannel",
            name="registered_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text="Дата/время завершения регистрации канала на стороне vtelemax.",
                null=True,
            ),
        ),
    ]

