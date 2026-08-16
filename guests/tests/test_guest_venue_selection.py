"""
Тесты отбора гостей по связи с заведением для разовых рассылок.
"""

from __future__ import annotations

from datetime import date

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

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
        self.assertEqual(
            result.guest_ids,
            (self.guest_favorite_sami.id, self.guest_sami_only.id),
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
        self.assertEqual(
            result.guest_ids,
            (self.guest_last_sami.id, self.guest_sami_only.id),
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

    def test_date_period_is_applied_to_competing_venues_before_favorite_ranking(self):
        """
        Оконный режим должен сравнивать заведения только по фактам выбранного
        периода, включая конкурирующие заведения в первом ORM-запросе.
        """
        result = build_guest_venue_selection(
            department_id=self.venue_sami,
            selection_mode=VENUE_SELECTION_FAVORITE,
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 10),
            guest_ids=[self.guest_last_sami.id],
            limit_enabled=False,
        )

        self.assertEqual(result.guest_ids, (self.guest_last_sami.id,))

    def test_guest_ids_limit_is_applied_before_venue_calculation(self):
        """
        Ограничение по аудитории должно применяться до группировки дневных фактов,
        чтобы исторический режим не рассчитывал посторонних гостей.
        """
        selected = build_guest_venue_selection(
            department_id=self.venue_sami,
            selection_mode=VENUE_SELECTION_VISITED_ONCE,
            guest_ids=[self.guest_favorite_sami.id],
            limit_enabled=False,
        )
        empty = build_guest_venue_selection(
            department_id=self.venue_sami,
            selection_mode=VENUE_SELECTION_VISITED_ONCE,
            guest_ids=[],
            limit_enabled=False,
        )

        self.assertEqual(selected.guest_ids, (self.guest_favorite_sami.id,))
        self.assertEqual(empty.guest_ids, ())

    def test_last_visit_same_date_uses_orders_as_deterministic_tie_breaker(self):
        """
        Если точное время неизвестно и даты совпали, последнее заведение
        определяется по числу заказов, а не зависит от порядка строк в базе.
        """
        same_day_guest = Guest.objects.create(phone="+79990000006", first_name="Один день")
        self._daily(
            same_day_guest,
            self.venue_sami,
            date(2026, 6, 13),
            orders_count=1,
        )
        self._daily(
            same_day_guest,
            self.venue_other,
            date(2026, 6, 13),
            orders_count=2,
        )

        sami_result = build_guest_venue_selection(
            department_id=self.venue_sami,
            selection_mode=VENUE_SELECTION_LAST_VISIT,
            guest_ids=[same_day_guest.id],
            limit_enabled=False,
        )
        other_result = build_guest_venue_selection(
            department_id=self.venue_other,
            selection_mode=VENUE_SELECTION_LAST_VISIT,
            guest_ids=[same_day_guest.id],
            limit_enabled=False,
        )

        self.assertEqual(sami_result.guest_ids, ())
        self.assertEqual(other_result.guest_ids, (same_day_guest.id,))

    def test_full_tie_uses_department_id_as_final_tie_breaker(self):
        """
        Полное равенство агрегатов должно разрешаться одинаково при каждом
        запуске с помощью идентификатора заведения.
        """
        full_tie_guest = Guest.objects.create(phone="+79990000007", first_name="Полная ничья")
        for department_id in (self.venue_other, self.venue_sami):
            self._daily(
                full_tie_guest,
                department_id,
                date(2026, 6, 14),
                orders_count=2,
            )

        for selection_mode in (VENUE_SELECTION_FAVORITE, VENUE_SELECTION_LAST_VISIT):
            result = build_guest_venue_selection(
                department_id=self.venue_sami,
                selection_mode=selection_mode,
                guest_ids=[full_tie_guest.id],
                limit_enabled=False,
            )
            self.assertEqual(result.guest_ids, (full_tie_guest.id,))

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

    def test_favorite_limit_keeps_guests_with_most_orders_first(self):
        """
        Лимит режима «Любимое заведение» должен оставлять наиболее активных
        гостей, а не строки с минимальным числом заказов.
        """
        most_active_guest = Guest.objects.create(
            phone="+79990000008",
            first_name="Самый активный",
        )
        self._daily(
            most_active_guest,
            self.venue_sami,
            date(2026, 6, 15),
            orders_count=10,
        )

        with CaptureQueriesContext(connection) as captured_queries:
            result = build_guest_venue_selection(
                department_id=self.venue_sami,
                selection_mode=VENUE_SELECTION_FAVORITE,
                limit_enabled=True,
                limit_value=1,
            )

        self.assertEqual(result.guest_ids, (most_active_guest.id,))
        self.assertEqual(result.total_before_limit, 3)
        self.assertTrue(result.truncated)
        fact_table_name = GuestRestaurantDailyOrderFact._meta.db_table
        selection_queries = [
            query["sql"]
            for query in captured_queries.captured_queries
            if fact_table_name in query["sql"]
        ]
        self.assertEqual(
            len(selection_queries),
            2,
            msg="Ожидаются COUNT и ограниченный оконный запрос без N+1.",
        )
        count_query, limited_query = (query.upper() for query in selection_queries)
        self.assertIn(
            "ORDER BY",
            count_query,
            msg="Оконное ранжирование должно сортировать заведения в СУБД.",
        )
        self.assertNotIn(
            "LIMIT 1",
            count_query,
            msg="Подсчёт полной аудитории не должен применять пользовательский лимит.",
        )
        self.assertIn(
            "ORDER BY",
            limited_query,
            msg="Финальная аудитория должна сортироваться в СУБД.",
        )
        self.assertIn(
            "LIMIT 1",
            limited_query,
            msg="Пользовательский лимит должен применяться финальным ORM-запросом.",
        )
        fact_table_from = f'FROM "{fact_table_name.upper()}"'
        for query in (count_query, limited_query):
            self.assertIn("ROW_NUMBER() OVER", query)
            self.assertIn("FIRST_VALUE(", query)
            self.assertEqual(
                query.count(fact_table_from),
                1,
                msg="Оконный запрос не должен повторно читать таблицу через корреляцию.",
            )

    def test_competitive_modes_use_one_window_query_without_n_plus_one(self):
        """
        Без внутреннего лимита оба конкурентных режима должны выполняться одним
        оконным запросом без N+1 и повторного чтения таблицы через корреляцию.
        """

        fact_table_name = GuestRestaurantDailyOrderFact._meta.db_table
        for selection_mode in (VENUE_SELECTION_FAVORITE, VENUE_SELECTION_LAST_VISIT):
            with self.subTest(selection_mode=selection_mode):
                with CaptureQueriesContext(connection) as captured_queries:
                    build_guest_venue_selection(
                        department_id=self.venue_sami,
                        selection_mode=selection_mode,
                        limit_enabled=False,
                    )

                selection_queries = [
                    query["sql"].upper()
                    for query in captured_queries.captured_queries
                    if fact_table_name in query["sql"]
                ]
                self.assertEqual(len(selection_queries), 1)
                window_query = selection_queries[0]
                self.assertIn("ORDER BY", window_query)
                self.assertIn("ROW_NUMBER() OVER", window_query)
                self.assertIn("FIRST_VALUE(", window_query)
                self.assertEqual(
                    window_query.count(f'FROM "{fact_table_name.upper()}"'),
                    1,
                )

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
