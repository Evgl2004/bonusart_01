from django import forms
from django.db.models import Max
from django.utils import timezone

from .models import BotProfile, Category, Mailing, MessageTemplate, TerminalDepartmentMap
from .services.coupon_constants import COUPON_VENUE_GLOBAL_CODE, COUPON_VENUE_GLOBAL_NAME, is_coupon_global_venue


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
            "coupon_series": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Например: BDAY_2026",
                }
            ),
            "coupon_venue_code": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "coupon_promo_text": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Например: Скидка 20% на сет при заказе от 1500 ₽.",
                }
            ),
            "bot_profiles": forms.SelectMultiple(
                attrs={
                    "class": "form-select",
                    "size": "6",
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
            self.fields["coupon_venue_code"].required = False

    def _build_coupon_venue_choices(self) -> tuple[list[tuple[str, str]], dict[str, str]]:
        """
        Формирует список заведений для купонной кампании.

        Источник:
        1. активные сопоставления `TerminalDepartmentMap`;
        2. группировка по `department_id`, чтобы убрать дубли по terminalGroupId.
        """
        rows = (
            TerminalDepartmentMap.objects.filter(is_active=True)
            .exclude(department_id="")
            .values("department_id")
            .annotate(department_name=Max("department_name"))
            .order_by("department_name", "department_id")
        )
        choices: list[tuple[str, str]] = [("", "— Выберите заведение —")]
        venue_map: dict[str, str] = {
            COUPON_VENUE_GLOBAL_CODE: COUPON_VENUE_GLOBAL_NAME,
        }
        choices.append((COUPON_VENUE_GLOBAL_CODE, f"{COUPON_VENUE_GLOBAL_NAME} (для всех заведений)"))
        for row in rows:
            dep_id = str(row.get("department_id") or "").strip()
            if not dep_id:
                continue
            dep_name = str(row.get("department_name") or "").strip() or dep_id
            venue_map[dep_id] = dep_name
            choices.append((dep_id, f"{dep_name} ({dep_id})"))

        instance_dep_id = str(getattr(self.instance, "coupon_venue_code", "") or "").strip()
        if instance_dep_id and instance_dep_id not in venue_map:
            instance_dep_name = str(getattr(self.instance, "coupon_venue_name", "") or "").strip() or instance_dep_id
            venue_map[instance_dep_id] = instance_dep_name
            choices.append((instance_dep_id, f"{instance_dep_name} ({instance_dep_id})"))
        return choices, venue_map

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
        if not promo_text:
            self.add_error("coupon_promo_text", "Для купонной кампании укажите текст акции для гостя.")

        cleaned_data["coupon_series"] = series
        cleaned_data["coupon_venue_code"] = venue_code or None
        cleaned_data["coupon_promo_text"] = promo_text or None
        if is_coupon_global_venue(venue_code):
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
