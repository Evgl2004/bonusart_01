from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from django.db.models import Q
from django.utils import timezone

from guests.models import BotProfile, Guest, HistoricalTelegramChannel, VtelemaxRecipientChannel


PHONE_DIGITS_RE = re.compile(r"\D+")


def normalize_phone10(value: str | None) -> str:
    """
    Возвращает последние 10 цифр телефона для сопоставления с vtelemax.
    """

    digits = PHONE_DIGITS_RE.sub("", str(value or ""))
    if len(digits) < 10:
        return ""
    return digits[-10:]


def ordered_unique_ints(values: Iterable[int | str | None]) -> list[int]:
    """
    Нормализует список идентификаторов без изменения порядка первого появления.
    """

    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            continue
        if normalized <= 0 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def collect_registered_new_bot_guest_ids(guest_ids: Iterable[int] | None = None) -> set[int]:
    """
    Возвращает гостей, у которых есть факт регистрации в новом контуре бота.

    Согласие на рассылку здесь не учитывается: если гость зарегистрировался,
    он уже не относится к исторической Telegram-аудитории.
    """

    normalized_guest_ids = ordered_unique_ints(guest_ids or [])
    result: set[int] = set()

    channel_scope = VtelemaxRecipientChannel.objects.filter(is_registered=True)
    if normalized_guest_ids:
        channel_scope = channel_scope.filter(
            Q(guest_id__in=normalized_guest_ids)
            | Q(phone_e164__isnull=False)
        )

    linked_guest_ids = (
        channel_scope.exclude(guest_id__isnull=True)
        .values_list("guest_id", flat=True)
        .distinct()
    )
    for guest_id in linked_guest_ids:
        normalized = int(guest_id)
        if not normalized_guest_ids or normalized in normalized_guest_ids:
            result.add(normalized)

    phone10_values = {
        normalize_phone10(phone)
        for phone in channel_scope.exclude(phone_e164__isnull=True)
        .exclude(phone_e164="")
        .values_list("phone_e164", flat=True)
    }
    phone10_values.discard("")
    if not phone10_values:
        return result

    guest_scope = Guest.objects.exclude(phone__isnull=True).exclude(phone="")
    if normalized_guest_ids:
        guest_scope = guest_scope.filter(id__in=normalized_guest_ids)

    for guest_id, phone in guest_scope.values_list("id", "phone").iterator(chunk_size=5000):
        if normalize_phone10(phone) in phone10_values:
            result.add(int(guest_id))
    return result


def is_guest_registered_in_new_bot(guest_id: int | None) -> bool:
    """
    Проверяет факт регистрации одного гостя в новом контуре бота.
    """

    if not guest_id:
        return False
    return int(guest_id) in collect_registered_new_bot_guest_ids([int(guest_id)])


def collect_sendable_historical_telegram_guest_ids(*, exclude_registered: bool = True) -> set[int]:
    """
    Возвращает гостей с рабочим историческим Telegram-каналом.
    """

    guest_ids = {
        int(guest_id)
        for guest_id in HistoricalTelegramChannel.objects.filter(
            delivery_state=HistoricalTelegramChannel.DeliveryState.SENDABLE,
            bot_profile__is_active=True,
            bot_profile__provider_type=BotProfile.ProviderType.TELEGRAM,
        )
        .exclude(telegram_chat_id="")
        .values_list("guest_id", flat=True)
        .distinct()
    }
    if exclude_registered and guest_ids:
        guest_ids -= collect_registered_new_bot_guest_ids(guest_ids)
    return guest_ids


def build_historical_telegram_channels_map(
    guest_ids: Iterable[int],
    *,
    selected_bot_ids: Iterable[int],
    exclude_registered: bool = True,
) -> dict[int, HistoricalTelegramChannel]:
    """
    Подбирает рабочие исторические Telegram-каналы для выбранных гостей.
    """

    normalized_guest_ids = ordered_unique_ints(guest_ids)
    normalized_bot_ids = set(ordered_unique_ints(selected_bot_ids))
    if not normalized_guest_ids or not normalized_bot_ids:
        return {}

    excluded_guest_ids: set[int] = set()
    if exclude_registered:
        excluded_guest_ids = collect_registered_new_bot_guest_ids(normalized_guest_ids)

    queryset = (
        HistoricalTelegramChannel.objects.select_related("bot_profile")
        .filter(
            guest_id__in=normalized_guest_ids,
            bot_profile_id__in=normalized_bot_ids,
            bot_profile__is_active=True,
            bot_profile__provider_type=BotProfile.ProviderType.TELEGRAM,
            delivery_state=HistoricalTelegramChannel.DeliveryState.SENDABLE,
        )
        .exclude(guest_id__in=excluded_guest_ids)
        .exclude(telegram_chat_id="")
        .order_by("guest_id", "-last_success_at", "-updated_at", "id")
    )

    result: dict[int, HistoricalTelegramChannel] = {}
    for channel in queryset:
        guest_id = int(channel.guest_id)
        if guest_id not in result:
            result[guest_id] = channel
    return result


def mark_historical_telegram_success(channel_id: int | None, *, sent_at: datetime | None = None) -> None:
    """
    Фиксирует успешную отправку через исторический Telegram-канал.
    """

    if not channel_id:
        return
    HistoricalTelegramChannel.objects.filter(id=int(channel_id)).update(
        delivery_state=HistoricalTelegramChannel.DeliveryState.SENDABLE,
        last_success_at=sent_at or timezone.now(),
        last_error_at=None,
        last_error_text=None,
        updated_at=timezone.now(),
    )


def mark_historical_telegram_error(
    channel_id: int | None,
    *,
    error_text: str,
    blocked: bool = False,
) -> None:
    """
    Фиксирует ошибку отправки через исторический Telegram-канал.
    """

    if not channel_id:
        return
    update_values = {
        "last_error_at": timezone.now(),
        "last_error_text": str(error_text or "")[:2000],
        "updated_at": timezone.now(),
    }
    if blocked:
        update_values["delivery_state"] = HistoricalTelegramChannel.DeliveryState.BLOCKED
    HistoricalTelegramChannel.objects.filter(id=int(channel_id)).update(**update_values)
