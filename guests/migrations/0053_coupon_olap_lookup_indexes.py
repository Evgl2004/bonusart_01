from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0052_iiko_customer_category_sync"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="olapsalesrawline",
            index=models.Index(
                fields=["coupon_series", "coupon_number"],
                name="osrl_coupon_key_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="orderfact",
            index=models.Index(
                fields=["coupon_used", "coupon_series", "coupon_number"],
                name="of_coupon_used_key_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="couponcampaignassignment",
            index=models.Index(
                fields=["coupon_series", "coupon_code", "status"],
                name="cpass_coupon_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="couponautoscenarioassignment",
            index=models.Index(
                fields=["coupon_series", "coupon_code", "status"],
                name="cautoass_coupon_status_idx",
            ),
        ),
    ]
