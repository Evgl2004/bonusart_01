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
    is_archived = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Архивная кампания скрывается из списка по умолчанию и недоступна к запуску.",
    )

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
    source_filter_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="Снимок фильтров рабочего экрана гостей, по которым создана аудитория кампании.",
    )

    bot_profiles = models.ManyToManyField(
        "BotProfile",
        through="MailingBotProfileLink",
        related_name="mailings",
        help_text="Список конкретных ботов, через которые должна идти рассылка.",
    )
    coupon_series = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        db_index=True,
        help_text=(
            "Опциональная серия купонов iikoCard для кампании. "
            "Если заполнено, перед отправкой включается купонный sync-gate."
        ),
    )
    coupon_venue_code = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text=(
            "Код заведения для купонной кампании. "
            "Используется для проверки соответствия серии купонов и кампании."
        ),
    )
    coupon_venue_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Человекочитаемое название заведения купонной кампании.",
    )
    coupon_title = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        help_text="Название купона для кнопки и карточки гостя в vtelemax.",
    )
    coupon_promo_text = models.TextField(
        blank=True,
        null=True,
        help_text=(
            "Текст акции купона для показа гостю в карточке купона "
            "и для передачи в vtelemax."
        ),
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


class VtelemaxRecipientChannel(models.Model):
    """
    Локальный снимок канала получателя из vtelemax.

    Единица записи синка:
    1. один `person_id` + одна `platform`;
    2. хранит актуальные согласия/статусы канала и связь с локальным гостем.
    """

    class Platform(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        MAX = "max", "MAX"
        VK = "vk", "VK"

    person_id = models.UUIDField()
    platform = models.CharField(
        max_length=32,
        choices=Platform.choices,
        db_index=True,
    )
    phone_e164 = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        db_index=True,
    )
    external_id = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        help_text="Идентификатор пользователя/чата в платформе, полученный из vtelemax.",
    )
    rules_accepted = models.BooleanField(default=False)
    notifications_allowed = models.BooleanField(default=False, db_index=True)
    is_registered = models.BooleanField(default=False)
    registered_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Дата/время завершения регистрации канала на стороне vtelemax.",
    )

    state_updated_at = models.DateTimeField(blank=True, null=True)
    account_created_at = models.DateTimeField(blank=True, null=True)
    effective_updated_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Временная метка изменения записи для delta-цикла.",
    )

    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="vtelemax_channels",
    )
    guest_binding = models.ForeignKey(
        "GuestBotBinding",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="vtelemax_channels",
    )
    source_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Последний сырой payload строки канала из API vtelemax.",
    )

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        managed = True
        db_table = "vtelemax_recipient_channels"
        constraints = [
            models.UniqueConstraint(
                fields=["person_id", "platform"],
                name="vtelemax_channels_person_platform_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["platform", "notifications_allowed"], name="vtmx_ch_platform_optin_idx"),
            models.Index(fields=["guest", "platform"], name="vtmx_ch_guest_platform_idx"),
        ]

    def __str__(self):
        return f"person={self.person_id} platform={self.platform}"


class HistoricalTelegramChannel(models.Model):
    """
    Проверенный исторический Telegram-канал для гостя.

    Таблица хранит только адрес доставки и состояние возможности отправки.
    Данные гостя, посещения и регистрацию в новом боте берём из существующих
    контуров, чтобы не создавать вторую версию правды.
    """

    class DeliveryState(models.TextChoices):
        SENDABLE = "sendable", "можно отправлять"
        BLOCKED = "blocked", "заблокирован или недоступен"
        MANUALLY_EXCLUDED = "manually_excluded", "исключён вручную"

    guest = models.ForeignKey(
        "Guest",
        on_delete=models.CASCADE,
        related_name="historical_telegram_channels",
    )
    bot_profile = models.ForeignKey(
        "BotProfile",
        on_delete=models.RESTRICT,
        related_name="historical_telegram_channels",
    )
    telegram_chat_id = models.CharField(max_length=128, db_index=True)
    delivery_state = models.CharField(
        max_length=32,
        choices=DeliveryState.choices,
        default=DeliveryState.SENDABLE,
        db_index=True,
    )
    last_success_at = models.DateTimeField(blank=True, null=True, db_index=True)
    last_error_at = models.DateTimeField(blank=True, null=True)
    last_error_text = models.TextField(blank=True, null=True)
    excluded_at = models.DateTimeField(blank=True, null=True)
    excluded_reason = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "historical_telegram_channels"
        verbose_name = "Исторический Telegram-канал"
        verbose_name_plural = "Исторические Telegram-каналы"
        constraints = [
            models.UniqueConstraint(
                fields=["guest", "bot_profile"],
                name="hist_tg_channel_guest_bot_uniq",
            ),
            models.UniqueConstraint(
                fields=["bot_profile", "telegram_chat_id"],
                name="hist_tg_channel_bot_chat_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["delivery_state", "updated_at"], name="hist_tg_state_updated_idx"),
            models.Index(fields=["guest", "delivery_state"], name="hist_tg_guest_state_idx"),
        ]

    def __str__(self):
        return f"guest={self.guest_id} bot={self.bot_profile_id} chat={self.telegram_chat_id}"


class VtelemaxSyncState(models.Model):
    """
    Состояние потока синхронизации SAGUR <- vtelemax.
    """

    class Status(models.TextChoices):
        IDLE = "idle", "Ожидание"
        RUNNING = "running", "В процессе"
        SUCCESS = "success", "Успешно"
        ERROR = "error", "Ошибка"

    key = models.CharField(
        max_length=64,
        unique=True,
        default="vtelemax_recipients",
        help_text="Ключ синхронизации (singleton для данного потока).",
    )
    watermark = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Текущая нижняя граница `since` для delta.",
    )
    last_status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.IDLE,
        db_index=True,
    )
    last_mode = models.CharField(max_length=16, blank=True, null=True)
    last_error = models.TextField(blank=True, null=True)
    last_rows = models.PositiveIntegerField(default=0)
    last_pages = models.PositiveIntegerField(default=0)
    last_started_at = models.DateTimeField(blank=True, null=True)
    last_finished_at = models.DateTimeField(blank=True, null=True)
    last_success_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = True
        db_table = "vtelemax_sync_state"

    def __str__(self):
        return f"{self.key}: {self.last_status}"


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


