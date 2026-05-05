import os
from datetime import timedelta

from django import forms
from django.contrib import admin, messages
from django.db.models import Count
from django.utils import timezone

from .models import (
    BotProfile,
    Category,
    DispatchTask,
    FocusCategory,
    FocusCategoryNomenclatureResolved,
    Guest,
    GuestBotBinding,
    GuestOrderFocusFact,
    GuestRestaurantDailyCategoryFact,
    GuestRestaurantDailyOrderFact,
    GuestRestaurantWindowCategoryMetrics,
    GuestRestaurantWindowMetrics,
    GuestCategory,
    Mailing,
    MailingBotProfileLink,
    MailingGuest,
    NotificationEvent,
    NotificationScenario,
    NotificationScenarioBotProfileLink,
    OlapCategoryDict,
    OlapCheckSyncJournal,
    OlapNomenclatureDict,
    OlapSalesRawLine,
    OrderFact,
    Restaurant,
    TerminalDepartmentMap,
    VtelemaxRecipientChannel,
    VtelemaxSyncState,
    VirtualCategory,
    VisitHistory,
)
from guests.services.notification_registry import (
    get_registered_notification_scenario_code_choices,
    is_registered_notification_scenario_code,
)


@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "iiko_id")
    search_fields = ("name", "iiko_id")


@admin.register(TerminalDepartmentMap)
class TerminalDepartmentMapAdmin(admin.ModelAdmin):
    """
    Техническая панель сопоставления terminalGroupId и Department.Id.
    """

    list_display = (
        "id",
        "terminal_group_id",
        "department_id",
        "department_code",
        "department_name",
        "organization_id",
        "is_active",
        "verified_at",
        "updated_at",
    )
    list_filter = ("is_active", "organization_id", "department_id")
    search_fields = (
        "terminal_group_id",
        "department_id",
        "department_code",
        "department_name",
        "organization_id",
        "restoraunt_group_id",
    )
    readonly_fields = ("created_at", "updated_at")
    list_per_page = 100


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


@admin.register(VtelemaxRecipientChannel)
class VtelemaxRecipientChannelAdmin(admin.ModelAdmin):
    """
    Техническая панель каналов получателей, синхронизируемых из vtelemax.
    """

    list_display = (
        "id",
        "person_id",
        "platform",
        "phone_e164",
        "external_id",
        "notifications_allowed",
        "is_registered",
        "guest_id",
        "guest_binding_id",
        "effective_updated_at",
        "last_synced_at",
    )
    list_filter = ("platform", "notifications_allowed", "is_registered", "rules_accepted")
    search_fields = ("person_id", "phone_e164", "external_id", "guest__phone")
    raw_id_fields = ("guest", "guest_binding")
    readonly_fields = ("first_seen_at", "last_synced_at")
    list_per_page = 100


@admin.register(VtelemaxSyncState)
class VtelemaxSyncStateAdmin(admin.ModelAdmin):
    """
    Singleton-состояние синхронизации SAGUR <- vtelemax.
    """

    list_display = (
        "id",
        "key",
        "last_status",
        "last_mode",
        "watermark",
        "last_rows",
        "last_pages",
        "last_started_at",
        "last_finished_at",
        "last_success_at",
        "updated_at",
    )
    list_filter = ("last_status", "last_mode")
    search_fields = ("key", "last_error")
    readonly_fields = ("created_at", "updated_at")
    list_per_page = 50


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


@admin.register(OlapCheckSyncJournal)
class OlapCheckSyncJournalAdmin(admin.ModelAdmin):
    """
    Журнал дозагрузки чеков из OLAP.
    """

    list_display = (
        "id",
        "status",
        "guest_id",
        "order_number",
        "business_date",
        "department_id",
        "attempt_count",
        "next_try_at",
        "loaded_at",
        "created_at",
        "last_error_short",
    )
    list_filter = ("status", "business_date", "department_id", "created_at")
    search_fields = (
        "idempotency_key",
        "source_webhook_id",
        "transaction_id",
        "order_external_id",
        "order_number",
        "terminal_group_id",
        "last_error",
    )
    raw_id_fields = ("guest",)
    readonly_fields = ("created_at", "updated_at", "loaded_at", "last_error_short")
    list_per_page = 100
    actions = ("action_requeue_now", "action_mark_skipped")

    @admin.display(description="Краткая ошибка")
    def last_error_short(self, obj: OlapCheckSyncJournal) -> str:
        text = (obj.last_error or "").strip()
        if not text:
            return "—"
        if len(text) <= 250:
            return text
        return text[:250] + "..."

    @admin.action(description="Вернуть в retry (запуск сейчас)")
    def action_requeue_now(self, request, queryset):
        updated = queryset.update(
            status=OlapCheckSyncJournal.Status.RETRY,
            next_try_at=timezone.now(),
            locked_at=None,
        )
        self.message_user(request, f"Переведено в retry: {updated}", level=messages.SUCCESS)

    @admin.action(description="Пометить как skipped")
    def action_mark_skipped(self, request, queryset):
        updated = queryset.update(
            status=OlapCheckSyncJournal.Status.SKIPPED,
            next_try_at=None,
            locked_at=None,
        )
        self.message_user(request, f"Помечено как skipped: {updated}", level=messages.WARNING)


