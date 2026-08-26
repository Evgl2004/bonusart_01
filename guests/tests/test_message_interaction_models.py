"""Проверки модели хранения интерактивных сообщений."""

import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from guests.models import (
    DispatchTask,
    InteractionButtonSet,
    Mailing,
    MessageInteraction,
    MessageInteractionEvent,
    MessageTemplate,
    NotificationScenario,
)


def _create_dispatch_task() -> DispatchTask:
    """Создаёт минимальную задачу конкретной отправки для модельных тестов."""

    return DispatchTask.objects.create(provider_type="telegram")


def _create_interaction(
    *,
    button_set: str = InteractionButtonSet.RATING_MENU,
) -> MessageInteraction:
    """Создаёт интерактивность сообщения с отдельной задачей отправки."""

    return MessageInteraction.objects.create(
        dispatch_task=_create_dispatch_task(),
        button_set=button_set,
    )


def _create_event(
    interaction: MessageInteraction,
    *,
    action: str,
    result: str = MessageInteractionEvent.Result.ACCEPTED,
    event_id: uuid.UUID | None = None,
) -> MessageInteractionEvent:
    """Создаёт событие с честным временем фиксации vtelemax."""

    return MessageInteractionEvent.objects.create(
        event_id=event_id or uuid.uuid4(),
        interaction=interaction,
        action=action,
        occurred_at=timezone.now(),
        result=result,
    )


