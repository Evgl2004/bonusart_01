"""
Тесты отбора гостей по связи с заведением для разовых рассылок.
"""

from __future__ import annotations

from datetime import date

from django.test import TestCase

from guests.models import Guest, GuestRestaurantDailyOrderFact
from guests.services.guest_venue_selection import (
    VENUE_SELECTION_FAVORITE,
    VENUE_SELECTION_LAST_VISIT,
    VENUE_SELECTION_VISITED_ONCE,
    build_guest_venue_selection,
)


class GuestVenueSelectionTests(TestCase):
    """
    Проверяем аудиторию по заведению без привязки к интерфейсу.
    """

    def setUp(self):
        self.venue_sami = "dep-sami"
        self.venue_other = "dep-other"

        self.guest_sami_only = Guest.objects.create(phone="+79990000001", first_name="Сами")
        self.guest_favorite_sami = Guest.objects.create(phone="+79990000002", first_name="Любимое")
        self.guest_last_sami = Guest.objects.create(phone="+79990000003", first_name="Последнее")
        self.guest_not_sami = Guest.objects.create(phone="+79990000004", first_name="Другое")
        self.guest_tie_other_newer = Guest.objects.create(phone="+79990000005", first_name="Равенство")

        self._daily(
            self.guest_sami_only,
            self.venue_sami,
            date(2026, 6, 1),
            orders_count=1,
        )

        self._daily(
            self.guest_favorite_sami,
            self.venue_sami,
            date(2026, 6, 2),
            orders_count=5,
        )
        self._daily(
            self.guest_favorite_sami,
            self.venue_other,
            date(2026, 6, 9),
            orders_count=2,
        )

        self._daily(
            self.guest_last_sami,
            self.venue_other,
            date(2026, 5, 25),
            orders_count=7,
        )
        self._daily(
            self.guest_last_sami,
            self.venue_sami,
            date(2026, 6, 10),
            orders_count=1,
        )

        self._daily(
            self.guest_not_sami,
            self.venue_other,
            date(2026, 6, 11),
            orders_count=3,
        )

        self._daily(
            self.guest_tie_other_newer,
            self.venue_sami,
            date(2026, 6, 3),
            orders_count=2,
        )
        self._daily(
            self.guest_tie_other_newer,
            self.venue_other,
            date(2026, 6, 12),
            orders_count=2,
        )

    def test_visited_once_returns_every_guest_with_selected_venue_orders(self):
        """
        Режим «был хотя бы 1 раз» берёт всех гостей с заказами в заведении.
        """

        result = build_guest_venue_selection(
            department_id=self.venue_sami,
            selection_mode=VENUE_SELECTION_VISITED_ONCE,
            limit_enabled=False,
        )

        self.assertEqual(result.total_before_limit, 4)
        self.assertEqual(
            set(result.guest_ids),
            {
                self.guest_sami_only.id,
                self.guest_favorite_sami.id,
                self.guest_last_sami.id,
                self.guest_tie_other_newer.id,
            },
        )

    def test_favorite_returns_only_guests_where_selected_venue_wins_by_orders(self):
        """
        Режим «любимое заведение» выбирает заведение по максимальному числу заказов.
        """

        result = build_guest_venue_selection(
            department_id=self.venue_sami,
            selection_mode=VENUE_SELECTION_FAVORITE,
            limit_enabled=False,
        )

        self.assertEqual(
            set(result.guest_ids),
            {
                self.guest_sami_only.id,
                self.guest_favorite_sami.id,
            },
        )
        self.assertNotIn(self.guest_last_sami.id, result.guest_ids)
        self.assertNotIn(self.guest_tie_other_newer.id, result.guest_ids)

    def test_last_visit_returns_only_guests_where_selected_venue_is_latest(self):
        """
        Режим «самое последнее посещение» выбирает последнее заведение гостя.
        """

        result = build_guest_venue_selection(
            department_id=self.venue_sami,
            selection_mode=VENUE_SELECTION_LAST_VISIT,
            limit_enabled=False,
        )

        self.assertEqual(
            set(result.guest_ids),
            {
                self.guest_sami_only.id,
                self.guest_last_sami.id,
            },
        )
        self.assertNotIn(self.guest_favorite_sami.id, result.guest_ids)

    def test_date_period_limits_source_facts(self):
        """
        Период применяется до выбора режима.
        """

        result = build_guest_venue_selection(
            department_id=self.venue_sami,
            selection_mode=VENUE_SELECTION_VISITED_ONCE,
            date_from=date(2026, 6, 10),
            date_to=date(2026, 6, 10),
            limit_enabled=False,
        )

        self.assertEqual(result.guest_ids, (self.guest_last_sami.id,))

    def test_limit_is_applied_only_when_enabled(self):
        """
        Лимит можно включать и отключать явно.
        """

        limited = build_guest_venue_selection(
            department_id=self.venue_sami,
            selection_mode=VENUE_SELECTION_VISITED_ONCE,
            limit_enabled=True,
            limit_value=2,
        )
        unlimited = build_guest_venue_selection(
            department_id=self.venue_sami,
            selection_mode=VENUE_SELECTION_VISITED_ONCE,
            limit_enabled=False,
            limit_value=2,
        )

        self.assertEqual(limited.total, 2)
        self.assertTrue(limited.truncated)
        self.assertEqual(unlimited.total, 4)
        self.assertFalse(unlimited.truncated)

    def _daily(
        self,
        guest: Guest,
        department_id: str,
        business_date: date,
        *,
        orders_count: int,
    ) -> GuestRestaurantDailyOrderFact:
        return GuestRestaurantDailyOrderFact.objects.create(
            guest=guest,
            department_id=department_id,
            business_date=business_date,
            orders_count=orders_count,
        )
