from django.test import TestCase
from django.utils import timezone

from guests.models import Guest
from guests.services.template_render import render_message_for_guest


class TemplateRenderTests(TestCase):
    """
    Тесты рендера текстов для ручных/массовых рассылок.
    """

    def setUp(self):
        now = timezone.now()
        self.guest = Guest.objects.create(
            phone="+79990000001",
            first_name="Илья",
            last_name="Тестов",
            created_at=now,
            updated_at=now,
        )

    def test_render_supports_django_and_format_placeholders(self):
        """
        Рендер должен подставлять и Django-плейсхолдеры, и format-плейсхолдеры.
        """
        message = "Здравствуйте, {{ first_name }}! Купон: {coupon_code}. Дней: {days_without_visits}"
        rendered = render_message_for_guest(
            message,
            self.guest,
            {"coupon_code": "CPN-777", "days_without_visits": 12},
        )
        self.assertIn("Здравствуйте, Илья!", rendered)
        self.assertIn("Купон: CPN-777", rendered)
        self.assertIn("Дней: 12", rendered)

    def test_render_handles_legacy_coupon_typo_placeholder(self):
        """
        Legacy-опечатка `{courpon_code}` не должна уходить в итоговое сообщение "как есть".
        """
        message = "Здравствуйте, {{ first_name }}! Ваш купон: {courpon_code}"
        rendered = render_message_for_guest(message, self.guest, {"coupon_code": "CPN-42"})
        self.assertIn("Здравствуйте, Илья!", rendered)
        self.assertIn("CPN-42", rendered)
        self.assertNotIn("{courpon_code}", rendered)

    def test_render_fallbacks_for_empty_first_name_and_coupon_code(self):
        """
        Если имя гостя и код купона пустые, должны использоваться безопасные fallback-значения.
        """
        guest_without_name = Guest.objects.create(
            phone="+79990000002",
            first_name="",
            last_name="",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        message = "Привет, {{ first_name }}! Купон: {coupon_code}"
        rendered = render_message_for_guest(message, guest_without_name, {})
        self.assertIn("Привет, гость!", rendered)
        self.assertIn("Купон: купон отсутствует", rendered)

    def test_render_fallbacks_for_other_key_variables(self):
        """
        Для ключевых переменных шаблона должны применяться заглушки при пустых значениях.
        """
        guest_without_profile = Guest.objects.create(
            phone="",
            first_name="",
            last_name="",
            email="",
            birthdate=None,
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        message = (
            "Имя: {{ first_name }}; Фамилия: {{ last_name }}; "
            "Телефон: {{ phone }}; Email: {{ email }}; "
            "Дата: {{ birthdate }}; Возраст: {{ age }}; "
            "Без визитов: {days_without_visits}; Купон: {coupon_code}"
        )
        rendered = render_message_for_guest(message, guest_without_profile, {})
        self.assertIn("Имя: гость", rendered)
        self.assertIn("Фамилия: гость", rendered)
        self.assertIn("Телефон: телефон не указан", rendered)
        self.assertIn("Email: email не указан", rendered)
        self.assertIn("Дата: дата рождения не указана", rendered)
        self.assertIn("Возраст: возраст не указан", rendered)
        self.assertIn("Без визитов: нет данных", rendered)
        self.assertIn("Купон: купон отсутствует", rendered)
