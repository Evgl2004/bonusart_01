from __future__ import annotations

import uuid
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    InteractionButtonSet,
    MessageInteraction,
    MessageInteractionEvent,
)
from guests.services.message_interaction_reporting import (
    build_message_interaction_report_snapshot,
)


class MessageInteractionReportingTests(TestCase):
    """Проверки единого расчёта показателей взаимодействия."""

    @staticmethod
    def _create_task(
        *,
        guest: Guest | None,
        status: str = DispatchTask.Status.DONE,
        button_set: str | None = InteractionButtonSet.RATING_MENU,
    ) -> tuple[DispatchTask, MessageInteraction | None]:
        task = DispatchTask.objects.create(
            guest=guest,
            provider_type=BotProfile.ProviderType.TELEGRAM,
            status=status,
        )
        interaction = None
        if button_set is not None:
            interaction = MessageInteraction.objects.create(
                dispatch_task=task,
                button_set=button_set,
            )
        return task, interaction

    @staticmethod
    def _create_event(
        *,
        interaction: MessageInteraction,
        action: str,
        result: str = MessageInteractionEvent.Result.ACCEPTED,
    ) -> MessageInteractionEvent:
        return MessageInteractionEvent.objects.create(
            event_id=uuid.uuid4(),
            interaction=interaction,
            action=action,
            occurred_at=timezone.now(),
            result=result,
        )

    def test_counts_only_accepted_events_of_successful_interactive_tasks(self):
        first_guest = Guest.objects.create(phone="+70000000001")
        second_guest = Guest.objects.create(phone="+70000000002")
        silent_guest = Guest.objects.create(phone="+70000000003")
        excluded_guest = Guest.objects.create(phone="+70000000004")

        _, coupon_interaction = self._create_task(
            guest=first_guest,
            button_set=InteractionButtonSet.RATING_COUPONS,
        )
        _, menu_interaction = self._create_task(guest=first_guest)
        _, dislike_interaction = self._create_task(guest=second_guest)
        self._create_task(guest=silent_guest)
        _, anonymous_interaction = self._create_task(guest=None)

        self._create_event(
            interaction=coupon_interaction,
            action=MessageInteractionEvent.Action.LIKE,
        )
        self._create_event(
            interaction=coupon_interaction,
            action=MessageInteractionEvent.Action.COUPONS,
        )
        self._create_event(
            interaction=coupon_interaction,
            action=MessageInteractionEvent.Action.COUPONS,
        )
        self._create_event(
            interaction=menu_interaction,
            action=MessageInteractionEvent.Action.MENU,
        )
        self._create_event(
            interaction=dislike_interaction,
            action=MessageInteractionEvent.Action.DISLIKE,
        )
        self._create_event(
            interaction=dislike_interaction,
            action=MessageInteractionEvent.Action.LIKE,
            result=MessageInteractionEvent.Result.RATING_ALREADY_RECORDED,
        )
        self._create_event(
            interaction=anonymous_interaction,
            action=MessageInteractionEvent.Action.MENU,
        )

        _, failed_interaction = self._create_task(
            guest=excluded_guest,
            status=DispatchTask.Status.FAILED,
        )
        self._create_event(
            interaction=failed_interaction,
            action=MessageInteractionEvent.Action.LIKE,
        )
        self._create_task(guest=excluded_guest, button_set=None)

        snapshot = build_message_interaction_report_snapshot(
            tasks_queryset=DispatchTask.objects.all()
        )

        self.assertEqual(snapshot.messages_with_buttons_total, 5)
        self.assertEqual(snapshot.guests_with_buttons_total, 3)
        self.assertEqual(snapshot.interacted_messages_total, 4)
        self.assertEqual(snapshot.interacted_guests_total, 2)
        self.assertEqual(snapshot.likes_total, 1)
        self.assertEqual(snapshot.dislikes_total, 1)
        self.assertEqual(snapshot.coupon_opened_messages_total, 1)
        self.assertEqual(snapshot.coupon_opened_guests_total, 1)
        self.assertEqual(snapshot.coupon_clicks_total, 2)
        self.assertEqual(snapshot.menu_opened_messages_total, 2)
        self.assertEqual(snapshot.menu_opened_guests_total, 1)
        self.assertEqual(snapshot.menu_clicks_total, 2)
        self.assertEqual(snapshot.interaction_share_percent, Decimal("80.00"))

    def test_empty_scope_returns_zeroes(self):
        snapshot = build_message_interaction_report_snapshot(
            tasks_queryset=DispatchTask.objects.none()
        )

        self.assertEqual(snapshot.messages_with_buttons_total, 0)
        self.assertEqual(snapshot.interacted_messages_total, 0)
        self.assertEqual(snapshot.interaction_share_percent, Decimal("0.00"))

    def test_rejects_queryset_of_another_model(self):
        with self.assertRaisesMessage(
            TypeError,
            "Для отчёта требуется набор задач отправки DispatchTask.",
        ):
            build_message_interaction_report_snapshot(
                tasks_queryset=MessageInteraction.objects.all()  # type: ignore[arg-type]
            )
