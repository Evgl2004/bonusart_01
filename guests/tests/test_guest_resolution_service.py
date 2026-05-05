from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from guests.models import Guest
from guests.services.guest_resolution import (
    build_phone_variants,
    normalize_phone10,
    normalize_phone11,
    normalize_phone_e164,
    resolve_or_create_guest,
)


class GuestResolutionServiceTests(TestCase):
    def test_phone_normalization_helpers(self):
        self.assertEqual(normalize_phone11("+7 (999) 123-45-67"), "79991234567")
        self.assertEqual(normalize_phone10("8 (999) 123-45-67"), "9991234567")
        self.assertEqual(normalize_phone_e164("9991234567"), "+79991234567")
        self.assertEqual(
            build_phone_variants("9991234567"),
            {"+79991234567", "79991234567", "89991234567", "9991234567"},
        )

    def test_resolve_creates_guest_when_missing_and_allowed(self):
        result = resolve_or_create_guest(
            phone="+79994443322",
            iiko_id="iiko-new-1",
            first_name="New",
            allow_create=True,
            source="test",
        )

        self.assertIsNotNone(result.guest)
        self.assertTrue(result.created)
        self.assertEqual(result.duplicate_candidates, 0)
        self.assertEqual(Guest.objects.count(), 1)
        guest = Guest.objects.get()
        self.assertEqual(guest.phone, "+79994443322")
        self.assertEqual(guest.iiko_id, "iiko-new-1")
        self.assertEqual(guest.first_name, "New")

    def test_resolve_returns_none_when_creation_disabled(self):
        result = resolve_or_create_guest(
            phone="+79990000000",
            iiko_id="iiko-disabled",
            allow_create=False,
            source="test",
        )
        self.assertIsNone(result.guest)
        self.assertFalse(result.created)
        self.assertEqual(Guest.objects.count(), 0)

    def test_resolve_prefers_iiko_match_and_fills_missing_fields(self):
        now_value = timezone.now()
        guest = Guest.objects.create(
            iiko_id="iiko-777",
            phone=None,
            first_name="",
            created_at=now_value,
            updated_at=now_value,
        )
        duplicate_by_phone = Guest.objects.create(
            iiko_id=None,
            phone="+79991234567",
            first_name="Duplicate",
            created_at=now_value,
            updated_at=now_value,
        )

        result = resolve_or_create_guest(
            phone="+79991234567",
            iiko_id="iiko-777",
            first_name="Name",
            last_name="Surname",
            allow_create=True,
            source="test",
        )

        self.assertIsNotNone(result.guest)
        self.assertEqual(result.guest.id, guest.id)
        self.assertFalse(result.created)
        self.assertEqual(result.duplicate_candidates, 1)

        guest.refresh_from_db()
        duplicate_by_phone.refresh_from_db()
        self.assertEqual(guest.phone, "+79991234567")
        self.assertEqual(guest.first_name, "Name")
        self.assertEqual(guest.last_name, "Surname")
        self.assertEqual(duplicate_by_phone.first_name, "Duplicate")

    def test_resolve_is_idempotent_for_same_guest_across_formats(self):
        first = resolve_or_create_guest(
            phone="+7 (999) 222-11-00",
            iiko_id="iiko-shared-1",
            first_name="Guest",
            allow_create=True,
            source="test.first",
        )
        second = resolve_or_create_guest(
            phone="8 999 222 11 00",
            iiko_id="iiko-shared-1",
            last_name="User",
            allow_create=True,
            source="test.second",
        )
        third = resolve_or_create_guest(
            phone="9992221100",
            iiko_id=None,
            allow_create=True,
            source="test.third",
        )

        self.assertIsNotNone(first.guest)
        self.assertIsNotNone(second.guest)
        self.assertIsNotNone(third.guest)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertFalse(third.created)
        self.assertEqual(first.guest.id, second.guest.id)
        self.assertEqual(first.guest.id, third.guest.id)
        self.assertEqual(Guest.objects.count(), 1)
