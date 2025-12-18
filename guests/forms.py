from django import forms
from .models import Category


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "description", "is_active","external_id"]
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
