from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
import os


class Command(BaseCommand):
    help = 'Создание Администратора если он не создан'

    def handle(self, *args, **options):
        user_adm = get_user_model()

        # Данные из переменных окружения
        username = os.getenv('DJANGO_ADMIN_USERNAME', 'admin')
        email = os.getenv('DJANGO_ADMIN_EMAIL', 'admin@example.com')
        password = os.getenv('DJANGO_ADMIN_PASSWORD', 'admin123')

        if not user_adm.objects.filter(username=username).exists():
            user_adm.objects.create_superuser(
                username=username,
                email=email,
                password=password
            )
            self.stdout.write(
                self.style.SUCCESS(f'Администратор {username} создан успешно!')
            )
        else:
            self.stdout.write(
                self.style.WARNING(f'Администратор {username} уже существует')
            )