@admin.register(OlapSalesRawLine)
class OlapSalesRawLineAdmin(admin.ModelAdmin):
    """
    Сырой слой OLAP-позиций чека.
    """

    list_display = (
        "id",
        "business_date",
        "department_id",
        "order_number",
        "dish_code",
        "dish_category_name",
        "guest_id",
        "sync_journal_id",
        "created_at",
    )
    list_filter = ("business_date", "department_id", "dish_category_name", "created_at")
    search_fields = (
        "row_fingerprint",
        "order_number",
        "uniq_order_id",
        "dish_code",
        "dish_name",
        "dish_category_id",
        "coupon_number",
    )
    raw_id_fields = ("sync_journal", "guest")
    readonly_fields = (
        "row_fingerprint",
        "sync_journal",
        "guest",
        "business_date",
        "department_id",
        "department_code",
        "department_name",
        "restaurant_section_id",
        "restoraunt_group_id",
        "restoraunt_group_name",
        "order_number",
        "uniq_order_id",
        "item_sale_event_id",
        "dish_code",
        "dish_name",
        "dish_category_id",
        "dish_category_name",
        "dish_group_id",
        "dish_group_name",
        "dish_amount",
        "dish_sum_before_discount",
        "dish_sum_after_discount",
        "discount_sum",
        "bonus_sum",
        "coupon_series",
        "coupon_number",
        "raw_payload",
        "created_at",
    )
    list_per_page = 100

    def has_add_permission(self, request):
        return False


@admin.register(OlapCategoryDict)
class OlapCategoryDictAdmin(admin.ModelAdmin):
    list_display = ("id", "iiko_category_external_id", "category_name", "is_active", "last_seen_at")
    list_filter = ("is_active",)
    search_fields = ("iiko_category_external_id", "category_name")
    readonly_fields = ("created_at", "updated_at", "first_seen_at", "last_seen_at")


@admin.register(OlapNomenclatureDict)
class OlapNomenclatureDictAdmin(admin.ModelAdmin):
    list_display = ("id", "iiko_nomenclature_external_id", "nomenclature_name", "olap_category", "is_active")
    list_filter = ("is_active", "olap_category")
    search_fields = ("iiko_nomenclature_external_id", "nomenclature_name", "olap_category__category_name")
    raw_id_fields = ("olap_category",)
    readonly_fields = ("created_at", "updated_at", "first_seen_at", "last_seen_at")


@admin.register(VirtualCategory)
class VirtualCategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name", "description")
    readonly_fields = ("created_at", "updated_at")


@admin.register(FocusCategory)
class FocusCategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "source_type", "is_enabled", "priority_weight", "updated_at")
    list_filter = ("source_type", "is_enabled")
    search_fields = ("code", "name", "tag_code", "comment")
    raw_id_fields = ("olap_category", "virtual_category")
    readonly_fields = ("created_at", "updated_at")


@admin.register(FocusCategoryNomenclatureResolved)
class FocusCategoryNomenclatureResolvedAdmin(admin.ModelAdmin):
    list_display = ("id", "focus_category", "nomenclature", "source_reason", "updated_at")
    list_filter = ("source_reason", "focus_category")
    search_fields = (
        "focus_category__code",
        "focus_category__name",
        "nomenclature__iiko_nomenclature_external_id",
        "nomenclature__nomenclature_name",
    )
    raw_id_fields = ("focus_category", "nomenclature")
    readonly_fields = ("created_at", "updated_at")


@admin.register(OrderFact)
class OrderFactAdmin(admin.ModelAdmin):
    """
    Агрегаты по заказам (один факт = один чек).
    """

    list_display = (
        "id",
        "business_date",
        "department_id",
        "guest_id",
        "order_number",
        "net_sum",
        "items_count",
        "categories_count",
        "coupon_used",
        "is_delivery",
        "updated_at",
    )
    list_filter = ("business_date", "department_id", "coupon_used", "is_delivery")
    search_fields = ("order_number", "uniq_order_id", "department_id", "guest__phone", "coupon_number")
    raw_id_fields = ("guest",)
    readonly_fields = ("created_at", "updated_at", "first_seen_at")
    list_per_page = 100