@pytest.mark.django_db(transaction=True)
class TestMessageInteractionModels:
    """Положительные и отрицательные проверки ограничений уровня базы."""

    def test_source_models_default_to_messages_without_buttons(self):
        """Новая рассылка или сценарий не получают кнопки неявно."""

        assert Mailing().button_set == InteractionButtonSet.NONE
        assert NotificationScenario().button_set == InteractionButtonSet.NONE

    def test_button_set_contains_only_approved_source_values(self):
        """Источник допускает только четыре утверждённых набора кнопок."""

        assert set(InteractionButtonSet.values) == {
            "none",
            "rating_menu",
            "rating_coupons",
            "rating_menu_link",
        }

    @pytest.mark.parametrize("source_model", ["mailing", "scenario"])
    def test_source_model_rejects_unknown_button_set_in_database(self, source_model):
        """Прямой обход формы не сохраняет неизвестный набор кнопок."""

        template = MessageTemplate.objects.create(
            name=f"interaction-template-{source_model}",
            message_text="Тестовое сообщение",
        )
        now = timezone.now()

        with pytest.raises(IntegrityError), transaction.atomic():
            if source_model == "mailing":
                Mailing.objects.create(
                    name="Проверка набора кнопок",
                    template=template,
                    scheduled_date=now.date(),
                    scheduled_time_begin=now,
                    scheduled_time_end=now,
                    created_at=now,
                    updated_at=now,
                    send_window_begin=now.time(),
                    send_window_end=now.time(),
                    button_set="unknown",
                )
            else:
                NotificationScenario.objects.create(
                    code="interaction_unknown_button_set",
                    name="Проверка набора кнопок",
                    template=template,
                    button_set="unknown",
                )

    def test_interaction_has_only_approved_minimal_fields(self):
        """Горячие и отчётные данные не дублируются в интерактивности."""

        assert [field.name for field in MessageInteraction._meta.fields] == [
            "id",
            "dispatch_task",
            "button_set",
            "created_at",
        ]

    def test_event_has_only_approved_fields(self):
        """Событие не содержит сырое тело пакета и избыточные связи."""

        assert [field.name for field in MessageInteractionEvent._meta.fields] == [
            "id",
            "event_id",
            "interaction",
            "action",
            "occurred_at",
            "received_at",
            "result",
            "provider_message_id",
        ]

    def test_interaction_rejects_none_button_set(self):
        """Для сообщения без кнопок отдельная интерактивность не создаётся."""

        task = _create_dispatch_task()
        interaction = MessageInteraction(
            dispatch_task=task,
            button_set=InteractionButtonSet.NONE,
        )

        with pytest.raises(ValidationError):
            interaction.full_clean()

        with pytest.raises(IntegrityError), transaction.atomic():
            MessageInteraction.objects.create(
                dispatch_task=task,
                button_set=InteractionButtonSet.NONE,
            )

    def test_dispatch_task_has_only_one_interaction(self):
        """Одна конкретная отправка не может получить две интерактивности."""

        task = _create_dispatch_task()
        MessageInteraction.objects.create(
            dispatch_task=task,
            button_set=InteractionButtonSet.RATING_MENU,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            MessageInteraction.objects.create(
                dispatch_task=task,
                button_set=InteractionButtonSet.RATING_COUPONS,
            )

    def test_dispatch_task_deletion_is_protected(self):
        """Старое нажатие остаётся сопоставимым с исходной отправкой."""

        interaction = _create_interaction()

        with pytest.raises(ProtectedError):
            interaction.dispatch_task.delete()

    def test_event_id_is_unique(self):
        """Повтор транспортной доставки не создаёт второе событие."""

        interaction = _create_interaction()
        event_id = uuid.uuid4()
        _create_event(
            interaction,
            action=MessageInteractionEvent.Action.MENU,
            event_id=event_id,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            _create_event(
                interaction,
                action=MessageInteractionEvent.Action.MENU,
                event_id=event_id,
            )

    def test_only_first_rating_can_be_accepted(self):
        """Противоположная оценка не обходит правило первой оценки."""

        interaction = _create_interaction()
        _create_event(interaction, action=MessageInteractionEvent.Action.LIKE)

        with pytest.raises(IntegrityError), transaction.atomic():
            _create_event(interaction, action=MessageInteractionEvent.Action.DISLIKE)

        repeated = _create_event(
            interaction,
            action=MessageInteractionEvent.Action.DISLIKE,
            result=MessageInteractionEvent.Result.RATING_ALREADY_RECORDED,
        )
        assert repeated.result == MessageInteractionEvent.Result.RATING_ALREADY_RECORDED

    @pytest.mark.parametrize(
        "action",
        [MessageInteractionEvent.Action.COUPONS, MessageInteractionEvent.Action.MENU],
    )
    def test_navigation_action_cannot_have_rejected_rating_result(self, action):
        """Результат повторной оценки неприменим к навигационным действиям."""

        interaction = _create_interaction()

        with pytest.raises(IntegrityError), transaction.atomic():
            _create_event(
                interaction,
                action=action,
                result=MessageInteractionEvent.Result.RATING_ALREADY_RECORDED,
            )

    @pytest.mark.parametrize(
        ("action", "result"),
        [
            ("x", MessageInteractionEvent.Result.ACCEPTED),
            (MessageInteractionEvent.Action.MENU, "unknown"),
        ],
    )
    def test_unknown_action_or_result_is_rejected_by_database(self, action, result):
        """Обход валидации модели не позволяет записать неизвестные коды."""

        interaction = _create_interaction()

        with pytest.raises(IntegrityError), transaction.atomic():
            _create_event(interaction, action=action, result=result)

    def test_navigation_actions_can_be_recorded_multiple_times(self):
        """Каждое отдельное открытие меню хранится самостоятельным событием."""

        interaction = _create_interaction()
        first = _create_event(interaction, action=MessageInteractionEvent.Action.MENU)
        second = _create_event(interaction, action=MessageInteractionEvent.Action.MENU)

        assert first.event_id != second.event_id
        assert interaction.events.count() == 2

    def test_event_protects_interaction_from_deletion(self):
        """История взаимодействий не удаляется вместе с интерактивностью."""

        interaction = _create_interaction()
        _create_event(interaction, action=MessageInteractionEvent.Action.COUPONS)

        with pytest.raises(ProtectedError):
            interaction.delete()

    def test_received_at_is_set_by_sagur(self):
        """Время приёма SAGUR создаётся независимо от времени vtelemax."""

        interaction = _create_interaction()
        occurred_at = timezone.now()
        event = MessageInteractionEvent.objects.create(
            event_id=uuid.uuid4(),
            interaction=interaction,
            action=MessageInteractionEvent.Action.MENU,
            occurred_at=occurred_at,
            result=MessageInteractionEvent.Result.ACCEPTED,
            provider_message_id=None,
        )

        assert event.received_at is not None
        assert event.received_at >= occurred_at
