"""Добавляет отслеживаемые ссылки и справочник разрешённых назначений.

Миграция разделяет состояние Django и физические действия. Повторный запуск
физической части не создаёт дубли таблиц, ограничений, индексов или справочных
строк, а несовместимую частичную схему останавливает с понятной ошибкой.
"""

from __future__ import annotations

import importlib

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


_base_schema = importlib.import_module("guests.migrations.0060_message_interactions")


INITIAL_DESTINATIONS = (
    (
        "delivery_main",
        "RestMarket — общая доставка",
        "delivery",
        "https://rest.market/",
    ),
    (
        "delivery_susami",
        "Сами Сусами — доставка",
        "delivery",
        "https://susami.rest.market/",
    ),
    (
        "delivery_uzbechka",
        "Узбечка — доставка",
        "delivery",
        "https://uzbechka.rest.market/",
    ),
    (
        "delivery_gruzinka",
        "Грузинка Нани — доставка",
        "delivery",
        "https://gruzinka.rest.market/",
    ),
    (
        "delivery_china",
        "Чина — доставка",
        "delivery",
        "https://china.rest.market/",
    ),
    (
        "booking_gruzinka",
        "Грузинка Нани — бронирование",
        "booking",
        "https://gruzinka.restoplace.ws/",
    ),
    (
        "booking_susami",
        "Сами Сусами — бронирование",
        "booking",
        "https://susami.restoplace.ws/",
    ),
    (
        "booking_china",
        "Чина — бронирование",
        "booking",
        "https://china.restoplace.ws/",
    ),
    (
        "booking_uzbechka",
        "Узбечка — бронирование",
        "booking",
        "https://usbechka.restoplace.ws/",
    ),
)


def _constraint(model, name):
    """Возвращает именованное ограничение из конечного состояния модели."""

    return next(item for item in model._meta.constraints if item.name == name)


def _replace_evolved_check_constraint(schema_editor, model, name):
    """Идемпотентно заменяет прежнее условие ограничения новым условием."""

    discovered = _base_schema._constraints(  # noqa: SLF001 - неизменяемая миграция 0060.
        schema_editor.connection,
        model._meta.db_table,
    )
    details = discovered.get(_base_schema._normalized(name))  # noqa: SLF001
    expected = _constraint(model, name)
    if details is None:
        _base_schema._ensure_named_constraint(schema_editor, model, expected)  # noqa: SLF001
        return
    if not details.get("check"):
        raise _base_schema.InteractionSchemaError(
            f"Таблица {model._meta.db_table}: объект {name} существует, "
            "но не является проверочным ограничением."
        )
    if _base_schema._conditions_are_compatible(  # noqa: SLF001
        schema_editor,
        model,
        expected,
        name,
    ):
        return

    # Имя ограничения принадлежит предыдущей миграции проекта, поэтому его
    # условие можно безопасно заменить, не трогая сторонние объекты схемы.
    schema_editor.remove_constraint(model, expected)
    _base_schema._ensure_named_constraint(schema_editor, model, expected)  # noqa: SLF001


def _ensure_source_foreign_key(schema_editor, model, field_name, constraint_name):
    """Добавляет и строго проверяет новую связь существующей исходной таблицы."""

    connection = schema_editor.connection
    table_name = model._meta.db_table
    if _base_schema._normalized(table_name) not in _base_schema._table_names(connection):  # noqa: SLF001
        raise _base_schema.InteractionSchemaError(
            f"Не найдена обязательная существующая таблица {table_name}."
        )

    field = model._meta.get_field(field_name)
    columns = _base_schema._column_descriptions(connection, table_name)  # noqa: SLF001
    description = columns.get(_base_schema._normalized(field.column))  # noqa: SLF001
    if description is None:
        schema_editor.add_field(model, field)
        columns = _base_schema._column_descriptions(connection, table_name)  # noqa: SLF001
        description = columns.get(_base_schema._normalized(field.column))  # noqa: SLF001
    if description is None:
        raise _base_schema.InteractionSchemaError(
            f"Не удалось создать столбец {table_name}.{field.column}."
        )

    actual_type = _base_schema._column_type_family(connection, description)  # noqa: SLF001
    allowed_types = _base_schema._allowed_type_families(field)  # noqa: SLF001
    raw_type = str(getattr(description, "type_code", "") or "").casefold()
    if (
        actual_type in {"CharField", "TextField"}
        or "char" in raw_type
        or "text" in raw_type
        or (actual_type and actual_type not in allowed_types)
    ):
        raise _base_schema.InteractionSchemaError(
            f"Таблица {table_name}: столбец {field.column} имеет несовместимый "
            f"тип {actual_type}; ожидался один из {sorted(allowed_types)}."
        )
    _base_schema._validate_column(connection, model, field, description)  # noqa: SLF001
    discovered = _base_schema._constraints(connection, table_name)  # noqa: SLF001
    if not _base_schema._has_foreign_key(discovered, field):  # noqa: SLF001
        _base_schema._add_foreign_key(schema_editor, model, field)  # noqa: SLF001
    _base_schema._ensure_named_constraint(  # noqa: SLF001
        schema_editor,
        model,
        _constraint(model, constraint_name),
    )


