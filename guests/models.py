from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
import os
import uuid

from guests.services.notification_registry import is_registered_notification_scenario_code


class Guest(models.Model):
    id = models.BigAutoField(primary_key=True)
    iiko_id = models.CharField(max_length=50, blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    first_name = models.CharField(max_length=150, blank=True, null=True)
    last_name = models.CharField(max_length=150, blank=True, null=True)
    email = models.CharField(max_length=150, blank=True, null=True)
    birthdate = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    gender= models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        managed = True  # таблица полностью управляется Django migrations
        db_table = "guests"

    def __str__(self):
        return f"{self.first_name or ''} {self.last_name or ''} ({self.phone})"

    # === ВАЖНО: вычисляемые поля для последнего визита ===
    @property
    def last_visit(self):
        """Объект последнего визита (VisitHistory) или None."""
        return (
            self.visits
            .select_related("restaurant")
            .order_by("-visit_date")
            .first()
        )

    @property
    def last_visit_date(self):
        """Дата/время последнего посещения любого заведения."""
        v = self.last_visit
        return v.visit_date if v else None


class Restaurant(models.Model):
    id = models.BigAutoField(primary_key=True)
    iiko_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    name = models.CharField(max_length=255)

    class Meta:
        managed = True
        db_table = "restaurants"

    def __str__(self):
        return self.name


class VisitHistory(models.Model):
    id = models.BigAutoField(primary_key=True)
    guest = models.ForeignKey(
        Guest,
        on_delete=models.CASCADE,
        related_name="visits",      # guest.visits -> все посещения гостя
    )
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="visits",
    )
    visit_date = models.DateTimeField()
    visit_count = models.IntegerField(default=1)

    class Meta:
        managed = True
        db_table = "visit_history"
        unique_together = ("guest", "restaurant")

    def __str__(self):
        return f"{self.guest} -> {self.restaurant} @ {self.visit_date}"
# --- Category (категория) ---
class Category(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=255)
    description = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    # ВАЖНО: теперь даты выставляются автоматически
    created_at = models.DateTimeField(auto_now_add=True)  # была: blank=True, null=True
    updated_at = models.DateTimeField(auto_now=True)  # была: blank=True, null=True

    external_id = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        managed = True
        db_table = "categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # если external_id не задан — сгенерируем автоматически
        if not self.external_id:
            self.external_id = uuid.uuid4().hex  # 32 символа, влезает в max_length=50
        super().save(*args, **kwargs)



# --- GuestCategory (связующая таблица many-to-many) ---
class GuestCategory(models.Model):
    id = models.BigAutoField(primary_key=True)
    guest = models.ForeignKey("Guest", on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    last_assigned_at = models.DateTimeField(null=True, blank=True)
    assign_count = models.IntegerField(default=1)

    class Meta:
        managed = True
        db_table = "guest_categories"
        unique_together = ("guest", "category")

    def __str__(self):
        return f"{self.guest} -> {self.category}"

class GuestCategoryAssignment(models.Model):
    guest = models.ForeignKey("Guest", on_delete=models.CASCADE, db_column="guest_id")
    category = models.ForeignKey("Category", on_delete=models.CASCADE, db_column="category_id")
    restaurant = models.ForeignKey("Restaurant", null=True, on_delete=models.SET_NULL, db_column="restaurant_id")
    assigned_at = models.DateTimeField()

    class Meta:
        db_table = "guest_category_assignments"
        managed = True

class MessageTemplate(models.Model):
    name = models.CharField(max_length=150)
    description = models.CharField(max_length=255, blank=True, null=True)
    message_text = models.TextField()
    created_by = models.CharField(max_length=100, default="test_user")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "message_templates"
        managed = True

    def __str__(self):
        return self.name

class Mailing(models.Model):
    class TargetMode(models.TextChoices):
        PRIMARY_ONLY = "primary_only", "Только основной бот"
        ALL_BOTS = "all_bots", "Все активные боты"

    class QueuePriority(models.TextChoices):
        HIGH = "high", "Высокий"
        NORMAL = "normal", "Обычный"
        BULK = "bulk", "Массовый"

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=150)

    template = models.ForeignKey(
        "MessageTemplate",
        on_delete=models.RESTRICT,
        db_column="template_id",
        related_name="mailings",
    )

    scheduled_date = models.DateField()
    scheduled_time_begin = models.DateTimeField()
    scheduled_time_end = models.DateTimeField()

    is_active = models.BooleanField(default=False)

    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    send_window_begin = models.TimeField()
    send_window_end = models.TimeField()

    target_mode = models.CharField(
        max_length=20,
        choices=TargetMode.choices,
        default=TargetMode.PRIMARY_ONLY,
        help_text="Режим выбора целей: только основной бот или все активные боты гостя.",
    )
    queue_priority = models.CharField(
        max_length=20,
        choices=QueuePriority.choices,
        default=QueuePriority.BULK,
        help_text="Приоритет задач рассылки в универсальной очереди.",
    )

    bot_profiles = models.ManyToManyField(
        "BotProfile",
        through="MailingBotProfileLink",
        related_name="mailings",
        help_text="Список конкретных ботов, через которые должна идти рассылка.",
    )

    class Meta:
        db_table = "mailings"
        managed = True  # таблица полностью управляется Django migrations
        constraints = [
            models.CheckConstraint(
                condition=models.Q(scheduled_time_begin__lte=models.F("scheduled_time_end")),
                name="mailings_time_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(target_mode__in=["primary_only", "all_bots"]),
                name="mailings_target_mode_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(queue_priority__in=["high", "normal", "bulk"]),
                name="mailings_queue_priority_chk",
            ),
        ]

    def __str__(self):
        return f"{self.id}: {self.name}"


