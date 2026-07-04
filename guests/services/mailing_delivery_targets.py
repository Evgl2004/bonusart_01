"""
Проверка доступности доставки для обычных рассылок.

Модуль держит ту же логику выбора целей, которую использует постановка
`MailingGuest` в `DispatchTask`: выбранные боты, активные привязки гостей,
согласие на сообщения и режим отправки.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from django.db.models import Max

from guests.models import BotProfile, GuestBotBinding, HistoricalTelegramChannel, Mailing, VtelemaxRecipientChannel
from guests.services.historical_telegram import build_historical_telegram_channels_map

SUPPORTED_PROVIDERS = ("telegram", "max", "vk")
CHANNEL_MODE_BINDING = "guest_bot_binding"
CHANNEL_MODE_HISTORICAL_TELEGRAM = "historical_telegram_channel"
CHANNEL_MODE_LEGACY_TELEGRAM = "legacy_vtelemax_channel"


@dataclass(frozen=True)
class MailingDeliveryRow:
    """
    Результат проверки доставки по одному гостю.
    """

    guest_id: int
    target_count: int
    providers: tuple[str, ...]
    bot_profile_ids: tuple[int, ...]
    bot_codes: tuple[str, ...]
    channel_modes: tuple[str, ...]


@dataclass(frozen=True)
class MailingDeliveryPlan:
    """
    Сводка доступности отправки для выбранной аудитории.
    """

    total_guests: int
    selected_bots_total: int
    active_selected_bots_total: int
    deliverable_guests: int
    blocked_without_bot_binding: int
    blocked_without_message_permission: int
    legacy_telegram_guests: int
    planned_dispatch_tasks: int
    rows: tuple[MailingDeliveryRow, ...]

    @property
    def deliverable_guest_ids(self) -> tuple[int, ...]:
        return tuple(row.guest_id for row in self.rows)


def normalize_mailing_target_mode(value: str | None) -> str:
    """
    Нормализует режим выбора целей обычной рассылки.
    """
    normalized = str(value or "").strip().lower()
    if normalized in {Mailing.TargetMode.PRIMARY_ONLY, Mailing.TargetMode.ALL_BOTS}:
        return normalized
    return Mailing.TargetMode.PRIMARY_ONLY


def build_mailing_delivery_plan(
    guest_ids: Iterable[int],
    *,
    selected_bot_ids: Iterable[int],
    target_mode: str | None = Mailing.TargetMode.PRIMARY_ONLY,
) -> MailingDeliveryPlan:
    """
    Считает, каким гостям из аудитории реально можно поставить задачу доставки.
    """
    ordered_guest_ids = _ordered_unique_ints(guest_ids)
    selected_bot_ids_tuple = _ordered_unique_ints(selected_bot_ids)
    active_selected_bots = list(
        BotProfile.objects.filter(id__in=selected_bot_ids_tuple, is_active=True).order_by("id")
    )
    active_selected_bot_ids = {int(bot.id) for bot in active_selected_bots}
    legacy_telegram_bot = _select_legacy_telegram_bot(active_selected_bots)

    total_guests = len(ordered_guest_ids)
    if not ordered_guest_ids or not active_selected_bot_ids:
        return MailingDeliveryPlan(
            total_guests=total_guests,
            selected_bots_total=len(selected_bot_ids_tuple),
            active_selected_bots_total=len(active_selected_bot_ids),
            deliverable_guests=0,
            blocked_without_bot_binding=total_guests,
            blocked_without_message_permission=0,
            legacy_telegram_guests=0,
            planned_dispatch_tasks=0,
            rows=(),
        )

    normalized_target_mode = normalize_mailing_target_mode(target_mode)
    base_bindings_map = build_guest_bot_bindings_map(
        ordered_guest_ids,
        selected_bot_ids=active_selected_bot_ids,
        require_message_permission=False,
    )
    new_bot_bound_guest_ids = _collect_new_bot_bound_guest_ids(ordered_guest_ids)
    historical_channels_map = build_historical_telegram_channels_map(
        ordered_guest_ids,
        selected_bot_ids=active_selected_bot_ids,
        exclude_registered=True,
    )
    legacy_channels_map = _build_legacy_telegram_channels_map(
        ordered_guest_ids,
        exclude_guest_ids=new_bot_bound_guest_ids,
    )

    rows: list[MailingDeliveryRow] = []
    blocked_without_bot_binding = 0
    blocked_without_message_permission = 0
    legacy_telegram_guests = 0
    planned_dispatch_tasks = 0

    for guest_id in ordered_guest_ids:
        base_bindings = base_bindings_map.get(guest_id, [])
        targets: list[dict[str, Any]] = []
        if base_bindings:
            permitted_bindings = [
                binding
                for binding in base_bindings
                if bool(binding.is_opt_in) and not bool(binding.is_stop_sending)
            ]
            if not permitted_bindings:
                blocked_without_message_permission += 1
                continue
            targets = build_targets_from_bindings(permitted_bindings, target_mode=normalized_target_mode)
        elif legacy_telegram_bot is not None and guest_id in historical_channels_map:
            historical_target = _build_historical_telegram_target(
                channel=historical_channels_map[guest_id],
            )
            if historical_target:
                targets = [historical_target]
                legacy_telegram_guests += 1
        elif legacy_telegram_bot is not None and guest_id in legacy_channels_map:
            legacy_target = _build_legacy_telegram_target(
                channel=legacy_channels_map[guest_id],
                telegram_bot=legacy_telegram_bot,
            )
            if legacy_target:
                targets = [legacy_target]
                legacy_telegram_guests += 1

        if not targets:
            blocked_without_bot_binding += 1
            continue

        planned_dispatch_tasks += len(targets)
        rows.append(
            MailingDeliveryRow(
                guest_id=guest_id,
                target_count=len(targets),
                providers=tuple(str(target["provider_type"]) for target in targets),
                bot_profile_ids=tuple(int(target["bot_profile"].id) for target in targets),
                bot_codes=tuple(str(target["bot_profile"].code) for target in targets),
                channel_modes=tuple(str(target.get("channel_mode") or CHANNEL_MODE_BINDING) for target in targets),
            )
        )

    return MailingDeliveryPlan(
        total_guests=total_guests,
        selected_bots_total=len(selected_bot_ids_tuple),
        active_selected_bots_total=len(active_selected_bot_ids),
        deliverable_guests=len(rows),
        blocked_without_bot_binding=blocked_without_bot_binding,
        blocked_without_message_permission=blocked_without_message_permission,
        legacy_telegram_guests=legacy_telegram_guests,
        planned_dispatch_tasks=planned_dispatch_tasks,
        rows=tuple(rows),
    )


def build_mailing_delivery_preview_state(
    guest_ids: Iterable[int],
    *,
    selected_bot_ids: Iterable[int] | None = None,
) -> dict[str, object]:
    """
    Готовит безопасные данные для предварительного расчёта доставки на форме.
    """
    ordered_guest_ids = _ordered_unique_ints(guest_ids)
    bots_queryset = BotProfile.objects.filter(is_active=True)
    if selected_bot_ids is not None:
        bots_queryset = bots_queryset.filter(id__in=_ordered_unique_ints(selected_bot_ids))
    active_bots = list(bots_queryset.order_by("provider_type", "name", "id"))
    active_bot_ids = [int(bot.id) for bot in active_bots]

    if not ordered_guest_ids:
        return {
            "guests": [],
            "bots": [
                _serialize_preview_bot(bot)
                for bot in active_bots
                if str(bot.provider_type or "").strip().lower() in SUPPORTED_PROVIDERS
            ],
        }
    if not active_bot_ids:
        return {
            "guests": [
                {
                    "guest_id": int(guest_id),
                    "new_bot_bound": False,
                    "legacy_telegram_available": False,
                    "bindings": [],
                }
                for guest_id in ordered_guest_ids
            ],
            "bots": [],
        }

    bindings_map = build_guest_bot_bindings_map(
        ordered_guest_ids,
        selected_bot_ids=active_bot_ids,
        require_message_permission=False,
    )
    new_bot_bound_guest_ids = _collect_new_bot_bound_guest_ids(ordered_guest_ids)
    historical_channels_map = build_historical_telegram_channels_map(
        ordered_guest_ids,
        selected_bot_ids=active_bot_ids,
        exclude_registered=True,
    )
    legacy_channels_map = _build_legacy_telegram_channels_map(
        ordered_guest_ids,
        exclude_guest_ids=new_bot_bound_guest_ids,
    )

    return {
        "guests": [
            {
                "guest_id": int(guest_id),
                "new_bot_bound": int(guest_id) in new_bot_bound_guest_ids,
                "legacy_telegram_available": (
                    int(guest_id) in historical_channels_map
                    or int(guest_id) in legacy_channels_map
                ),
                "bindings": [
                    {
                        "bot_profile_id": int(binding.bot_id),
                        "permitted": bool(binding.is_opt_in) and not bool(binding.is_stop_sending),
                    }
                    for binding in bindings_map.get(int(guest_id), [])
                    if int(binding.bot_id) in active_bot_ids
                ],
            }
            for guest_id in ordered_guest_ids
        ],
        "bots": [
            _serialize_preview_bot(bot)
            for bot in active_bots
            if str(bot.provider_type or "").strip().lower() in SUPPORTED_PROVIDERS
        ],
    }


def build_mailing_delivery_targets_map(
    guest_ids: Iterable[int],
    *,
    selected_bot_ids: Iterable[int],
    target_mode: str | None = Mailing.TargetMode.PRIMARY_ONLY,
) -> dict[int, list[dict[str, Any]]]:
    """
    Собирает реальные цели доставки для постановки обычной рассылки в очередь.

    Приоритет:
    1. новые привязки `GuestBotBinding`;
    2. проверенный исторический Telegram-канал, если новой привязки у гостя нет;
    3. старый запасной Telegram-канал из `VtelemaxRecipientChannel`.
    """
    ordered_guest_ids = _ordered_unique_ints(guest_ids)
    selected_bot_ids_tuple = _ordered_unique_ints(selected_bot_ids)
    active_selected_bots = list(
        BotProfile.objects.filter(id__in=selected_bot_ids_tuple, is_active=True).order_by("id")
    )
    active_selected_bot_ids = {int(bot.id) for bot in active_selected_bots}
    if not ordered_guest_ids or not active_selected_bot_ids:
        return {}

    normalized_target_mode = normalize_mailing_target_mode(target_mode)
    bindings_map = build_guest_bot_bindings_map(
        ordered_guest_ids,
        selected_bot_ids=active_selected_bot_ids,
        require_message_permission=True,
    )

    targets_map: dict[int, list[dict[str, Any]]] = {}
    for guest_id, bindings in bindings_map.items():
        targets = build_targets_from_bindings(bindings, target_mode=normalized_target_mode)
        if targets:
            targets_map[int(guest_id)] = targets

    legacy_telegram_bot = _select_legacy_telegram_bot(active_selected_bots)
    if legacy_telegram_bot is None:
        return targets_map

    new_bot_bound_guest_ids = _collect_new_bot_bound_guest_ids(ordered_guest_ids)
    historical_channels_map = build_historical_telegram_channels_map(
        ordered_guest_ids,
        selected_bot_ids=active_selected_bot_ids,
        exclude_registered=True,
    )
    legacy_channels_map = _build_legacy_telegram_channels_map(
        ordered_guest_ids,
        exclude_guest_ids=new_bot_bound_guest_ids,
    )
    for guest_id in ordered_guest_ids:
        if guest_id in targets_map:
            continue
        historical_channel = historical_channels_map.get(guest_id)
        if historical_channel is not None:
            historical_target = _build_historical_telegram_target(
                channel=historical_channel,
            )
            if historical_target:
                targets_map[guest_id] = [historical_target]
                continue
        channel = legacy_channels_map.get(guest_id)
        if channel is None:
            continue
        legacy_target = _build_legacy_telegram_target(
            channel=channel,
            telegram_bot=legacy_telegram_bot,
        )
        if legacy_target:
            targets_map[guest_id] = [legacy_target]

    return targets_map


def build_guest_bot_bindings_map(
    guest_ids: Iterable[int],
    *,
    selected_bot_ids: Iterable[int],
    require_message_permission: bool = True,
) -> dict[int, list[GuestBotBinding]]:
    """
    Собирает активные привязки гостей к выбранным ботам.
    """
    normalized_guest_ids = _ordered_unique_ints(guest_ids)
    normalized_bot_ids = _ordered_unique_ints(selected_bot_ids)
    if not normalized_guest_ids or not normalized_bot_ids:
        return {}

    bindings = list(
        GuestBotBinding.objects.select_related("bot")
        .filter(
            guest_id__in=normalized_guest_ids,
            bot_id__in=normalized_bot_ids,
            is_active=True,
            bot__is_active=True,
        )
        .exclude(external_chat_id__isnull=True)
        .exclude(external_chat_id="")
        .order_by("guest_id", "id")
    )
    if require_message_permission:
        bindings = [binding for binding in bindings if binding.is_opt_in and not binding.is_stop_sending]

    activity_map = _build_binding_activity_map(binding_ids=[int(binding.id) for binding in bindings])

    result: dict[int, list[GuestBotBinding]] = {}
    for binding in bindings:
        result.setdefault(int(binding.guest_id), []).append(binding)
    for guest_bindings in result.values():
        guest_bindings.sort(
            key=lambda binding: (
                _binding_activity_at(binding, activity_map),
                bool(binding.is_primary),
                -int(binding.id),
            ),
            reverse=True,
        )
    return result


def build_targets_from_bindings(
    bindings: list[GuestBotBinding],
    *,
    target_mode: str | None,
) -> list[dict[str, Any]]:
    """
    Формирует целевые каналы отправки из привязок гостя к ботам.
    """
    if not bindings:
        return []

    normalized_target_mode = normalize_mailing_target_mode(target_mode)
    selected_bindings = bindings
    if normalized_target_mode == Mailing.TargetMode.PRIMARY_ONLY:
        selected_bindings = bindings[:1]

    targets: list[dict[str, Any]] = []
    for binding in selected_bindings:
        provider = str(binding.bot.provider_type or "").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            continue
        targets.append(
            {
                "provider_type": provider,
                "external_chat_id": str(binding.external_chat_id or "").strip(),
                "external_user_id": str(binding.external_user_id or "").strip(),
                "guest_binding": binding,
                "bot_profile": binding.bot,
                "channel_mode": CHANNEL_MODE_BINDING,
            }
        )
    return targets


def _select_legacy_telegram_bot(active_selected_bots: list[BotProfile]) -> BotProfile | None:
    """
    Возвращает выбранный активный Telegram-бот для отправки legacy-гостям.
    """
    for bot in active_selected_bots:
        if str(bot.provider_type or "").strip().lower() == BotProfile.ProviderType.TELEGRAM:
            return bot
    return None


def _serialize_preview_bot(bot: BotProfile) -> dict[str, object]:
    """
    Преобразует бота в формат для клиентского предварительного расчёта.
    """
    provider = str(bot.provider_type or "").strip().lower()
    return {
        "id": int(bot.id),
        "provider": provider,
        "code": str(bot.code or ""),
        "name": str(bot.name or ""),
        "is_telegram": provider == BotProfile.ProviderType.TELEGRAM,
    }


def _collect_new_bot_bound_guest_ids(guest_ids: Iterable[int]) -> set[int]:
    """
    Возвращает гостей, у которых уже есть новая активная привязка к Telegram/ВК/Макс.
    """
    normalized_guest_ids = _ordered_unique_ints(guest_ids)
    if not normalized_guest_ids:
        return set()
    return {
        int(guest_id)
        for guest_id in (
            GuestBotBinding.objects.filter(
                guest_id__in=normalized_guest_ids,
                is_active=True,
                bot__is_active=True,
                bot__provider_type__in=SUPPORTED_PROVIDERS,
            )
            .exclude(external_chat_id__isnull=True)
            .exclude(external_chat_id="")
            .values_list("guest_id", flat=True)
            .distinct()
        )
    }


def _build_legacy_telegram_channels_map(
    guest_ids: Iterable[int],
    *,
    exclude_guest_ids: set[int],
) -> dict[int, VtelemaxRecipientChannel]:
    """
    Возвращает sendable legacy Telegram-каналы для гостей без новой регистрации.
    """
    normalized_guest_ids = _ordered_unique_ints(guest_ids)
    if not normalized_guest_ids:
        return {}

    queryset = (
        VtelemaxRecipientChannel.objects.filter(
            guest_id__in=normalized_guest_ids,
            platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
            is_registered=True,
            notifications_allowed=True,
        )
        .exclude(guest_id__in=exclude_guest_ids)
        .exclude(external_id__isnull=True)
        .exclude(external_id="")
        .order_by("guest_id", "-registered_at", "-last_synced_at", "id")
    )
    result: dict[int, VtelemaxRecipientChannel] = {}
    for channel in queryset:
        guest_id = int(channel.guest_id)
        if guest_id not in result:
            result[guest_id] = channel
    return result


def _build_historical_telegram_target(
    *,
    channel: HistoricalTelegramChannel,
) -> dict[str, Any] | None:
    """
    Формирует цель отправки через рабочий исторический Telegram-канал.
    """
    external_chat_id = str(channel.telegram_chat_id or "").strip()
    if not external_chat_id:
        return None
    return {
        "provider_type": BotProfile.ProviderType.TELEGRAM,
        "external_chat_id": external_chat_id,
        "external_user_id": "",
        "guest_binding": None,
        "bot_profile": channel.bot_profile,
        "channel_mode": CHANNEL_MODE_HISTORICAL_TELEGRAM,
        "historical_telegram_channel_id": int(channel.id),
    }


def _build_legacy_telegram_target(
    *,
    channel: VtelemaxRecipientChannel,
    telegram_bot: BotProfile,
) -> dict[str, Any] | None:
    """
    Формирует цель отправки по старому Telegram-каналу того же физического бота.
    """
    external_chat_id = str(channel.external_id or "").strip()
    if not external_chat_id:
        return None
    return {
        "provider_type": BotProfile.ProviderType.TELEGRAM,
        "external_chat_id": external_chat_id,
        "external_user_id": "",
        "guest_binding": None,
        "bot_profile": telegram_bot,
        "channel_mode": CHANNEL_MODE_LEGACY_TELEGRAM,
        "vtelemax_channel_id": int(channel.id),
    }


def _build_binding_activity_map(*, binding_ids: Iterable[int]) -> dict[int, datetime]:
    """
    Возвращает свежесть канала vtelemax для новых привязок к ботам.
    """
    normalized_binding_ids = _ordered_unique_ints(binding_ids)
    if not normalized_binding_ids:
        return {}

    result: dict[int, datetime] = {}
    rows = (
        VtelemaxRecipientChannel.objects.filter(guest_binding_id__in=normalized_binding_ids)
        .values("guest_binding_id")
        .annotate(last_channel_at=Max("effective_updated_at"))
    )
    for row in rows:
        binding_id = int(row["guest_binding_id"])
        last_channel_at = row.get("last_channel_at")
        if last_channel_at is not None:
            result[binding_id] = last_channel_at
    return result


def _binding_activity_at(
    binding: GuestBotBinding,
    activity_map: dict[int, datetime],
) -> datetime:
    """
    Определяет дату для выбора последнего активного бота гостя.
    """
    return activity_map.get(int(binding.id)) or binding.updated_at or binding.created_at


def _ordered_unique_ints(values: Iterable[int]) -> tuple[int, ...]:
    """
    Возвращает уникальные положительные идентификаторы с сохранением порядка.
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
