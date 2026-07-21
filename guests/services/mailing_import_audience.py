"""
Отбор аудитории при импорте телефонов в обычную рассылку.

Сервис использует штатный планировщик доставки, чтобы состав импортированной
аудитории совпадал с фактическими правилами постановки сообщений в очередь.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from guests.models import BotProfile, GuestBotBinding, Mailing
from guests.services.historical_telegram import collect_registered_new_bot_guest_ids
from guests.services.mailing_delivery_targets import (
    CHANNEL_MODE_BINDING,
    CHANNEL_MODE_HISTORICAL_TELEGRAM,
    CHANNEL_MODE_LEGACY_TELEGRAM,
    build_mailing_delivery_plan,
)


MAILING_IMPORT_AUDIENCE_NEW_BOTS = "new_bots"
MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM = "historical_telegram"
MAILING_IMPORT_AUDIENCE_ALL_SENDABLE = "all_sendable"
MAILING_IMPORT_AUDIENCE_CHOICES = (
    (MAILING_IMPORT_AUDIENCE_NEW_BOTS, "Гости новых ботов"),
    (
        MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM,
        "Исторические Telegram-гости (старый бот)",
    ),
    (
        MAILING_IMPORT_AUDIENCE_ALL_SENDABLE,
        "Все гости с доступным каналом отправки",
    ),
)
MAILING_IMPORT_AUDIENCE_LABELS = dict(MAILING_IMPORT_AUDIENCE_CHOICES)
DEFAULT_MAILING_IMPORT_AUDIENCE = MAILING_IMPORT_AUDIENCE_NEW_BOTS

HISTORICAL_CHANNEL_MODES = {
    CHANNEL_MODE_HISTORICAL_TELEGRAM,
    CHANNEL_MODE_LEGACY_TELEGRAM,
}


@dataclass(frozen=True)
class MailingImportAudienceSelection:
    """
    Результат классификации найденных гостей по доступным каналам.

    Идентификаторы хранятся в неизменяемых множествах, чтобы вызывающий код
    не мог случайно изменить результат после расчёта.
    """

    audience_group: str
    audience_group_label: str
    selected_guest_ids: frozenset[int]
    new_bot_guest_ids: frozenset[int]
    historical_guest_ids: frozenset[int]
    sendable_guest_ids: frozenset[int]
    without_sendable_channel_guest_ids: frozenset[int]
    excluded_by_audience_group_guest_ids: frozenset[int]
    file_telegram_external_id_guest_ids: frozenset[int]


def normalize_mailing_import_audience(value: str | None) -> str:
    """
    Возвращает поддерживаемую группу аудитории или безопасное значение по умолчанию.
    """

    normalized = str(value or "").strip().lower()
    if normalized in MAILING_IMPORT_AUDIENCE_LABELS:
        return normalized
    return DEFAULT_MAILING_IMPORT_AUDIENCE


def build_mailing_import_audience_selection(
    guest_ids: Iterable[int],
    *,
    selected_bot_ids: Iterable[int],
    target_mode: str | None = Mailing.TargetMode.PRIMARY_ONLY,
    audience_group: str | None = DEFAULT_MAILING_IMPORT_AUDIENCE,
    telegram_external_ids: Mapping[int, str] | None = None,
) -> MailingImportAudienceSelection:
    """
    Классифицирует гостей и выбирает группу для добавления в кампанию.

    Новые и исторические каналы определяются штатным планировщиком рассылки.
    Telegram ID из файла остаётся резервным историческим адресом только тогда,
    когда планировщик не нашёл для гостя другого доступного канала и в кампании
    выбран активный Telegram-бот.
    """

    normalized_guest_ids = _ordered_unique_positive_ints(guest_ids)
    normalized_bot_ids = _ordered_unique_positive_ints(selected_bot_ids)
    normalized_audience_group = normalize_mailing_import_audience(audience_group)

    delivery_plan = build_mailing_delivery_plan(
        normalized_guest_ids,
        selected_bot_ids=normalized_bot_ids,
        target_mode=target_mode,
    )

    new_bot_guest_ids: set[int] = set()
    historical_guest_ids: set[int] = set()
    sendable_guest_ids = set(delivery_plan.deliverable_guest_ids)

    for row in delivery_plan.rows:
        channel_modes = set(row.channel_modes)
        if CHANNEL_MODE_BINDING in channel_modes:
            new_bot_guest_ids.add(int(row.guest_id))
        elif channel_modes & HISTORICAL_CHANNEL_MODES:
            historical_guest_ids.add(int(row.guest_id))

    file_telegram_external_id_guest_ids: set[int] = set()
    has_active_telegram_bot = BotProfile.objects.filter(
        id__in=normalized_bot_ids,
        is_active=True,
        provider_type=BotProfile.ProviderType.TELEGRAM,
    ).exists()
    if has_active_telegram_bot and telegram_external_ids:
        new_contour_guest_ids = collect_registered_new_bot_guest_ids(
            normalized_guest_ids
        )
        new_contour_guest_ids.update(
            int(guest_id)
            for guest_id in GuestBotBinding.objects.filter(
                guest_id__in=normalized_guest_ids,
                is_active=True,
            )
            .exclude(external_chat_id="")
            .values_list("guest_id", flat=True)
            .distinct()
        )
        file_telegram_external_id_guest_ids = {
            guest_id
            for guest_id in normalized_guest_ids
            if guest_id not in sendable_guest_ids
            and guest_id not in new_contour_guest_ids
            and str(telegram_external_ids.get(guest_id) or "").strip()
        }
        historical_guest_ids.update(file_telegram_external_id_guest_ids)
        sendable_guest_ids.update(file_telegram_external_id_guest_ids)

    if normalized_audience_group == MAILING_IMPORT_AUDIENCE_HISTORICAL_TELEGRAM:
        selected_guest_ids = historical_guest_ids
    elif normalized_audience_group == MAILING_IMPORT_AUDIENCE_ALL_SENDABLE:
        selected_guest_ids = sendable_guest_ids
    else:
        selected_guest_ids = new_bot_guest_ids

    all_input_guest_ids = set(normalized_guest_ids)
    return MailingImportAudienceSelection(
        audience_group=normalized_audience_group,
        audience_group_label=MAILING_IMPORT_AUDIENCE_LABELS[normalized_audience_group],
        selected_guest_ids=frozenset(selected_guest_ids),
        new_bot_guest_ids=frozenset(new_bot_guest_ids),
        historical_guest_ids=frozenset(historical_guest_ids),
        sendable_guest_ids=frozenset(sendable_guest_ids),
        without_sendable_channel_guest_ids=frozenset(
            all_input_guest_ids - sendable_guest_ids
        ),
        excluded_by_audience_group_guest_ids=frozenset(
            sendable_guest_ids - selected_guest_ids
        ),
        file_telegram_external_id_guest_ids=frozenset(
            file_telegram_external_id_guest_ids
        ),
    )


def _ordered_unique_positive_ints(values: Iterable[int]) -> tuple[int, ...]:
    """
    Нормализует идентификаторы, сохраняя порядок первого появления.
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
    return tuple(result)