class MailingBotProfileLink(models.Model):
    """
    Связь рассылки с конкретными профилями ботов.

    Через эту таблицу определяется, какими именно ботами (Telegram/Max/VK)
    должна отправляться конкретная рассылка.
    """

    id = models.BigAutoField(primary_key=True)

    mailing = models.ForeignKey(
        "Mailing",
        on_delete=models.CASCADE,
        db_column="mailing_id",
        related_name="bot_profile_links",
    )
    bot_profile = models.ForeignKey(
        "BotProfile",
        on_delete=models.RESTRICT,
        db_column="bot_profile_id",
        related_name="mailing_links",
    )

    class Meta:
        db_table = "mailing_bot_profile_links"
        managed = True
        constraints = [
            models.UniqueConstraint(
                fields=["mailing", "bot_profile"],
                name="mailing_bot_profile_links_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["mailing"], name="mbpl_mailing_id_idx"),
            models.Index(fields=["bot_profile"], name="mbpl_bot_profile_id_idx"),
        ]

    def __str__(self):
        return f"mailing={self.mailing_id} bot_profile={self.bot_profile_id}"


class MailingGuest(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned", "запланировано"
        IN_PROGRESS = "in_progress", "выполняется"
        DONE = "done", "завершено"
        ERROR = "error", "ошибка"

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PLANNED
    )

    id = models.BigAutoField(primary_key=True)

    mailing = models.ForeignKey(
        "Mailing",
        on_delete=models.CASCADE,
        db_column="mailing_id",
        related_name="guests_rows",
    )

    guest = models.ForeignKey(
        "Guest",
        on_delete=models.CASCADE,
        db_column="guest_id",
        related_name="mailings_rows",
    )

    phone = models.CharField(max_length=50, blank=True, null=True)
    email = models.CharField(max_length=150, blank=True, null=True)

    text_mailing_list = models.TextField()
    scheduled_datetime = models.DateTimeField()

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNED)

    error_description = models.TextField(blank=True, null=True)
    external_id = models.CharField(max_length=32, blank=True, null=True)

    sent_at = models.DateTimeField(blank=True, null=True)
    delivery_status = models.CharField(max_length=50, blank=True, null=True)

    created_at = models.DateTimeField()

    class Meta:
        db_table = "mailing_guests"
        managed = True  # таблица полностью управляется Django migrations
        constraints = [
            models.UniqueConstraint(fields=["mailing", "guest"], name="mailing_guests_uniq"),
            models.CheckConstraint(
                condition=models.Q(status__in=["planned", "in_progress", "done", "error"]),
                name="mailing_guests_status_chk",
            ),
        ]
        indexes = [
            models.Index(fields=["mailing"], name="mailing_guests_mailing_id_idx"),
            models.Index(fields=["status"], name="mailing_guests_status_idx"),
            models.Index(fields=["scheduled_datetime"], name="mailing_guests_scheduled_idx"),
        ]

    def __str__(self):
        return f"mailing={self.mailing_id} guest={self.guest_id} status={self.status}"

class BotProfile(models.Model):
    """
    Справочник подключенных ботов.

    Модель хранит канальные настройки отправки для каждого провайдера:
    Telegram, Max и VK. Один провайдер может иметь несколько профилей
    (например, разные боты/сообщества для разных брендов).
    """

    class ProviderType(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        MAX = "max", "MAX"
        VK = "vk", "VK"

    code = models.SlugField(
        max_length=64,
        unique=True,
        help_text="Уникальный код профиля бота для внутренних интеграций.",
    )
    name = models.CharField(max_length=150)
    provider_type = models.CharField(
        max_length=32,
        choices=ProviderType.choices,
        db_index=True,
    )
    token = models.TextField(
        blank=True,
        null=True,
        help_text="Токен/секрет доступа. На данном этапе хранится в открытом виде.",
    )
    secret_ref = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        db_index=True,
        help_text=(
            "Ссылка на секрет в окружении (например, BOT_TOKEN_TG_MAIN). "
            "Если указано, токен берётся из переменной окружения, а не из БД."
        ),
    )
    settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Параметры провайдера в JSON (таймауты, базовые URL, флаги и пр.).",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "bot_profiles"
        verbose_name = "Профиль бота"
        verbose_name_plural = "Профили ботов"

    def __str__(self):
        return f"{self.name} ({self.provider_type})"

    def resolve_token(self) -> str:
        """
        Возвращает действующий токен бота.

        Приоритет источников:
        1. Переменная окружения по ключу `secret_ref` (предпочтительный и безопасный путь).
        2. Поле `token` в базе данных (legacy-режим совместимости).
        """
        if self.secret_ref:
            token_from_env = os.getenv(self.secret_ref, "").strip()
            if token_from_env:
                return token_from_env
        return (self.token or "").strip()


