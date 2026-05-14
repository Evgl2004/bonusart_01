import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0032_vtelemaxrecipientchannel_registered_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="CouponPoolBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "batch_code",
                    models.CharField(
                        db_index=True,
                        help_text="Уникальный технический код партии (например, TEST_20260514_001).",
                        max_length=80,
                        unique=True,
                    ),
                ),
                ("series", models.CharField(db_index=True, help_text="Серия купонов в iikoCard.", max_length=120)),
                (
                    "prefix",
                    models.CharField(
                        blank=True,
                        help_text="Префикс перед случайной частью кода купона (например, TST-).",
                        max_length=32,
                        null=True,
                    ),
                ),
                (
                    "alphabet_mode",
                    models.CharField(
                        choices=[
                            ("digits", "Только цифры"),
                            ("latin_upper", "Только латинские буквы (верхний регистр)"),
                            ("digits_latin_upper", "Цифры и латинские буквы (верхний регистр)"),
                        ],
                        default="digits_latin_upper",
                        help_text="Режим алфавита при генерации случайной части купона.",
                        max_length=32,
                    ),
                ),
                ("random_length", models.PositiveSmallIntegerField(default=12, help_text="Длина случайной части кода купона.")),
                ("count_requested", models.PositiveIntegerField(default=0)),
                ("count_generated", models.PositiveIntegerField(default=0)),
                (
                    "generated_by",
                    models.CharField(
                        blank=True,
                        help_text="Пользователь/оператор, запустивший генерацию.",
                        max_length=150,
                        null=True,
                    ),
                ),
                (
                    "export_file_path",
                    models.CharField(
                        blank=True,
                        help_text="Абсолютный или относительный путь к CSV, выгруженному для iikoCard.",
                        max_length=500,
                        null=True,
                    ),
                ),
                (
                    "verification_status",
                    models.CharField(
                        choices=[
                            ("not_checked", "Не проверено"),
                            ("partially_loaded", "Частично загружено"),
                            ("loaded", "Загружено"),
                            ("failed", "Проверка завершилась ошибкой"),
                        ],
                        db_index=True,
                        default="not_checked",
                        max_length=24,
                    ),
                ),
                ("last_verified_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("verified_found_count", models.PositiveIntegerField(default=0)),
                ("verified_not_found_count", models.PositiveIntegerField(default=0)),
                ("verification_note", models.TextField(blank=True, null=True)),
                ("generated_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Партия купонов",
                "verbose_name_plural": "Партии купонов",
                "db_table": "coupon_pool_batches",
                "indexes": [
                    models.Index(fields=["series", "verification_status"], name="cpbatch_series_ver_idx"),
                    models.Index(fields=["generated_at"], name="cpbatch_generated_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="CouponRegistryEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("series", models.CharField(db_index=True, max_length=120)),
                ("code", models.CharField(db_index=True, max_length=120)),
                (
                    "source",
                    models.CharField(
                        choices=[("generated", "Сгенерировано в SAGUR"), ("import_csv", "Импортировано из CSV"), ("manual", "Создано вручную")],
                        db_index=True,
                        default="generated",
                        max_length=20,
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        db_index=True,
                        default=True,
                        help_text="Технический флаг доступности купона для назначения в новых кампаниях.",
                    ),
                ),
                (
                    "pool_status",
                    models.CharField(
                        choices=[
                            ("generated", "Сгенерирован"),
                            ("uploaded_pending_check", "Загружен в iikoCard, ждёт проверки"),
                            ("verified_loaded", "Подтверждён в iikoCard"),
                            ("verify_failed", "Проверка в iikoCard не пройдена"),
                            ("assigned", "Назначен гостю"),
                            ("used", "Использован"),
                            ("expired", "Срок действия истёк"),
                            ("canceled", "Отменён"),
                        ],
                        db_index=True,
                        default="generated",
                        max_length=32,
                    ),
                ),
                (
                    "iiko_check_status",
                    models.CharField(
                        choices=[
                            ("not_checked", "Не проверен"),
                            ("found", "Найден в iikoCard"),
                            ("not_found", "Не найден в iikoCard"),
                            ("check_error", "Ошибка проверки iikoCard"),
                        ],
                        db_index=True,
                        default="not_checked",
                        max_length=20,
                    ),
                ),
                ("iiko_checked_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("iiko_check_error", models.TextField(blank=True, null=True)),
                ("assigned_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "batch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="coupons",
                        to="guests.couponpoolbatch",
                    ),
                ),
            ],
            options={
                "verbose_name": "Купон в реестре",
                "verbose_name_plural": "Реестр купонов",
                "db_table": "coupon_registry_entries",
                "indexes": [
                    models.Index(fields=["series", "pool_status"], name="cpreg_series_status_idx"),
                    models.Index(fields=["batch", "pool_status"], name="cpreg_batch_status_idx"),
                    models.Index(fields=["iiko_check_status", "iiko_checked_at"], name="cpreg_iiko_status_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("series", "code"), name="coupon_registry_series_code_uniq"),
                ],
            },
        ),
        migrations.CreateModel(
            name="CouponCampaignAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("person_id", models.UUIDField(blank=True, db_index=True, null=True)),
                ("phone_e164", models.CharField(blank=True, db_index=True, max_length=32, null=True)),
                ("coupon_series", models.CharField(max_length=120)),
                ("coupon_code", models.CharField(max_length=120)),
                ("assigned_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("lifetime_expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("reserved", "Зарезервирован"),
                            ("sent", "Отправлен"),
                            ("used", "Использован"),
                            ("expired", "Истёк"),
                            ("canceled", "Отменён"),
                            ("error", "Ошибка"),
                        ],
                        db_index=True,
                        default="reserved",
                        max_length=16,
                    ),
                ),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("used_order_id", models.BigIntegerField(blank=True, db_index=True, null=True)),
                (
                    "vtelemax_sync_status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает синхронизации"),
                            ("ok", "Синхронизирован"),
                            ("error", "Ошибка синхронизации"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("vtelemax_synced_at", models.DateTimeField(blank=True, null=True)),
                ("vtelemax_sync_error", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "campaign",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="coupon_assignments", to="guests.mailing"),
                ),
                (
                    "coupon",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="campaign_assignments", to="guests.couponregistryentry"),
                ),
                (
                    "guest",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="coupon_assignments", to="guests.guest"),
                ),
            ],
            options={
                "verbose_name": "Назначение купона кампании",
                "verbose_name_plural": "Назначения купонов кампаний",
                "db_table": "coupon_campaign_assignments",
                "indexes": [
                    models.Index(fields=["campaign", "status"], name="cpass_campaign_status_idx"),
                    models.Index(fields=["status", "vtelemax_sync_status"], name="cpass_sync_status_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("campaign", "guest"), name="cpass_campaign_guest_uniq"),
                    models.UniqueConstraint(fields=("campaign", "coupon_series", "coupon_code"), name="cpass_campaign_coupon_uniq"),
                    models.UniqueConstraint(condition=models.Q(("person_id__isnull", False)), fields=("campaign", "person_id"), name="cpass_campaign_person_uniq"),
                ],
            },
        ),
        migrations.CreateModel(
            name="CouponVtelemaxSyncQueue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                (
                    "direction",
                    models.CharField(
                        choices=[("assignments", "Назначение купонов"), ("status_update", "Обновление статуса купона")],
                        db_index=True,
                        max_length=20,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает отправки"),
                            ("sent", "Отправлено"),
                            ("acked", "Подтверждено"),
                            ("error", "Ошибка"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("payload_json", models.JSONField(blank=True, default=dict)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_retry_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("last_error", models.TextField(blank=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("ack_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "assignment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="vtelemax_queue_events",
                        to="guests.couponcampaignassignment",
                    ),
                ),
            ],
            options={
                "verbose_name": "Очередь синхронизации купонов в vtelemax",
                "verbose_name_plural": "Очередь синхронизации купонов в vtelemax",
                "db_table": "coupon_vtelemax_sync_queue",
                "indexes": [
                    models.Index(fields=["status", "next_retry_at"], name="cpvq_status_retry_idx"),
                    models.Index(fields=["direction", "status"], name="cpvq_dir_status_idx"),
                ],
            },
        ),
    ]
