from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0047_coupon_cyrillic_lookalike_alphabet"),
    ]

    operations = [
        migrations.AddField(
            model_name="couponautomationconfig",
            name="audience_venue_filter_mode",
            field=models.CharField(
                choices=[
                    ("disabled", "Без ограничения по заведению"),
                    ("visited_once_and_inactive", "Был хотя бы 1 раз и не был N+ дней"),
                ],
                db_index=True,
                default="disabled",
                help_text="Как ограничивать аудиторию автосценария конкретным заведением.",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="couponautomationconfig",
            name="audience_venue_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Код заведения, по которому отбирается аудитория автосценария.",
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponautomationconfig",
            name="audience_venue_name",
            field=models.CharField(
                blank=True,
                help_text="Название заведения, по которому отбирается аудитория автосценария.",
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="couponautomationconfig",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("audience_venue_filter_mode__in", ["disabled", "visited_once_and_inactive"])
                ),
                name="cauto_audience_venue_mode_chk",
            ),
        ),
        migrations.AddIndex(
            model_name="couponautomationconfig",
            index=models.Index(
                fields=["audience_venue_filter_mode"],
                name="cauto_aud_venue_mode_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="couponautomationconfig",
            index=models.Index(
                fields=["audience_venue_code"],
                name="cauto_aud_venue_idx",
            ),
        ),
    ]
