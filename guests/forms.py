from django import forms
from django.utils import timezone

from .models import BotProfile, Category, Mailing, MailingChannel, MessageTemplate


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
            "bot_profiles",
            "channels",
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
            "bot_profiles": forms.SelectMultiple(
                attrs={
                    "class": "form-select",
                    "size": "6",
                }
            ),
            "channels": forms.SelectMultiple(
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
            "bot_profiles": "Боты для рассылки",
            "channels": "Каналы рассылки",
            # "is_active": "Активна",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

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

        if "channels" in self.fields:
            self.fields["channels"].queryset = MailingChannel.objects.order_by("id")
            self.fields["channels"].required = False

        if self.instance and self.instance.pk:
            if self.instance.send_window_begin:
                self.initial["send_window_begin"] = self.instance.send_window_begin.strftime("%H:%M")
            if self.instance.send_window_end:
                self.initial["send_window_end"] = self.instance.send_window_end.strftime("%H:%M")


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