class OlapLivePipelineQueue(models.Model):
    """
    Очередь оперативной обработки OLAP-события.

    Таблица хранит состояние короткого конвейера после входящего webhook:
    загрузка OLAP -> сборка OrderFact -> обработка применённого купона.
    `OlapCheckSyncJournal` при этом остаётся журналом только OLAP-загрузки.
    """

    class Status(models.TextChoices):
        NEW = "new", "Новая"
        IN_PROGRESS = "in_progress", "В работе"
        WAITING_OLAP = "waiting_olap", "Ожидает OLAP"
        OLAP_LOADED = "olap_loaded", "OLAP загружен"
        FACT_BUILT = "fact_built", "Факт чека собран"
        DONE = "done", "Завершена"
        RETRY = "retry", "Повторить позже"
        SKIPPED = "skipped", "Пропущена"
        FAILED = "failed", "Ошибка"

    sync_journal = models.OneToOneField(
        "OlapCheckSyncJournal",
        on_delete=models.CASCADE,
        related_name="live_pipeline",
        help_text="Связанная задача загрузки чека из OLAP.",
    )

    source_webhook_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    business_date = models.DateField(blank=True, null=True, db_index=True)
    department_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    order_number = models.BigIntegerField(blank=True, null=True, db_index=True)
    order_external_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(blank=True, null=True, db_index=True)
    locked_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True, null=True)
    last_step_result = models.JSONField(
        default=dict,
        blank=True,
        help_text="Последняя техническая сводка по стадиям оперативного конвейера.",
    )
    processed_at = models.DateTimeField(blank=True, null=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "olap_live_pipeline_queue"
        verbose_name = "Очередь оперативного OLAP-конвейера"
        verbose_name_plural = "Очередь оперативного OLAP-конвейера"
        indexes = [
            models.Index(fields=["status", "next_retry_at", "created_at"], name="olpq_status_next_idx"),
            models.Index(fields=["business_date", "department_id", "order_number"], name="olpq_order_key_idx"),
            models.Index(fields=["source_webhook_id", "status"], name="olpq_source_status_idx"),
        ]

    def __str__(self):
        return f"live_pipeline={self.id} status={self.status} journal={self.sync_journal_id}"


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
            models.Index(fields=["business_date", "department_id", "dish_code"], name="osrl_date_dep_dish_idx"),
            models.Index(fields=["coupon_series", "coupon_number"], name="osrl_coupon_key_idx"),
            models.Index(
                fields=["business_date", "department_id", "order_number", "uniq_order_id"],
                name="osrl_order_key_full_idx",
            ),
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
            models.Index(fields=["coupon_used", "coupon_series", "coupon_number"], name="of_coupon_used_key_idx"),
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
    venue_selection_mode = models.CharField(
        max_length=32,
        blank=True,
        default="visited_once",
        db_index=True,
        help_text="Способ связи гостя с заведением для отбора.",
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
    audience_channel_group = models.CharField(
        max_length=32,
        blank=True,
        default="all",
        db_index=True,
        help_text="Тип аудитории по доступности канала для рассылки.",
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

        self.code = normalized_code
        if not self.is_system:
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


class CouponAutomationConfig(models.Model):
    """
    Купонные настройки для автоматического сценария уведомлений.

    Модель не запускает отправки сама по себе. Она хранит утверждённые правила
    будущего автосценария рядом с `NotificationScenario`, чтобы купонная
    логика не пряталась в свободном JSON без валидации.
    """

    class ExecutionMode(models.TextChoices):
        REPORT_ONLY = "report_only", "Черновик"
        PILOT = "pilot", "Пилот"
        AUTOMATIC = "automatic", "Активен"
        PAUSED = "paused", "Пауза"

    class ScenarioType(models.TextChoices):
        INACTIVE_DAYS_COUPON = "inactive_days_coupon", "Гость не был N дней + купон"
        BIRTHDAY_COUPON = "birthday_coupon", "День рождения + купон"
        BIRTHDATE_FILLED_COUPON = "birthdate_filled_coupon", "Дата рождения заполнена + купон"
        WELCOME_REGISTRATION_COUPON = (
            "welcome_registration_coupon",
            "Регистрация гостя + приветственный купон",
        )

    class VenueSelectionMode(models.TextChoices):
        LAST_ORDER = "last_order", "Последнее заведение"
        ALL_VISITED = "all_visited", "Все посещённые заведения"
        FAVORITE = "favorite", "Любимое заведение"

    class AudienceVenueFilterMode(models.TextChoices):
        DISABLED = "disabled", "Без ограничения по заведению"
        VISITED_ONCE_AND_INACTIVE = (
            "visited_once_and_inactive",
            "Был хотя бы 1 раз и не был N+ дней",
        )

    scenario = models.OneToOneField(
        "NotificationScenario",
        on_delete=models.CASCADE,
        related_name="coupon_automation_config",
        help_text="Сценарий уведомлений, для которого настроена купонная автоматизация.",
    )
    scenario_type = models.CharField(
        max_length=40,
        choices=ScenarioType.choices,
        default=ScenarioType.INACTIVE_DAYS_COUPON,
        db_index=True,
        help_text="Тип купонного автосценария: какая логика отбора гостей используется.",
    )
    execution_mode = models.CharField(
        max_length=24,
        choices=ExecutionMode.choices,
        default=ExecutionMode.REPORT_ONLY,
        db_index=True,
        help_text="Режим работы купонного автосценария.",
    )
    venue_selection_mode = models.CharField(
        max_length=24,
        choices=VenueSelectionMode.choices,
        default=VenueSelectionMode.LAST_ORDER,
        db_index=True,
        help_text="Как выбирать заведения гостя для правил купонного автосценария.",
    )
    audience_venue_filter_mode = models.CharField(
        max_length=32,
        choices=AudienceVenueFilterMode.choices,
        default=AudienceVenueFilterMode.DISABLED,
        db_index=True,
        help_text="Как ограничивать аудиторию автосценария конкретным заведением.",
    )
    audience_venue_code = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Код заведения, по которому отбирается аудитория автосценария.",
    )
    audience_venue_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Название заведения, по которому отбирается аудитория автосценария.",
    )
    coupon_series = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        db_index=True,
        help_text="Серия купонов, из которой автосценарий будет брать доступные купоны.",
    )
    venue_code = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Код заведения или __global__ для сетевой акции.",
    )
    venue_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Название заведения для отображения и payload vtelemax.",
    )
    coupon_validity_days = models.PositiveSmallIntegerField(
        default=14,
        help_text="Срок действия выдаваемого купона в днях.",
    )
    coupon_title_template = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        help_text="Название купона для кнопки и карточки vtelemax; поддерживает переменные шаблона.",
    )
    coupon_promo_text_template = models.TextField(
        blank=True,
        null=True,
        help_text="Текст акции для карточки купона; поддержка переменных добавляется на уровне executor.",
    )
    min_order_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Справочная минимальная сумма заказа, настроенная в iikoCard.",
    )
    iikocard_action_note = models.TextField(
        blank=True,
        null=True,
        help_text="Что именно настроено в iikoCard: подарок, скидка, место продаж, тип заказа.",
    )
    max_recipients_per_run = models.PositiveIntegerField(
        default=100,
        help_text="Максимум гостей, которых сценарий может обработать за один проход.",
    )
    max_active_coupons_per_guest = models.PositiveSmallIntegerField(
        default=1,
        help_text="Защита от нескольких активных купонов одной акции у одного гостя.",
    )
    cooldown_days = models.PositiveIntegerField(
        default=30,
        help_text="Минимальная пауза перед повторным попаданием гостя в этот купонный сценарий.",
    )
    settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Резерв для дополнительных параметров сценария до появления специализированных полей.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "coupon_automation_configs"
        verbose_name = "Купонная настройка автосценария"
        verbose_name_plural = "Купонные настройки автосценариев"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    scenario_type__in=[
                        "inactive_days_coupon",
                        "birthday_coupon",
                        "birthdate_filled_coupon",
                        "welcome_registration_coupon",
                    ]
                ),
                name="cauto_scenario_type_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    execution_mode__in=["report_only", "pilot", "automatic", "paused"]
                ),
                name="cauto_execution_mode_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    venue_selection_mode__in=["last_order", "all_visited", "favorite"]
                ),
                name="cauto_venue_mode_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    audience_venue_filter_mode__in=["disabled", "visited_once_and_inactive"]
                ),
                name="cauto_audience_venue_mode_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(coupon_validity_days__gte=1),
                name="cauto_validity_days_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(max_recipients_per_run__gte=1),
                name="cauto_max_recipients_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(max_active_coupons_per_guest__gte=1),
                name="cauto_max_active_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(cooldown_days__gte=0),
                name="cauto_cooldown_days_gte_0",
            ),
        ]
        indexes = [
            models.Index(fields=["scenario_type"], name="cauto_scenario_type_idx"),
            models.Index(fields=["execution_mode"], name="cauto_mode_idx"),
            models.Index(fields=["venue_selection_mode"], name="cauto_venue_mode_idx"),
            models.Index(fields=["audience_venue_filter_mode"], name="cauto_aud_venue_mode_idx"),
            models.Index(fields=["audience_venue_code"], name="cauto_aud_venue_idx"),
            models.Index(fields=["coupon_series"], name="cauto_series_idx"),
            models.Index(fields=["venue_code"], name="cauto_venue_idx"),
        ]

    def clean(self):
        super().clean()

        if self.coupon_series:
            self.coupon_series = self.coupon_series.strip()
        if self.venue_code:
            self.venue_code = self.venue_code.strip()
        if self.venue_name:
            self.venue_name = self.venue_name.strip()
        if self.audience_venue_code:
            self.audience_venue_code = self.audience_venue_code.strip()
        if self.audience_venue_name:
            self.audience_venue_name = self.audience_venue_name.strip()

        errors = {}

        if self.scenario_type not in {
            self.ScenarioType.INACTIVE_DAYS_COUPON,
            self.ScenarioType.BIRTHDAY_COUPON,
            self.ScenarioType.BIRTHDATE_FILLED_COUPON,
            self.ScenarioType.WELCOME_REGISTRATION_COUPON,
        }:
            errors["scenario_type"] = "Выберите тип купонного автосценария."
        if self.venue_selection_mode not in {
            self.VenueSelectionMode.LAST_ORDER,
            self.VenueSelectionMode.ALL_VISITED,
            self.VenueSelectionMode.FAVORITE,
        }:
            errors["venue_selection_mode"] = "Выберите способ выбора заведений."
        if self.audience_venue_filter_mode not in {
            self.AudienceVenueFilterMode.DISABLED,
            self.AudienceVenueFilterMode.VISITED_ONCE_AND_INACTIVE,
        }:
            errors["audience_venue_filter_mode"] = "Выберите способ отбора гостей по заведению."
        if self.audience_venue_filter_mode == self.AudienceVenueFilterMode.DISABLED:
            self.audience_venue_code = None
            self.audience_venue_name = None
        elif not self.audience_venue_code:
            errors["audience_venue_code"] = "Для отбора гостей выберите заведение."
        if self.coupon_validity_days is not None and self.coupon_validity_days < 1:
            errors["coupon_validity_days"] = "Срок действия купона должен быть не меньше 1 дня."
        if self.max_recipients_per_run is not None and self.max_recipients_per_run < 1:
            errors["max_recipients_per_run"] = "Лимит получателей за проход должен быть не меньше 1."
        if self.max_active_coupons_per_guest is not None and self.max_active_coupons_per_guest < 1:
            errors["max_active_coupons_per_guest"] = "Лимит активных купонов гостя должен быть не меньше 1."
        if self.cooldown_days is not None and self.cooldown_days < 0:
            errors["cooldown_days"] = "Пауза повторного попадания не может быть отрицательной."
        if self.min_order_amount is not None and self.min_order_amount < 0:
            errors["min_order_amount"] = "Минимальная сумма заказа не может быть отрицательной."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"scenario={self.scenario_id} mode={self.execution_mode} series={self.coupon_series or '-'}"


