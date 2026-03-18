"""
Тесты helper- и runner-логики модуля notification_scenarios.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from guests.models import MessageTemplate, NotificationScenario
from guests.services import notification_scenarios
from guests.services.notification_registry import (
    SCENARIO_CODE_INACTIVE_30D_COUPON,
    SCENARIO_CODE_INACTIVE_7D,
)


class NotificationScenarioHelpersTests(SimpleTestCase):
    """
    Тесты приватных helper-функций notification_scenarios.
    """

    @staticmethod
    def _scenario(
        *,
        code: str = SCENARIO_CODE_INACTIVE_7D,
        settings: dict | None = None,
        tz_name: str = "Asia/Yekaterinburg",
    ):
        return SimpleNamespace(code=code, settings=settings or {}, timezone=tz_name)

    def test_resolve_zoneinfo_fallback_to_utc_when_all_invalid(self):
        """
        При невалидных timezone в сценарии и Django окружении должен вернуться UTC.
        """
        fake_tz = SimpleNamespace(key="Bad/Timezone")
        with patch("guests.services.notification_scenarios.timezone.get_current_timezone", return_value=fake_tz):
            zone = notification_scenarios._resolve_zoneinfo("Invalid/Zone")
        self.assertEqual(getattr(zone, "key", str(zone)), "UTC")

    def test_default_inactive_days_for_known_and_unknown_codes(self):
        """
        Для inactive_7d и unknown возвращается 7, для inactive_30d_coupon — 30.
        """
        self.assertEqual(notification_scenarios._default_inactive_days_for_code(SCENARIO_CODE_INACTIVE_7D), 7)
        self.assertEqual(notification_scenarios._default_inactive_days_for_code(SCENARIO_CODE_INACTIVE_30D_COUPON), 30)
        self.assertEqual(notification_scenarios._default_inactive_days_for_code("unknown_code"), 7)

    def test_extract_inactive_days_handles_invalid_and_non_positive_values(self):
        """
        _extract_inactive_days должен падать в default при невалидных/неположительных значениях.
        """
        scenario_valid = self._scenario(settings={"inactive_days": "14"})
        scenario_invalid = self._scenario(settings={"inactive_days": "abc"})
        scenario_non_positive = self._scenario(settings={"inactive_days": 0})

        self.assertEqual(notification_scenarios._extract_inactive_days(scenario_valid), 14)
        self.assertEqual(notification_scenarios._extract_inactive_days(scenario_invalid), 7)
        self.assertEqual(notification_scenarios._extract_inactive_days(scenario_non_positive), 7)

    def test_is_coupon_required_default_and_override(self):
        """
        coupon_required по умолчанию true только для 30d-сценария.
        """
        scenario_30_default = self._scenario(code=SCENARIO_CODE_INACTIVE_30D_COUPON, settings={})
        scenario_7_default = self._scenario(code=SCENARIO_CODE_INACTIVE_7D, settings={})
        scenario_30_override_false = self._scenario(
            code=SCENARIO_CODE_INACTIVE_30D_COUPON,
            settings={"coupon_required": False},
        )

        self.assertTrue(notification_scenarios._is_coupon_required(scenario_30_default))
        self.assertFalse(notification_scenarios._is_coupon_required(scenario_7_default))
        self.assertFalse(notification_scenarios._is_coupon_required(scenario_30_override_false))

    def test_parse_coupon_expires_at_variants(self):
        """
        Проверяем ветки parse для datetime/строки/невалидного значения.
        """
        naive_dt = datetime(2026, 3, 18, 10, 0, 0)
        aware_dt = timezone.now()
        from_naive_dt = notification_scenarios._parse_coupon_expires_at(naive_dt)
        from_aware_dt = notification_scenarios._parse_coupon_expires_at(aware_dt)
        from_naive_string = notification_scenarios._parse_coupon_expires_at("2026-03-18T11:00:00")
        from_invalid = notification_scenarios._parse_coupon_expires_at("not-a-date")

        self.assertTrue(timezone.is_aware(from_naive_dt))
        self.assertEqual(from_aware_dt, aware_dt)
        self.assertTrue(timezone.is_aware(from_naive_string))
        self.assertIsNone(from_invalid)

    def test_build_coupon_payload_prefers_resolver_then_settings_then_empty(self):
        """
        Порядок источников купона: resolver -> settings.coupon_payload -> {}.
        """
        guest = SimpleNamespace(id=1)
        scenario_with_settings = self._scenario(
            settings={"coupon_payload": {"coupon_code": "SET-1", "coupon_external_id": "E1"}}
        )
        scenario_empty = self._scenario(settings={})

        from_resolver = notification_scenarios._build_coupon_payload(
            guest=guest,
            scenario=scenario_with_settings,
            coupon_resolver=lambda _g, _s: {"coupon_code": "RES-1"},
        )
        from_settings = notification_scenarios._build_coupon_payload(
            guest=guest,
            scenario=scenario_with_settings,
            coupon_resolver=lambda _g, _s: "invalid-type",
        )
        empty_payload = notification_scenarios._build_coupon_payload(
            guest=guest,
            scenario=scenario_empty,
            coupon_resolver=None,
        )

        self.assertEqual(from_resolver["coupon_code"], "RES-1")
        self.assertEqual(from_settings["coupon_code"], "SET-1")
        self.assertEqual(empty_payload, {})

    def test_build_fallback_message_branches(self):
        """
        Для купонного и обычного сценария fallback-текст должен отличаться.
        """
        coupon_scenario = self._scenario(code=SCENARIO_CODE_INACTIVE_30D_COUPON)
        regular_scenario = self._scenario(code=SCENARIO_CODE_INACTIVE_7D)

        with_coupon = notification_scenarios._build_fallback_message(
            scenario=coupon_scenario,
            days_without_visits=30,
            coupon_code="CPN-123",
        )
        without_coupon = notification_scenarios._build_fallback_message(
            scenario=coupon_scenario,
            days_without_visits=30,
            coupon_code="",
        )
        regular = notification_scenarios._build_fallback_message(
            scenario=regular_scenario,
            days_without_visits=7,
            coupon_code="",
        )

        self.assertIn("персональный купон", with_coupon)
        self.assertNotIn("персональный купон", without_coupon)
        self.assertIn("7 дней", regular)

    def test_local_bucket_date_iso_uses_scenario_timezone(self):
        """
        Дата дедупликации должна считаться в timezone сценария.
        """
        scenario = self._scenario(tz_name="Asia/Yekaterinburg")
        now_utc = datetime(2026, 3, 17, 22, 30, 0, tzinfo=dt_timezone.utc)

        bucket = notification_scenarios._local_bucket_date_iso(scenario=scenario, now=now_utc)
        self.assertEqual(bucket, "2026-03-18")


class NotificationScenarioRunnerBranchesTests(TestCase):
    """
    Тесты веток раннера run_scheduled_inactive_scenario(s).
    """

    def setUp(self):
        super().setUp()
        self.template = MessageTemplate.objects.create(
            name="SCENARIO_TEMPLATE_TEST",
            description="Тестовый шаблон",
            message_text="Мы скучаем по вам, {first_name}",
            created_by="tests",
            is_active=True,
        )

    def _prepare_schedule_scenario(self, code: str, *, settings: dict | None = None) -> NotificationScenario:
        scenario, _ = NotificationScenario.objects.get_or_create(
            code=code,
            defaults={
                "name": f"Scenario {code}",
                "description": "Тестовый scenario",
                "is_active": True,
                "is_system": True,
                "trigger_type": NotificationScenario.TriggerType.SCHEDULE,
                "template": self.template,
                "priority": NotificationScenario.Priority.NORMAL,
                "target_mode": NotificationScenario.TargetMode.PRIMARY_ONLY,
                "distribution_mode": NotificationScenario.DistributionMode.IMMEDIATE,
                "timezone": "Asia/Yekaterinburg",
                "cooldown_minutes": 0,
                "max_per_day_per_guest": None,
                "settings": settings or {},
            },
        )
        scenario.is_active = True
        scenario.trigger_type = NotificationScenario.TriggerType.SCHEDULE
        scenario.template = self.template
        scenario.settings = settings or {}
        scenario.timezone = "Asia/Yekaterinburg"
        scenario.save(
            update_fields=[
                "is_active",
                "trigger_type",
                "template",
                "settings",
                "timezone",
                "updated_at",
            ]
        )
        return scenario

    def test_run_scheduled_inactive_scenario_empty_code_returns_empty_stat(self):
        """
        Пустой код сценария должен приводить к пустой статистике без ошибок.
        """
        stat = notification_scenarios.run_scheduled_inactive_scenario(scenario_code="   ")
        self.assertEqual(stat.scenario_code, "")
        self.assertEqual(stat.created_tasks, 0)

    def test_run_scheduled_inactive_scenario_unknown_code_returns_default_stat(self):
        """
        Для неизвестного сценария раннер возвращает stat с default threshold.
        """
        stat = notification_scenarios.run_scheduled_inactive_scenario(scenario_code="unknown_schedule_code")
        self.assertEqual(stat.scenario_code, "unknown_schedule_code")
        self.assertEqual(stat.inactive_days_threshold, 7)
        self.assertEqual(stat.created_tasks, 0)

    def test_run_scheduled_inactive_scenario_skips_missing_last_visit_and_recent_guest(self):
        """
        Кандидаты без last_visit_at и с слишком свежим визитом должны пропускаться.
        """
        scenario = self._prepare_schedule_scenario(
            SCENARIO_CODE_INACTIVE_7D,
            settings={"inactive_days": 7, "coupon_required": False},
        )
        now = timezone.now()
        candidates = [
            SimpleNamespace(id=1, first_name="NoVisit", last_visit_at=None),
            SimpleNamespace(id=2, first_name="Recent", last_visit_at=now - timedelta(days=1)),
        ]

        with (
            patch("guests.services.notification_scenarios._collect_candidate_guests", return_value=candidates),
            patch("guests.services.notification_scenarios.enqueue_notification_event_from_scenario") as mocked_enqueue,
        ):
            stat = notification_scenarios.run_scheduled_inactive_scenario(
                scenario_code=scenario.code,
                limit_per_scenario=100,
                now=now,
            )

        self.assertEqual(stat.scanned_guests, 2)
        self.assertEqual(stat.matched_guests, 0)
        self.assertEqual(stat.created_tasks, 0)
        mocked_enqueue.assert_not_called()

    def test_run_scheduled_inactive_scenario_coupon_required_without_coupon_is_skipped(self):
        """
        Если coupon_required=true и купона нет, задача не должна ставиться.
        """
        scenario = self._prepare_schedule_scenario(
            SCENARIO_CODE_INACTIVE_30D_COUPON,
            settings={"inactive_days": 30, "coupon_required": True},
        )
        now = timezone.now()
        candidates = [SimpleNamespace(id=10, first_name="CouponGuest", last_visit_at=now - timedelta(days=45))]

        with (
            patch("guests.services.notification_scenarios._collect_candidate_guests", return_value=candidates),
            patch("guests.services.notification_scenarios.enqueue_notification_event_from_scenario") as mocked_enqueue,
        ):
            stat = notification_scenarios.run_scheduled_inactive_scenario(
                scenario_code=scenario.code,
                limit_per_scenario=100,
                now=now,
            )

        self.assertEqual(stat.scanned_guests, 1)
        self.assertEqual(stat.matched_guests, 1)
        self.assertEqual(stat.skipped_without_coupon, 1)
        self.assertEqual(stat.created_tasks, 0)
        mocked_enqueue.assert_not_called()

    def test_run_scheduled_inactive_scenario_counts_duplicates_when_enqueue_returns_zero(self):
        """
        created_tasks=0 из enqueue должен учитываться как duplicate/no-target.
        """
        scenario = self._prepare_schedule_scenario(
            SCENARIO_CODE_INACTIVE_7D,
            settings={"inactive_days": 7, "coupon_required": False},
        )
        now = timezone.now()
        candidates = [SimpleNamespace(id=20, first_name="DupGuest", last_visit_at=now - timedelta(days=10))]

        with (
            patch("guests.services.notification_scenarios._collect_candidate_guests", return_value=candidates),
            patch("guests.services.notification_scenarios.enqueue_notification_event_from_scenario", return_value=0),
        ):
            stat = notification_scenarios.run_scheduled_inactive_scenario(
                scenario_code=scenario.code,
                limit_per_scenario=100,
                now=now,
            )

        self.assertEqual(stat.scanned_guests, 1)
        self.assertEqual(stat.matched_guests, 1)
        self.assertEqual(stat.created_tasks, 0)
        self.assertEqual(stat.skipped_duplicate_or_no_targets, 1)

    def test_run_scheduled_inactive_scenarios_returns_empty_for_blank_codes(self):
        """
        run_scheduled_inactive_scenarios должен вернуть {} при пустом списке code после нормализации.
        """
        result = notification_scenarios.run_scheduled_inactive_scenarios(
            scenario_codes=["", "   "],
            limit_per_scenario=100,
        )
        self.assertEqual(result, {})
