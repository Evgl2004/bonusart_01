"""
Тесты новой структуры пользовательской навигации.

Проверяем:
1. Доступность новых URL разделов.
2. Наличие новых пунктов меню в базовом шаблоне.
3. Скрытие legacy-навигации для обычных пользователей.
4. Отображение legacy-навигации только для staff.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class NavigationMenuTests(TestCase):
    """
    Smoke-тесты новой боковой навигации в базовом шаблоне.
    """

    def test_new_navigation_routes_are_available(self):
        """
        Новые разделы должны отвечать со статусом 200.
        """
        for route_name in ("dashboard", "segments", "focus_categories", "reports"):
            response = self.client.get(reverse(route_name), secure=True)
            self.assertEqual(response.status_code, 200, msg=f"Route `{route_name}` is unavailable")

    def test_sidebar_contains_new_working_menu_links(self):
        """
        На любой странице нового меню должны быть ссылки на все рабочие разделы.
        """
        response = self.client.get(reverse("segments"), secure=True)
        self.assertEqual(response.status_code, 200)

        for route_name in ("dashboard", "segments", "guests", "focus_categories", "mailings", "reports"):
            self.assertContains(response, f'href="{reverse(route_name)}"', html=False)

    def test_sidebar_hides_legacy_block_for_regular_user(self):
        """
        Для обычного пользователя legacy-пункты должны быть скрыты.
        """
        response = self.client.get(reverse("segments"), secure=True)
        self.assertEqual(response.status_code, 200)

        self.assertNotContains(response, 'id="legacy-nav"', html=False)
        self.assertNotContains(response, f'href="{reverse("categories")}"', html=False)
        self.assertNotContains(response, f'href="{reverse("analytics_dashboard")}"', html=False)

    def test_sidebar_shows_legacy_block_for_staff_user(self):
        """
        Для staff должны отображаться прямые ссылки на legacy-разделы.
        """
        user_model = get_user_model()
        staff_user = user_model.objects.create_user(
            username="staff_nav_user",
            password="StrongPass123!",
            is_staff=True,
        )
        self.client.force_login(staff_user)

        response = self.client.get(reverse("segments"), secure=True)
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, 'id="legacy-nav"', html=False)
        self.assertContains(response, f'href="{reverse("categories")}"', html=False)
        self.assertContains(response, f'href="{reverse("analytics_dashboard")}"', html=False)
