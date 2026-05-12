from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from django.db import transaction
from django.utils import timezone as dj_timezone

from guests.models import (
    BotProfile,
    Guest,
    GuestBotBinding,
    VtelemaxRecipientChannel,
)
from guests.services.guest_resolution import resolve_or_create_guest

logger = logging.getLogger(__name__)

_SUPPORTED_PLATFORMS = {"telegram", "max", "vk"}


def _parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_uuid(raw_value: Any) -> uuid.UUID | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except (TypeError, ValueError):
        return None


def _parse_rfc3339_utc(raw_value: Any) -> datetime | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_birthdate(raw_value: Any) -> date | None:
    text = str(raw_value or "").strip()
    if not text:
        return None

    normalized = text.replace("/", ".")
    for parser in (
        lambda s: date.fromisoformat(s),
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).date(),
        lambda s: datetime.strptime(s, "%d.%m.%Y").date(),
        lambda s: datetime.strptime(s, "%Y.%m.%d").date(),
    ):
        try:
            return parser(normalized)
        except (ValueError, TypeError):
            continue
    return None


def _pick_first_str(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalize_phone11(raw_value: Any) -> str | None:
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


def _phone10_from_phone(raw_value: Any) -> str | None:
    phone11 = _normalize_phone11(raw_value)
    if not phone11:
        return None
    return phone11[-10:]


def _is_valid_channel_for_guest_creation(
    *,
    phone_e164: str | None,
    external_id: str | None,
    notifications_allowed: bool,
    is_registered: bool,
) -> bool:
    """
    Строгий контракт автосоздания гостя из vtelemax.

    Создание разрешается только для валидного канала:
    1. канал зарегистрирован;
    2. есть согласие на уведомления;
    3. задан внешний идентификатор канала;
    4. телефон нормализуется до RU-11 формата.
    """
    return bool(
        is_registered
        and notifications_allowed
        and external_id
        and _normalize_phone11(phone_e164)
    )


@dataclass(slots=True)
class VtelemaxApplyStats:
    rows_total: int = 0
    rows_created: int = 0
    rows_updated: int = 0
    rows_skipped_invalid: int = 0
    rows_guest_unresolved: int = 0
    rows_binding_created: int = 0
    rows_binding_updated: int = 0
    rows_binding_disabled: int = 0


class VtelemaxRecipientsApplyService:
    """
    Применяет страницы `snapshot/delta` из vtelemax к локальным моделям SAGUR.
    """

    def __init__(
        self,
        *,
        bot_code_telegram: str = "",
        bot_code_max: str = "",
        bot_code_vk: str = "",
        create_missing_guests: bool = False,
    ):
        self.create_missing_guests = bool(create_missing_guests)
        self.bot_code_by_platform = {
            "telegram": str(bot_code_telegram or "").strip(),
            "max": str(bot_code_max or "").strip(),
            "vk": str(bot_code_vk or "").strip(),
        }
        self._bot_cache: dict[str, BotProfile | None] = {}
        self._guest_map_built = False
        self._guest_by_phone10: dict[str, Guest] = {}

    def apply_items(
        self,
        *,
        items: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> VtelemaxApplyStats:
        stats = VtelemaxApplyStats()
        for item in items:
            stats.rows_total += 1
            row_result = self._apply_one(item=item, dry_run=dry_run)
            stats.rows_created += row_result.rows_created
            stats.rows_updated += row_result.rows_updated
            stats.rows_skipped_invalid += row_result.rows_skipped_invalid
            stats.rows_guest_unresolved += row_result.rows_guest_unresolved
            stats.rows_binding_created += row_result.rows_binding_created
            stats.rows_binding_updated += row_result.rows_binding_updated
            stats.rows_binding_disabled += row_result.rows_binding_disabled
        return stats

    def _apply_one(
        self,
        *,
        item: dict[str, Any],
        dry_run: bool,
    ) -> VtelemaxApplyStats:
        stats = VtelemaxApplyStats(rows_total=1)
        if not isinstance(item, dict):
            stats.rows_skipped_invalid = 1
            return stats

        person_id = _parse_uuid(item.get("person_id"))
        platform = str(item.get("platform") or "").strip().lower()
        if person_id is None or platform not in _SUPPORTED_PLATFORMS:
            stats.rows_skipped_invalid = 1
            return stats

        phone_e164 = str(item.get("phone_e164") or "").strip() or None
        external_id = str(item.get("external_id") or "").strip() or None
        profile = item.get("profile") if isinstance(item.get("profile"), dict) else {}
        first_name = _pick_first_str(item, "first_name", "firstName", "name") or _pick_first_str(
            profile,
            "first_name",
            "firstName",
            "name",
        )
        last_name = _pick_first_str(item, "last_name", "lastName", "surname") or _pick_first_str(
            profile,
            "last_name",
            "lastName",
            "surname",
        )
        email = _pick_first_str(item, "email") or _pick_first_str(profile, "email")
        gender = _pick_first_str(item, "gender", "sex") or _pick_first_str(profile, "gender", "sex")
        birthdate = _parse_birthdate(
            item.get("birthdate")
            or item.get("birthday")
            or item.get("date_of_birth")
            or item.get("dateOfBirth")
            or profile.get("birthdate")
            or profile.get("birthday")
            or profile.get("date_of_birth")
            or profile.get("dateOfBirth")
        )
        rules_accepted = _parse_bool(item.get("rules_accepted"), default=False)
        notifications_allowed = _parse_bool(item.get("notifications_allowed"), default=False)
        is_registered = _parse_bool(item.get("is_registered"), default=False)
        allow_guest_create_by_channel = _is_valid_channel_for_guest_creation(
            phone_e164=phone_e164,
            external_id=external_id,
            notifications_allowed=notifications_allowed,
            is_registered=is_registered,
        )
        registered_at = _parse_rfc3339_utc(item.get("registered_at"))
        state_updated_at = _parse_rfc3339_utc(item.get("state_updated_at"))
        account_created_at = _parse_rfc3339_utc(item.get("account_created_at"))
        effective_updated_at = _parse_rfc3339_utc(item.get("effective_updated_at"))
        if effective_updated_at is None:
            effective_updated_at = max(
                [x for x in (registered_at, state_updated_at, account_created_at) if x is not None],
                default=None,
            )

        guest = self._resolve_guest_by_phone(
            phone_e164=phone_e164,
            first_name=first_name,
            last_name=last_name,
            email=email,
            gender=gender,
            birthdate=birthdate,
            allow_guest_create_by_channel=allow_guest_create_by_channel,
            dry_run=dry_run,
        )
        if guest is None and allow_guest_create_by_channel:
            stats.rows_guest_unresolved = 1

        bot_profile = self._resolve_bot_for_platform(platform=platform)

        if dry_run:
            return stats

        with transaction.atomic():
            channel_defaults = {
                "phone_e164": phone_e164,
                "external_id": external_id,
                "rules_accepted": rules_accepted,
                "notifications_allowed": notifications_allowed,
                "is_registered": is_registered,
                "registered_at": registered_at,
                "state_updated_at": state_updated_at,
                "account_created_at": account_created_at,
                "effective_updated_at": effective_updated_at,
                "guest": guest,
                "source_payload": item,
            }
            channel, channel_created = VtelemaxRecipientChannel.objects.get_or_create(
                person_id=person_id,
                platform=platform,
                defaults=channel_defaults,
            )
            if channel_created:
                stats.rows_created = 1
            else:
                channel_changed = False
                for field_name, desired_value in channel_defaults.items():
                    if getattr(channel, field_name) != desired_value:
                        setattr(channel, field_name, desired_value)
                        channel_changed = True
                if channel_changed:
                    channel.save(
                        update_fields=[
                            "phone_e164",
                            "external_id",
                            "rules_accepted",
                            "notifications_allowed",
                            "is_registered",
                            "registered_at",
                            "state_updated_at",
                            "account_created_at",
                            "effective_updated_at",
                            "guest",
                            "source_payload",
                            "last_synced_at",
                        ]
                    )
                    stats.rows_updated = 1

            binding = self._upsert_binding_for_channel(
                channel=channel,
                platform=platform,
                guest=guest,
                bot_profile=bot_profile,
                external_id=external_id,
                notifications_allowed=notifications_allowed,
                is_registered=is_registered,
                stats=stats,
            )

            if channel.guest != guest or channel.guest_binding != binding:
                channel.guest = guest
                channel.guest_binding = binding
                channel.save(update_fields=["guest", "guest_binding", "last_synced_at"])

        return stats

    def _upsert_binding_for_channel(
        self,
        *,
        channel: VtelemaxRecipientChannel,
        platform: str,
        guest: Guest | None,
        bot_profile: BotProfile | None,
        external_id: str | None,
        notifications_allowed: bool,
        is_registered: bool,
        stats: VtelemaxApplyStats,
    ) -> GuestBotBinding | None:
        sending_allowed = bool(notifications_allowed and is_registered and external_id)

        if guest is None or bot_profile is None or not external_id:
            if channel.guest_binding_id:
                binding = channel.guest_binding
                if binding and (binding.is_active or binding.is_opt_in or not binding.is_stop_sending):
                    binding.is_active = False
                    binding.is_opt_in = bool(notifications_allowed)
                    binding.is_stop_sending = True
                    binding.save(update_fields=["is_active", "is_opt_in", "is_stop_sending", "updated_at"])
                    stats.rows_binding_disabled += 1
            return None

        binding_defaults = {
            "external_chat_id": external_id,
            "external_user_id": external_id,
            "is_active": sending_allowed,
            "is_opt_in": bool(notifications_allowed),
            "is_stop_sending": not sending_allowed,
            "is_primary": False,
        }
        binding, binding_created = GuestBotBinding.objects.get_or_create(
            guest=guest,
            bot=bot_profile,
            defaults=binding_defaults,
        )

        if binding_created:
            if not GuestBotBinding.objects.filter(guest=guest, is_primary=True).exclude(id=binding.id).exists():
                binding.is_primary = True
                binding.save(update_fields=["is_primary", "updated_at"])
            stats.rows_binding_created += 1
            return binding

        changed = False
        desired_values = {
            "external_chat_id": external_id,
            "external_user_id": external_id,
            "is_active": sending_allowed,
            "is_opt_in": bool(notifications_allowed),
            "is_stop_sending": not sending_allowed,
        }
        for field_name, desired_value in desired_values.items():
            if getattr(binding, field_name) != desired_value:
                setattr(binding, field_name, desired_value)
                changed = True
        if changed:
            binding.save(
                update_fields=[
                    "external_chat_id",
                    "external_user_id",
                    "is_active",
                    "is_opt_in",
                    "is_stop_sending",
                    "updated_at",
                ]
            )
            stats.rows_binding_updated += 1
        return binding

    def _resolve_bot_for_platform(self, *, platform: str) -> BotProfile | None:
        if platform in self._bot_cache:
            return self._bot_cache[platform]

        configured_code = self.bot_code_by_platform.get(platform, "")
        bot_qs = BotProfile.objects.filter(provider_type=platform, is_active=True)
        bot_profile: BotProfile | None
        if configured_code:
            bot_profile = bot_qs.filter(code=configured_code).order_by("id").first()
            if bot_profile is None:
                logger.warning(
                    "Vtelemax sync: bot profile code '%s' not found for platform=%s.",
                    configured_code,
                    platform,
                )
        else:
            bot_profile = bot_qs.order_by("id").first()

        self._bot_cache[platform] = bot_profile
        return bot_profile

    def _resolve_guest_by_phone(
        self,
        *,
        phone_e164: str | None,
        first_name: str | None,
        last_name: str | None,
        email: str | None,
        gender: str | None,
        birthdate: date | None,
        allow_guest_create_by_channel: bool,
        dry_run: bool,
    ) -> Guest | None:
        phone10 = _phone10_from_phone(phone_e164)
        if not phone10:
            return None

        self._ensure_guest_map()
        guest = self._guest_by_phone10.get(phone10)
        if guest is not None:
            if not dry_run:
                self._fill_guest_profile_if_empty(
                    guest=guest,
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    gender=gender,
                    birthdate=birthdate,
                )
            return guest

        resolved_existing = resolve_or_create_guest(
            phone=phone_e164,
            first_name=first_name,
            last_name=last_name,
            email=email,
            gender=gender,
            birthdate=birthdate,
            allow_create=False,
            source="vtelemax.sync",
        )
        if resolved_existing.guest is not None:
            self._guest_by_phone10[phone10] = resolved_existing.guest
            return resolved_existing.guest

        if not self.create_missing_guests or dry_run or not allow_guest_create_by_channel:
            return None

        resolved_created = resolve_or_create_guest(
            phone=phone_e164 or phone10,
            first_name=first_name,
            last_name=last_name,
            email=email,
            gender=gender,
            birthdate=birthdate,
            allow_create=True,
            source="vtelemax.sync",
        )
        if resolved_created.guest is not None:
            self._guest_by_phone10[phone10] = resolved_created.guest
            return resolved_created.guest
        return None

    def _ensure_guest_map(self) -> None:
        if self._guest_map_built:
            return

        mapping: dict[str, Guest] = {}
        queryset = Guest.objects.exclude(phone__isnull=True).exclude(phone="").only("id", "phone")
        for guest in queryset.iterator(chunk_size=1000):
            phone10 = _phone10_from_phone(guest.phone)
            if not phone10:
                continue
            current = mapping.get(phone10)
            if current is None or guest.id < current.id:
                mapping[phone10] = guest

        self._guest_by_phone10 = mapping
        self._guest_map_built = True

    @staticmethod
    def _fill_guest_profile_if_empty(
        *,
        guest: Guest,
        first_name: str | None,
        last_name: str | None,
        email: str | None,
        gender: str | None,
        birthdate: date | None,
    ) -> None:
        updated_fields: list[str] = []
        candidates = (
            ("first_name", first_name),
            ("last_name", last_name),
            ("email", email),
            ("gender", gender),
            ("birthdate", birthdate),
        )
        for field_name, value in candidates:
            if value in (None, ""):
                continue
            if getattr(guest, field_name) in (None, ""):
                setattr(guest, field_name, value)
                updated_fields.append(field_name)

        if not updated_fields:
            return

        guest.updated_at = dj_timezone.now()
        guest.save(update_fields=updated_fields + ["updated_at"])