class CouponAutomationRule(models.Model):
    """
    Купонное правило внутри автосценария.

    Один автосценарий может иметь несколько правил: по конкретным заведениям и
    одно общее правило для всей сети. Исполнитель выбирает одно правило на гостя.
    """

    class ScopeType(models.TextChoices):
        VENUE = "venue", "Заведение"
        GLOBAL = "global", "Вся сеть (global)"

    config = models.ForeignKey(
        "CouponAutomationConfig",
        on_delete=models.CASCADE,
        related_name="coupon_rules",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    scope_type = models.CharField(
        max_length=16,
        choices=ScopeType.choices,
        default=ScopeType.VENUE,
        db_index=True,
    )
    coupon_series = models.CharField(
        max_length=120,
        db_index=True,
        help_text="Серия купонов, из которой правило будет брать доступные купоны.",
    )
    venue_code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Department.Id для правила по заведению; для правила Вся сеть (global) хранится __global__.",
    )
    venue_name = models.CharField(max_length=255, blank=True, default="")
    coupon_validity_days = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        help_text="Если задано, переопределяет срок действия из общей настройки.",
    )
    priority = models.PositiveSmallIntegerField(
        default=100,
        db_index=True,
        help_text="Меньшее значение означает более высокий приоритет среди правил одного типа.",
    )
    min_order_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Справочная минимальная сумма заказа, настроенная в iikoCard для этого правила.",
    )
    iikocard_action_note = models.TextField(blank=True, null=True)
    coupon_title_template = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        help_text="Если задано, переопределяет название купона из общей настройки.",
    )
    coupon_promo_text_template = models.TextField(
        blank=True,
        null=True,
        help_text="Если задано, переопределяет описание купона из общей настройки.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "coupon_automation_rules"
        verbose_name = "Купонное правило автосценария"
        verbose_name_plural = "Купонные правила автосценариев"
        indexes = [
            models.Index(fields=["config", "is_active"], name="cautorule_cfg_active_idx"),
            models.Index(fields=["config", "scope_type", "priority"], name="cautorule_cfg_scope_pri"),
            models.Index(fields=["coupon_series"], name="cautorule_series_idx"),
            models.Index(fields=["venue_code"], name="cautorule_venue_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(scope_type__in=["venue", "global"]),
                name="cautorule_scope_type_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(coupon_validity_days__isnull=True)
                | models.Q(coupon_validity_days__gte=1),
                name="cautorule_validity_null_or_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(min_order_amount__isnull=True)
                | models.Q(min_order_amount__gte=0),
                name="cautorule_min_order_null_or_gte_0",
            ),
        ]

    def clean(self):
        super().clean()

        if self.coupon_series:
            self.coupon_series = self.coupon_series.strip()
        if self.venue_code:
            self.venue_code = self.venue_code.strip()
        if self.venue_name:
            self.venue_name = self.venue_name.strip()

        errors = {}
        if not self.coupon_series:
            errors["coupon_series"] = "Укажите серию купонов для правила."
        if self.scope_type == self.ScopeType.GLOBAL:
            self.venue_code = "__global__"
            if not self.venue_name:
                self.venue_name = "Вся сеть"
        elif not self.venue_code:
            errors["venue_code"] = "Для правила по заведению укажите код заведения."

        if self.coupon_validity_days is not None and self.coupon_validity_days < 1:
            errors["coupon_validity_days"] = "Срок действия купона должен быть не меньше 1 дня."
        if self.min_order_amount is not None and self.min_order_amount < 0:
            errors["min_order_amount"] = "Минимальная сумма заказа не может быть отрицательной."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        scope = "global" if self.scope_type == self.ScopeType.GLOBAL else self.venue_code
        return f"config={self.config_id} scope={scope} series={self.coupon_series}"


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


class CouponPoolBatch(models.Model):
    """
    Партия генерации купонов для последующей загрузки в iikoCard.

    Назначение:
    1. фиксировать параметры генерации пула;
    2. хранить ссылку на экспортный CSV;
    3. хранить итог проверки фактической загрузки в iikoCard.
    """

    class AlphabetMode(models.TextChoices):
        DIGITS = "digits", "Только цифры"
        LATIN_UPPER = "latin_upper", "Только латинские буквы (верхний регистр)"
        LATIN_CYRILLIC_LOOKALIKE_UPPER = (
            "latin_cyrillic_lookalike_upper",
            "Латинские буквы, похожие на кириллицу",
        )
        DIGITS_LATIN_CYRILLIC_LOOKALIKE_UPPER = (
            "digits_latin_lookalike_upper",
            "Цифры и латинские буквы, похожие на кириллицу",
        )
        DIGITS_LATIN_UPPER = "digits_latin_upper", "Цифры и латинские буквы (верхний регистр)"

    class VerificationStatus(models.TextChoices):
        NOT_CHECKED = "not_checked", "Не проверено"
        PARTIALLY_LOADED = "partially_loaded", "Частично загружено"
        LOADED = "loaded", "Загружено"
        FAILED = "failed", "Проверка завершилась ошибкой"

    batch_code = models.CharField(
        max_length=80,
        unique=True,
        db_index=True,
        help_text="Уникальный технический код партии (например, TEST_20260514_001).",
    )
    series = models.CharField(max_length=120, db_index=True, help_text="Серия купонов в iikoCard.")
    venue_code = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Код заведения, для которого сформирован пул купонов.",
    )
    venue_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Название заведения, для которого сформирован пул купонов.",
    )
    prefix = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        help_text="Префикс перед случайной частью кода купона (например, TST-).",
    )
    alphabet_mode = models.CharField(
        max_length=32,
        choices=AlphabetMode.choices,
        default=AlphabetMode.DIGITS_LATIN_UPPER,
        help_text="Режим алфавита при генерации случайной части купона.",
    )
    random_length = models.PositiveSmallIntegerField(
        default=12,
        help_text="Длина случайной части кода купона.",
    )
    count_requested = models.PositiveIntegerField(default=0)
    count_generated = models.PositiveIntegerField(default=0)
    generated_by = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        help_text="Пользователь/оператор, запустивший генерацию.",
    )
    export_file_path = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Абсолютный или относительный путь к CSV, выгруженному для iikoCard.",
    )
    verification_status = models.CharField(
        max_length=24,
        choices=VerificationStatus.choices,
        default=VerificationStatus.NOT_CHECKED,
        db_index=True,
    )
    last_verified_at = models.DateTimeField(blank=True, null=True, db_index=True)
    verified_found_count = models.PositiveIntegerField(default=0)
    verified_not_found_count = models.PositiveIntegerField(default=0)
    verification_note = models.TextField(blank=True, null=True)
    generated_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "coupon_pool_batches"
        verbose_name = "Партия купонов"
        verbose_name_plural = "Партии купонов"
        indexes = [
            models.Index(fields=["series", "verification_status"], name="cpbatch_series_ver_idx"),
            models.Index(fields=["venue_code", "verification_status"], name="cpbatch_venue_ver_idx"),
            models.Index(fields=["generated_at"], name="cpbatch_generated_idx"),
        ]

    def __str__(self):
        return f"{self.batch_code} ({self.series})"


