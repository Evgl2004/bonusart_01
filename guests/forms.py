from django import forms
from django.utils import timezone

from .models import BotProfile, Category, CouponAutomationConfig, Mailing, MessageTemplate
from .services.coupon_constants import COUPON_VENUE_GLOBAL_NAME, is_coupon_global_venue
from .services.coupon_series import build_available_coupon_series_choices
from .services.coupon_venues import build_coupon_venue_choices
from .services.guest_resolution import normalize_phone_e164


COUPON_AUTOSCENARIO_STATE_CHOICES = [
    (CouponAutomationConfig.ExecutionMode.REPORT_ONLY, "Черновик"),
    (CouponAutomationConfig.ExecutionMode.PILOT, "Пилот"),
    (CouponAutomationConfig.ExecutionMode.AUTOMATIC, "Активен"),
    (CouponAutomationConfig.ExecutionMode.PAUSED, "Пауза"),
]


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
        promo_text = str(cleaned_data.get("coupon_promo_text") or "").strip()

        if not series:
            cleaned_data["coupon_venue_code"] = None
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
            instance.coupon_promo_text = None
        else:
            instance.coupon_series = series
            venue_code = str(getattr(instance, "coupon_venue_code", "") or "").strip() or None
            instance.coupon_venue_code = venue_code
            instance.coupon_venue_name = self._resolved_coupon_venue_name or instance.coupon_venue_name or None
            promo_text = str(getattr(instance, "coupon_promo_text", "") or "").strip()
            instance.coupon_promo_text = promo_text or None

        if commit:
            instance.save()
            self.save_m2m()
        return instance


class MailingImportPhonesForm(forms.Form):
    file = forms.FileField(
        label="Excel файл (.xlsx) с телефонами",
        help_text="Один столбец phone/телефон/phone_number",
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
        label="Серия купонов",
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

    class Meta:
        model = CouponAutomationConfig
        fields = [
            "execution_mode",
            "coupon_series",
            "venue_code",
            "venue_name",
            "coupon_validity_days",
            "max_recipients_per_run",
            "cooldown_days",
            "coupon_promo_text_template",
            "min_order_amount",
            "iikocard_action_note",
            "pilot_phones",
            "pilot_include_unmatched",
        ]
        widgets = {
            "execution_mode": forms.Select(attrs={"class": "form-select"}),
            "venue_code": forms.Select(attrs={"class": "form-select"}),
            "venue_name": forms.TextInput(attrs={"class": "form-control"}),
            "coupon_validity_days": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "max_recipients_per_run": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "cooldown_days": forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
            "coupon_promo_text_template": forms.Textarea(attrs={"class": "form-control", "rows": 5}),
            "min_order_amount": forms.NumberInput(
                attrs={"class": "form-control", "min": "0", "step": "0.01"}
            ),
            "iikocard_action_note": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        }
        labels = {
            "execution_mode": "Состояние автосценария",
            "venue_code": "Заведение",
            "venue_name": "Название заведения",
            "coupon_validity_days": "Срок действия купона, дней",
            "max_recipients_per_run": "Лимит гостей за проход",
            "cooldown_days": "Пауза перед повтором, дней",
            "coupon_promo_text_template": "Описание купона для vtelemax",
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

        self.fields["coupon_series"].choices = build_available_coupon_series_choices(
            existing_series=str(getattr(self.instance, "coupon_series", "") or "").strip()
        )[0]

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
        venue_code = str(cleaned_data.get("venue_code") or "").strip()

        if execution_mode == CouponAutomationConfig.ExecutionMode.PILOT and not pilot_phones:
            self.add_error(
                "pilot_phones",
                "Для режима «Пилот» укажите хотя бы один контрольный телефон.",
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
        instance.settings = settings

        if commit:
            instance.full_clean()
            instance.save()
        return instance