class GuestBotBinding(models.Model):
    """
    Привязка гостя к конкретному боту и внешнему идентификатору чата.

    Через эту модель определяется:
    1. куда отправлять сообщения пользователю в конкретном провайдере;
    2. какой бот считается основным для повседневных коммуникаций.
    """

    guest = models.ForeignKey(
        "Guest",
        on_delete=models.CASCADE,
        related_name="bot_bindings",
    )
    bot = models.ForeignKey(
        "BotProfile",
        on_delete=models.CASCADE,
        related_name="guest_bindings",
    )
    external_chat_id = models.CharField(
        max_length=128,
        help_text="Идентификатор чата/peer, куда отправляются сообщения.",
    )
    external_user_id = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        help_text="Опциональный идентификатор пользователя у провайдера.",
    )
    is_primary = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Признак основного бота для гостя.",
    )
    is_active = models.BooleanField(default=True)
    is_opt_in = models.BooleanField(default=True)
    is_stop_sending = models.BooleanField(default=False)
    last_error = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "guest_bot_bindings"
        verbose_name = "Привязка гостя к боту"
        verbose_name_plural = "Привязки гостей к ботам"
        constraints = [
            models.UniqueConstraint(
                fields=["guest", "bot"],
                name="guest_bot_bindings_guest_bot_uniq",
            ),
            models.UniqueConstraint(
                fields=["guest"],
                condition=models.Q(is_primary=True),
                name="guest_bot_bindings_primary_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["bot", "is_active"], name="gbb_bot_active_idx"),
            models.Index(fields=["guest", "is_active"], name="gbb_guest_active_idx"),
        ]

    def __str__(self):
        return f"guest={self.guest_id} bot={self.bot_id} chat={self.external_chat_id}"


class TerminalDepartmentMap(models.Model):
    """
    Сопоставление идентификатора терминала iiko (`terminalGroupId`) и `Department.Id` для OLAP.

    Назначение:
    1. заполнять `department_id` в задачах `OlapCheckSyncJournal`, когда в webhook нет `departmentId`;
    2. хранить верифицированные технические идентификаторы заведения;
    3. исключить ручные корректировки журнала при историческом прогоне.
    """

    organization_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Идентификатор организации iiko (organizationId).",
    )
    terminal_group_id = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Идентификатор терминальной группы iiko (terminalGroupId).",
    )
    restoraunt_group_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Идентификатор группы ресторана из OLAP (RestorauntGroup.Id).",
    )
    department_id = models.CharField(
        max_length=64,
        db_index=True,
        help_text="Идентификатор заведения из OLAP (Department.Id).",
    )
    department_code = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        help_text="Код заведения из OLAP (Department.Code).",
    )
    department_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Название заведения из OLAP (Department).",
    )
    restaurant_section_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        help_text="Идентификатор секции зала из OLAP (RestaurantSection.Id).",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Активность сопоставления для рабочего контура.",
    )
    verified_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Когда сопоставление было проверено вручную/операционно.",
    )
    source_order_num = models.BigIntegerField(
        blank=True,
        null=True,
        help_text="Контрольный номер чека, по которому подтверждали сопоставление.",
    )
    source_business_date = models.DateField(
        blank=True,
        null=True,
        help_text="Контрольная бизнес-дата проверки сопоставления.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "terminal_department_map"
        verbose_name = "Сопоставление terminalGroupId -> Department.Id"
        verbose_name_plural = "Сопоставления terminalGroupId -> Department.Id"
        indexes = [
            models.Index(fields=["organization_id", "is_active"], name="tdm_org_active_idx"),
            models.Index(fields=["department_id", "is_active"], name="tdm_dept_active_idx"),
        ]

    def __str__(self):
        return (
            f"terminal={self.terminal_group_id} -> department={self.department_id}"
        )


class OlapCheckSyncJournal(models.Model):
    """
    Журнал синхронизации чеков с OLAP.

    Таблица хранит задания на дозагрузку чеков из OLAP и служебный статус обработки.
    Важно: `idempotency_key` защищает от повторной постановки одной и той же задачи.
    """

    class Status(models.TextChoices):
        NEW = "new", "Новая"
        IN_PROGRESS = "in_progress", "В работе"
        LOADED = "loaded", "Загружена"
        RETRY = "retry", "Повторить позже"
        FAILED = "failed", "Ошибка"
        SKIPPED = "skipped", "Пропущена"

    idempotency_key = models.CharField(
        max_length=150,
        unique=True,
        db_index=True,
        help_text="Детерминированный ключ задачи для защиты от дублей.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )

    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="olap_check_sync_rows",
        help_text="Гость, для которого требуется дозагрузка чека (если определён).",
    )
    source_webhook_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)

    organization_id = models.CharField(max_length=64, blank=True, null=True)
    terminal_group_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    order_number = models.BigIntegerField(blank=True, null=True, db_index=True)
    order_external_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    transaction_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)

    event_at = models.DateTimeField(blank=True, null=True, db_index=True)
    business_date = models.DateField(blank=True, null=True, db_index=True)

    department_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    department_code = models.CharField(max_length=32, blank=True, null=True)
    restoraunt_group_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    attempt_count = models.PositiveIntegerField(default=0)
    next_try_at = models.DateTimeField(blank=True, null=True, db_index=True)
    last_error = models.TextField(blank=True, null=True)

    locked_at = models.DateTimeField(blank=True, null=True)
    loaded_at = models.DateTimeField(blank=True, null=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "olap_check_sync_journal"
        verbose_name = "Журнал синхронизации чеков с OLAP"
        verbose_name_plural = "Журнал синхронизации чеков с OLAP"
        indexes = [
            models.Index(fields=["status", "next_try_at"], name="ocsj_status_next_idx"),
            models.Index(fields=["order_number", "business_date"], name="ocsj_ord_date_idx"),
            models.Index(fields=["terminal_group_id", "business_date"], name="ocsj_term_date_idx"),
            models.Index(fields=["guest", "business_date"], name="ocsj_guest_date_idx"),
        ]

    def __str__(self):
        return f"sync={self.id} status={self.status} order={self.order_number}"


class OlapSalesRawLine(models.Model):
    """
    Сырые строки OLAP по позициям чека.

    Таблица хранит данные «как пришли» из OLAP для аудита и повторных пересчётов.
    Поле `row_fingerprint` используется как идемпотентный ключ строки позиции.
    """

    row_fingerprint = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="SHA-256 ключ строки позиции для защиты от дублей.",
    )

    sync_journal = models.ForeignKey(
        "OlapCheckSyncJournal",
        on_delete=models.CASCADE,
        related_name="raw_lines",
    )
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="olap_sales_raw_lines",
    )

    business_date = models.DateField(db_index=True)

    department_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    department_code = models.CharField(max_length=32, blank=True, null=True)
    department_name = models.CharField(max_length=255, blank=True, null=True)

    restaurant_section_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    restoraunt_group_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    restoraunt_group_name = models.CharField(max_length=255, blank=True, null=True)

    order_number = models.BigIntegerField(db_index=True)
    uniq_order_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    item_sale_event_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)

    dish_code = models.CharField(max_length=100, blank=True, null=True)
    dish_name = models.CharField(max_length=255, blank=True, null=True)
    dish_category_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    dish_category_name = models.CharField(max_length=255, blank=True, null=True)
    dish_group_id = models.CharField(max_length=100, blank=True, null=True)
    dish_group_name = models.CharField(max_length=255, blank=True, null=True)

    dish_amount = models.DecimalField(max_digits=14, decimal_places=3, blank=True, null=True)
    dish_sum_before_discount = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    dish_sum_after_discount = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    discount_sum = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    bonus_sum = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)

    coupon_series = models.CharField(max_length=100, blank=True, null=True)
    coupon_number = models.CharField(max_length=100, blank=True, null=True)

    raw_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Оригинальная строка OLAP-ответа для аудита и повторной обработки.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "olap_sales_raw_line"
        verbose_name = "Сырая строка OLAP"
        verbose_name_plural = "Сырые строки OLAP"
        indexes = [
            models.Index(fields=["guest", "business_date"], name="osrl_guest_date_idx"),
            models.Index(fields=["department_id", "business_date"], name="osrl_dept_date_idx"),
            models.Index(fields=["order_number", "business_date"], name="osrl_ord_date_idx"),
            models.Index(fields=["dish_category_id", "business_date"], name="osrl_cat_date_idx"),
        ]

    def __str__(self):
        return f"raw={self.id} order={self.order_number} dish={self.dish_code}"


