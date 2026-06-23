from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0046_mailing_source_filter_snapshot"),
    ]

    operations = [
        migrations.AlterField(
            model_name="couponpoolbatch",
            name="alphabet_mode",
            field=models.CharField(
                choices=[
                    ("digits", "Только цифры"),
                    ("latin_upper", "Только латинские буквы (верхний регистр)"),
                    ("latin_cyrillic_lookalike_upper", "Латинские буквы, похожие на кириллицу"),
                    ("digits_latin_lookalike_upper", "Цифры и латинские буквы, похожие на кириллицу"),
                    ("digits_latin_upper", "Цифры и латинские буквы (верхний регистр)"),
                ],
                default="digits_latin_upper",
                help_text="Режим алфавита при генерации случайной части купона.",
                max_length=32,
            ),
        ),
    ]