class CouponRegistryEntry(models.Model):
    """
    Локальный реестр купонов для управления назначением и жизненным циклом.
    """

    class SourceType(models.TextChoices):
        GENERATED = "generated", "Сгенерировано в SAGUR"
        IMPORT_CSV = "import_csv", "Импортировано из CSV"
        MANUAL = "manual", "Создано вручную"

    class PoolStatus(models.TextChoices):
        GENERATED = "generated", "Сгенерирован"
        UPLOADED_PENDING_CHECK = "uploaded_pending_check", "Загружен в iikoCard, ждёт проверки"
        VERIFIED_LOADED = "verified_loaded", "Подтверждён в iikoCard"
        VERIFY_FAILED = "verify_failed", "Проверка в iikoCard не пройдена"
        ASSIGNED = "assigned", "Назначен гостю"
        USED = "used", "Использован"
        USED_AFTER_CAMPAIGN = "used_after_campaign", "Использован после завершения акции"
        EXPIRED = "expired", "Срок действия истёк"
        CANCELED = "canceled", "Отменён"

    class IikoCheckStatus(models.TextChoices):
        NOT_CHECKED = "not_checked", "Не проверен"
        FOUND = "found", "Найден в iikoCard"
        NOT_FOUND = "not_found", "Не найден в iikoCard"
        CHECK_ERROR = "check_error", "Ошибка проверки iikoCard"

    series = models.CharField(max_length=120, db_index=True)
    code = models.CharField(max_length=120, db_index=True)
    venue_code = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Код заведения, к которому относится купон.",
    )
    venue_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Название заведения, к которому относится купон.",
    )
    source = models.CharField(
        max_length=20,
        choices=SourceType.choices,
        default=SourceType.GENERATED,
        db_index=True,
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Технический флаг доступности купона для назначения в новых кампаниях.",
    )
    batch = models.ForeignKey(
        "CouponPoolBatch",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="coupons",
    )
    pool_status = models.CharField(
        max_length=32,
        choices=PoolStatus.choices,
        default=PoolStatus.GENERATED,
        db_index=True,
    )
    iiko_check_status = models.CharField(
        max_length=20,
        choices=IikoCheckStatus.choices,
        default=IikoCheckStatus.NOT_CHECKED,
        db_index=True,
    )
    iiko_checked_at = models.DateTimeField(blank=True, null=True, db_index=True)
    iiko_check_error = models.TextField(blank=True, null=True)
    assigned_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "coupon_registry_entries"
        verbose_name = "Купон в реестре"
        verbose_name_plural = "Реестр купонов"
        constraints = [
            models.UniqueConstraint(
                fields=["series", "code"],
                name="coupon_registry_series_code_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["series", "pool_status"], name="cpreg_series_status_idx"),
            models.Index(fields=["venue_code", "pool_status"], name="cpreg_venue_status_idx"),
            models.Index(fields=["batch", "pool_status"], name="cpreg_batch_status_idx"),
            models.Index(fields=["iiko_check_status", "iiko_checked_at"], name="cpreg_iiko_status_idx"),
        ]

    def __str__(self):
        return f"{self.series}:{self.code} [{self.pool_status}]"


class CouponCampaignAssignment(models.Model):
    """
    Назначение купона конкретному гостю в рамках кампании.
    """

    class Status(models.TextChoices):
        RESERVED = "reserved", "Зарезервирован"
        SENT = "sent", "Отправлен"
        USED = "used", "Использован"
        USED_AFTER_CAMPAIGN = "used_after_campaign", "Использован после завершения акции"
        EXPIRED = "expired", "Истёк"
        CANCELED = "canceled", "Отменён"
        ERROR = "error", "Ошибка"

    class VtelemaxSyncStatus(models.TextChoices):
        PENDING = "pending", "Ожидает синхронизации"
        OK = "ok", "Синхронизирован"
        ERROR = "error", "Ошибка синхронизации"

    class IikoCategorySyncStatus(models.TextChoices):
        DISABLED = "disabled", "Контур iikoCard отключён"
        PENDING = "pending", "Ожидает iikoCard"
        OK = "ok", "Подтверждено iikoCard"
        ERROR = "error", "Ошибка iikoCard"

    campaign = models.ForeignKey(
        "Mailing",
        on_delete=models.CASCADE,
        related_name="coupon_assignments",
    )
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="coupon_assignments",
    )
    coupon = models.ForeignKey(
        "CouponRegistryEntry",
        on_delete=models.PROTECT,
        related_name="campaign_assignments",
    )
    person_id = models.UUIDField(blank=True, null=True, db_index=True)
    phone_e164 = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    coupon_series = models.CharField(max_length=120)
    coupon_code = models.CharField(max_length=120)
    venue_code = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Код заведения, в рамках которого выпущен и назначен купон.",
    )
    venue_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Название заведения для назначенного купона.",
    )
    promo_text = models.TextField(
        blank=True,
        null=True,
        help_text="Текст акции, который передаётся гостю вместе с купоном.",
    )
    coupon_title = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        help_text="Снимок названия купона, отправленного гостю в vtelemax.",
    )
    assigned_at = models.DateTimeField(default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    lifetime_expires_at = models.DateTimeField(blank=True, null=True, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.RESERVED,
        db_index=True,
    )
    used_at = models.DateTimeField(blank=True, null=True)
    used_order_id = models.BigIntegerField(blank=True, null=True, db_index=True)
    vtelemax_sync_status = models.CharField(
        max_length=16,
        choices=VtelemaxSyncStatus.choices,
        default=VtelemaxSyncStatus.PENDING,
        db_index=True,
    )
    vtelemax_synced_at = models.DateTimeField(blank=True, null=True)
    vtelemax_sync_error = models.TextField(blank=True, null=True)
    iiko_category_add_status = models.CharField(
        max_length=16,
        choices=IikoCategorySyncStatus.choices,
        default=IikoCategorySyncStatus.DISABLED,
        db_index=True,
        help_text="Статус добавления гостя в категорию iikoCard, разрешающую применение купона.",
    )
    iiko_category_add_synced_at = models.DateTimeField(blank=True, null=True)
    iiko_category_add_error = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "coupon_campaign_assignments"
        verbose_name = "Назначение купона кампании"
        verbose_name_plural = "Назначения купонов кампаний"
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "guest"],
                name="cpass_campaign_guest_uniq",
            ),
            models.UniqueConstraint(
                fields=["campaign", "coupon_series", "coupon_code"],
                name="cpass_campaign_coupon_uniq",
            ),
            models.UniqueConstraint(
                fields=["campaign", "person_id"],
                condition=models.Q(person_id__isnull=False),
                name="cpass_campaign_person_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"], name="cpass_campaign_status_idx"),
            models.Index(fields=["campaign", "venue_code", "status"], name="cpass_camp_venue_st_idx"),
            models.Index(fields=["status", "vtelemax_sync_status"], name="cpass_sync_status_idx"),
            models.Index(fields=["status", "iiko_category_add_status"], name="cpass_iiko_status_idx"),
            models.Index(fields=["coupon_series", "coupon_code", "status"], name="cpass_coupon_status_idx"),
        ]

    def __str__(self):
        return f"campaign={self.campaign_id} coupon={self.coupon_series}:{self.coupon_code} status={self.status}"


