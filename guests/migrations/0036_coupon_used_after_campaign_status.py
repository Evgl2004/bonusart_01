from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0035_coupon_venues_and_promo"),
    ]

    operations = [
        migrations.AlterField(
            model_name="couponcampaignassignment",
            name="status",
            field=models.CharField(
                choices=[
                    ("reserved", "Зарезервирован"),
                    ("sent", "Отправлен"),
                    ("used", "Использован"),
                    ("used_after_campaign", "Использован после завершения акции"),
                    ("expired", "Истёк"),
                    ("canceled", "Отменён"),
                    ("error", "Ошибка"),
                ],
                db_index=True,
                default="reserved",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="couponregistryentry",
            name="pool_status",
            field=models.CharField(
                choices=[
                    ("generated", "Сгенерирован"),
                    ("uploaded_pending_check", "Загружен в iikoCard, ждёт проверки"),
                    ("verified_loaded", "Подтверждён в iikoCard"),
                    ("verify_failed", "Проверка в iikoCard не пройдена"),
                    ("assigned", "Назначен гостю"),
                    ("used", "Использован"),
                    ("used_after_campaign", "Использован после завершения акции"),
                    ("expired", "Срок действия истёк"),
                    ("canceled", "Отменён"),
                ],
                db_index=True,
                default="generated",
                max_length=32,
            ),
        ),
    ]
