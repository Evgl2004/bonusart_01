from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0053_coupon_olap_lookup_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="OlapLivePipelineQueue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_webhook_id", models.CharField(blank=True, db_index=True, max_length=100, null=True)),
                ("business_date", models.DateField(blank=True, db_index=True, null=True)),
                ("department_id", models.CharField(blank=True, db_index=True, max_length=64, null=True)),
                ("order_number", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("order_external_id", models.CharField(blank=True, db_index=True, max_length=100, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("new", "Новая"),
                            ("in_progress", "В работе"),
                            ("waiting_olap", "Ожидает OLAP"),
                            ("olap_loaded", "OLAP загружен"),
                            ("fact_built", "Факт чека собран"),
                            ("done", "Завершена"),
                            ("retry", "Повторить позже"),
                            ("skipped", "Пропущена"),
                            ("failed", "Ошибка"),
                        ],
                        db_index=True,
                        default="new",
                        max_length=20,
                    ),
                ),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("next_retry_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("locked_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, null=True)),
                (
                    "last_step_result",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Последняя техническая сводка по стадиям оперативного конвейера.",
                    ),
                ),
                ("processed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "sync_journal",
                    models.OneToOneField(
                        help_text="Связанная задача загрузки чека из OLAP.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="live_pipeline",
                        to="guests.olapchecksyncjournal",
                    ),
                ),
            ],
            options={
                "verbose_name": "Очередь оперативного OLAP-конвейера",
                "verbose_name_plural": "Очередь оперативного OLAP-конвейера",
                "db_table": "olap_live_pipeline_queue",
            },
        ),
        migrations.AddIndex(
            model_name="olaplivepipelinequeue",
            index=models.Index(fields=["status", "next_retry_at", "created_at"], name="olpq_status_next_idx"),
        ),
        migrations.AddIndex(
            model_name="olaplivepipelinequeue",
            index=models.Index(fields=["business_date", "department_id", "order_number"], name="olpq_order_key_idx"),
        ),
        migrations.AddIndex(
            model_name="olaplivepipelinequeue",
            index=models.Index(fields=["source_webhook_id", "status"], name="olpq_source_status_idx"),
        ),
        migrations.AddIndex(
            model_name="olapsalesrawline",
            index=models.Index(
                fields=["business_date", "department_id", "order_number", "uniq_order_id"],
                name="osrl_order_key_full_idx",
            ),
        ),
    ]
