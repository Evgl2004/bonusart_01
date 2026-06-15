from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0041_birthday_coupon_autoscenario"),
    ]

    operations = [
        migrations.AddField(
            model_name="couponautomationconfig",
            name="venue_selection_mode",
            field=models.CharField(
                choices=[
                    ("last_order", "Последнее заведение"),
                    ("all_visited", "Все посещённые заведения"),
                    ("favorite", "Любимое заведение"),
                ],
                db_index=True,
                default="last_order",
                help_text="Как выбирать заведения гостя для правил купонного автосценария.",
                max_length=24,
            ),
        ),
        migrations.AddConstraint(
            model_name="couponautomationconfig",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("venue_selection_mode__in", ["last_order", "all_visited", "favorite"])
                ),
                name="cauto_venue_mode_chk",
            ),
        ),
        migrations.AddIndex(
            model_name="couponautomationconfig",
            index=models.Index(fields=["venue_selection_mode"], name="cauto_venue_mode_idx"),
        ),
    ]
