from django import forms
from django.db import transaction
from django.utils import timezone

from .models import (
    BotProfile,
    Category,
    CouponAutomationConfig,
    CouponAutomationRule,
    Mailing,
    MessageTemplate,
    NotificationScenario,
)
from .services.coupon_constants import COUPON_VENUE_GLOBAL_CODE, COUPON_VENUE_GLOBAL_NAME, is_coupon_global_venue
from .services.coupon_autoscenarios import resolve_coupon_autoscenario_type
from .services.coupon_series import build_available_coupon_series_choices
from .services.coupon_venues import build_coupon_venue_choices
from .services.guest_resolution import normalize_phone_e164


COUPON_AUTOSCENARIO_STATE_CHOICES = [
    (CouponAutomationConfig.ExecutionMode.REPORT_ONLY, "Черновик"),
    (CouponAutomationConfig.ExecutionMode.PILOT, "Пилот"),
    (CouponAutomationConfig.ExecutionMode.AUTOMATIC, "Активен"),
    (CouponAutomationConfig.ExecutionMode.PAUSED, "Пауза"),
]

COUPON_CODE_PLACEHOLDER = "{coupon_code}"
COUPON_CODE_PLACEHOLDER_HINTS = (
    "{{ coupon_code }}",
    "{{coupon_code}}",
    "{ coupon_code }",
    "[coupon_code]",
)
COUPON_TEMPLATE_MODE_CREATE = "create"
COUPON_TEMPLATE_MODE_EXISTING = "existing"
COUPON_TEMPLATE_MODE_CHOICES = (
    (COUPON_TEMPLATE_MODE_CREATE, "Создать новый шаблон"),
    (COUPON_TEMPLATE_MODE_EXISTING, "Использовать существующий шаблон"),
)


def validate_coupon_code_placeholder(text: str):
    """
    Купонный автосценарий обязан показать гостю фактический код купона.
    """
    safe_text = str(text or "")
    if COUPON_CODE_PLACEHOLDER in safe_text:
        return

    for wrong_placeholder in COUPON_CODE_PLACEHOLDER_HINTS:
        if wrong_placeholder in safe_text:
            raise forms.ValidationError(
                "Для купонного автосценария нужен плейсхолдер %(placeholder)s. "
                "Найден похожий, но неверный вариант: %(wrong)s.",
                params={
                    "placeholder": COUPON_CODE_PLACEHOLDER,
                    "wrong": wrong_placeholder,
                },
            )

    raise forms.ValidationError(
        "В шаблоне купонного автосценария должен быть параметр %(placeholder)s.",
        params={"placeholder": COUPON_CODE_PLACEHOLDER},
    )