class CouponAutoscenarioRun(models.Model):
    """
    Техническая волна купонного автосценария.

    Это не пользовательская рассылочная кампания. Запись нужна для аудита
    регулярного правила: сколько гостей нашли, сколько отсеяли и сколько
    купонов зарезервировали в конкретном проходе.
    """

    class Status(models.TextChoices):
        PLANNED = "planned", "Сформирован план"
        RESERVED = "reserved", "Купоны зарезервированы"
        SYNC_PENDING = "sync_pending", "Ожидает подтверждения vtelemax"
        COMPLETED = "completed", "Завершён"
        ERROR = "error", "Ошибка"

    scenario = models.ForeignKey(
        "NotificationScenario",
        on_delete=models.PROTECT,
        related_name="coupon_autoscenario_runs",
    )
    config = models.ForeignKey(
        "CouponAutomationConfig",
        on_delete=models.PROTECT,
        related_name="runs",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PLANNED,
        db_index=True,
    )
    execution_mode = models.CharField(max_length=24, db_index=True)
    scan_limit = models.PositiveIntegerField(default=0)
    max_recipients_per_run = models.PositiveIntegerField(default=0)
    scanned_guests = models.PositiveIntegerField(default=0)
    matched_guests = models.PositiveIntegerField(default=0)
    sendable_guests = models.PositiveIntegerField(default=0)
    blocked_without_channel = models.PositiveIntegerField(default=0)
    blocked_existing_active_coupon = models.PositiveIntegerField(default=0)
    blocked_existing_trigger = models.PositiveIntegerField(default=0)
    blocked_by_cooldown = models.PositiveIntegerField(default=0)
    eligible_guests = models.PositiveIntegerField(default=0)
    planned_assignments = models.PositiveIntegerField(default=0)
    created_assignments = models.PositiveIntegerField(default=0)
    queue_events_created = models.PositiveIntegerField(default=0)
    coupon_shortage = models.PositiveIntegerField(default=0)
    warnings = models.JSONField(default=list, blank=True)
    blockers = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "coupon_autoscenario_runs"
        verbose_name = "Технический запуск купонного автосценария"
        verbose_name_plural = "Технические запуски купонных автосценариев"
        indexes = [
            models.Index(fields=["scenario", "created_at"], name="cautorun_scen_created_idx"),
            models.Index(fields=["status", "created_at"], name="cautorun_status_created_idx"),
        ]

    def __str__(self):
        return f"scenario={self.scenario_id} run={self.id} status={self.status}"