def _relation_storage_field(field):
    """Раскрывает цепочку связанных первичных ключей до физического поля."""

    storage_field = field
    visited = set()
    while isinstance(storage_field, (models.ForeignKey, models.OneToOneField)):
        marker = (storage_field.model._meta.label_lower, storage_field.name)
        if marker in visited:
            raise _base_schema.InteractionSchemaError(
                f"Обнаружена циклическая цепочка связей для поля {field.column}."
            )
        visited.add(marker)
        storage_field = storage_field.target_field
    return storage_field


def _validate_transition_column(connection, model, field, description):
    """Проверяет столбец, учитывая связанный первичный ключ снимка ссылки."""

    if not isinstance(field, (models.ForeignKey, models.OneToOneField)):
        _base_schema._validate_column(connection, model, field, description)  # noqa: SLF001
        return

    storage_field = _relation_storage_field(field)
    actual_type = _base_schema._column_type_family(connection, description)  # noqa: SLF001
    allowed_types = _base_schema._allowed_type_families(storage_field)  # noqa: SLF001
    if actual_type and actual_type not in allowed_types:
        raise _base_schema.InteractionSchemaError(
            f"Таблица {model._meta.db_table}: столбец {field.column} имеет "
            f"несовместимый тип {actual_type}; ожидался один из {sorted(allowed_types)}."
        )
    if bool(description.null_ok) != bool(field.null):
        raise _base_schema.InteractionSchemaError(
            f"Таблица {model._meta.db_table}: несовместимая обязательность "
            f"столбца {field.column}."
        )


def _ensure_transition_table(schema_editor, model):
    """Создаёт или строго восстанавливает таблицу повторяемых переходов."""

    connection = schema_editor.connection
    table_name = model._meta.db_table
    if _base_schema._normalized(table_name) not in _base_schema._table_names(connection):  # noqa: SLF001
        schema_editor.create_model(model)
        return

    columns = _base_schema._column_descriptions(connection, table_name)  # noqa: SLF001
    missing_fields = [
        field
        for field in model._meta.local_fields
        if _base_schema._normalized(field.column) not in columns  # noqa: SLF001
    ]
    if missing_fields and connection.vendor == "sqlite" and not _base_schema._table_has_rows(  # noqa: SLF001
        schema_editor,
        table_name,
    ):
        _base_schema._recreate_empty_sqlite_table(schema_editor, model)  # noqa: SLF001
        return
    for field in missing_fields:
        if _base_schema._table_has_rows(schema_editor, table_name):  # noqa: SLF001
            raise _base_schema.InteractionSchemaError(
                f"Таблица {table_name} содержит данные, но в ней отсутствует "
                f"обязательный столбец {field.column}."
            )
        schema_editor.add_field(model, field)

    columns = _base_schema._column_descriptions(connection, table_name)  # noqa: SLF001
    for field in model._meta.local_fields:
        description = columns.get(_base_schema._normalized(field.column))  # noqa: SLF001
        if description is None:
            raise _base_schema.InteractionSchemaError(
                f"Таблица {table_name}: отсутствует обязательный столбец {field.column}."
            )
        _validate_transition_column(connection, model, field, description)

    discovered = _base_schema._constraints(connection, table_name)  # noqa: SLF001
    if not _base_schema._has_primary_key(discovered, model._meta.pk):  # noqa: SLF001
        raise _base_schema.InteractionSchemaError(
            f"Таблица {table_name}: столбец {model._meta.pk.column} не является "
            "первичным ключом."
        )
    tracked_link_field = model._meta.get_field("tracked_link")
    if not _base_schema._has_foreign_key(discovered, tracked_link_field):  # noqa: SLF001
        _base_schema._add_foreign_key(schema_editor, model, tracked_link_field)  # noqa: SLF001
    for index in model._meta.indexes:
        _base_schema._ensure_named_index(schema_editor, model, index)  # noqa: SLF001