class CouponAutomationScenarioCreateForm(forms.Form):
    """
    Форма первичного создания пользовательского купонного автосценария.

    После этого оператор попадает в существующую форму настройки правил купонов,
    пилота, отбора гостей и запуска.
    """

    code = forms.SlugField(
        label="Код автосценария",
        max_length=80,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "sami_susami_kanpeti_30d",
            }
        ),
        help_text="Технический код латиницей, цифрами, дефисом или подчёркиванием.",
        error_messages={
            "invalid": "Код может содержать только латинские буквы, цифры, дефис и подчёркивание.",
        },
    )
    name = forms.CharField(
        label="Название автосценария",
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Сами Сусами: не был 30 дней + Канпети",
            }
        ),
    )
    scenario_type = forms.ChoiceField(
        label="Типовая основа",
        choices=CouponAutomationConfig.ScenarioType.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text="Выберите типовую механику, на основе которой будет создан обособленный автосценарий.",
    )
    inactive_days = forms.IntegerField(
        label="Порог неактивности, дней",
        required=False,
        min_value=1,
        initial=30,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
        help_text="Используется для типа «Гость не был N дней + купон».",
    )
    birthday_preparation_window_days = forms.IntegerField(
        label="Окно подготовки ко дню рождения, дней",
        required=False,
        min_value=0,
        initial=7,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
        help_text="Используется для типа «День рождения + купон».",
    )
    template_mode = forms.ChoiceField(
        label="Как выбрать шаблон",
        choices=COUPON_TEMPLATE_MODE_CHOICES,
        required=False,
        initial=COUPON_TEMPLATE_MODE_CREATE,
        widget=forms.RadioSelect,
    )
    existing_template = forms.ModelChoiceField(
        label="Существующий шаблон",
        required=False,
        queryset=MessageTemplate.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
        empty_label="— Выберите шаблон —",
        help_text="Можно использовать активный шаблон из раздела «Шаблоны».",
    )
    template_name = forms.CharField(
        label="Название шаблона",
        max_length=150,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Сами Сусами: Канпети для остывших гостей",
            }
        ),
    )
    template_description = forms.CharField(
        label="Описание шаблона",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    template_text = forms.CharField(
        label="Текст сообщения",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 10,
                "style": "min-height: 260px;",
            }
        ),
    )
    notification_bot_profiles = forms.ModelMultipleChoiceField(
        label="Разрешённые боты",
        required=True,
        queryset=BotProfile.objects.none(),
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        error_messages={
            "required": "Выберите хотя бы один бот для отправки сообщений.",
        },
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["notification_bot_profiles"].queryset = BotProfile.objects.filter(
            is_active=True
        ).order_by(
            "provider_type",
            "name",
            "id",
        )
        self.fields["existing_template"].queryset = MessageTemplate.objects.filter(
            is_active=True
        ).order_by(
            "name",
            "id",
        )

    def clean_code(self):
        code = str(self.cleaned_data.get("code") or "").strip().lower()
        if NotificationScenario.objects.filter(code=code).exists():
            raise forms.ValidationError("Сценарий с таким кодом уже существует.")
        return code

    def clean(self):
        cleaned_data = super().clean()
        scenario_type = cleaned_data.get("scenario_type")
        template_mode = cleaned_data.get("template_mode") or COUPON_TEMPLATE_MODE_CREATE
        cleaned_data["template_mode"] = template_mode

        if scenario_type == CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON:
            if cleaned_data.get("inactive_days") is None:
                self.add_error("inactive_days", "Укажите порог неактивности.")
        elif scenario_type == CouponAutomationConfig.ScenarioType.BIRTHDAY_COUPON:
            if cleaned_data.get("birthday_preparation_window_days") is None:
                self.add_error(
                    "birthday_preparation_window_days",
                    "Укажите окно подготовки ко дню рождения.",
                )

        if template_mode == COUPON_TEMPLATE_MODE_EXISTING:
            template = cleaned_data.get("existing_template")
            if template is None:
                self.add_error("existing_template", "Выберите существующий шаблон.")
            else:
                try:
                    validate_coupon_code_placeholder(template.message_text)
                except forms.ValidationError as exc:
                    self.add_error("existing_template", exc)
        else:
            template_name = str(cleaned_data.get("template_name") or "").strip()
            template_text = str(cleaned_data.get("template_text") or "").strip()
            cleaned_data["template_name"] = template_name
            cleaned_data["template_text"] = template_text
            if not template_name:
                self.add_error("template_name", "Укажите название нового шаблона.")
            if not template_text:
                self.add_error("template_text", "Укажите текст нового шаблона.")
            else:
                try:
                    validate_coupon_code_placeholder(template_text)
                except forms.ValidationError as exc:
                    self.add_error("template_text", exc)

        return cleaned_data

    def _build_scenario_settings(self) -> dict:
        scenario_type = self.cleaned_data["scenario_type"]
        settings = {"coupon_required": True}
        if scenario_type == CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON:
            settings["inactive_days"] = int(self.cleaned_data.get("inactive_days") or 30)
        elif scenario_type == CouponAutomationConfig.ScenarioType.BIRTHDATE_FILLED_COUPON:
            settings["profile_event_type"] = "birthdate_filled"
        return settings

    def _build_config_settings(self) -> dict:
        scenario_type = self.cleaned_data["scenario_type"]
        if scenario_type == CouponAutomationConfig.ScenarioType.BIRTHDAY_COUPON:
            return {
                "birthday_preparation_window_days": int(
                    self.cleaned_data.get("birthday_preparation_window_days") or 7
                )
            }
        if scenario_type == CouponAutomationConfig.ScenarioType.BIRTHDATE_FILLED_COUPON:
            return {"profile_event_type": "birthdate_filled"}
        return {}

    def _build_cooldown_days(self) -> int:
        scenario_type = self.cleaned_data["scenario_type"]
        if scenario_type == CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON:
            return max(1, int(self.cleaned_data.get("inactive_days") or 30))
        return 365

    def save(self) -> CouponAutomationConfig:
        with transaction.atomic():
            if self.cleaned_data["template_mode"] == COUPON_TEMPLATE_MODE_EXISTING:
                template = self.cleaned_data["existing_template"]
            else:
                raw_description = str(self.cleaned_data.get("template_description") or "").strip()
                scenario_code = str(self.cleaned_data["code"]).strip()
                description_parts = [raw_description] if raw_description else []
                description_parts.append(
                    f"Создано для купонного автосценария: {scenario_code}"
                )
                template = MessageTemplate.objects.create(
                    name=str(self.cleaned_data["template_name"]).strip(),
                    description=" ".join(description_parts),
                    message_text=str(self.cleaned_data["template_text"]).strip(),
                    created_by="mailings_v2_user",
                    is_active=True,
                )
            scenario = NotificationScenario(
                code=self.cleaned_data["code"],
                name=str(self.cleaned_data["name"]).strip(),
                description="",
                is_active=False,
                is_system=False,
                trigger_type=NotificationScenario.TriggerType.SCHEDULE,
                template=template,
                priority=NotificationScenario.Priority.BULK,
                target_mode=NotificationScenario.TargetMode.PRIMARY_ONLY,
                distribution_mode=NotificationScenario.DistributionMode.IMMEDIATE,
                timezone="Asia/Yekaterinburg",
                settings=self._build_scenario_settings(),
            )
            scenario.full_clean()
            scenario.save()
            scenario.bot_profiles.set(self.cleaned_data.get("notification_bot_profiles") or [])

            config = CouponAutomationConfig(
                scenario=scenario,
                scenario_type=self.cleaned_data["scenario_type"],
                execution_mode=CouponAutomationConfig.ExecutionMode.REPORT_ONLY,
                coupon_validity_days=14,
                max_recipients_per_run=100,
                max_active_coupons_per_guest=1,
                cooldown_days=self._build_cooldown_days(),
                settings=self._build_config_settings(),
            )
            config.full_clean()
            config.save()

        return config


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "description", "is_active", "external_id"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.TextInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "external_id": forms.TextInput(attrs={"class": "form-control"}),
        }
        labels = {
            "name": "Название",
            "description": "Описание",
            "is_active": "Активна",
            "external_id": "Внешний ID",
        }


class MessageTemplateForm(forms.ModelForm):
    class Meta:
        model = MessageTemplate
        fields = ["name", "description", "message_text", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.TextInput(attrs={"class": "form-control"}),
            "message_text": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 10,
                    "style": "min-height: 280px;",
                }
            ),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "name": "Название шаблона",
            "description": "Описание",
            "message_text": "Текст сообщения",
            "is_active": "Активен",
        }