@admin.register(GuestRestaurantDailyCategoryFact)
class GuestRestaurantDailyCategoryFactAdmin(admin.ModelAdmin):
    """
    Дневной слой по гостю/заведению/фокусной категории.
    """

    list_display = (
        "id",
        "business_date",
        "guest_id",
        "department_id",
        "focus_category",
        "orders_count",
        "items_count",
        "sum_net",
        "updated_at",
    )
    list_filter = ("business_date", "department_id", "focus_category")
    search_fields = ("guest__phone", "department_id", "focus_category__code", "focus_category__name")
    raw_id_fields = ("guest", "focus_category")
    readonly_fields = ("updated_at",)
    list_per_page = 100


@admin.register(GuestRestaurantDailyOrderFact)
class GuestRestaurantDailyOrderFactAdmin(admin.ModelAdmin):
    """
    Дневной слой по полным чекам (гость/заведение/дата).
    """

    list_display = (
        "id",
        "business_date",
        "guest_id",
        "department_id",
        "orders_count",
        "sum_net",
        "bonus_in_sum",
        "bonus_out_sum",
        "updated_at",
    )
    list_filter = ("business_date", "department_id")
    search_fields = ("guest__phone", "department_id")
    raw_id_fields = ("guest",)
    readonly_fields = ("updated_at",)
    list_per_page = 100


@admin.register(GuestOrderFocusFact)
class GuestOrderFocusFactAdmin(admin.ModelAdmin):
    """
    Order-level мост заказа и фокусной категории.
    """

    list_display = (
        "id",
        "business_date",
        "guest_id",
        "department_id",
        "order_number",
        "uniq_order_id",
        "focus_category",
        "items_count",
        "sum_focus_net",
        "updated_at",
    )
    list_filter = ("business_date", "department_id", "focus_category")
    search_fields = (
        "guest__phone",
        "department_id",
        "order_number",
        "uniq_order_id",
        "focus_category__code",
        "focus_category__name",
    )
    raw_id_fields = ("guest", "focus_category")
    readonly_fields = ("updated_at",)
    list_per_page = 100


@admin.register(GuestRestaurantWindowMetrics)
class GuestRestaurantWindowMetricsAdmin(admin.ModelAdmin):
    """
    Оконные агрегаты и рейтинг гостя.
    """

    list_display = (
        "id",
        "as_of_date",
        "guest_id",
        "department_id",
        "window_days",
        "orders_count",
        "visits_count",
        "avg_check_net",
        "rating_score",
        "last_visit_at",
        "updated_at",
    )
    list_filter = ("as_of_date", "window_days", "department_id")
    search_fields = ("guest__phone", "department_id")
    raw_id_fields = ("guest",)
    readonly_fields = ("updated_at",)
    list_per_page = 100


@admin.register(GuestRestaurantWindowCategoryMetrics)
class GuestRestaurantWindowCategoryMetricsAdmin(admin.ModelAdmin):
    """
    Оконные агрегаты гостя в разрезе фокусной категории.
    """

    list_display = (
        "id",
        "as_of_date",
        "guest_id",
        "department_id",
        "focus_category",
        "window_days",
        "orders_count",
        "visits_count",
        "avg_check_net",
        "rating_score",
        "last_visit_at",
        "updated_at",
    )
    list_filter = ("as_of_date", "window_days", "department_id", "focus_category")
    search_fields = ("guest__phone", "department_id", "focus_category__code", "focus_category__name")
    raw_id_fields = ("guest", "focus_category")
    readonly_fields = ("updated_at",)
    list_per_page = 100


class NotificationScenarioAdminForm(forms.ModelForm):
    """
    Форма админки для сценариев уведомлений с выбором кода из реестра.
    """

    code = forms.ChoiceField(
        label="Код сценария",
        choices=(),
        required=True,
    )

    class Meta:
        model = NotificationScenario
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = list(get_registered_notification_scenario_code_choices())
        instance_code = str(getattr(self.instance, "code", "") or "").strip()

        # Если в БД уже есть незарегистрированный код, показываем его в списке,
        # чтобы карточка открылась и пользователь смог выбрать корректное значение.
        if instance_code and not any(value == instance_code for value, _ in choices):
            choices.append((instance_code, f"{instance_code} (не зарегистрирован)"))

        self.fields["code"].choices = choices
        self.fields["code"].help_text = (
            "Выберите код сценария из зарегистрированного списка. "
            "Свободный ввод кода запрещён."
        )

    def clean_code(self) -> str:
        code = str(self.cleaned_data.get("code") or "").strip()
        if not is_registered_notification_scenario_code(code):
            raise forms.ValidationError(
                f"Код сценария '{code}' не зарегистрирован. Выберите значение из списка."
            )
        return code


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

    form = NotificationScenarioAdminForm
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