def _seed_destinations(apps, schema_editor):
    """Создаёт начальный справочник без перезаписи уже существующих строк."""

    Destination = apps.get_model("guests", "MessageInteractionLinkDestination")
    table_name = schema_editor.quote_name(Destination._meta.db_table)
    now = timezone.now()
    for code, name, label_code, target_url in INITIAL_DESTINATIONS:
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                f"SELECT label_code, target_url FROM {table_name} WHERE code = %s",
                [code],
            )
            existing = cursor.fetchone()
            if existing is None:
                cursor.execute(
                    f"INSERT INTO {table_name} "
                    "(code, name, label_code, target_url, is_active, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [code, name, label_code, target_url, True, now, now],
                )
                continue
            if tuple(existing) != (label_code, target_url):
                raise _base_schema.InteractionSchemaError(
                    "Справочник отслеживаемых ссылок содержит несовместимую "
                    f"запись с кодом {code}."
                )


def ensure_tracked_link_schema(apps, schema_editor):
    """Приводит физическую схему ссылок к утверждённому состоянию."""

    Mailing = apps.get_model("guests", "Mailing")
    NotificationScenario = apps.get_model("guests", "NotificationScenario")
    MessageInteraction = apps.get_model("guests", "MessageInteraction")
    Destination = apps.get_model("guests", "MessageInteractionLinkDestination")
    TrackedLink = apps.get_model("guests", "MessageInteractionTrackedLink")
    Transition = apps.get_model("guests", "MessageInteractionLinkTransition")

    _base_schema._ensure_new_model_table(schema_editor, Destination)  # noqa: SLF001
    _base_schema._ensure_new_model_table(schema_editor, TrackedLink)  # noqa: SLF001
    _ensure_transition_table(schema_editor, Transition)

    _ensure_source_foreign_key(
        schema_editor,
        Mailing,
        "tracked_link_destination",
        "mailings_link_destination_chk",
    )
    _ensure_source_foreign_key(
        schema_editor,
        NotificationScenario,
        "tracked_link_destination",
        "ns_link_destination_chk",
    )
    _replace_evolved_check_constraint(
        schema_editor,
        MessageInteraction,
        "mi_button_set_chk",
    )
    _replace_evolved_check_constraint(
        schema_editor,
        Mailing,
        "mailings_button_set_chk",
    )
    _replace_evolved_check_constraint(
        schema_editor,
        NotificationScenario,
        "ns_button_set_chk",
    )
    _seed_destinations(apps, schema_editor)


def noop_reverse(apps, schema_editor):
    """Обратная миграция не удаляет ссылки и историю переходов."""


