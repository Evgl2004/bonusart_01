from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db.models import Count, Exists, OuterRef, Q, QuerySet

from guests.models import (
    DispatchTask,
    InteractionButtonSet,
    MessageInteractionEvent,
    MessageInteractionLinkTransition,
)


@dataclass(frozen=True, slots=True)
class MessageInteractionReportSnapshot:
    """Сводные показатели взаимодействий в заданной области отправок."""

    messages_with_buttons_total: int = 0
    guests_with_buttons_total: int = 0
    interacted_messages_total: int = 0
    interacted_guests_total: int = 0
    likes_total: int = 0
    dislikes_total: int = 0
    coupon_opened_messages_total: int = 0
    coupon_opened_guests_total: int = 0
    coupon_clicks_total: int = 0
    menu_opened_messages_total: int = 0
    menu_opened_guests_total: int = 0
    menu_clicks_total: int = 0
    messages_with_links_total: int = 0
    guests_with_links_total: int = 0
    link_opened_messages_total: int = 0
    link_opened_guests_total: int = 0
    link_clicks_total: int = 0
    interaction_share_percent: Decimal = Decimal("0.00")
    link_share_percent: Decimal = Decimal("0.00")

    def to_dict(self) -> dict[str, Any]:
        """Возвращает снимок в формате, пригодном для шаблонов и JSON."""

        return asdict(self)


def build_message_interaction_report_snapshot(
    *,
    tasks_queryset: QuerySet[DispatchTask],
) -> MessageInteractionReportSnapshot:
    """
    Рассчитывает показатели интерактивных сообщений для переданных задач.

    Знаменатель включает только технически успешные задачи, для которых была
    создана интерактивность. Поэтому обычные сообщения и исторические маршруты
    без кнопок не попадают в расчёт. Основные показатели учитывают только
    события с результатом «Принято»; повторная попытка изменить оценку остаётся
    диагностическим фактом и не влияет на отчёт.
    """

    if tasks_queryset.model is not DispatchTask:
        raise TypeError("Для отчёта требуется набор задач отправки DispatchTask.")

    accepted_event_exists = MessageInteractionEvent.objects.filter(
        interaction_id=OuterRef("message_interaction__id"),
        result=MessageInteractionEvent.Result.ACCEPTED,
    )
    link_transition_exists = MessageInteractionLinkTransition.objects.filter(
        tracked_link_id=OuterRef("message_interaction__id"),
    )
    successful_interactive_tasks = (
        tasks_queryset.filter(
            status=DispatchTask.Status.DONE,
            message_interaction__isnull=False,
        )
        .annotate(
            has_accepted_interaction_event=Exists(accepted_event_exists),
            has_link_transition=Exists(link_transition_exists),
        )
    )
    task_totals = successful_interactive_tasks.aggregate(
        messages_with_buttons_total=Count("id", distinct=True),
        guests_with_buttons_total=Count(
            "guest_id",
            distinct=True,
            filter=Q(guest_id__isnull=False),
        ),
        interacted_messages_total=Count(
            "id",
            distinct=True,
            filter=(
                Q(has_accepted_interaction_event=True)
                | Q(has_link_transition=True)
            ),
        ),
        interacted_guests_total=Count(
            "guest_id",
            distinct=True,
            filter=(
                Q(guest_id__isnull=False)
                & (
                    Q(has_accepted_interaction_event=True)
                    | Q(has_link_transition=True)
                )
            ),
        ),
        messages_with_links_total=Count(
            "id",
            distinct=True,
            filter=Q(
                message_interaction__button_set=InteractionButtonSet.RATING_MENU_LINK
            ),
        ),
        guests_with_links_total=Count(
            "guest_id",
            distinct=True,
            filter=Q(
                guest_id__isnull=False,
                message_interaction__button_set=InteractionButtonSet.RATING_MENU_LINK,
            ),
        ),
        link_opened_messages_total=Count(
            "id",
            distinct=True,
            filter=Q(has_link_transition=True),
        ),
        link_opened_guests_total=Count(
            "guest_id",
            distinct=True,
            filter=Q(
                guest_id__isnull=False,
                has_link_transition=True,
            ),
        ),
        link_clicks_total=Count(
            "message_interaction__tracked_link__transitions__id",
        ),
    )

    accepted_events = MessageInteractionEvent.objects.filter(
        interaction__dispatch_task__in=successful_interactive_tasks,
        result=MessageInteractionEvent.Result.ACCEPTED,
    )
    event_totals = accepted_events.aggregate(
        likes_total=Count(
            "id",
            filter=Q(action=MessageInteractionEvent.Action.LIKE),
        ),
        dislikes_total=Count(
            "id",
            filter=Q(action=MessageInteractionEvent.Action.DISLIKE),
        ),
        coupon_opened_messages_total=Count(
            "interaction_id",
            distinct=True,
            filter=Q(action=MessageInteractionEvent.Action.COUPONS),
        ),
        coupon_opened_guests_total=Count(
            "interaction__dispatch_task__guest_id",
            distinct=True,
            filter=Q(
                action=MessageInteractionEvent.Action.COUPONS,
                interaction__dispatch_task__guest_id__isnull=False,
            ),
        ),
        coupon_clicks_total=Count(
            "id",
            filter=Q(action=MessageInteractionEvent.Action.COUPONS),
        ),
        menu_opened_messages_total=Count(
            "interaction_id",
            distinct=True,
            filter=Q(action=MessageInteractionEvent.Action.MENU),
        ),
        menu_opened_guests_total=Count(
            "interaction__dispatch_task__guest_id",
            distinct=True,
            filter=Q(
                action=MessageInteractionEvent.Action.MENU,
                interaction__dispatch_task__guest_id__isnull=False,
            ),
        ),
        menu_clicks_total=Count(
            "id",
            filter=Q(action=MessageInteractionEvent.Action.MENU),
        ),
    )

    normalized_task_totals = {
        name: int(value or 0) for name, value in task_totals.items()
    }
    normalized_event_totals = {
        name: int(value or 0) for name, value in event_totals.items()
    }
    denominator = normalized_task_totals["messages_with_buttons_total"]
    interacted_messages = normalized_task_totals["interacted_messages_total"]
    interaction_share = (
        (Decimal(interacted_messages) * Decimal("100") / Decimal(denominator)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        if denominator
        else Decimal("0.00")
    )
    link_denominator = normalized_task_totals["messages_with_links_total"]
    link_opened_messages = normalized_task_totals["link_opened_messages_total"]
    link_share = (
        (
            Decimal(link_opened_messages)
            * Decimal("100")
            / Decimal(link_denominator)
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        if link_denominator
        else Decimal("0.00")
    )

    return MessageInteractionReportSnapshot(
        **normalized_task_totals,
        **normalized_event_totals,
        interaction_share_percent=interaction_share,
        link_share_percent=link_share,
    )