class MailingForm(forms.ModelForm):
    coupon_series = forms.ChoiceField(
        label="Серия купонов",
        required=False,
        choices=[],
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = Mailing
        fields = [
            "name",
            "template",
            "scheduled_date",
            "scheduled_time_begin",
            "scheduled_time_end",
            "send_window_begin",
            "send_window_end",
            "target_mode",
            "queue_priority",
            "coupon_series",
            "coupon_venue_code",
            "coupon_title",
            "coupon_promo_text",
            "bot_profiles",
            # "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "template": forms.Select(attrs={"class": "form-select"}),
            "scheduled_date": forms.DateInput(
                attrs={"type": "date", "class": "form-control"},
                format="%Y-%m-%d",
            ),
            "scheduled_time_begin": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "form-control"},
                format="%Y-%m-%dT%H:%M",
            ),
            "scheduled_time_end": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "form-control"},
                format="%Y-%m-%dT%H:%M",
            ),
            "send_window_begin": forms.TimeInput(
                attrs={"type": "time", "class": "form-control"},
                format="%H:%M",
            ),
            "send_window_end": forms.TimeInput(
                attrs={"type": "time", "class": "form-control"},
                format="%H:%M",
            ),
            "target_mode": forms.Select(attrs={"class": "form-select"}),
            "queue_priority": forms.Select(attrs={"class": "form-select"}),
            "coupon_venue_code": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "coupon_title": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "maxlength": "120",
                    "placeholder": "Например: Сет «Канпети» в подарок",
                }
            ),
            "coupon_promo_text": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 7,
                    "placeholder": "Например: Скидка 20% на сет при заказе от 1500 ₽.",
                }
            ),
            "bot_profiles": forms.CheckboxSelectMultiple(
                attrs={
                    "class": "form-check-input",
                }
            ),
            # "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "name": "Название рассылки",
            "template": "Шаблон",
            "scheduled_date": "Дата отправки",
            "scheduled_time_begin": "Начало (дата и время)",
            "scheduled_time_end": "Окончание (дата и время)",
            "send_window_begin": "Начало окна отправки",
            "send_window_end": "Конец окна отправки",
            "target_mode": "Режим получателей",
            "queue_priority": "Приоритет в очереди",
            "coupon_series": "Серия купонов",
            "coupon_venue_code": "Заведение для купонной кампании",
            "coupon_title": "Название купона в vtelemax",
            "coupon_promo_text": "Текст акции для гостя",
            "bot_profiles": "Боты для рассылки",
            # "is_active": "Активна",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._coupon_venue_map: dict[str, str] = {}
        self._resolved_coupon_venue_name: str | None = None

        self.fields["scheduled_time_begin"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["scheduled_time_end"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["send_window_begin"].input_formats = ["%H:%M"]
        self.fields["send_window_end"].input_formats = ["%H:%M"]
        self.fields["scheduled_date"].input_formats = ["%Y-%m-%d"]

        # Чтобы datetime-local корректно отображался при редактировании.
        if self.instance and self.instance.pk:
            if self.instance.scheduled_time_begin:
                dt = self.instance.scheduled_time_begin
                self.initial["scheduled_time_begin"] = timezone.localtime(dt) if timezone.is_aware(dt) else dt
            if self.instance.scheduled_time_end:
                dt = self.instance.scheduled_time_end
                self.initial["scheduled_time_end"] = timezone.localtime(dt) if timezone.is_aware(dt) else dt

        if "bot_profiles" in self.fields:
            self.fields["bot_profiles"].queryset = BotProfile.objects.filter(is_active=True).order_by(
                "provider_type", "name"
            )
            self.fields["bot_profiles"].required = True

        if self.instance and self.instance.pk:
            if self.instance.send_window_begin:
                self.initial["send_window_begin"] = self.instance.send_window_begin.strftime("%H:%M")
            if self.instance.send_window_end:
                self.initial["send_window_end"] = self.instance.send_window_end.strftime("%H:%M")

        if "coupon_venue_code" in self.fields:
            venue_choices, venue_map = self._build_coupon_venue_choices()
            self._coupon_venue_map = venue_map
            self.fields["coupon_venue_code"].choices = venue_choices
            self.fields["coupon_venue_code"].widget.choices = venue_choices
            self.fields["coupon_venue_code"].required = False

        if "coupon_series" in self.fields:
            series_choices, _ = build_available_coupon_series_choices(
                existing_series=str(getattr(self.instance, "coupon_series", "") or "").strip(),
            )
            self.fields["coupon_series"].choices = series_choices
            self.fields["coupon_series"].required = False

    def _build_coupon_venue_choices(self) -> tuple[list[tuple[str, str]], dict[str, str]]:
        """
        Формирует список заведений для купонной кампании.
        """
        return build_coupon_venue_choices(
            existing_venue_code=str(getattr(self.instance, "coupon_venue_code", "") or "").strip(),
            existing_venue_name=str(getattr(self.instance, "coupon_venue_name", "") or "").strip(),
        )

    def clean(self):
        cleaned_data = super().clean()
        series = str(cleaned_data.get("coupon_series") or "").strip()
        venue_code = str(cleaned_data.get("coupon_venue_code") or "").strip()
        coupon_title = str(cleaned_data.get("coupon_title") or "").strip()
        promo_text = str(cleaned_data.get("coupon_promo_text") or "").strip()

        if not series:
            cleaned_data["coupon_venue_code"] = None
            cleaned_data["coupon_title"] = None
            cleaned_data["coupon_promo_text"] = None
            self._resolved_coupon_venue_name = None
            return cleaned_data

        if not venue_code:
            self.add_error("coupon_venue_code", "Для купонной кампании выберите заведение.")
        elif venue_code not in self._coupon_venue_map:
            self.add_error("coupon_venue_code", "Выбранное заведение не найдено в справочнике активных заведений.")
        if not promo_text:
            self.add_error("coupon_promo_text", "Для купонной кампании укажите текст акции для гостя.")

        cleaned_data["coupon_series"] = series
        cleaned_data["coupon_venue_code"] = venue_code or None
        cleaned_data["coupon_title"] = coupon_title or None
        cleaned_data["coupon_promo_text"] = promo_text or None
        if venue_code not in self._coupon_venue_map:
            self._resolved_coupon_venue_name = None
        elif is_coupon_global_venue(venue_code):
            self._resolved_coupon_venue_name = COUPON_VENUE_GLOBAL_NAME
        else:
            self._resolved_coupon_venue_name = self._coupon_venue_map.get(venue_code, "") or None
        return cleaned_data

    def save(self, commit=True):
        """
        Дополняет запись кампании служебными полями купонного контура.
        """
        instance = super().save(commit=False)
        series = str(getattr(instance, "coupon_series", "") or "").strip()
        if not series:
            instance.coupon_series = None
            instance.coupon_venue_code = None
            instance.coupon_venue_name = None
            instance.coupon_title = None
            instance.coupon_promo_text = None
        else:
            instance.coupon_series = series
            venue_code = str(getattr(instance, "coupon_venue_code", "") or "").strip() or None
            instance.coupon_venue_code = venue_code
            instance.coupon_venue_name = self._resolved_coupon_venue_name or instance.coupon_venue_name or None
            coupon_title = str(getattr(instance, "coupon_title", "") or "").strip()
            instance.coupon_title = coupon_title or None
            promo_text = str(getattr(instance, "coupon_promo_text", "") or "").strip()
            instance.coupon_promo_text = promo_text or None

        if commit:
            instance.save()
            self.save_m2m()
        return instance


class MailingImportPhonesForm(forms.Form):
    file = forms.FileField(
        label="Excel файл (.xlsx) с телефонами",
        help_text="Столбец phone обязателен; telegram_external_id можно добавить для legacy Telegram.",
    )

    def clean_file(self):
        file_obj = self.cleaned_data["file"]
        if not file_obj.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("Нужен файл .xlsx")
        return file_obj


class CouponAutomationConfigForm(forms.ModelForm):
    """
    Пользовательская форма настройки купонного автосценария.

    Часть пилотных параметров пока хранится в `settings`, но на экране
    выводится как отдельные понятные поля, чтобы не заставлять оператора
    редактировать JSON вручную.
    """

    coupon_series = forms.ChoiceField(
        label="Резервная серия без правил",
        required=False,
        choices=[],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    pilot_phones = forms.CharField(
        label="Контрольные телефоны пилота",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "+79129923438",
            }
        ),
        help_text="Один или несколько телефонов через запятую. В режиме «Пилот» это обязательная защита от массового запуска.",
    )
    pilot_include_unmatched = forms.BooleanField(
        label="Добавлять контрольные телефоны вне основного сегмента",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Полезно для проверки сообщения на своём номере, даже если вы не подходите под условие сегмента.",
    )
    birthday_preparation_window_days = forms.IntegerField(
        label="Окно подготовки ко дню рождения, дней",
        required=False,
        min_value=0,
        initial=7,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
        help_text="Сценарий ищет гостей с днём рождения в окне от сегодня до указанного числа дней включительно.",
    )
    notification_distribution_mode = forms.ChoiceField(
        label="Режим отправки сообщений",
        required=False,
        choices=NotificationScenario.DistributionMode.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    notification_target_mode = forms.ChoiceField(
        label="Куда отправлять сообщение",
        required=False,
        choices=NotificationScenario.TargetMode.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text="«Только основной бот» создаёт одну отправку гостю. «Все активные боты» создаёт отправку в каждый подходящий бот гостя.",
    )
    notification_bot_profiles = forms.ModelMultipleChoiceField(
        label="Разрешённые боты",
        required=True,
        queryset=BotProfile.objects.none(),
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        help_text="Выберите хотя бы один активный бот для отправки сообщений.",
        error_messages={
            "required": "Выберите хотя бы один бот для отправки сообщений.",
        },
    )
    notification_timezone = forms.CharField(
        label="Часовой пояс отправки",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Asia/Yekaterinburg"}),
    )
    notification_send_window_begin = forms.TimeField(
        label="Начало окна отправки",
        required=False,
        input_formats=["%H:%M"],
        widget=forms.TimeInput(format="%H:%M", attrs={"class": "form-control", "type": "time"}),
    )
    notification_send_window_end = forms.TimeField(
        label="Конец окна отправки",
        required=False,
        input_formats=["%H:%M"],
        widget=forms.TimeInput(format="%H:%M", attrs={"class": "form-control", "type": "time"}),
    )

    class Meta:
        model = CouponAutomationConfig
        fields = [
            "execution_mode",
            "audience_venue_filter_mode",
            "audience_venue_code",
            "audience_venue_name",
            "venue_selection_mode",
            "coupon_series",
            "venue_code",
            "venue_name",
            "coupon_validity_days",
            "max_recipients_per_run",
            "cooldown_days",
            "coupon_title_template",
            "coupon_promo_text_template",
            "min_order_amount",
            "iikocard_action_note",
            "pilot_phones",
            "pilot_include_unmatched",
            "birthday_preparation_window_days",
        ]
        widgets = {
            "execution_mode": forms.Select(attrs={"class": "form-select"}),
            "audience_venue_filter_mode": forms.Select(attrs={"class": "form-select"}),
            "audience_venue_code": forms.Select(attrs={"class": "form-select"}),
            "audience_venue_name": forms.TextInput(attrs={"class": "form-control"}),
            "venue_selection_mode": forms.Select(attrs={"class": "form-select"}),
            "venue_code": forms.Select(attrs={"class": "form-select"}),
            "venue_name": forms.TextInput(attrs={"class": "form-control"}),
            "coupon_validity_days": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "max_recipients_per_run": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "cooldown_days": forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
            "coupon_title_template": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "maxlength": "120",
                    "placeholder": "Например: Сет «Канпети» в подарок",
                }
            ),
            "coupon_promo_text_template": forms.Textarea(attrs={"class": "form-control", "rows": 5}),
            "min_order_amount": forms.NumberInput(
                attrs={"class": "form-control", "min": "0", "step": "0.01"}
            ),
            "iikocard_action_note": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        }
        labels = {
            "execution_mode": "Состояние автосценария",
            "audience_venue_filter_mode": "Как отбирать гостей по заведению",
            "audience_venue_code": "Заведение для отбора гостей",
            "audience_venue_name": "Название заведения для отбора гостей",
            "venue_selection_mode": "Как выбирать заведения для купонов",
            "venue_code": "Резервное заведение без правил",
            "venue_name": "Название резервного заведения",
            "coupon_validity_days": "Срок действия купона, дней",
            "max_recipients_per_run": "Лимит гостей за проход",
            "cooldown_days": "Пауза перед повтором, дней",
            "coupon_title_template": "Название купона в vtelemax",
            "coupon_promo_text_template": "Текст карточки купона в vtelemax",
            "min_order_amount": "Минимальная сумма заказа в iikoCard",
            "iikocard_action_note": "Что настроено в iikoCard",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        settings = self.instance.settings if isinstance(self.instance.settings, dict) else {}

        self.fields["execution_mode"].choices = COUPON_AUTOSCENARIO_STATE_CHOICES
        self.fields["execution_mode"].help_text = (
            "Черновик ничего не запускает. Пилот разрешает только контрольные телефоны. "
            "Активен будет использоваться для боевого расписания после отдельного включения."
        )
        self.fields["venue_selection_mode"].required = False
        self.fields["audience_venue_filter_mode"].required = False

        self.fields["coupon_series"].choices = build_available_coupon_series_choices(
            existing_series=str(getattr(self.instance, "coupon_series", "") or "").strip()
        )[0]

        audience_venue_choices, self._audience_venue_map = build_coupon_venue_choices(
            existing_venue_code=str(getattr(self.instance, "audience_venue_code", "") or "").strip(),
            existing_venue_name=str(getattr(self.instance, "audience_venue_name", "") or "").strip(),
        )
        audience_venue_choices = [
            (value, label)
            for value, label in audience_venue_choices
            if value != COUPON_VENUE_GLOBAL_CODE
        ]
        self._audience_venue_map.pop(COUPON_VENUE_GLOBAL_CODE, None)
        self.fields["audience_venue_code"].choices = audience_venue_choices
        self.fields["audience_venue_code"].widget.choices = audience_venue_choices

        venue_choices, self._coupon_venue_map = build_coupon_venue_choices(
            existing_venue_code=str(getattr(self.instance, "venue_code", "") or "").strip(),
            existing_venue_name=str(getattr(self.instance, "venue_name", "") or "").strip(),
        )
        self.fields["venue_code"].choices = venue_choices
        self.fields["venue_code"].widget.choices = venue_choices

        pilot_phones = settings.get("pilot_phones") or settings.get("pilot_phone_e164s") or []
        if isinstance(pilot_phones, str):
            pilot_phones = [pilot_phones]
        if not isinstance(pilot_phones, (list, tuple, set)):
            pilot_phones = []
        self.initial["pilot_phones"] = ", ".join(str(phone) for phone in pilot_phones if str(phone).strip())
        self.initial["pilot_include_unmatched"] = bool(settings.get("pilot_include_unmatched"))
        scenario = getattr(self.instance, "scenario", None)
        effective_scenario_type = resolve_coupon_autoscenario_type(self.instance)
        is_birthday_scenario = (
            effective_scenario_type == CouponAutomationConfig.ScenarioType.BIRTHDAY_COUPON
        )
        self.fields["birthday_preparation_window_days"].required = is_birthday_scenario
        if is_birthday_scenario:
            try:
                self.initial["birthday_preparation_window_days"] = max(
                    0,
                    int(settings.get("birthday_preparation_window_days") or 7),
                )
            except (TypeError, ValueError):
                self.initial["birthday_preparation_window_days"] = 7
        else:
            self.fields["birthday_preparation_window_days"].widget = forms.HiddenInput()
        if scenario is not None:
            self.initial["notification_distribution_mode"] = scenario.distribution_mode
            self.initial["notification_target_mode"] = scenario.target_mode
            self.initial["notification_bot_profiles"] = list(
                scenario.bot_profiles.filter(is_active=True).values_list("id", flat=True)
            )
            self.initial["notification_timezone"] = scenario.timezone or "Asia/Yekaterinburg"
            if scenario.send_window_begin:
                self.initial["notification_send_window_begin"] = scenario.send_window_begin.strftime("%H:%M")
            if scenario.send_window_end:
                self.initial["notification_send_window_end"] = scenario.send_window_end.strftime("%H:%M")
        self.fields["notification_bot_profiles"].queryset = BotProfile.objects.filter(is_active=True).order_by(
            "provider_type",
            "name",
            "id",
        )

    def clean_pilot_phones(self):
        raw_value = str(self.cleaned_data.get("pilot_phones") or "").strip()
        if not raw_value:
            return []

        parts = raw_value.replace(";", ",").replace("\n", ",").split(",")
        result: list[str] = []
        invalid: list[str] = []
        for part in parts:
            candidate = str(part or "").strip()
            if not candidate:
                continue
            normalized = normalize_phone_e164(candidate)
            if not normalized:
                invalid.append(candidate)
                continue
            if normalized not in result:
                result.append(normalized)

        if invalid:
            raise forms.ValidationError(
                "Не удалось распознать телефоны: %(phones)s",
                params={"phones": ", ".join(invalid)},
            )
        return result

    def clean(self):
        cleaned_data = super().clean()
        execution_mode = cleaned_data.get("execution_mode")
        pilot_phones = cleaned_data.get("pilot_phones") or []
        audience_mode = (
            cleaned_data.get("audience_venue_filter_mode")
            or CouponAutomationConfig.AudienceVenueFilterMode.DISABLED
        )
        audience_venue_code = str(cleaned_data.get("audience_venue_code") or "").strip()
        venue_code = str(cleaned_data.get("venue_code") or "").strip()
        effective_scenario_type = resolve_coupon_autoscenario_type(self.instance)
        is_birthday_scenario = (
            effective_scenario_type == CouponAutomationConfig.ScenarioType.BIRTHDAY_COUPON
        )
        distribution_mode = (
            cleaned_data.get("notification_distribution_mode")
            or getattr(getattr(self.instance, "scenario", None), "distribution_mode", "")
            or NotificationScenario.DistributionMode.IMMEDIATE
        )
        send_window_begin = cleaned_data.get("notification_send_window_begin")
        send_window_end = cleaned_data.get("notification_send_window_end")

        if execution_mode == CouponAutomationConfig.ExecutionMode.PILOT and not pilot_phones:
            self.add_error(
                "pilot_phones",
                "Для режима «Пилот» укажите хотя бы один контрольный телефон.",
            )
        if is_birthday_scenario and cleaned_data.get("birthday_preparation_window_days") is None:
            self.add_error(
                "birthday_preparation_window_days",
                "Укажите окно подготовки ко дню рождения.",
            )
        if distribution_mode == NotificationScenario.DistributionMode.UNIFORM:
            if not send_window_begin:
                self.add_error("notification_send_window_begin", "Укажите начало окна отправки.")
            if not send_window_end:
                self.add_error("notification_send_window_end", "Укажите конец окна отправки.")
        if (
            execution_mode == CouponAutomationConfig.ExecutionMode.AUTOMATIC
            and distribution_mode == NotificationScenario.DistributionMode.IMMEDIATE
        ):
            self.add_error(
                "notification_distribution_mode",
                "Для состояния «Активен» выберите «Равномерно в окне», чтобы сообщения не уходили сразу после подтверждения vtelemax.",
            )
        if execution_mode in {
            CouponAutomationConfig.ExecutionMode.PILOT,
            CouponAutomationConfig.ExecutionMode.AUTOMATIC,
        }:
            template = getattr(getattr(self.instance, "scenario", None), "template", None)
            try:
                validate_coupon_code_placeholder(getattr(template, "message_text", ""))
            except forms.ValidationError as exc:
                self.add_error(
                    None,
                    forms.ValidationError(
                        "Нельзя перевести купонный автосценарий в «Пилот» или «Активен»: %(error)s",
                        params={"error": "; ".join(exc.messages)},
                    ),
                )

        if not cleaned_data.get("venue_selection_mode"):
            cleaned_data["venue_selection_mode"] = CouponAutomationConfig.VenueSelectionMode.LAST_ORDER

        if not audience_mode:
            audience_mode = CouponAutomationConfig.AudienceVenueFilterMode.DISABLED
        cleaned_data["audience_venue_filter_mode"] = audience_mode
        if audience_mode == CouponAutomationConfig.AudienceVenueFilterMode.DISABLED:
            cleaned_data["audience_venue_code"] = None
            cleaned_data["audience_venue_name"] = None
        else:
            if not audience_venue_code:
                self.add_error("audience_venue_code", "Для отбора гостей выберите заведение.")
            elif audience_venue_code == COUPON_VENUE_GLOBAL_CODE:
                self.add_error("audience_venue_code", "Для отбора гостей выберите конкретное заведение.")
            elif audience_venue_code not in getattr(self, "_audience_venue_map", {}):
                self.add_error("audience_venue_code", "Выбранное заведение не найдено в справочнике.")
            else:
                cleaned_data["audience_venue_code"] = audience_venue_code
                cleaned_data["audience_venue_name"] = (
                    self._audience_venue_map.get(audience_venue_code)
                    or cleaned_data.get("audience_venue_name")
                )
                cleaned_data["venue_selection_mode"] = (
                    CouponAutomationConfig.VenueSelectionMode.LAST_ORDER
                )

        if venue_code and venue_code in getattr(self, "_coupon_venue_map", {}):
            cleaned_data["venue_name"] = self._coupon_venue_map.get(venue_code) or cleaned_data.get("venue_name")

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        settings = dict(instance.settings or {})
        pilot_phones = self.cleaned_data.get("pilot_phones") or []
        if pilot_phones:
            settings["pilot_phones"] = pilot_phones
        else:
            settings.pop("pilot_phones", None)
            settings.pop("pilot_phone_e164s", None)

        settings["pilot_include_unmatched"] = bool(
            self.cleaned_data.get("pilot_include_unmatched")
        )
        scenario = getattr(instance, "scenario", None)
        effective_scenario_type = resolve_coupon_autoscenario_type(instance)
        if effective_scenario_type == CouponAutomationConfig.ScenarioType.BIRTHDAY_COUPON:
            settings["birthday_preparation_window_days"] = int(
                self.cleaned_data.get("birthday_preparation_window_days") or 0
            )
        instance.settings = settings

        if commit:
            instance.full_clean()
            instance.save()
            scenario = getattr(instance, "scenario", None)
            if scenario is not None:
                scenario.distribution_mode = (
                    self.cleaned_data.get("notification_distribution_mode")
                    or scenario.distribution_mode
                    or NotificationScenario.DistributionMode.IMMEDIATE
                )
                scenario.target_mode = (
                    self.cleaned_data.get("notification_target_mode")
                    or scenario.target_mode
                    or NotificationScenario.TargetMode.PRIMARY_ONLY
                )
                scenario.timezone = (
                    str(self.cleaned_data.get("notification_timezone") or "").strip()
                    or scenario.timezone
                    or "Asia/Yekaterinburg"
                )
                scenario.send_window_begin = self.cleaned_data.get("notification_send_window_begin")
                scenario.send_window_end = self.cleaned_data.get("notification_send_window_end")
                scenario.save(
                    update_fields=[
                        "distribution_mode",
                        "target_mode",
                        "timezone",
                        "send_window_begin",
                        "send_window_end",
                        "updated_at",
                    ]
                )
                scenario.bot_profiles.set(self.cleaned_data.get("notification_bot_profiles") or [])
        return instance


class FillBirthdayRequestScenarioForm(forms.ModelForm):
    request_repeat_days = forms.IntegerField(
        label="Пауза перед повторной просьбой, дней",
        required=True,
        min_value=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
    )
    bot_profiles = forms.ModelMultipleChoiceField(
        label="Разрешённые боты",
        required=False,
        queryset=BotProfile.objects.none(),
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = NotificationScenario
        fields = [
            "is_active",
            "distribution_mode",
            "target_mode",
            "send_window_begin",
            "send_window_end",
            "timezone",
        ]
        widgets = {
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "distribution_mode": forms.Select(attrs={"class": "form-select"}),
            "target_mode": forms.Select(attrs={"class": "form-select"}),
            "send_window_begin": forms.TimeInput(
                format="%H:%M",
                attrs={"class": "form-control", "type": "time"},
            ),
            "send_window_end": forms.TimeInput(
                format="%H:%M",
                attrs={"class": "form-control", "type": "time"},
            ),
            "timezone": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Asia/Yekaterinburg"}
            ),
        }
        labels = {
            "is_active": "Включить первое сообщение",
            "distribution_mode": "Режим отправки сообщений",
            "target_mode": "Куда отправлять сообщение",
            "send_window_begin": "Начало окна отправки",
            "send_window_end": "Конец окна отправки",
            "timezone": "Часовой пояс отправки",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        settings = self.instance.settings if isinstance(self.instance.settings, dict) else {}
        self.fields["send_window_begin"].input_formats = ["%H:%M"]
        self.fields["send_window_end"].input_formats = ["%H:%M"]
        self.fields["bot_profiles"].queryset = BotProfile.objects.filter(is_active=True).order_by(
            "provider_type",
            "name",
            "id",
        )
        try:
            self.initial["request_repeat_days"] = max(1, int(settings.get("request_repeat_days") or 30))
        except (TypeError, ValueError):
            self.initial["request_repeat_days"] = 30
        self.initial["bot_profiles"] = list(
            self.instance.bot_profiles.filter(is_active=True).values_list("id", flat=True)
        )
        if self.instance.send_window_begin:
            self.initial["send_window_begin"] = self.instance.send_window_begin.strftime("%H:%M")
        if self.instance.send_window_end:
            self.initial["send_window_end"] = self.instance.send_window_end.strftime("%H:%M")

    def clean(self):
        cleaned_data = super().clean()
        distribution_mode = (
            cleaned_data.get("distribution_mode")
            or self.instance.distribution_mode
            or NotificationScenario.DistributionMode.IMMEDIATE
        )
        if distribution_mode == NotificationScenario.DistributionMode.UNIFORM:
            if not cleaned_data.get("send_window_begin"):
                self.add_error("send_window_begin", "Укажите начало окна отправки.")
            if not cleaned_data.get("send_window_end"):
                self.add_error("send_window_end", "Укажите конец окна отправки.")

        if cleaned_data.get("is_active") and not cleaned_data.get("bot_profiles"):
            self.add_error("bot_profiles", "Выберите хотя бы один бот для первого сообщения.")

        if not str(cleaned_data.get("timezone") or "").strip():
            cleaned_data["timezone"] = "Asia/Yekaterinburg"

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        settings = dict(instance.settings or {})
        settings["request_repeat_days"] = int(self.cleaned_data.get("request_repeat_days") or 30)
        instance.settings = settings

        if commit:
            instance.full_clean()
            instance.save()
            instance.bot_profiles.set(self.cleaned_data.get("bot_profiles") or [])
        return instance


class CouponAutomationRuleForm(forms.ModelForm):
    """
    Строка пользовательского правила выбора купонной серии для автосценария.
    """

    coupon_series = forms.ChoiceField(
        label="Серия купонов",
        required=False,
        choices=[],
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )

    class Meta:
        model = CouponAutomationRule
        fields = [
            "is_active",
            "scope_type",
            "venue_code",
            "coupon_series",
            "coupon_validity_days",
            "priority",
            "min_order_amount",
            "iikocard_action_note",
            "coupon_title_template",
            "coupon_promo_text_template",
        ]
        widgets = {
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "scope_type": forms.HiddenInput(),
            "venue_code": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "coupon_validity_days": forms.HiddenInput(),
            "priority": forms.HiddenInput(),
            "min_order_amount": forms.HiddenInput(),
            "iikocard_action_note": forms.HiddenInput(),
            "coupon_title_template": forms.TextInput(
                attrs={
                    "class": "form-control form-control-sm",
                    "maxlength": "120",
                    "placeholder": "Если пусто — общее название",
                }
            ),
            "coupon_promo_text_template": forms.HiddenInput(),
        }
        labels = {
            "is_active": "Использовать",
            "scope_type": "Область",
            "venue_code": "Заведение или вся сеть",
            "coupon_series": "Серия купонов",
            "coupon_validity_days": "Срок, дней",
            "priority": "Приоритет",
            "min_order_amount": "Мин. заказ",
            "iikocard_action_note": "Что настроено в iikoCard",
            "coupon_title_template": "Название купона в vtelemax",
            "coupon_promo_text_template": "Текст карточки купона в vtelemax",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._coupon_venue_map: dict[str, str] = {}
        if not self.instance.pk:
            self.initial.setdefault("is_active", False)
            self.initial.setdefault("priority", 100)
            self.initial.setdefault("scope_type", CouponAutomationRule.ScopeType.VENUE)
        self.fields["scope_type"].required = False

        posted_series = ""
        posted_venue_code = ""
        if self.data:
            posted_series = str(self.data.get(self.add_prefix("coupon_series")) or "").strip()
            posted_venue_code = str(self.data.get(self.add_prefix("venue_code")) or "").strip()

        existing_series = posted_series or str(getattr(self.instance, "coupon_series", "") or "").strip()
        self.fields["coupon_series"].choices = build_available_coupon_series_choices(
            existing_series=existing_series,
        )[0]

        existing_venue_code = posted_venue_code or str(getattr(self.instance, "venue_code", "") or "").strip()
        existing_venue_name = str(getattr(self.instance, "venue_name", "") or "").strip()
        venue_choices, venue_map = build_coupon_venue_choices(
            existing_venue_code=existing_venue_code,
            existing_venue_name=existing_venue_name,
        )
        self._coupon_venue_map = venue_map
        self.fields["venue_code"].choices = venue_choices
        self.fields["venue_code"].widget.choices = venue_choices

    def _has_meaningful_rule_data(self) -> bool:
        """
        Проверяет, что свободная строка действительно заполнена как правило.
        """
        if not self.is_bound:
            return bool(self.instance and self.instance.pk)

        meaningful_field_names = [
            "is_active",
            "coupon_series",
            "venue_code",
            "coupon_validity_days",
            "min_order_amount",
            "iikocard_action_note",
            "coupon_title_template",
            "coupon_promo_text_template",
        ]
        for field_name in meaningful_field_names:
            value = self.data.get(self.add_prefix(field_name))
            if str(value or "").strip():
                return True
        return False

    def has_changed(self):
        if not self.instance.pk and not self._has_meaningful_rule_data():
            return False
        return super().has_changed()

    def clean(self):
        cleaned_data = super().clean()
        if self.cleaned_data.get("DELETE"):
            return cleaned_data

        coupon_series = str(cleaned_data.get("coupon_series") or "").strip()
        venue_code = str(cleaned_data.get("venue_code") or "").strip()
        scope_type = (
            CouponAutomationRule.ScopeType.GLOBAL
            if is_coupon_global_venue(venue_code)
            else CouponAutomationRule.ScopeType.VENUE
        )
        cleaned_data["scope_type"] = scope_type

        if not coupon_series:
            self.add_error("coupon_series", "Укажите серию купонов.")

        if scope_type == CouponAutomationRule.ScopeType.GLOBAL:
            cleaned_data["venue_code"] = COUPON_VENUE_GLOBAL_CODE
            cleaned_data["venue_name"] = COUPON_VENUE_GLOBAL_NAME
        else:
            if not venue_code:
                self.add_error("venue_code", "Выберите заведение или вариант «Вся сеть».")
            elif venue_code not in self._coupon_venue_map:
                self.add_error("venue_code", "Выбранное заведение не найдено в справочнике.")
            else:
                cleaned_data["venue_name"] = self._coupon_venue_map.get(venue_code) or venue_code

        cleaned_data["coupon_series"] = coupon_series
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        scope_type = self.cleaned_data.get("scope_type") or CouponAutomationRule.ScopeType.VENUE
        instance.scope_type = scope_type
        if scope_type == CouponAutomationRule.ScopeType.GLOBAL:
            instance.venue_code = COUPON_VENUE_GLOBAL_CODE
            instance.venue_name = COUPON_VENUE_GLOBAL_NAME
        else:
            venue_code = str(self.cleaned_data.get("venue_code") or "").strip()
            instance.venue_code = venue_code
            instance.venue_name = self._coupon_venue_map.get(venue_code) or venue_code
        instance.coupon_series = str(self.cleaned_data.get("coupon_series") or "").strip()
        if commit:
            instance.full_clean()
            instance.save()
        return instance


CouponAutomationRuleFormSet = forms.inlineformset_factory(
    CouponAutomationConfig,
    CouponAutomationRule,
    form=CouponAutomationRuleForm,
    fields=CouponAutomationRuleForm.Meta.fields,
    extra=0,
    can_delete=True,
)
