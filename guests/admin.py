import os
from datetime import timedelta

from django.contrib import admin, messages
from django.db.models import Count
from django.utils import timezone

from .models import (
    BotProfile,
    Category,
    DispatchTask,
    Guest,
    GuestBotBinding,
    GuestCategory,
    Mailing,
    MailingBotProfileLink,
    MailingGuest,
    NotificationEvent,
    NotificationScenario,
    NotificationScenarioBotProfileLink,
    Restaurant,
    VisitHistory,
)


@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "iiko_id")
    search_fields = ("name", "iiko_id")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "external_id")


@admin.register(VisitHistory)
class VisitHistoryAdmin(admin.ModelAdmin):
    list_display = ("id", "guest", "restaurant", "visit_date")
    list_filter = ("restaurant",)
    search_fields = ("guest__phone", "restaurant__name")
    raw_id_fields = ("guest", "restaurant")


@admin.register(GuestCategory)
class GuestCategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "guest", "category")
    list_filter = ("category",)
    search_fields = ("guest__phone", "category__name")
    raw_id_fields = ("guest", "category")


@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ("id", "phone", "first_name", "last_name", "email", "updated_at")
    search_fields = ("phone", "first_name", "last_name", "email", "iiko_id")
    list_per_page = 50


@admin.register(BotProfile)
class BotProfileAdmin(admin.ModelAdmin):
    """
    Техническое управление справочником ботов.
    """

    list_display = ("id", "code", "name", "provider_type", "is_active", "secret_ref", "token_source", "updated_at")
    list_filter = ("provider_type", "is_active")
    search_fields = ("code", "name", "secret_ref")
    readonly_fields = ("created_at", "updated_at", "token_source", "masked_token")
    fieldsets = (
        (
            "Основные данные",
            {
                "fields": ("code", "name", "provider_type", "is_active"),
            },
        ),
        (
            "Секреты и интеграция",
            {
                "fields": ("secret_ref", "token", "masked_token", "token_source", "settings"),
                "description": (
                    "Рекомендуется хранить токен через `secret_ref` (в переменной окружения). "
                    "Поле `token` используйте только как fallback."
                ),
            },
        ),
        (
            "Служебные поля",
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )
    list_per_page = 50

    @admin.display(description="Источник токена")
    def token_source(self, obj: BotProfile) -> str:
        if obj.secret_ref and os.getenv(obj.secret_ref, "").strip():
            return "env(secret_ref)"
        if (obj.token or "").strip():
            return "db(token)"
        return "missing"

    @admin.display(description="Токен (маска)")
    def masked_token(self, obj: BotProfile) -> str:
        token = (obj.token or "").strip()
        if not token:
            return "—"
        if len(token) <= 8:
            return "*" * len(token)
        return f"{token[:4]}...{token[-4:]}"


@admin.register(GuestBotBinding)
class GuestBotBindingAdmin(admin.ModelAdmin):
    """
    Техническая панель привязок гостей к ботам/чатам.
    """

    list_display = (
        "id",
        "guest_id",
        "guest_phone",
        "bot",
        "external_chat_id",
        "is_primary",
        "is_active",
        "is_opt_in",
        "is_stop_sending",
        "updated_at",
    )
    list_filter = ("bot__provider_type", "bot", "is_primary", "is_active", "is_opt_in", "is_stop_sending")
    search_fields = ("guest__phone", "external_chat_id", "external_user_id", "bot__code", "bot__name")
    raw_id_fields = ("guest", "bot")
    readonly_fields = ("created_at", "updated_at")
    actions = ("action_enable_sending", "action_disable_sending")
    list_per_page = 100

    @admin.display(description="Телефон гостя")
    def guest_phone(self, obj: GuestBotBinding) -> str:
        return obj.guest.phone or "—"

    @admin.action(description="Включить отправку (is_stop_sending=False, is_active=True)")
    def action_enable_sending(self, request, queryset):
        updated = queryset.update(
            is_stop_sending=False,
            is_active=True,
            updated_at=timezone.now(),
        )
        self.message_user(request, f"Обновлено привязок: {updated}", level=messages.SUCCESS)

    @admin.action(description="Остановить отправку (is_stop_sending=True)")
    def action_disable_sending(self, request, queryset):
        updated = queryset.update(
            is_stop_sending=True,
            updated_at=timezone.now(),
        )
        self.message_user(request, f"Обновлено привязок: {updated}", level=messages.WARNING)


class MailingBotProfileLinkInline(admin.TabularInline):
    """
    Inline-настройка выбранных ботов для конкретной рассылки.
    """

    model = MailingBotProfileLink
    extra = 1
    autocomplete_fields = ("bot_profile",)
    verbose_name = "Бот рассылки"
    verbose_name_plural = "Боты рассылки"


@admin.register(Mailing)
class MailingAdmin(admin.ModelAdmin):
    """
    Сервисная админ-панель карточек рассылок.
    """

    list_display = (
        "id",
        "name",
        "template",
        "is_active",
        "target_mode",
        "queue_priority",
        "scheduled_date",
        "scheduled_time_begin",
        "scheduled_time_end",
    )
    list_filter = ("is_active", "target_mode", "queue_priority", "scheduled_date")
    search_fields = ("name", "template__name")
    raw_id_fields = ("template",)
    inlines = (MailingBotProfileLinkInline,)
    list_per_page = 50


@admin.register(MailingGuest)
class MailingGuestAdmin(admin.ModelAdmin):
    """
    Операционный журнал строк массовых рассылок.
    """

    list_display = (
        "id",
        "mailing_id",
        "guest_id",
        "status",
        "delivery_status",
        "dispatch_tasks_count",
        "scheduled_datetime",
        "sent_at",
    )
    list_filter = ("status", "delivery_status", "mailing")
    search_fields = ("guest__phone", "guest__first_name", "guest__last_name", "error_description", "text_mailing_list")
    raw_id_fields = ("mailing", "guest")
    readonly_fields = ("created_at", "sent_at", "external_id")
    list_per_page = 100

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.annotate(dispatch_tasks_total=Count("dispatch_tasks"))

    @admin.display(description="Задач DispatchTask")
    def dispatch_tasks_count(self, obj: MailingGuest) -> int:
        return int(getattr(obj, "dispatch_tasks_total", 0))


class NotificationScenarioBotProfileLinkInline(admin.TabularInline):
    """
    Связь сценария авто-уведомления с разрешёнными ботами.
    """

    model = NotificationScenarioBotProfileLink
    extra = 1
    autocomplete_fields = ("bot_profile",)
    verbose_name = "Разрешённый бот"
    verbose_name_plural = "Разрешённые боты"


@admin.register(NotificationScenario)
class NotificationScenarioAdmin(admin.ModelAdmin):
    """
    Техническая панель управления правилами авто-уведомлений.
    """

    list_display = (
        "id",
        "code",
        "name",
        "is_active",
        "is_system",
        "trigger_type",
        "priority",
        "target_mode",
        "distribution_mode",
        "send_window_begin",
        "send_window_end",
        "updated_at",
    )
    list_filter = (
        "is_active",
        "is_system",
        "trigger_type",
        "priority",
        "target_mode",
        "distribution_mode",
    )
    search_fields = (
        "code",
        "name",
        "description",
        "webhook_category_external_id",
        "template__name",
    )
    raw_id_fields = ("template",)
    readonly_fields = ("created_at", "updated_at")
    inlines = (NotificationScenarioBotProfileLinkInline,)
    list_per_page = 100
    actions = ("action_activate", "action_deactivate")

    fieldsets = (
        (
            "Идентификация сценария",
            {
                "fields": ("code", "name", "description", "is_active", "is_system"),
            },
        ),
        (
            "Триггер и шаблон",
            {
                "fields": ("trigger_type", "webhook_category_external_id", "template"),
            },
        ),
        (
            "Маршрутизация и приоритет",
            {
                "fields": ("priority", "target_mode", "distribution_mode"),
            },
        ),
        (
            "Окно отправки и ограничения",
            {
                "fields": (
                    "send_window_begin",
                    "send_window_end",
                    "timezone",
                    "cooldown_minutes",
                    "max_per_day_per_guest",
                ),
            },
        ),
        (
            "Дополнительные настройки",
            {
                "fields": ("settings",),
            },
        ),
        (
            "Служебные поля",
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    @admin.action(description="Включить выбранные сценарии")
    def action_activate(self, request, queryset):
        updated = queryset.update(is_active=True, updated_at=timezone.now())
        self.message_user(request, f"Включено сценариев: {updated}", level=messages.SUCCESS)

    @admin.action(description="Отключить выбранные сценарии")
    def action_deactivate(self, request, queryset):
        updated = queryset.update(is_active=False, updated_at=timezone.now())
        self.message_user(request, f"Отключено сценариев: {updated}", level=messages.WARNING)

    def has_delete_permission(self, request, obj=None):
        """
        Системные сценарии нельзя удалять из админки.
        """
        if obj is not None and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        protected = queryset.filter(is_system=True).count()
        deleted = queryset.exclude(is_system=True).delete()[0]
        if protected:
            self.message_user(
                request,
                f"Системные сценарии пропущены и не удалены: {protected}.",
                level=messages.WARNING,
            )
        self.message_user(request, f"Удалено сценариев: {deleted}", level=messages.SUCCESS)


@admin.register(NotificationEvent)
class NotificationEventAdmin(admin.ModelAdmin):
    """
    Операционный журнал фактов срабатывания авто-уведомлений.
    """

    list_display = (
        "id",
        "scenario_code",
        "guest_id",
        "source_type",
        "status",
        "duplicate_hits",
        "dispatch_tasks_count",
        "planned_send_at",
        "created_at",
    )
    list_filter = ("source_type", "status", "scenario", "created_at")
    search_fields = (
        "scenario__code",
        "scenario__name",
        "guest__phone",
        "source_ref",
        "dedupe_key",
        "coupon_code",
        "error_text",
    )
    raw_id_fields = ("scenario", "guest")
    readonly_fields = (
        "uuid",
        "scenario",
        "guest",
        "source_type",
        "source_ref",
        "dedupe_key",
        "status",
        "event_at",
        "planned_send_at",
        "duplicate_hits",
        "last_duplicate_at",
        "payload",
        "coupon_code",
        "coupon_external_id",
        "coupon_expires_at",
        "error_text",
        "created_at",
        "updated_at",
    )
    date_hierarchy = "created_at"
    list_per_page = 100

    fieldsets = (
        (
            "Идентификация события",
            {
                "fields": (
                    "uuid",
                    "scenario",
                    "guest",
                    "source_type",
                    "source_ref",
                    "dedupe_key",
                    "status",
                )
            },
        ),
        (
            "Планирование и дубли",
            {
                "fields": (
                    "event_at",
                    "planned_send_at",
                    "duplicate_hits",
                    "last_duplicate_at",
                ),
            },
        ),
        (
            "Купоны и payload",
            {
                "fields": ("coupon_code", "coupon_external_id", "coupon_expires_at", "payload"),
            },
        ),
        (
            "Ошибки и аудит",
            {
                "fields": ("error_text", "created_at", "updated_at"),
            },
        ),
    )

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.select_related("scenario", "guest").annotate(dispatch_tasks_total=Count("dispatch_tasks"))

    @admin.display(description="Сценарий")
    def scenario_code(self, obj: NotificationEvent) -> str:
        if obj.scenario_id:
            return obj.scenario.code
        return "—"

    @admin.display(description="DispatchTask")
    def dispatch_tasks_count(self, obj: NotificationEvent) -> int:
        return int(getattr(obj, "dispatch_tasks_total", 0))

    def has_add_permission(self, request):
        return False


@admin.register(DispatchTask)
class DispatchTaskAdmin(admin.ModelAdmin):
    """
    Основная техническая панель сопровождения очереди доставки.
    """

    list_display = (
        "id",
        "source_type",
        "provider_type",
        "priority",
        "status",
        "attempt_progress",
        "mailing_guest_id",
        "notification_scenario_code",
        "notification_event_id",
        "guest_id",
        "bot_profile_code",
        "external_chat_id",
        "available_at",
        "updated_at",
    )
    list_filter = (
        "source_type",
        "provider_type",
        "priority",
        "status",
        "notification_scenario",
        "created_at",
    )
    search_fields = (
        "idempotency_key",
        "external_chat_id",
        "guest__phone",
        "bot_profile__code",
        "notification_scenario__code",
        "notification_event__dedupe_key",
        "last_error",
        "message_text",
    )
    raw_id_fields = (
        "guest",
        "mailing_guest",
        "notification_scenario",
        "notification_event",
        "bot_profile",
        "guest_binding",
    )
    readonly_fields = (
        "uuid",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "attempt",
        "last_error_short",
    )
    date_hierarchy = "created_at"
    list_per_page = 100
    actions = ("action_requeue_now", "action_defer_5_minutes", "action_cancel_tasks")

    fieldsets = (
        (
            "Маршрутизация",
            {
                "fields": (
                    "uuid",
                    "source_type",
                    "provider_type",
                    "priority",
                    "status",
                    "mailing_guest",
                    "notification_scenario",
                    "notification_event",
                    "guest",
                    "bot_profile",
                    "guest_binding",
                    "external_chat_id",
                )
            },
        ),
        (
            "Сообщение и payload",
            {
                "fields": ("message_text", "payload", "idempotency_key"),
            },
        ),
        (
            "Планирование и исполнение",
            {
                "fields": (
                    "scheduled_at",
                    "available_at",
                    "enqueued_at",
                    "queue_name",
                    "started_at",
                    "finished_at",
                )
            },
        ),
        (
            "Повторы и ошибки",
            {
                "fields": ("attempt", "max_attempts", "last_error_short"),
            },
        ),
        (
            "Служебные поля",
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.select_related(
            "guest",
            "mailing_guest",
            "notification_scenario",
            "notification_event",
            "bot_profile",
        )

    @admin.display(description="Попытки")
    def attempt_progress(self, obj: DispatchTask) -> str:
        return f"{obj.attempt}/{obj.max_attempts}"

    @admin.display(description="Код бота")
    def bot_profile_code(self, obj: DispatchTask) -> str:
        if obj.bot_profile:
            return obj.bot_profile.code
        return "—"

    @admin.display(description="Scenario")
    def notification_scenario_code(self, obj: DispatchTask) -> str:
        if obj.notification_scenario_id:
            return obj.notification_scenario.code
        return "—"

    @admin.display(description="Краткая ошибка")
    def last_error_short(self, obj: DispatchTask) -> str:
        text = (obj.last_error or "").strip()
        if not text:
            return "—"
        if len(text) <= 400:
            return text
        return text[:400] + "..."

    @admin.action(description="Requeue: вернуть в pending (доступно сейчас)")
    def action_requeue_now(self, request, queryset):
        now = timezone.now()
        updated = queryset.update(
            status=DispatchTask.Status.PENDING,
            enqueued_at=None,
            queue_name=None,
            started_at=None,
            finished_at=None,
            available_at=now,
        )
        self.message_user(request, f"Requeue выполнен, задач: {updated}", level=messages.SUCCESS)

    @admin.action(description="Отложить на 5 минут и вернуть в pending")
    def action_defer_5_minutes(self, request, queryset):
        available_at = timezone.now() + timedelta(minutes=5)
        updated = queryset.update(
            status=DispatchTask.Status.PENDING,
            enqueued_at=None,
            queue_name=None,
            started_at=None,
            finished_at=None,
            available_at=available_at,
        )
        self.message_user(request, f"Отложено задач: {updated}", level=messages.WARNING)

    @admin.action(description="Отменить задачи (status=canceled)")
    def action_cancel_tasks(self, request, queryset):
        updated = queryset.update(
            status=DispatchTask.Status.CANCELED,
            finished_at=timezone.now(),
        )
        self.message_user(request, f"Отменено задач: {updated}", level=messages.WARNING)