class OrderFact(models.Model):
    """
    Факт чека (одна строка = один заказ).

    Модель предназначена для быстрых аналитических срезов:
    1. средний чек;
    2. частота визитов;
    3. купоны/скидки;
    4. базовые агрегаты по заведению и гостю.
    """

    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_facts",
    )
    business_date = models.DateField(db_index=True)

    department_id = models.CharField(
        max_length=64,
        default="",
        blank=True,
        db_index=True,
        help_text="Идентификатор заведения (Department.Id) из OLAP.",
    )
    department_name = models.CharField(max_length=255, blank=True, null=True)

    order_number = models.BigIntegerField(db_index=True)
    uniq_order_id = models.CharField(
        max_length=100,
        default="",
        blank=True,
        db_index=True,
        help_text="Уникальный идентификатор заказа из OLAP (если передан).",
    )

    gross_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bonus_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    items_count = models.PositiveIntegerField(default=0)
    categories_count = models.PositiveIntegerField(default=0)

    coupon_used = models.BooleanField(default=False, db_index=True)
    coupon_series = models.CharField(max_length=100, blank=True, null=True)
    coupon_number = models.CharField(max_length=100, blank=True, null=True)

    order_type = models.CharField(max_length=64, blank=True, null=True)
    is_delivery = models.BooleanField(default=False, db_index=True)

    first_seen_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Когда заказ впервые был замечен в сыром OLAP-слое.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "order_fact"
        verbose_name = "Факт чека"
        verbose_name_plural = "Факты чеков"
        constraints = [
            models.UniqueConstraint(
                fields=["business_date", "department_id", "order_number", "uniq_order_id"],
                name="order_fact_order_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["guest", "business_date"], name="of_guest_date_idx"),
            models.Index(fields=["department_id", "business_date"], name="of_dept_date_idx"),
            models.Index(fields=["business_date", "order_number"], name="of_date_order_idx"),
        ]

    def __str__(self):
        return f"order_fact={self.id} order={self.order_number} date={self.business_date}"


class GuestRestaurantDailyOrderFact(models.Model):
    """
    Дневной агрегат по гостю и заведению на основе полных чеков.

    Таблица служит быстрым промежуточным слоем для пересчёта общего
    `GuestRestaurantWindowMetrics` без сканирования всего `order_fact`.
    """

    business_date = models.DateField(db_index=True)
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.CASCADE,
        related_name="daily_order_facts",
    )
    department_id = models.CharField(
        max_length=64,
        default="",
        blank=True,
        db_index=True,
        help_text="Идентификатор заведения (Department.Id) из OLAP.",
    )

    orders_count = models.PositiveIntegerField(default=0)
    sum_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bonus_in_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bonus_out_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "guest_restaurant_daily_order_fact"
        verbose_name = "Дневной факт гостя по полным чекам"
        verbose_name_plural = "Дневные факты гостей по полным чекам"
        constraints = [
            models.UniqueConstraint(
                fields=["business_date", "guest", "department_id"],
                name="grdof_uniq_day_guest_dept",
            ),
        ]
        indexes = [
            models.Index(fields=["guest", "department_id", "business_date"], name="grdof_g_dep_date_idx"),
            models.Index(fields=["department_id", "business_date"], name="grdof_dep_date_idx"),
            models.Index(fields=["business_date", "department_id"], name="grdof_date_dep_idx"),
        ]

    def __str__(self):
        return (
            f"grdof={self.id} guest={self.guest_id} dept={self.department_id} "
            f"date={self.business_date}"
        )


class GuestRestaurantDailyCategoryFact(models.Model):
    """
    Дневной агрегат по гостю, заведению и фокусной категории.

    Таблица используется как промежуточный слой для расчёта оконных метрик
    (`7/14/30/60/180`) без сканирования всего сырого OLAP-слоя.
    """

    business_date = models.DateField(db_index=True)
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.CASCADE,
        related_name="daily_category_facts",
    )
    department_id = models.CharField(
        max_length=64,
        default="",
        blank=True,
        db_index=True,
        help_text="Идентификатор заведения (Department.Id) из OLAP.",
    )
    focus_category = models.ForeignKey(
        "FocusCategory",
        on_delete=models.RESTRICT,
        related_name="daily_guest_facts",
    )

    orders_count = models.PositiveIntegerField(default=0)
    items_count = models.PositiveIntegerField(default=0)
    sum_gross = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sum_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bonus_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "guest_restaurant_daily_category_fact"
        verbose_name = "Дневной факт гостя по категории"
        verbose_name_plural = "Дневные факты гостей по категориям"
        constraints = [
            models.UniqueConstraint(
                fields=["business_date", "guest", "department_id", "focus_category"],
                name="grdcf_unique_day_guest_dept_focus",
            ),
        ]
        indexes = [
            models.Index(fields=["guest", "department_id", "business_date"], name="grdcf_guest_dept_date_idx"),
            models.Index(fields=["focus_category", "business_date"], name="grdcf_focus_date_idx"),
        ]

    def __str__(self):
        return (
            f"grdcf={self.id} guest={self.guest_id} dept={self.department_id} "
            f"focus={self.focus_category_id} date={self.business_date}"
        )


class GuestOrderFocusFact(models.Model):
    """
    Связь заказа и фокусной категории (order-level мост).

    Каждая строка описывает факт присутствия категории в конкретном заказе.
    Используется для быстрого расчёта category-window метрик без полного
    пересканирования сырого OLAP-слоя на каждый запуск.
    """

    business_date = models.DateField(db_index=True)
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_focus_facts",
    )
    department_id = models.CharField(
        max_length=64,
        default="",
        blank=True,
        db_index=True,
        help_text="Идентификатор заведения (Department.Id) из OLAP.",
    )

    order_number = models.BigIntegerField(db_index=True)
    uniq_order_id = models.CharField(
        max_length=100,
        default="",
        blank=True,
        db_index=True,
        help_text="Уникальный идентификатор заказа из OLAP (если передан).",
    )
    focus_category = models.ForeignKey(
        "FocusCategory",
        on_delete=models.RESTRICT,
        related_name="order_focus_facts",
    )

    items_count = models.PositiveIntegerField(default=0)
    sum_focus_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "guest_order_focus_fact"
        verbose_name = "Факт заказа по фокусной категории"
        verbose_name_plural = "Факты заказов по фокусным категориям"
        constraints = [
            models.UniqueConstraint(
                fields=["business_date", "department_id", "order_number", "uniq_order_id", "focus_category"],
                name="goff_uniq_order_focus",
            ),
        ]
        indexes = [
            models.Index(fields=["focus_category", "business_date", "department_id"], name="goff_f_cat_date_dep"),
            models.Index(fields=["business_date", "department_id", "focus_category"], name="goff_date_dep_f_cat"),
            models.Index(fields=["guest", "business_date", "focus_category"], name="goff_guest_date_focus"),
            models.Index(
                fields=["business_date", "department_id", "order_number", "uniq_order_id"],
                name="goff_order_join_idx",
            ),
        ]

    def __str__(self):
        return (
            f"goff={self.id} order={self.order_number} dept={self.department_id} "
            f"focus={self.focus_category_id} date={self.business_date}"
        )


