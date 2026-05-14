from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0034_mailing_coupon_series"),
    ]

    operations = [
        migrations.AddField(
            model_name="mailing",
            name="coupon_promo_text",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Текст акции купона для показа гостю в карточке купона "
                    "и для передачи в vtelemax."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="mailing",
            name="coupon_venue_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text=(
                    "Код заведения для купонной кампании. "
                    "Используется для проверки соответствия серии купонов и кампании."
                ),
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="mailing",
            name="coupon_venue_name",
            field=models.CharField(
                blank=True,
                help_text="Человекочитаемое название заведения купонной кампании.",
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponcampaignassignment",
            name="promo_text",
            field=models.TextField(
                blank=True,
                help_text="Текст акции, который передаётся гостю вместе с купоном.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponcampaignassignment",
            name="venue_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Код заведения, в рамках которого выпущен и назначен купон.",
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponcampaignassignment",
            name="venue_name",
            field=models.CharField(
                blank=True,
                help_text="Название заведения для назначенного купона.",
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponpoolbatch",
            name="venue_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Код заведения, для которого сформирован пул купонов.",
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponpoolbatch",
            name="venue_name",
            field=models.CharField(
                blank=True,
                help_text="Название заведения, для которого сформирован пул купонов.",
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponregistryentry",
            name="venue_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Код заведения, к которому относится купон.",
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="couponregistryentry",
            name="venue_name",
            field=models.CharField(
                blank=True,
                help_text="Название заведения, к которому относится купон.",
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name="couponpoolbatch",
            index=models.Index(fields=["venue_code", "verification_status"], name="cpbatch_venue_ver_idx"),
        ),
        migrations.AddIndex(
            model_name="couponregistryentry",
            index=models.Index(fields=["venue_code", "pool_status"], name="cpreg_venue_status_idx"),
        ),
        migrations.AddIndex(
            model_name="couponcampaignassignment",
            index=models.Index(fields=["campaign", "venue_code", "status"], name="cpass_camp_venue_st_idx"),
        ),
    ]
