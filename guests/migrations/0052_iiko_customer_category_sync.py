from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0051_align_coupon_rule_constraints"),
    ]

    operations = [
        migrations.AddField(
            model_name="couponcampaignassignment",
            name="iiko_category_add_error",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="couponcampaignassignment",
            name="iiko_category_add_status",
            field=models.CharField(
                choices=[
                    ("disabled", "Контур iikoCard отключён"),
                    ("pending", "Ожидает iikoCard"),
                    ("ok", "Подтверждено iikoCard"),
                    ("error", "Ошибка iikoCard"),
                ],
                db_index=True,
                default="disabled",
                help_text="Статус добавления гостя в категорию iikoCard, разрешающую применение купона.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="couponcampaignassignment",
            name="iiko_category_add_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="couponautoscenarioassignment",
            name="iiko_category_add_error",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="couponautoscenarioassignment",
            name="iiko_category_add_status",
            field=models.CharField(
                choices=[
                    ("disabled", "Контур iikoCard отключён"),
                    ("pending", "Ожидает iikoCard"),
                    ("ok", "Подтверждено iikoCard"),
                    ("error", "Ошибка iikoCard"),
                ],
                db_index=True,
                default="disabled",
                help_text="Статус добавления гостя в категорию iikoCard, разрешающую применение купона.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="couponautoscenarioassignment",
            name="iiko_category_add_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="IikoCustomerCategorySyncEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                (
                    "action",
                    models.CharField(
                        choices=[("add", "Добавить категорию"), ("remove", "Удалить категорию")],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                (
                    "source_type",
                    models.CharField(
                        choices=[
                            ("campaign", "Купонная кампания"),
                            ("autoscenario", "Купонный автосценарий"),
                            ("manual", "Ручная операция"),
                        ],
                        db_index=True,
                        default="manual",
                        max_length=32,
                    ),
                ),
                ("iiko_customer_id", models.CharField(blank=True, db_index=True, max_length=64, null=True)),
                ("category_id", models.CharField(db_index=True, max_length=64)),
                ("organization_id", models.CharField(blank=True, max_length=64, null=True)),
                ("payload_json", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает отправки"),
                            ("sent", "Отправлено"),
                            ("acked", "Подтверждено"),
                            ("error", "Ошибка"),
                            ("skipped", "Пропущено"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_retry_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("last_error", models.TextField(blank=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("ack_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "autoscenario_assignment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="iiko_category_sync_events",
                        to="guests.couponautoscenarioassignment",
                    ),
                ),
                (
                    "campaign_assignment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="iiko_category_sync_events",
                        to="guests.couponcampaignassignment",
                    ),
                ),
                (
                    "guest",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="iiko_category_sync_events",
                        to="guests.guest",
                    ),
                ),
            ],
            options={
                "verbose_name": "Событие синхронизации категории гостя iikoCard",
                "verbose_name_plural": "События синхронизации категорий гостей iikoCard",
                "db_table": "iiko_customer_category_sync_events",
            },
        ),
        migrations.AddIndex(
            model_name="couponcampaignassignment",
            index=models.Index(fields=["status", "iiko_category_add_status"], name="cpass_iiko_status_idx"),
        ),
        migrations.AddIndex(
            model_name="couponautoscenarioassignment",
            index=models.Index(fields=["status", "iiko_category_add_status"], name="cautoass_iiko_status_idx"),
        ),
        migrations.AddIndex(
            model_name="iikocustomercategorysyncevent",
            index=models.Index(fields=["status", "next_retry_at"], name="iikocat_status_retry_idx"),
        ),
        migrations.AddIndex(
            model_name="iikocustomercategorysyncevent",
            index=models.Index(fields=["action", "status"], name="iikocat_action_status_idx"),
        ),
        migrations.AddIndex(
            model_name="iikocustomercategorysyncevent",
            index=models.Index(fields=["guest", "category_id", "status"], name="iikocat_guest_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="iikocustomercategorysyncevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(campaign_assignment__isnull=False),
                fields=("campaign_assignment", "action", "category_id"),
                name="iikocat_cpass_action_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="iikocustomercategorysyncevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(autoscenario_assignment__isnull=False),
                fields=("autoscenario_assignment", "action", "category_id"),
                name="iikocat_cauto_action_uniq",
            ),
        ),
    ]