class GuestRestaurantWindowMetrics(models.Model):
    """
    Оконные метрики гостя по заведению.

    Формируется на основе дневного слоя по окнам `7/14/30/60/180` и служит
    быстрым источником для сегментации и дашбордов.
    """

    as_of_date = models.DateField(db_index=True)
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.CASCADE,
        related_name="window_metrics",
    )
    department_id = models.CharField(
        max_length=64,
        default="",
        blank=True,
        db_index=True,
        help_text="Идентификатор заведения (Department.Id) из OLAP.",
    )
    window_days = models.PositiveIntegerField(
        db_index=True,
        help_text="Размер окна в днях (например 7/14/30/60/180).",
    )

    orders_count = models.PositiveIntegerField(default=0)
    visits_count = models.PositiveIntegerField(default=0)

    avg_check_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sum_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bonus_in_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bonus_out_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    last_visit_at = models.DateField(blank=True, null=True, db_index=True)
    rating_score = models.DecimalField(max_digits=14, decimal_places=2, default=0, db_index=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "guest_restaurant_window_metrics"
        verbose_name = "Оконная метрика гостя"
        verbose_name_plural = "Оконные метрики гостей"
        constraints = [
            models.UniqueConstraint(
                fields=["as_of_date", "guest", "department_id", "window_days"],
                name="grwm_unique_asof_guest_dept_window",
            ),
        ]
        indexes = [
            models.Index(fields=["department_id", "window_days", "rating_score"], name="grwm_dept_win_rating_idx"),
            models.Index(fields=["guest", "window_days", "as_of_date"], name="grwm_guest_window_date_idx"),
        ]

    def __str__(self):
        return (
            f"grwm={self.id} guest={self.guest_id} dept={self.department_id} "
            f"window={self.window_days} as_of={self.as_of_date}"
        )


class GuestRestaurantWindowCategoryMetrics(models.Model):
    """
    Оконные метрики гостя по заведению и фокусной категории.

    Слой используется в режиме workbench с выбранной категорией, где метрики
    рассчитываются по заказам, содержащим эту категорию.
    """

    as_of_date = models.DateField(db_index=True)
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.CASCADE,
        related_name="window_category_metrics",
    )
    department_id = models.CharField(
        max_length=64,
        default="",
        blank=True,
        db_index=True,
        help_text="Идентификатор заведения (Department.Id) из OLAP.",
    )
    window_days = models.PositiveIntegerField(
        db_index=True,
        help_text="Размер окна в днях (например 7/14/30/60/180).",
    )
    focus_category = models.ForeignKey(
        "FocusCategory",
        on_delete=models.RESTRICT,
        related_name="window_category_metrics",
    )

    orders_count = models.PositiveIntegerField(default=0)
    visits_count = models.PositiveIntegerField(default=0)

    avg_check_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sum_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sum_focus_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bonus_in_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bonus_out_sum = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    last_visit_at = models.DateField(blank=True, null=True, db_index=True)
    rating_score = models.DecimalField(max_digits=14, decimal_places=2, default=0, db_index=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "guest_restaurant_window_category_metrics"
        verbose_name = "Оконная метрика гостя по категории"
        verbose_name_plural = "Оконные метрики гостей по категориям"
        constraints = [
            models.UniqueConstraint(
                fields=["as_of_date", "guest", "department_id", "window_days", "focus_category"],
                name="grwcm_uniq_asof_guest_dept_win_focus",
            ),
        ]
        indexes = [
            models.Index(
                fields=["department_id", "window_days", "focus_category", "rating_score"],
                name="grwcm_dept_win_focus_rt_idx",
            ),
            models.Index(fields=["guest", "window_days", "as_of_date"], name="grwcm_guest_window_date_idx"),
            models.Index(
                fields=["focus_category", "as_of_date", "window_days"],
                name="grwcm_focus_date_window_idx",
            ),
        ]

    def __str__(self):
        return (
            f"grwcm={self.id} guest={self.guest_id} dept={self.department_id} "
            f"focus={self.focus_category_id} window={self.window_days} as_of={self.as_of_date}"
        )


class GuestWorkbenchFilterPreset(models.Model):
    """
    Сохранённый пресет фильтров рабочего экрана гостей (workbench).

    Используется для быстрых повторных отборов маркетолога без ручного
    заполнения формы фильтров.
    """

    name = models.CharField(
        max_length=120,
        unique=True,
        help_text="Уникальное имя пресета фильтра.",
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Короткое описание назначения пресета.",
    )
    window_days = models.PositiveIntegerField(
        default=30,
        db_index=True,
        help_text="Размер окна метрик в днях.",
    )
    department_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Department.Id для отбора. Пусто — все заведения.",
    )
    segment_code = models.CharField(
        max_length=32,
        blank=True,
        default="",
        db_index=True,
        help_text="Код сегмента активности из workbench.",
    )
    focus_category_code = models.SlugField(
        max_length=80,
        blank=True,
        default="",
        db_index=True,
        help_text="Код фокусной категории из workbench.",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Включён ли пресет для отображения в интерфейсе.",
    )
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "guest_workbench_filter_preset"
        verbose_name = "Пресет фильтра workbench"
        verbose_name_plural = "Пресеты фильтров workbench"
        indexes = [
            models.Index(fields=["is_active", "updated_at"], name="gwfp_active_updated_idx"),
        ]

    def __str__(self):
        return self.name


class OlapCategoryDict(models.Model):
    """
    Справочник категорий из OLAP.

    Хранит категории номенклатуры, пришедшие из iiko OLAP, с внешним идентификатором.
    Используется как первичный слой сопоставления категории из отчёта с внутренней аналитикой.
    """

    iiko_category_external_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Внешний идентификатор категории из iiko OLAP.",
    )
    category_name = models.CharField(
        max_length=255,
        help_text="Название категории из OLAP.",
    )
    first_seen_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Когда категория впервые встретилась в данных OLAP.",
    )
    last_seen_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Когда категория в последний раз встретилась в данных OLAP.",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Участвует ли категория в текущем справочнике.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "olap_category_dict"
        verbose_name = "Справочник категорий OLAP"
        verbose_name_plural = "Справочник категорий OLAP"
        indexes = [
            models.Index(fields=["category_name"], name="ocd_name_idx"),
            models.Index(fields=["is_active", "category_name"], name="ocd_active_name_idx"),
        ]

    def __str__(self):
        return f"{self.category_name} ({self.iiko_category_external_id})"


