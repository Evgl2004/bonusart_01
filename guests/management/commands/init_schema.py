from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """
    Устаревший bootstrap-командлет для схемы БД.

    Ранее команда выполняла прямой SQL, что противоречит текущей политике
    проекта: все изменения схемы должны идти только через Django migrations.
    """

    help = "DEPRECATED: use Django migrations instead of raw SQL schema init."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Сразу запустить `python manage.py migrate` в рамках этой команды.",
        )

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.WARNING(
                "Команда init_schema устарела. "
                "Для инициализации схемы используйте Django migrations."
            )
        )
        self.stdout.write("Рекомендуемая команда: python manage.py migrate")

        if options.get("apply"):
            self.stdout.write("Запускаю migrate...")
            call_command("migrate", interactive=False)
            self.stdout.write(self.style.SUCCESS("Migrate выполнен успешно."))
