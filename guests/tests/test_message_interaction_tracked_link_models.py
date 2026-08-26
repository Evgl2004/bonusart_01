"""Проверки модели справочника, ссылок и повторяемых переходов."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from guests.models import (
    DispatchTask,
    InteractionButtonSet,
    InteractionLinkLabelCode,
    Mailing,
    MessageInteraction,
    MessageInteractionLinkDestination,
    MessageInteractionLinkTransition,
    MessageInteractionTrackedLink,
    MessageTemplate,
    NotificationScenario,
)


VALID_TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz01_-23"


def _create_destination(**overrides) -> MessageInteractionLinkDestination:
    """Создаёт активное разрешённое назначение для модельных тестов."""

    values = {
        "code": "test_booking",
        "name": "Тестовое бронирование",
        "label_code": InteractionLinkLabelCode.BOOKING,
        "target_url": "https://example.test/booking",
        "is_active": True,
    }
    values.update(overrides)
    return MessageInteractionLinkDestination.objects.create(**values)


def _create_interaction(
    *,
    button_set: str = InteractionButtonSet.RATING_MENU_LINK,
) -> MessageInteraction:
    """Создаёт интерактивность отдельного сообщения."""

    task = DispatchTask.objects.create(provider_type="telegram")
    return MessageInteraction.objects.create(
        dispatch_task=task,
        button_set=button_set,
    )


def _create_tracked_link(
    *,
    interaction: MessageInteraction | None = None,
    token: str = VALID_TOKEN,
) -> MessageInteractionTrackedLink:
    """Создаёт неизменяемый снимок ссылки конкретного сообщения."""

    return MessageInteractionTrackedLink.objects.create(
        interaction=interaction or _create_interaction(),
        public_token=token,
        label_code=InteractionLinkLabelCode.BOOKING,
        target_url="https://example.test/booking",
    )


def _mailing_fields(
    template: MessageTemplate,
    *,
    button_set: str,
    destination: MessageInteractionLinkDestination | None,
) -> dict:
    """Возвращает обязательные поля рассылки для проверки ограничений."""

    now = timezone.now()
    return {
        "name": "Проверка отслеживаемой ссылки",
        "template": template,
        "scheduled_date": now.date(),
        "scheduled_time_begin": now,
        "scheduled_time_end": now,
        "created_at": now,
        "updated_at": now,
        "send_window_begin": now.time(),
        "send_window_end": now.time(),
        "button_set": button_set,
        "tracked_link_destination": destination,
    }


@pytest.mark.django_db(transaction=True)
class TestTrackedLinkModels:
    """Проверяет положительные и отрицательные ограничения уровня модели и базы."""

    def test_button_set_and_label_codes_are_closed(self):
        """Новый набор и четыре подписи добавлены без произвольного конструктора."""

        assert set(InteractionButtonSet.values) == {
            "none",
            "rating_menu",
            "rating_coupons",
            "rating_menu_link",
        }
        assert set(InteractionLinkLabelCode.values) == {
            "booking",
            "delivery",
            "website",
            "details",
        }

    def test_new_models_have_only_approved_fields(self):
        """Персональные и отчётные данные не дублируются в таблицах ссылок."""

        assert [field.name for field in MessageInteractionLinkDestination._meta.fields] == [
            "id",
            "code",
            "name",
            "label_code",
            "target_url",
            "is_active",
            "created_at",
            "updated_at",
        ]
        assert [field.name for field in MessageInteractionTrackedLink._meta.fields] == [
            "interaction",
            "public_token",
            "label_code",
            "target_url",
            "created_at",
            "disabled_at",
        ]
        assert [field.name for field in MessageInteractionLinkTransition._meta.fields] == [
            "id",
            "tracked_link",
            "received_at",
        ]

    def test_source_requires_destination_only_for_link_button_set(self):
        """База не допускает ссылочный набор без назначения и назначение без набора."""

        template = MessageTemplate.objects.create(
            name="Шаблон ссылочного набора",
            message_text="Тест",
        )
        destination = _create_destination()

        with pytest.raises(IntegrityError), transaction.atomic():
            Mailing.objects.create(
                **_mailing_fields(
                    template,
                    button_set=InteractionButtonSet.RATING_MENU_LINK,
                    destination=None,
                )
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            Mailing.objects.create(
                **_mailing_fields(
                    template,
                    button_set=InteractionButtonSet.RATING_MENU,
                    destination=destination,
                )
            )

        mailing = Mailing.objects.create(
            **_mailing_fields(
                template,
                button_set=InteractionButtonSet.RATING_MENU_LINK,
                destination=destination,
            )
        )
        assert mailing.tracked_link_destination == destination

    def test_scenario_requires_destination_for_link_button_set(self):
        """То же условие действует на автоматические сценарии."""

        template = MessageTemplate.objects.create(
            name="Шаблон сценария со ссылкой",
            message_text="Тест",
        )
        destination = _create_destination()

        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationScenario.objects.create(
                code="tracked_link_without_destination",
                name="Сценарий без назначения",
                template=template,
                button_set=InteractionButtonSet.RATING_MENU_LINK,
            )

        scenario = NotificationScenario.objects.create(
            code="tracked_link_with_destination",
            name="Сценарий с назначением",
            template=template,
            button_set=InteractionButtonSet.RATING_MENU_LINK,
            tracked_link_destination=destination,
        )
        assert scenario.tracked_link_destination == destination

    @pytest.mark.parametrize(
        ("field_name", "invalid_value"),
        [
            ("code", "changed_code"),
            ("label_code", InteractionLinkLabelCode.DELIVERY),
            ("target_url", "https://example.test/changed"),
        ],
    )
    def test_destination_technical_fields_are_immutable(self, field_name, invalid_value):
        """Исправление назначения выполняется новой строкой, а не подменой старой."""

        destination = _create_destination()
        setattr(destination, field_name, invalid_value)

        with pytest.raises(ValidationError, match="нельзя изменять"):
            destination.save()

    def test_destination_name_and_activity_remain_editable(self):
        """Справочник можно пояснять и выключать без смены технического смысла."""

        destination = _create_destination()
        destination.name = "Уточнённое название"
        destination.is_active = False
        destination.save()
        destination.refresh_from_db()

        assert destination.name == "Уточнённое название"
        assert destination.is_active is False

    @pytest.mark.parametrize(
        ("model_name", "create_invalid"),
        [
            (
                "destination",
                lambda: _create_destination(
                    code="insecure_destination",
                    target_url="http://example.test/insecure",
                ),
            ),
            (
                "tracked_link",
                lambda: MessageInteractionTrackedLink.objects.create(
                    interaction=_create_interaction(),
                    public_token=VALID_TOKEN,
                    label_code=InteractionLinkLabelCode.BOOKING,
                    target_url="http://example.test/insecure",
                ),
            ),
        ],
    )
    def test_database_rejects_insecure_target_url(self, model_name, create_invalid):
        """Прямой обход формы не сохраняет незашифрованный адрес назначения."""

        with pytest.raises(IntegrityError), transaction.atomic():
            create_invalid()

    @pytest.mark.parametrize(
        "token",
        [
            "short",
            "AbCdEfGhIjKlMnOpQrStUvWxYz01+=23",
            "AbCdEfGhIjKlMnOpQrStUvWxYz01_-234",
        ],
    )
    def test_database_rejects_invalid_public_token(self, token):
        """Токен имеет ровно 32 безопасных символа Base64URL."""

        with pytest.raises(IntegrityError), transaction.atomic():
            _create_tracked_link(token=token)

    def test_tracked_link_is_allowed_only_for_link_button_set(self):
        """Снимок нельзя связать с сообщением действующего двухрядного набора."""

        interaction = _create_interaction(button_set=InteractionButtonSet.RATING_MENU)

        with pytest.raises(ValidationError, match="допустима только"):
            _create_tracked_link(interaction=interaction)

    @pytest.mark.parametrize(
        ("field_name", "invalid_value"),
        [
            ("public_token", "ZbCdEfGhIjKlMnOpQrStUvWxYz01_-23"),
            ("label_code", InteractionLinkLabelCode.DETAILS),
            ("target_url", "https://example.test/changed"),
        ],
    )
    def test_tracked_link_snapshot_is_immutable(self, field_name, invalid_value):
        """Уже отправленный адрес, подпись и токен нельзя подменить."""

        tracked_link = _create_tracked_link()
        setattr(tracked_link, field_name, invalid_value)

        with pytest.raises(ValidationError, match="нельзя править"):
            tracked_link.save()

    def test_each_transition_is_stored_and_protects_link(self):
        """Повторные запросы не дедуплицируются и сохраняют исходную ссылку."""

        tracked_link = _create_tracked_link()
        first = MessageInteractionLinkTransition.objects.create(tracked_link=tracked_link)
        second = MessageInteractionLinkTransition.objects.create(tracked_link=tracked_link)

        assert first.received_at is not None
        assert second.received_at is not None
        assert tracked_link.transitions.count() == 2
        with pytest.raises(ProtectedError):
            tracked_link.delete()

    def test_one_interaction_has_only_one_tracked_link(self):
        """Повторная ссылка для того же сообщения отклоняется первичным ключом."""

        interaction = _create_interaction()
        _create_tracked_link(interaction=interaction)

        with pytest.raises(IntegrityError), transaction.atomic():
            MessageInteractionTrackedLink.objects.bulk_create(
                [
                    MessageInteractionTrackedLink(
                        interaction=interaction,
                        public_token="BbCdEfGhIjKlMnOpQrStUvWxYz01_-23",
                        label_code=InteractionLinkLabelCode.BOOKING,
                        target_url="https://example.test/booking",
                    )
                ]
            )
