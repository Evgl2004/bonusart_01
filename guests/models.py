from django.db import models
import uuid


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
        managed = False  # таблица уже создана в БД
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
        managed = False
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
        managed = False
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
        managed = False
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
        managed = False
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
        managed = False

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
        managed = False

    def __str__(self):
        return self.name

class Mailing(models.Model):
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

    # удобно иметь доступ к каналам как many-to-many
    channels = models.ManyToManyField(
        "MailingChannel",
        through="MailingChannelLink",
        related_name="mailings",
    )

    class Meta:
        db_table = "mailings"
        managed = False  # если таблица уже создана SQL
        constraints = [
            models.CheckConstraint(
                check=models.Q(scheduled_time_begin__lte=models.F("scheduled_time_end")),
                name="mailings_time_chk",
            )
        ]

    def __str__(self):
        return f"{self.id}: {self.name}"


class MailingChannel(models.Model):
    class ChannelKind(models.TextChoices):
        PHONE_TELEGRAM = "phone_telegram", "Телефон Телеграмм"
        PHONE_TELEGRAM_BOT = "phone_telegram_bot", "Телефон Телеграмм Бот"
        EMAIL = "email", "Электронная почта"

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=150)
    channel_kind = models.CharField(max_length=50, choices=ChannelKind.choices)

    token = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        db_table = "mailing_channels"
        managed = False  # если таблица уже создана SQL
        constraints = [
            models.CheckConstraint(
                check=models.Q(channel_kind__in=["phone_telegram", "phone_telegram_bot", "email"]),
                name="mailing_channels_kind_chk",
            )
        ]

    def __str__(self):
        #return f"{self.id}: {self.name} ({self.channel_kind})"
        return getattr(self, "name", f"Channel {self.pk}")


class MailingChannelLink(models.Model):
    id = models.BigAutoField(primary_key=True)

    mailing = models.ForeignKey(
        "Mailing",
        on_delete=models.CASCADE,
        db_column="mailing_id",
        related_name="channel_links",
    )

    channel = models.ForeignKey(
        "MailingChannel",
        on_delete=models.RESTRICT,
        db_column="channel_id",
        related_name="mailing_links",
    )

    class Meta:
        db_table = "mailing_channel_links"
        managed = False  # если таблица уже создана SQL
        constraints = [
            models.UniqueConstraint(fields=["mailing", "channel"], name="mailing_channel_links_uniq"),
        ]
        indexes = [
            models.Index(fields=["mailing"], name="mcl_mailing_id_idx"),
            models.Index(fields=["channel"], name="mcl_channel_id_idx"),
        ]

    def __str__(self):
        return f"mailing={self.mailing_id} channel={self.channel_id}"


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
        managed = False  # если таблица уже создана SQL
        constraints = [
            models.UniqueConstraint(fields=["mailing", "guest"], name="mailing_guests_uniq"),
            models.CheckConstraint(
                check=models.Q(status__in=["planned", "in_progress", "done", "error"]),
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

class GuestChannelLink(models.Model):
    guest = models.ForeignKey("Guest", on_delete=models.CASCADE, db_column="guest_id")
    channel = models.ForeignKey("MailingChannel", on_delete=models.CASCADE, db_column="channel_id")

    external_chat_id = models.CharField(max_length=64, blank=True, null=True)
    is_opt_in = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    is_stop_sending = models.BooleanField(default=False)
    last_error = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "guest_channel_links"
        unique_together = (("guest", "channel"),)