class CouponAutoscenarioAssignment(models.Model):
    """
    Назначение купона гостю в рамках технической волны автосценария.
    """

    class Status(models.TextChoices):
        RESERVED = CouponCampaignAssignment.Status.RESERVED, "Зарезервирован"
        SENT = CouponCampaignAssignment.Status.SENT, "Отправлен"
        USED = CouponCampaignAssignment.Status.USED, "Использован"
        USED_AFTER_CAMPAIGN = (
            CouponCampaignAssignment.Status.USED_AFTER_CAMPAIGN,
            "Использован после завершения акции",
        )
        EXPIRED = CouponCampaignAssignment.Status.EXPIRED, "Истёк"
        CANCELED = CouponCampaignAssignment.Status.CANCELED, "Отменён"
        ERROR = CouponCampaignAssignment.Status.ERROR, "Ошибка"

    class VtelemaxSyncStatus(models.TextChoices):
        PENDING = CouponCampaignAssignment.VtelemaxSyncStatus.PENDING, "Ожидает синхронизации"
        OK = CouponCampaignAssignment.VtelemaxSyncStatus.OK, "Синхронизирован"
        ERROR = CouponCampaignAssignment.VtelemaxSyncStatus.ERROR, "Ошибка синхронизации"

    class IikoCategorySyncStatus(models.TextChoices):
        DISABLED = CouponCampaignAssignment.IikoCategorySyncStatus.DISABLED, "Контур iikoCard отключён"
        PENDING = CouponCampaignAssignment.IikoCategorySyncStatus.PENDING, "Ожидает iikoCard"
        OK = CouponCampaignAssignment.IikoCategorySyncStatus.OK, "Подтверждено iikoCard"
        ERROR = CouponCampaignAssignment.IikoCategorySyncStatus.ERROR, "Ошибка iikoCard"

    run = models.ForeignKey(
        "CouponAutoscenarioRun",
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    scenario = models.ForeignKey(
        "NotificationScenario",
        on_delete=models.PROTECT,
        related_name="coupon_autoscenario_assignments",
    )
    config = models.ForeignKey(
        "CouponAutomationConfig",
        on_delete=models.PROTECT,
        related_name="assignments",
    )
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="coupon_autoscenario_assignments",
    )
    coupon = models.ForeignKey(
        "CouponRegistryEntry",
        on_delete=models.PROTECT,
        related_name="autoscenario_assignments",
    )
    coupon_rule = models.ForeignKey(
        "CouponAutomationRule",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="assignments",
    )
    person_id = models.UUIDField(blank=True, null=True, db_index=True)
    phone_e164 = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    coupon_series = models.CharField(max_length=120, db_index=True)
    coupon_code = models.CharField(max_length=120)
    venue_code = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    venue_name = models.CharField(max_length=255, blank=True, null=True)
    coupon_selection_source = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    trigger_key = models.CharField(max_length=120, blank=True, null=True, db_index=True)
    trigger_date = models.DateField(blank=True, null=True, db_index=True)
    coupon_title = models.CharField(max_length=120, blank=True, null=True)
    promo_text = models.TextField(blank=True, null=True)
    assigned_at = models.DateTimeField(default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    lifetime_expires_at = models.DateTimeField(blank=True, null=True, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.RESERVED,
        db_index=True,
    )
    status_reason = models.CharField(
        max_length=80,
        blank=True,
        null=True,
        db_index=True,
        help_text="Техническая причина текущего статуса назначения купона.",
    )
    status_details = models.TextField(
        blank=True,
        null=True,
        help_text="Человекочитаемое пояснение к причине текущего статуса назначения купона.",
    )
    used_at = models.DateTimeField(blank=True, null=True)
    used_order_id = models.BigIntegerField(blank=True, null=True, db_index=True)
    used_business_date = models.DateField(blank=True, null=True, db_index=True)
    vtelemax_sync_status = models.CharField(
        max_length=16,
        choices=VtelemaxSyncStatus.choices,
        default=VtelemaxSyncStatus.PENDING,
        db_index=True,
    )
    vtelemax_synced_at = models.DateTimeField(blank=True, null=True)
    vtelemax_sync_error = models.TextField(blank=True, null=True)
    iiko_category_add_status = models.CharField(
        max_length=16,
        choices=IikoCategorySyncStatus.choices,
        default=IikoCategorySyncStatus.DISABLED,
        db_index=True,
        help_text="Статус добавления гостя в категорию iikoCard, разрешающую применение купона.",
    )
    iiko_category_add_synced_at = models.DateTimeField(blank=True, null=True)
    iiko_category_add_error = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "coupon_autoscenario_assignments"
        verbose_name = "Назначение купона автосценария"
        verbose_name_plural = "Назначения купонов автосценариев"
        constraints = [
            models.UniqueConstraint(
                fields=["run", "guest"],
                name="cautoass_run_guest_uniq",
            ),
            models.UniqueConstraint(
                fields=["run", "coupon_series", "coupon_code"],
                name="cautoass_run_coupon_uniq",
            ),
            models.UniqueConstraint(
                fields=["run", "person_id"],
                condition=models.Q(person_id__isnull=False),
                name="cautoass_run_person_uniq",
            ),
            models.UniqueConstraint(
                fields=["scenario", "guest", "trigger_key"],
                condition=models.Q(trigger_key__isnull=False) & ~models.Q(status="canceled"),
                name="cautoass_scen_guest_trigger_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["scenario", "status"], name="cautoass_scen_status_idx"),
            models.Index(fields=["status", "vtelemax_sync_status"], name="cautoass_sync_status_idx"),
            models.Index(fields=["coupon_series", "status"], name="cautoass_series_status_idx"),
            models.Index(fields=["coupon_series", "coupon_code", "status"], name="cautoass_coupon_status_idx"),
            models.Index(fields=["scenario", "trigger_key"], name="cautoass_scen_trigger_idx"),
            models.Index(fields=["status", "iiko_category_add_status"], name="cautoass_iiko_status_idx"),
        ]

    def __str__(self):
        return f"run={self.run_id} coupon={self.coupon_series}:{self.coupon_code} status={self.status}"


class GuestProfileCompletionEvent(models.Model):
    """
    Факт появления важного поля профиля гостя.

    Нужен для автосценария "Заполни дату рождения": купон выдаётся не всем,
    у кого дата рождения уже есть в базе, а только гостям, у которых она
    появилась после запуска механики.
    """

    class EventType(models.TextChoices):
        BIRTHDATE_FILLED = "birthdate_filled", "Дата рождения заполнена"

    class Source(models.TextChoices):
        VTELEMAX = "vtelemax", "vtelemax"
        IIKO = "iiko", "iiko"
        MANUAL = "manual", "Ручное изменение"

    class Status(models.TextChoices):
        NEW = "new", "Ожидает обработки"
        COUPON_RESERVED = "coupon_reserved", "Купон зарезервирован"
        SKIPPED = "skipped", "Пропущено"
        ERROR = "error", "Ошибка"

    guest = models.ForeignKey(
        "Guest",
        on_delete=models.CASCADE,
        related_name="profile_completion_events",
    )
    event_type = models.CharField(
        max_length=32,
        choices=EventType.choices,
        db_index=True,
    )
    source = models.CharField(
        max_length=32,
        choices=Source.choices,
        default=Source.VTELEMAX,
        db_index=True,
    )
    source_ref = models.CharField(max_length=180, blank=True, null=True, db_index=True)
    detected_at = models.DateTimeField(default=timezone.now, db_index=True)
    profile_value = models.JSONField(default=dict, blank=True)
    request_notification_event = models.ForeignKey(
        "NotificationEvent",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="profile_completion_request_events",
    )
    coupon_assignment = models.OneToOneField(
        "CouponAutoscenarioAssignment",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="profile_completion_event",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )
    payload = models.JSONField(default=dict, blank=True)
    error_text = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "guest_profile_completion_events"
        verbose_name = "Событие заполнения профиля гостя"
        verbose_name_plural = "События заполнения профилей гостей"
        constraints = [
            models.UniqueConstraint(
                fields=["guest", "event_type"],
                name="gprofile_event_guest_type_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["event_type", "status", "detected_at"], name="gprofile_event_flow_idx"),
            models.Index(fields=["source", "source_ref"], name="gprofile_event_source_idx"),
        ]

    def __str__(self):
        return f"guest={self.guest_id} event={self.event_type} status={self.status}"


class GuestWelcomeRegistrationEvent(models.Model):
    """
    Журнал входящих событий регистрации гостя из vtelemax.

    Событие фиксируется отдельно от назначения купона: это даёт идемпотентность
    по `event_id`, диагностику входящего контракта и безопасную точку для
    последующей обработки welcome-автосценарием.
    """

    class EventType(models.TextChoices):
        GUEST_REGISTERED = "guest_registered", "Гость зарегистрирован"

    class Platform(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        MAX = "max", "MAX"
        VK = "vk", "VK"

    class Status(models.TextChoices):
        NEW = "new", "Ожидает обработки"
        CHANNEL_APPLIED = "channel_applied", "Канал применён"
        COUPON_RESERVED = "coupon_reserved", "Купон зарезервирован"
        SKIPPED = "skipped", "Пропущено"
        ERROR = "error", "Ошибка"

    event_id = models.CharField(max_length=128, unique=True, db_index=True)
    request_id = models.CharField(max_length=128, blank=True, null=True, db_index=True)
    event_type = models.CharField(
        max_length=64,
        choices=EventType.choices,
        default=EventType.GUEST_REGISTERED,
        db_index=True,
    )
    person_id = models.UUIDField(blank=True, null=True, db_index=True)
    platform = models.CharField(
        max_length=32,
        choices=Platform.choices,
        db_index=True,
    )
    phone_e164 = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    iiko_customer_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Идентификатор гостя iikoCard (`customerId`), переданный vtelemax.",
    )
    external_id = models.CharField(max_length=128, blank=True, null=True)
    rules_accepted = models.BooleanField(default=False)
    notifications_allowed = models.BooleanField(default=False)
    is_registered = models.BooleanField(default=False)
    registered_at = models.DateTimeField(blank=True, null=True, db_index=True)
    state_updated_at = models.DateTimeField(blank=True, null=True)
    account_created_at = models.DateTimeField(blank=True, null=True)
    effective_updated_at = models.DateTimeField(blank=True, null=True, db_index=True)
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="welcome_registration_events",
    )
    vtelemax_channel = models.ForeignKey(
        "VtelemaxRecipientChannel",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="welcome_registration_events",
    )
    coupon_assignment = models.OneToOneField(
        "CouponAutoscenarioAssignment",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="welcome_registration_event",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )
    skip_reason = models.CharField(max_length=120, blank=True, null=True, db_index=True)
    error_text = models.TextField(blank=True, null=True)
    attempts = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(default=timezone.now, db_index=True)
    profile = models.JSONField(default=dict, blank=True)
    payload_json = models.JSONField(default=dict, blank=True)
    payload_sha256 = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "guest_welcome_registration_events"
        verbose_name = "Событие welcome-регистрации гостя"
        verbose_name_plural = "События welcome-регистраций гостей"
        indexes = [
            models.Index(fields=["status", "next_retry_at", "received_at"], name="gwre_status_retry_idx"),
            models.Index(fields=["person_id", "platform"], name="gwre_person_platform_idx"),
            models.Index(fields=["phone_e164", "platform"], name="gwre_phone_platform_idx"),
            models.Index(fields=["guest", "status"], name="gwre_guest_status_idx"),
        ]

    def __str__(self):
        return f"event={self.event_id} platform={self.platform} status={self.status}"


class CouponVtelemaxSyncQueue(models.Model):
    """
    Очередь отправки событий по купонам из SAGUR в vtelemax.
    """

    class Direction(models.TextChoices):
        ASSIGNMENTS = "assignments", "Назначение купонов"
        STATUS_UPDATE = "status_update", "Обновление статуса купона"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает отправки"
        SENT = "sent", "Отправлено"
        ACKED = "acked", "Подтверждено"
        ERROR = "error", "Ошибка"

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    direction = models.CharField(max_length=20, choices=Direction.choices, db_index=True)
    assignment = models.ForeignKey(
        "CouponCampaignAssignment",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="vtelemax_queue_events",
    )
    autoscenario_assignment = models.ForeignKey(
        "CouponAutoscenarioAssignment",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="vtelemax_queue_events",
    )
    payload_json = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error = models.TextField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    ack_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "coupon_vtelemax_sync_queue"
        verbose_name = "Очередь синхронизации купонов в vtelemax"
        verbose_name_plural = "Очередь синхронизации купонов в vtelemax"
        indexes = [
            models.Index(fields=["status", "next_retry_at"], name="cpvq_status_retry_idx"),
            models.Index(fields=["direction", "status"], name="cpvq_dir_status_idx"),
        ]

    def __str__(self):
        return f"event={self.event_id} direction={self.direction} status={self.status}"


class IikoCustomerCategorySyncEvent(models.Model):
    """
    Очередь синхронизации общей категории iikoCard для гостей с активным купоном SAGUR.

    Событие `add` является обязательным pre-send gate при включённом контуре:
    сообщение с купоном не уходит гостю, пока iikoCard не подтвердил добавление
    категории. Событие `remove` всегда проверяет, что у гостя не осталось других
    живых купонов, чтобы не снять общую категорию преждевременно.
    """

    class Action(models.TextChoices):
        ADD = "add", "Добавить категорию"
        REMOVE = "remove", "Удалить категорию"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает отправки"
        SENT = "sent", "Отправлено"
        ACKED = "acked", "Подтверждено"
        ERROR = "error", "Ошибка"
        SKIPPED = "skipped", "Пропущено"

    class SourceType(models.TextChoices):
        CAMPAIGN = "campaign", "Купонная кампания"
        AUTOSCENARIO = "autoscenario", "Купонный автосценарий"
        MANUAL = "manual", "Ручная операция"

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    action = models.CharField(max_length=16, choices=Action.choices, db_index=True)
    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        default=SourceType.MANUAL,
        db_index=True,
    )
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="iiko_category_sync_events",
    )
    campaign_assignment = models.ForeignKey(
        "CouponCampaignAssignment",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="iiko_category_sync_events",
    )
    autoscenario_assignment = models.ForeignKey(
        "CouponAutoscenarioAssignment",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="iiko_category_sync_events",
    )
    iiko_customer_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    category_id = models.CharField(max_length=64, db_index=True)
    organization_id = models.CharField(max_length=64, blank=True, null=True)
    payload_json = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error = models.TextField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    ack_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "iiko_customer_category_sync_events"
        verbose_name = "Событие синхронизации категории гостя iikoCard"
        verbose_name_plural = "События синхронизации категорий гостей iikoCard"
        constraints = [
            models.UniqueConstraint(
                fields=["campaign_assignment", "action", "category_id"],
                condition=models.Q(campaign_assignment__isnull=False),
                name="iikocat_cpass_action_uniq",
            ),
            models.UniqueConstraint(
                fields=["autoscenario_assignment", "action", "category_id"],
                condition=models.Q(autoscenario_assignment__isnull=False),
                name="iikocat_cauto_action_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "next_retry_at"], name="iikocat_status_retry_idx"),
            models.Index(fields=["action", "status"], name="iikocat_action_status_idx"),
            models.Index(fields=["guest", "category_id", "status"], name="iikocat_guest_status_idx"),
        ]

    def __str__(self):
        return f"event={self.event_id} action={self.action} status={self.status}"