class OlapNomenclatureDict(models.Model):
    """
    Справочник номенклатуры из OLAP.

    Каждая запись соответствует конкретной позиции меню/блюду из OLAP и содержит
    стабильный внешний идентификатор iiko.
    """

    iiko_nomenclature_external_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Внешний идентификатор номенклатуры (блюда) из iiko OLAP.",
    )
    nomenclature_name = models.CharField(
        max_length=255,
        help_text="Название номенклатуры (блюда) из OLAP.",
    )
    olap_category = models.ForeignKey(
        "OlapCategoryDict",
        on_delete=models.RESTRICT,
        related_name="nomenclatures",
        help_text="Категория OLAP, к которой относится номенклатура.",
    )
    iiko_dish_group_external_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text="Внешний идентификатор группы блюда из OLAP (если доступен).",
    )
    dish_group_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Название группы блюда из OLAP.",
    )
    first_seen_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Когда номенклатура впервые встретилась в данных OLAP.",
    )
    last_seen_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Когда номенклатура в последний раз встретилась в данных OLAP.",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Участвует ли номенклатура в текущем справочнике.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "olap_nomenclature_dict"
        verbose_name = "Справочник номенклатуры OLAP"
        verbose_name_plural = "Справочник номенклатуры OLAP"
        indexes = [
            models.Index(fields=["olap_category", "is_active"], name="ond_cat_active_idx"),
            models.Index(fields=["nomenclature_name"], name="ond_name_idx"),
        ]

    def __str__(self):
        return f"{self.nomenclature_name} ({self.iiko_nomenclature_external_id})"


