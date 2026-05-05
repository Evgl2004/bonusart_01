from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import connection, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from guests.models import Guest

logger = logging.getLogger(__name__)


def normalize_phone11(raw_value: Any) -> str | None:
    digits = re.sub(r"\D+", "", str(raw_value or ""))
    if not digits:
        return None
    if len(digits) == 10:
        return "7" + digits
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return digits
    return None


def normalize_phone_e164(raw_value: Any) -> str | None:
    phone11 = normalize_phone11(raw_value)
    if not phone11:
        return None
    return f"+{phone11}"


def normalize_phone10(raw_value: Any) -> str | None:
    phone11 = normalize_phone11(raw_value)
    if not phone11:
        return None
    return phone11[-10:]


def build_phone_variants(raw_value: Any) -> set[str]:
    phone11 = normalize_phone11(raw_value)
    if not phone11:
        return set()

    phone10 = phone11[-10:]
    return {
        f"+{phone11}",
        phone11,
        "8" + phone10,
        phone10,
    }


@dataclass(slots=True)
class GuestResolveResult:
    guest: Guest | None
    created: bool
    duplicate_candidates: int


def _hash_lock_key(raw_key: str) -> int:
    digest = hashlib.sha1(raw_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False) % (2**63 - 1)


def _acquire_identity_locks(*, phone11: str | None, iiko_id: str | None) -> None:
    """
    На PostgreSQL блокируем ключи идентичности в рамках транзакции.
    Это снижает риск параллельного создания дублей при одновременных webhook/sync вызовах.
    """
    if connection.vendor != "postgresql":
        return

    lock_keys = set()
    if phone11:
        lock_keys.add(f"guest:phone11:{phone11}")
    if iiko_id:
        lock_keys.add(f"guest:iiko:{iiko_id}")

    if not lock_keys:
        return

    with connection.cursor() as cursor:
        for key in sorted(lock_keys):
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_hash_lock_key(key)])


def _guest_candidates_queryset(*, phone: str | None, iiko_id: str | None) -> QuerySet[Guest]:
    query = Q()
    if iiko_id:
        query |= Q(iiko_id=iiko_id)

    phone_variants = build_phone_variants(phone)
    if phone_variants:
        query |= Q(phone__in=phone_variants)
        # Fallback на совпадение последних 10 цифр для старых форматов хранения.
        phone10 = normalize_phone10(phone)
        if phone10:
            query |= Q(phone__endswith=phone10)

    if not query:
        return Guest.objects.none()

    return Guest.objects.filter(query).order_by("id")


def _pick_canonical_guest(
    *,
    candidates: list[Guest],
    phone: str | None,
    iiko_id: str | None,
) -> Guest | None:
    if not candidates:
        return None

    if iiko_id:
        by_iiko = [g for g in candidates if (g.iiko_id or "").strip() == iiko_id]
        if by_iiko:
            return sorted(by_iiko, key=lambda g: g.id)[0]

    phone10 = normalize_phone10(phone)
    if phone10:
        by_phone10 = []
        for guest in candidates:
            guest_phone10 = normalize_phone10(guest.phone)
            if guest_phone10 and guest_phone10 == phone10:
                by_phone10.append(guest)
        if by_phone10:
            return sorted(by_phone10, key=lambda g: g.id)[0]

    return candidates[0]


def resolve_or_create_guest(
    *,
    phone: str | None = None,
    iiko_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    gender: str | None = None,
    birthdate: date | str | None = None,
    allow_create: bool = True,
    source: str = "",
) -> GuestResolveResult:
    """
    Единая точка резолва гостя из внешних контуров (iiko, webhook, vtelemax).

    Стратегия:
    1) Ищем кандидатов по iiko_id и телефону.
    2) Выбираем канонического гостя детерминированно.
    3) Если не найден и разрешено создание — создаём нового.
    4) Дозаполняем пустые поля.
    """
    iiko_id_clean = str(iiko_id or "").strip() or None
    phone_e164 = normalize_phone_e164(phone)
    phone11 = normalize_phone11(phone)

    now_value = timezone.now()
    source_label = source.strip() or "unknown"

    with transaction.atomic():
        _acquire_identity_locks(phone11=phone11, iiko_id=iiko_id_clean)

        candidates_qs = _guest_candidates_queryset(phone=phone_e164, iiko_id=iiko_id_clean).select_for_update()
        candidates = list(candidates_qs)
        guest = _pick_canonical_guest(candidates=candidates, phone=phone_e164, iiko_id=iiko_id_clean)
        created = False

        if guest is None:
            if not allow_create:
                return GuestResolveResult(guest=None, created=False, duplicate_candidates=0)

            guest = Guest.objects.create(
                phone=phone_e164,
                iiko_id=iiko_id_clean,
                first_name=first_name or "",
                last_name=last_name or "",
                email=email or "",
                gender=gender or None,
                birthdate=birthdate or None,
                created_at=now_value,
                updated_at=now_value,
            )
            created = True
        else:
            updated = False

            def _fill_if_empty(field_name: str, value: Any) -> None:
                nonlocal updated
                if value in (None, ""):
                    return
                current_value = getattr(guest, field_name)
                if current_value in (None, ""):
                    setattr(guest, field_name, value)
                    updated = True

            _fill_if_empty("iiko_id", iiko_id_clean)
            _fill_if_empty("phone", phone_e164)
            _fill_if_empty("first_name", first_name)
            _fill_if_empty("last_name", last_name)
            _fill_if_empty("email", email)
            _fill_if_empty("gender", gender)
            _fill_if_empty("birthdate", birthdate)

            if updated:
                guest.updated_at = now_value
                guest.save()

        duplicate_count = max(0, len(candidates) - 1)
        if duplicate_count > 0:
            logger.warning(
                "Guest resolve (%s): найдено %s дублей-кандидатов для phone=%s iiko_id=%s, выбран guest_id=%s.",
                source_label,
                duplicate_count,
                phone_e164,
                iiko_id_clean,
                guest.id if guest else None,
            )

        return GuestResolveResult(guest=guest, created=created, duplicate_candidates=duplicate_count)
