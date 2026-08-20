"""
Producer для перевода строк массовой рассылки в универсальную очередь DispatchTask.

Важно: маршрутизация использует выбранные в рассылке `Mailing.bot_profiles`.
Основной путь - `GuestBotBinding`; для исторической Telegram-аудитории
поддержан отдельный проверенный канал и старый запасной путь через `VtelemaxRecipientChannel`.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Set

from django.utils import timezone

from guests.models import (
    BotProfile,
    CouponCampaignAssignment,
    DispatchTask,
    InteractionButtonSet,
    Mailing,
    MailingGuest,
)
from guests.services.message_interaction_outgoing import (
    DispatchTaskAlreadyExists,
    create_dispatch_task_with_optional_interaction,
    interactions_enabled_for_new_task,
)
from guests.services.mailing_delivery_targets import (
    CHANNEL_MODE_BINDING,
    build_mailing_delivery_targets_map,
)

logger = logging.getLogger(__name__)

CHANNEL_MODE_MAILING_ROW_EXTERNAL_ID = "mailing_row_external_id"


@dataclass
class MailingDispatchSummary:
    """
    Сводка постановки пачки строк рассылки в универсальную очередь.
    """

    rows_total: int = 0
    rows_queued: int = 0
    rows_failed: int = 0
    tasks_created: int = 0
    tasks_duplicates: int = 0


def _resolve_target_mode_for_mailing(mailing: Mailing) -> str:
    """
    Возвращает режим маршрутизации из параметров конкретной рассылки.

    Значения:
    1. `primary_only` - только основной бот гостя;
    2. `all_bots` - все активные привязки гостя.
    """
    value = str(getattr(mailing, "target_mode", "primary_only") or "").strip().lower()
    return value if value in ("primary_only", "all_bots") else "primary_only"


def _resolve_priority_for_mailing(mailing: Mailing) -> str:
    """
    Возвращает приоритет задач из параметров конкретной рассылки.
    """
    value = str(getattr(mailing, "queue_priority", DispatchTask.Priority.BULK) or "").strip().lower()
    allowed = {
        DispatchTask.Priority.HIGH,
        DispatchTask.Priority.NORMAL,
        DispatchTask.Priority.BULK,
    }
    return value if value in allowed else DispatchTask.Priority.BULK


def _resolve_selected_bot_profiles(mailing: Mailing) -> tuple[Set[int], Set[str]]:
    """
    Возвращает набор выбранных активных ботов рассылки:
    1. идентификаторы профилей ботов;
    2. типы провайдеров (`telegram|max|vk`).
    """
    selected_rows = list(mailing.bot_profiles.filter(is_active=True).values("id", "provider_type"))
    selected_bot_ids = {row["id"] for row in selected_rows}
    selected_providers = {str(row["provider_type"]).strip().lower() for row in selected_rows}
    return selected_bot_ids, selected_providers


def _build_coupon_assignments_map(mailing: Mailing, guest_ids: Iterable[int]) -> Dict[int, CouponCampaignAssignment]:
    """
    Возвращает назначенные купоны кампании в виде `guest_id -> assignment`.
    """
    mapping: Dict[int, CouponCampaignAssignment] = {}
    if not getattr(mailing, "coupon_series", None):
        return mapping

    assignments = (
        CouponCampaignAssignment.objects.filter(
            campaign=mailing,
            guest_id__in=list(guest_ids),
        )
        .select_related("coupon")
        .order_by("id")
    )
    for assignment in assignments:
        if not assignment.guest_id:
            continue
        mapping[int(assignment.guest_id)] = assignment
    return mapping


def _build_external_id_telegram_target(
    mailing: Mailing,
    *,
    selected_bot_ids: Set[int],
    row: MailingGuest,
) -> List[dict]:
    """
    Возвращает цель доставки для разовых исторических рассылок, где Telegram-идентификатор
    пришёл из файла и сохранён прямо в строке аудитории кампании.
    """
    external_chat_id = str(row.external_id or "").strip()
    if not external_chat_id:
        return []

    telegram_bot = (
        mailing.bot_profiles.filter(
            id__in=selected_bot_ids,
            is_active=True,
            provider_type=BotProfile.ProviderType.TELEGRAM,
        )
        .order_by("id")
        .first()
    )
    if telegram_bot is None:
        return []

    return [
        {
            "provider_type": BotProfile.ProviderType.TELEGRAM,
            "external_chat_id": external_chat_id,
            "external_user_id": "",
            "guest_binding": None,
            "bot_profile": telegram_bot,
            "channel_mode": CHANNEL_MODE_MAILING_ROW_EXTERNAL_ID,
        }
    ]


def enqueue_mailing_rows_as_dispatch_tasks(
    mailing: Mailing,
    rows: List[MailingGuest],
    now=None,
) -> MailingDispatchSummary:
    """
    Переводит пачку строк MailingGuest в задачи DispatchTask.

    Важно:
    1. строки `MailingGuest` помечаются `done`, когда задачи успешно поставлены в очередь;
    2. фактическая отправка выполняется провайдерными async-воркерами.
    """
    summary = MailingDispatchSummary(rows_total=len(rows))
    if not rows:
        return summary

    target_mode = _resolve_target_mode_for_mailing(mailing)
    priority = _resolve_priority_for_mailing(mailing)
    now = now or timezone.now()
    selected_bot_ids, _selected_provider_types = _resolve_selected_bot_profiles(mailing)

    if not selected_bot_ids:
        for row in rows:
            row.status = MailingGuest.Status.ERROR
            row.delivery_status = "dispatch_no_bot_profiles"
            row.error_description = "В рассылке не выбраны активные профили ботов."
            row.save(update_fields=["status", "delivery_status", "error_description"])
            summary.rows_failed += 1
        return summary

    guest_ids = [row.guest_id for row in rows]
    targets_map = build_mailing_delivery_targets_map(
        guest_ids,
        selected_bot_ids=selected_bot_ids,
        target_mode=target_mode,
    )
    coupon_assignments_map = _build_coupon_assignments_map(mailing, guest_ids)

    for row in rows:
        assignment = coupon_assignments_map.get(int(row.guest_id)) if row.guest_id else None
        row_targets = targets_map.get(int(row.guest_id), []) if row.guest_id else []
        if not row_targets:
            row_targets = _build_external_id_telegram_target(
                mailing,
                selected_bot_ids=selected_bot_ids,
                row=row,
            )

        if not row_targets:
            row.status = MailingGuest.Status.ERROR
            row.delivery_status = "dispatch_no_targets"
            row.error_description = (
                "Не найдено новой bot-привязки или доступного legacy Telegram-канала "
                "для постановки в универсальную очередь; внешний Telegram ID в строке кампании также отсутствует."
            )
            row.save(update_fields=["status", "delivery_status", "error_description"])
            summary.rows_failed += 1
            continue

        available_at = row.scheduled_datetime if row.scheduled_datetime and row.scheduled_datetime > now else now
        row_created = 0
        row_duplicates = 0
        row_last_error: str | None = None

        for target in row_targets:
            provider_type = target["provider_type"]
            external_chat_id = target["external_chat_id"]
            external_user_id = target.get("external_user_id") or ""
            channel_mode = target.get("channel_mode") or CHANNEL_MODE_BINDING
            idempotency_key = f"mailing:{mailing.id}:row:{row.id}:provider:{provider_type}:chat:{external_chat_id}"
            task_payload = {
                "mailing_id": mailing.id,
                "mailing_guest_id": row.id,
                "channel_mode": channel_mode,
                "historical_telegram_channel_id": target.get("historical_telegram_channel_id"),
                "vtelemax_channel_id": target.get("vtelemax_channel_id"),
                "coupon_series": assignment.coupon_series if assignment else None,
                "coupon_code": assignment.coupon_code if assignment else None,
                "coupon_venue_code": assignment.venue_code if assignment else None,
                "coupon_venue_name": assignment.venue_name if assignment else None,
                "coupon_title": assignment.coupon_title if assignment else None,
                "coupon_promo_text": assignment.promo_text if assignment else None,
            }
            if provider_type == "max":
                task_payload["max_user_id"] = external_user_id or external_chat_id

            try:
                create_dispatch_task_with_optional_interaction(
                    button_set=getattr(mailing, "button_set", InteractionButtonSet.NONE),
                    interaction_enabled=(
                        channel_mode == CHANNEL_MODE_BINDING
                        and target.get("guest_binding") is not None
                        and interactions_enabled_for_new_task(provider_type)
                    ),
                    source_type=DispatchTask.SourceType.MAILING,
                    provider_type=provider_type,
                    priority=priority,
                    status=DispatchTask.Status.PENDING,
                    guest_id=row.guest_id,
                    mailing_guest=row,
                    bot_profile=target["bot_profile"],
                    guest_binding=target["guest_binding"],
                    external_chat_id=external_chat_id,
                    message_text=row.text_mailing_list or "",
                    payload=task_payload,
                    scheduled_at=row.scheduled_datetime,
                    available_at=available_at,
                    idempotency_key=idempotency_key,
                )
                row_created += 1
            except DispatchTaskAlreadyExists:
                row_duplicates += 1
            except Exception as err:
                row_last_error = str(err)[:2000]
                logger.exception(
                    "Ошибка постановки dispatch-задачи для mailing_row=%s guest_id=%s",
                    row.id,
                    row.guest_id,
                )

        summary.tasks_created += row_created
        summary.tasks_duplicates += row_duplicates

        if row_created > 0 or row_duplicates > 0:
            row.status = MailingGuest.Status.DONE
            row.delivery_status = "queued_to_dispatch"
            row.error_description = None
            row.save(update_fields=["status", "delivery_status", "error_description"])
            if assignment and assignment.status == CouponCampaignAssignment.Status.RESERVED:
                assignment.status = CouponCampaignAssignment.Status.SENT
                assignment.sent_at = now
                assignment.save(update_fields=["status", "sent_at", "updated_at"])
            summary.rows_queued += 1
        else:
            row.status = MailingGuest.Status.ERROR
            row.delivery_status = "dispatch_enqueue_error"
            row.error_description = row_last_error or "Не удалось поставить задачу в универсальную очередь."
            row.save(update_fields=["status", "delivery_status", "error_description"])
            summary.rows_failed += 1

    return summary