class VirtualCategory(models.Model):
    """
    Пользовательская (виртуальная) категория.

    Формируется маркетологом вручную из номенклатур и/или категорий OLAP.
    Сама по себе не участвует в расчётах, пока не добавлена в focus_category.
    """

    code = models.SlugField(
        max_length=80,
        unique=True,
        help_text="Уникальный технический код виртуальной категории.",
    )
    name = models.CharField(max_length=150, help_text="Название виртуальной категории.")
    description = models.TextField(blank=True, null=True, help_text="Описание логики категории.")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "virtual_category"
        verbose_name = "Виртуальная категория"
        verbose_name_plural = "Виртуальные категории"
        indexes = [
            models.Index(fields=["is_active", "name"], name="vc_active_name_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class VirtualCategoryNomenclatureLink(models.Model):
    """
    Состав виртуальной категории по номенклатурам.

    Явная связь «виртуальная категория -> номенклатура OLAP».
    """

    virtual_category = models.ForeignKey(
        "VirtualCategory",
        on_delete=models.CASCADE,
        related_name="nomenclature_links",
    )
    nomenclature = models.ForeignKey(
        "OlapNomenclatureDict",
        on_delete=models.RESTRICT,
        related_name="virtual_category_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "virtual_category_nomenclature_link"
        verbose_name = "Связь виртуальной категории с номенклатурой"
        verbose_name_plural = "Связи виртуальной категории с номенклатурой"
        constraints = [
            models.UniqueConstraint(
                fields=["virtual_category", "nomenclature"],
                name="vcnl_virtual_nomenclature_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["nomenclature"], name="vcnl_nomenclature_idx"),
        ]

    def __str__(self):
        return f"{self.virtual_category_id}:{self.nomenclature_id}"


class VirtualCategoryOlapCategoryLink(models.Model):
    """
    Состав виртуальной категории по категориям OLAP.

    Связь нужна, когда маркетолог хочет включать в виртуальную категорию
    сразу целые категории OLAP, а не перечислять номенклатуры вручную.
    """

    virtual_category = models.ForeignKey(
        "VirtualCategory",
        on_delete=models.CASCADE,
        related_name="olap_category_links",
    )
    olap_category = models.ForeignKey(
        "OlapCategoryDict",
        on_delete=models.RESTRICT,
        related_name="virtual_category_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "virtual_category_olap_category_link"
        verbose_name = "Связь виртуальной категории с OLAP-категорией"
        verbose_name_plural = "Связи виртуальной категории с OLAP-категориями"
        constraints = [
            models.UniqueConstraint(
                fields=["virtual_category", "olap_category"],
                name="vcocl_virtual_olap_cat_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["olap_category"], name="vcocl_olap_category_idx"),
        ]

    def __str__(self):
        return f"{self.virtual_category_id}:{self.olap_category_id}"


class FocusCategory(models.Model):
    """
    Единый фокус-каталог категорий для аналитики и отбора.

    Категория может ссылаться либо на категорию OLAP напрямую, либо на виртуальную
    категорию. Ограничение целостности контролируется через source_type + CHECK.
    """

    class SourceType(models.TextChoices):
        OLAP_DIRECT = "olap_direct", "Прямая категория OLAP"
        VIRTUAL = "virtual", "Виртуальная категория"

    code = models.SlugField(
        max_length=80,
        unique=True,
        help_text="Уникальный технический код фокусной категории.",
    )
    name = models.CharField(max_length=150, help_text="Название фокусной категории.")
    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
        db_index=True,
        help_text="Источник категории: прямая OLAP-категория или виртуальная категория.",
    )
    olap_category = models.ForeignKey(
        "OlapCategoryDict",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="focus_category_rows",
        help_text="Ссылка на категорию OLAP (только для source_type=olap_direct).",
    )
    virtual_category = models.ForeignKey(
        "VirtualCategory",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="focus_category_rows",
        help_text="Ссылка на виртуальную категорию (только для source_type=virtual).",
    )
    is_enabled = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Включена ли категория в расчёты и отборы.",
    )
    priority_weight = models.PositiveIntegerField(
        default=1,
        help_text="Вес категории в рейтинговых формулах.",
    )
    tag_code = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Технический тег для группировок (например: meat, wine).",
    )
    comment = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "focus_category"
        verbose_name = "Фокусная категория"
        verbose_name_plural = "Фокусные категории"
        constraints = [
            models.CheckConstraint(
                name="focus_category_source_fk_chk",
                condition=(
                    (
                        models.Q(source_type="olap_direct")
                        & models.Q(olap_category__isnull=False)
                        & models.Q(virtual_category__isnull=True)
                    )
                    | (
                        models.Q(source_type="virtual")
                        & models.Q(virtual_category__isnull=False)
                        & models.Q(olap_category__isnull=True)
                    )
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["is_enabled", "source_type", "tag_code"], name="fc_enabled_src_tag_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class FocusCategoryNomenclatureResolved(models.Model):
    """
    Предрассчитанный состав фокусной категории по номенклатурам.

    Таблица служит исключительно для ускорения ночных и оконных расчётов:
    вместо сложных ветвлений выполняется прямой join по номенклатуре.
    """

    class SourceReason(models.TextChoices):
        DIRECT_OLAP = "direct_olap", "Прямая категория OLAP"
        VIRTUAL_NOMENCLATURE = "virtual_nomenclature", "Виртуальная категория по номенклатурам"
        VIRTUAL_OLAP_CATEGORY = "virtual_olap_category", "Виртуальная категория по OLAP-категориям"

    focus_category = models.ForeignKey(
        "FocusCategory",
        on_delete=models.CASCADE,
        related_name="resolved_nomenclatures",
    )
    nomenclature = models.ForeignKey(
        "OlapNomenclatureDict",
        on_delete=models.RESTRICT,
        related_name="resolved_focus_categories",
    )
    source_reason = models.CharField(
        max_length=30,
        choices=SourceReason.choices,
        db_index=True,
        help_text="Причина, по которой номенклатура вошла в фокусную категорию.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "focus_category_nomenclature_resolved"
        verbose_name = "Предрассчитанная связь фокусной категории и номенклатуры"
        verbose_name_plural = "Предрассчитанные связи фокусных категорий и номенклатур"
        constraints = [
            models.UniqueConstraint(
                fields=["focus_category", "nomenclature"],
                name="fcnr_focus_nomenclature_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["nomenclature"], name="fcnr_nomenclature_idx"),
            models.Index(fields=["focus_category", "source_reason"], name="fcnr_focus_reason_idx"),
        ]

    def __str__(self):
        return f"{self.focus_category_id}:{self.nomenclature_id}"


class NotificationScenario(models.Model):
    """
    Правило автоматизированного уведомления.

    Сценарий описывает политику отправки:
    1. источник триггера (веб-хук/планировщик/ручной запуск);
    2. приоритет, режим выбора ботов и режим распределения по времени;
    3. шаблон сообщения и список разрешённых BotProfile.
    """

    class TriggerType(models.TextChoices):
        WEBHOOK = "webhook", "Веб-хук"
        SCHEDULE = "schedule", "Планировщик"
        MANUAL = "manual", "Ручной запуск"

    class Priority(models.TextChoices):
        HIGH = "high", "Высокий"
        NORMAL = "normal", "Обычный"
        BULK = "bulk", "Массовый"

    class TargetMode(models.TextChoices):
        PRIMARY_ONLY = "primary_only", "Только основной бот"
        ALL_BOTS = "all_bots", "Все активные боты"

    class DistributionMode(models.TextChoices):
        IMMEDIATE = "immediate", "Сразу"
        UNIFORM = "uniform", "Равномерно в окне"

    code = models.SlugField(
        max_length=80,
        unique=True,
        help_text="Уникальный технический код сценария, например balance_changed.",
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, null=True)

    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(
        default=False,
        help_text="Служебный сценарий: запрещено удаление/изменение через пользовательский UI.",
    )

    trigger_type = models.CharField(
        max_length=20,
        choices=TriggerType.choices,
        default=TriggerType.WEBHOOK,
        db_index=True,
    )
    webhook_category_external_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Внешний category_id для webhook-сценариев (если используется).",
    )

    template = models.ForeignKey(
        "MessageTemplate",
        on_delete=models.RESTRICT,
        related_name="notification_scenarios",
    )

    priority = models.CharField(
        max_length=16,
        choices=Priority.choices,
        default=Priority.NORMAL,
    )
    target_mode = models.CharField(
        max_length=20,
        choices=TargetMode.choices,
        default=TargetMode.PRIMARY_ONLY,
    )
    distribution_mode = models.CharField(
        max_length=20,
        choices=DistributionMode.choices,
        default=DistributionMode.IMMEDIATE,
    )

    send_window_begin = models.TimeField(blank=True, null=True)
    send_window_end = models.TimeField(blank=True, null=True)
    timezone = models.CharField(max_length=64, default="Asia/Yekaterinburg")

    cooldown_minutes = models.PositiveIntegerField(
        default=0,
        help_text="Минимальная пауза между отправками одному гостю по этому сценарию.",
    )
    max_per_day_per_guest = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="Дневной лимит отправок одному гостю по сценарию (если задан).",
    )

    settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Расширенные параметры сценария в JSON.",
    )

    bot_profiles = models.ManyToManyField(
        "BotProfile",
        through="NotificationScenarioBotProfileLink",
        related_name="notification_scenarios",
        help_text="Список разрешённых ботов для этого сценария.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notification_scenarios"
        verbose_name = "Сценарий уведомления"
        verbose_name_plural = "Сценарии уведомлений"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(priority__in=["high", "normal", "bulk"]),
                name="ns_priority_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(target_mode__in=["primary_only", "all_bots"]),
                name="ns_target_mode_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(distribution_mode__in=["immediate", "uniform"]),
                name="ns_distribution_mode_chk",
            ),
        ]
        indexes = [
            models.Index(fields=["is_active", "trigger_type"], name="ns_active_trigger_idx"),
            models.Index(fields=["code"], name="ns_code_idx"),
        ]

    def clean(self):
        """
        Проверяет, что код сценария зарегистрирован в реестре допустимых кодов.

        Валидация вызывается формами (в том числе Django Admin) и защищает
        от сохранения сценариев с опечатками в `code`.
        """
        super().clean()
        normalized_code = str(self.code or "").strip()
        if not normalized_code:
            return

        if not is_registered_notification_scenario_code(normalized_code):
            raise ValidationError(
                {
                    "code": (
                        f"Код сценария '{normalized_code}' не зарегистрирован. "
                        "Выберите значение из списка поддерживаемых кодов."
                    )
                }
            )
        self.code = normalized_code

    def __str__(self):
        return f"{self.code} ({self.name})"


class NotificationScenarioBotProfileLink(models.Model):
    """
    Явная связь сценария с конкретными ботами.

    Нужна для точного контроля, через какие BotProfile разрешено отправлять
    уведомления в рамках конкретного сценария.
    """

    scenario = models.ForeignKey(
        "NotificationScenario",
        on_delete=models.CASCADE,
        related_name="bot_profile_links",
    )
    bot_profile = models.ForeignKey(
        "BotProfile",
        on_delete=models.RESTRICT,
        related_name="notification_scenario_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notification_scenario_bot_profile_links"
        verbose_name = "Связь сценария с ботом"
        verbose_name_plural = "Связи сценариев с ботами"
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "bot_profile"],
                name="nsbpl_scenario_bot_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["scenario"], name="nsbpl_scenario_idx"),
            models.Index(fields=["bot_profile"], name="nsbpl_bot_idx"),
        ]

    def __str__(self):
        return f"scenario={self.scenario_id} bot={self.bot_profile_id}"


class NotificationEvent(models.Model):
    """
    Факт срабатывания сценария уведомления для конкретного гостя.

    Таблица хранит:
    1. бизнес-факт (почему возникла отправка);
    2. дедупликацию события;
    3. плановое время отправки для создания DispatchTask.
    """

    class SourceType(models.TextChoices):
        WEBHOOK = "webhook", "Веб-хук"
        SCHEDULE = "schedule", "Планировщик"
        MANUAL = "manual", "Ручной запуск"

    class Status(models.TextChoices):
        NEW = "new", "Новое"
        DUPLICATED = "duplicated", "Дубликат"
        TASK_CREATED = "task_created", "Задача создана"
        SKIPPED = "skipped", "Пропущено по правилам"
        ERROR = "error", "Ошибка обработки"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    scenario = models.ForeignKey(
        "NotificationScenario",
        on_delete=models.CASCADE,
        related_name="events",
    )
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="notification_events",
    )

    source_type = models.CharField(max_length=20, choices=SourceType.choices, db_index=True)
    source_ref = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        db_index=True,
        help_text="Внешний идентификатор события (например webhook id).",
    )
    dedupe_key = models.CharField(
        max_length=180,
        help_text="Ключ дедупликации: повтор с тем же ключом не создаёт новую отправку.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )

    event_at = models.DateTimeField(default=timezone.now, db_index=True)
    planned_send_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="Рассчитанное время, когда уведомление можно отправлять в очередь доставки.",
    )

    duplicate_hits = models.PositiveIntegerField(default=0)
    last_duplicate_at = models.DateTimeField(blank=True, null=True)

    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Контекст события для рендера шаблона и аудита.",
    )

    coupon_code = models.CharField(max_length=120, blank=True, null=True)
    coupon_external_id = models.CharField(max_length=150, blank=True, null=True)
    coupon_expires_at = models.DateTimeField(blank=True, null=True)

    error_text = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notification_events"
        verbose_name = "Событие уведомления"
        verbose_name_plural = "События уведомлений"
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "dedupe_key"],
                name="ne_scenario_dedupe_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "planned_send_at"], name="ne_status_plan_idx"),
            models.Index(fields=["guest", "scenario", "event_at"], name="ne_guest_scn_evt_idx"),
        ]

    def __str__(self):
        return f"event={self.id} scenario={self.scenario_id} status={self.status}"


