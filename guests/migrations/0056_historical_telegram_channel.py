from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0055_coupon_autoscenario_assignment_status_reason"),
    ]

    operations = [
        migrations.CreateModel(
            name="HistoricalTelegramChannel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("telegram_chat_id", models.CharField(db_index=True, max_length=128)),
                (
                    "delivery_state",
                    models.CharField(
                        choices=[
                            ("sendable", "можно отправлять"),
                            ("blocked", "заблокирован или недоступен"),
                            ("manually_excluded", "исключён вручную"),
                        ],
                        db_index=True,
                        default="sendable",
                        max_length=32,
                    ),
                ),
                ("last_success_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_error_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_text", models.TextField(blank=True, null=True)),
                ("excluded_at", models.DateTimeField(blank=True, null=True)),
                ("excluded_reason", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "bot_profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="historical_telegram_channels",
                        to="guests.botprofile",
                    ),
                ),
                (
                    "guest",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="historical_telegram_channels",
                        to="guests.guest",
                    ),
                ),
            ],
            options={
                "verbose_name": "Исторический Telegram-канал",
                "verbose_name_plural": "Исторические Telegram-каналы",
                "db_table": "historical_telegram_channels",
            },
        ),
        migrations.AddConstraint(
            model_name="historicaltelegramchannel",
            constraint=models.UniqueConstraint(
                fields=("guest", "bot_profile"),
                name="hist_tg_channel_guest_bot_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="historicaltelegramchannel",
            constraint=models.UniqueConstraint(
                fields=("bot_profile", "telegram_chat_id"),
                name="hist_tg_channel_bot_chat_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="historicaltelegramchannel",
            index=models.Index(fields=["delivery_state", "updated_at"], name="hist_tg_state_updated_idx"),
        ),
        migrations.AddIndex(
            model_name="historicaltelegramchannel",
            index=models.Index(fields=["guest", "delivery_state"], name="hist_tg_guest_state_idx"),
        ),
    ]