STATE_OPERATIONS = [
    migrations.CreateModel(
        name="MessageInteractionLinkDestination",
        fields=[
            (
                "id",
                models.BigAutoField(
                    auto_created=True,
                    primary_key=True,
                    serialize=False,
                    verbose_name="ID",
                ),
            ),
            (
                "code",
                models.CharField(
                    help_text="Неизменяемый технический код назначения.",
                    max_length=80,
                    unique=True,
                    validators=[
                        django.core.validators.RegexValidator(
                            message=(
                                "Код должен начинаться со строчной латинской буквы и "
                                "содержать только строчные латинские буквы, цифры и "
                                "подчёркивания."
                            ),
                            regex=r"^[a-z][a-z0-9_]*$",
                        )
                    ],
                ),
            ),
            (
                "name",
                models.CharField(
                    help_text="Понятное пользователю название назначения.",
                    max_length=150,
                ),
            ),
            (
                "label_code",
                models.CharField(
                    choices=[
                        ("booking", "Забронировать столик"),
                        ("delivery", "Заказать доставку"),
                        ("website", "Перейти на сайт"),
                        ("details", "Подробнее"),
                    ],
                    help_text="Предопределённая подпись ссылочной кнопки.",
                    max_length=16,
                ),
            ),
            (
                "target_url",
                models.URLField(
                    help_text="Конечный защищённый адрес перенаправления.",
                    max_length=2048,
                ),
            ),
            (
                "is_active",
                models.BooleanField(
                    default=True,
                    help_text="Разрешает выбирать назначение для новых сообщений.",
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
        ],
        options={
            "db_table": "message_interaction_link_destinations",
            "verbose_name": "Назначение отслеживаемой ссылки",
            "verbose_name_plural": "Назначения отслеживаемых ссылок",
        },
    ),
    migrations.RemoveConstraint(model_name="mailing", name="mailings_button_set_chk"),
    migrations.RemoveConstraint(
        model_name="messageinteraction",
        name="mi_button_set_chk",
    ),
    migrations.RemoveConstraint(
        model_name="notificationscenario",
        name="ns_button_set_chk",
    ),
    migrations.AlterField(
        model_name="mailing",
        name="button_set",
        field=models.CharField(
            choices=[
                ("none", "Без кнопок"),
                ("rating_menu", "Оценка и главное меню"),
                ("rating_coupons", "Оценка и купоны"),
                ("rating_menu_link", "Оценка, ссылка и главное меню"),
            ],
            default="none",
            help_text="Набор интерактивных кнопок для современных маршрутов рассылки.",
            max_length=20,
        ),
    ),
    migrations.AlterField(
        model_name="messageinteraction",
        name="button_set",
        field=models.CharField(
            choices=[
                ("rating_menu", "Оценка и главное меню"),
                ("rating_coupons", "Оценка и купоны"),
                ("rating_menu_link", "Оценка, ссылка и главное меню"),
            ],
            help_text="Фактически отправленный набор интерактивных кнопок.",
            max_length=20,
        ),
    ),
    migrations.AlterField(
        model_name="notificationscenario",
        name="button_set",
        field=models.CharField(
            choices=[
                ("none", "Без кнопок"),
                ("rating_menu", "Оценка и главное меню"),
                ("rating_coupons", "Оценка и купоны"),
                ("rating_menu_link", "Оценка, ссылка и главное меню"),
            ],
            default="none",
            help_text="Набор интерактивных кнопок для сообщений этого сценария.",
            max_length=20,
        ),
    ),
    migrations.AddConstraint(
        model_name="messageinteraction",
        constraint=models.CheckConstraint(
            condition=models.Q(
                button_set__in=["rating_menu", "rating_coupons", "rating_menu_link"]
            ),
            name="mi_button_set_chk",
        ),
    ),
    migrations.AddConstraint(
        model_name="messageinteractionlinkdestination",
        constraint=models.CheckConstraint(
            condition=models.Q(
                label_code__in=["booking", "delivery", "website", "details"]
            ),
            name="mild_label_code_chk",
        ),
    ),
    migrations.AddConstraint(
        model_name="messageinteractionlinkdestination",
        constraint=models.CheckConstraint(
            condition=models.Q(target_url__startswith="https://"),
            name="mild_target_https_chk",
        ),
    ),
    migrations.AddField(
        model_name="mailing",
        name="tracked_link_destination",
        field=models.ForeignKey(
            blank=True,
            db_index=False,
            help_text="Предопределённое назначение отслеживаемой ссылки.",
            null=True,
            on_delete=django.db.models.deletion.PROTECT,
            related_name="mailings",
            to="guests.messageinteractionlinkdestination",
        ),
    ),
    migrations.AddField(
        model_name="notificationscenario",
        name="tracked_link_destination",
        field=models.ForeignKey(
            blank=True,
            db_index=False,
            help_text="Предопределённое назначение отслеживаемой ссылки.",
            null=True,
            on_delete=django.db.models.deletion.PROTECT,
            related_name="notification_scenarios",
            to="guests.messageinteractionlinkdestination",
        ),
    ),
    migrations.AddConstraint(
        model_name="mailing",
        constraint=models.CheckConstraint(
            condition=models.Q(
                button_set__in=[
                    "none",
                    "rating_menu",
                    "rating_coupons",
                    "rating_menu_link",
                ]
            ),
            name="mailings_button_set_chk",
        ),
    ),
    migrations.AddConstraint(
        model_name="mailing",
        constraint=models.CheckConstraint(
            condition=(
                models.Q(
                    button_set="rating_menu_link",
                    tracked_link_destination__isnull=False,
                )
                | (
                    ~models.Q(button_set="rating_menu_link")
                    & models.Q(tracked_link_destination__isnull=True)
                )
            ),
            name="mailings_link_destination_chk",
        ),
    ),
    migrations.AddConstraint(
        model_name="notificationscenario",
        constraint=models.CheckConstraint(
            condition=models.Q(
                button_set__in=[
                    "none",
                    "rating_menu",
                    "rating_coupons",
                    "rating_menu_link",
                ]
            ),
            name="ns_button_set_chk",
        ),
    ),
    migrations.AddConstraint(
        model_name="notificationscenario",
        constraint=models.CheckConstraint(
            condition=(
                models.Q(
                    button_set="rating_menu_link",
                    tracked_link_destination__isnull=False,
                )
                | (
                    ~models.Q(button_set="rating_menu_link")
                    & models.Q(tracked_link_destination__isnull=True)
                )
            ),
            name="ns_link_destination_chk",
        ),
    ),
    migrations.CreateModel(
        name="MessageInteractionTrackedLink",
        fields=[
            (
                "interaction",
                models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT,
                    primary_key=True,
                    related_name="tracked_link",
                    serialize=False,
                    to="guests.messageinteraction",
                ),
            ),
            (
                "public_token",
                models.CharField(
                    help_text="Криптографически случайный публичный токен Base64URL.",
                    max_length=32,
                    unique=True,
                    validators=[
                        django.core.validators.RegexValidator(
                            message=(
                                "Токен должен содержать ровно 32 символа "
                                "Base64URL без заполнения."
                            ),
                            regex=r"^[A-Za-z0-9_-]{32}$",
                        )
                    ],
                ),
            ),
            (
                "label_code",
                models.CharField(
                    choices=[
                        ("booking", "Забронировать столик"),
                        ("delivery", "Заказать доставку"),
                        ("website", "Перейти на сайт"),
                        ("details", "Подробнее"),
                    ],
                    help_text="Неизменяемый снимок подписи ссылочной кнопки.",
                    max_length=16,
                ),
            ),
            (
                "target_url",
                models.URLField(
                    help_text="Неизменяемый снимок конечного защищённого адреса.",
                    max_length=2048,
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "disabled_at",
                models.DateTimeField(
                    blank=True,
                    help_text=(
                        "Время аварийного вывода уже отправленной ссылки из эксплуатации."
                    ),
                    null=True,
                ),
            ),
        ],
        options={
            "db_table": "message_interaction_tracked_links",
            "verbose_name": "Отслеживаемая ссылка сообщения",
            "verbose_name_plural": "Отслеживаемые ссылки сообщений",
            "constraints": [
                models.CheckConstraint(
                    condition=models.Q(public_token__regex=r"^[A-Za-z0-9_-]{32}$"),
                    name="mitl_token_format_chk",
                ),
                models.CheckConstraint(
                    condition=models.Q(
                        label_code__in=["booking", "delivery", "website", "details"]
                    ),
                    name="mitl_label_code_chk",
                ),
                models.CheckConstraint(
                    condition=models.Q(target_url__startswith="https://"),
                    name="mitl_target_https_chk",
                ),
            ],
        },
    ),
    migrations.CreateModel(
        name="MessageInteractionLinkTransition",
        fields=[
            ("id", models.BigAutoField(primary_key=True, serialize=False)),
            ("received_at", models.DateTimeField(auto_now_add=True)),
            (
                "tracked_link",
                models.ForeignKey(
                    db_index=False,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="transitions",
                    to="guests.messageinteractiontrackedlink",
                ),
            ),
        ],
        options={
            "db_table": "message_interaction_link_transitions",
            "verbose_name": "Переход по отслеживаемой ссылке",
            "verbose_name_plural": "Переходы по отслеживаемым ссылкам",
            "indexes": [
                models.Index(
                    fields=["tracked_link", "received_at"],
                    name="milt_link_received_idx",
                )
            ],
        },
    ),
]


class Migration(migrations.Migration):
    dependencies = [
        ("guests", "0060_message_interactions"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=STATE_OPERATIONS,
        ),
        migrations.RunPython(ensure_tracked_link_schema, noop_reverse),
    ]