class DispatchTask(models.Model):
    """
    Универсальная задача на отправку сообщения.

    В эту модель попадают задачи из любых источников:
    массовые рассылки, веб-хуки, ручные триггеры. Далее диспетчер
    маршрутизирует их в Redis-очереди нужного провайдера и приоритета.
    """

    class SourceType(models.TextChoices):
        MAILING = "mailing", "Массовая рассылка"
        WEBHOOK = "webhook", "Веб-хук"
        MANUAL = "manual", "Ручной запуск"
        SYSTEM = "system", "Системная задача"

    class Priority(models.TextChoices):
        HIGH = "high", "Высокий"
        NORMAL = "normal", "Обычный"
        BULK = "bulk", "Пакетный"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает отправки"
        QUEUED = "queued", "Поставлено в Redis-очередь"
        IN_PROGRESS = "in_progress", "В обработке"
        DONE = "done", "Успешно отправлено"
        FAILED = "failed", "Ошибка отправки"
        CANCELED = "canceled", "Отменено"

    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )
    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        default=SourceType.MAILING,
    )
    provider_type = models.CharField(
        max_length=32,
        choices=BotProfile.ProviderType.choices,
        db_index=True,
    )
    priority = models.CharField(
        max_length=16,
        choices=Priority.choices,
        default=Priority.BULK,
        db_index=True,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="dispatch_tasks",
    )
    mailing_guest = models.ForeignKey(
        "MailingGuest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="dispatch_tasks",
        help_text="Ссылка на строку массовой рассылки (если задача создана из MailingGuest).",
    )
    notification_scenario = models.ForeignKey(
        "NotificationScenario",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="dispatch_tasks",
        help_text="Ссылка на сценарий авто-уведомления (для задач не из MailingGuest).",
    )
    notification_event = models.ForeignKey(
        "NotificationEvent",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="dispatch_tasks",
        help_text="Ссылка на событие, из которого сформирована задача доставки.",
    )
    bot_profile = models.ForeignKey(
        "BotProfile",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="dispatch_tasks",
    )
    guest_binding = models.ForeignKey(
        "GuestBotBinding",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="dispatch_tasks",
    )
    external_chat_id = models.CharField(max_length=128, blank=True, null=True)

    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Полезная нагрузка задачи (шаблон, переменные, метаданные).",
    )
    message_text = models.TextField(blank=True, default="")

    idempotency_key = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        unique=True,
        help_text="Ключ дедупликации задачи для безопасных повторов.",
    )

    scheduled_at = models.DateTimeField(blank=True, null=True)
    available_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="Время, начиная с которого задачу можно брать в обработку.",
    )
    enqueued_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Время постановки задачи в Redis-очередь провайдера.",
    )
    queue_name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        help_text="Физическое имя Redis-очереди (lane), куда отправлена задача.",
    )

    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    attempt = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    last_error = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dispatch_tasks"
        verbose_name = "Задача диспетчера"
        verbose_name_plural = "Задачи диспетчера"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(attempt__lte=models.F("max_attempts")),
                name="dispatch_tasks_attempt_lte_max",
            ),
        ]
        indexes = [
            models.Index(
                fields=["provider_type", "priority", "status", "available_at"],
                name="dispatch_tasks_lane_idx",
            ),
            models.Index(
                fields=["status", "available_at"],
                name="dispatch_tasks_status_avl_idx",
            ),
            models.Index(
                fields=["guest", "created_at"],
                name="dispatch_tasks_guest_idx",
            ),
        ]

    def __str__(self):
        return f"task={self.id} provider={self.provider_type} priority={self.priority} status={self.status}"




