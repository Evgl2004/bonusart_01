"""
POST-действия экрана «Категории и фокусы».
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.text import slugify
from django.views import View

from guests.models import FocusCategory, OlapCategoryDict, VirtualCategory
from guests.services.olap_catalogs import rebuild_focus_category_nomenclature_resolved


class FocusCategoriesActionsView(View):
    """
    Обрабатывает действия управления фокусными категориями.
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        action = (request.POST.get("action") or "").strip()

        if action == "create_focus_from_olap":
            return self._create_focus_from_olap(request)
        if action == "create_focus_from_virtual":
            return self._create_focus_from_virtual(request)
        if action == "set_focus_enabled":
            return self._set_focus_enabled(request)
        if action == "rebuild_focus_resolved":
            return self._rebuild_focus_resolved(request)

        messages.error(request, "Неизвестное действие для экрана категорий.")
        return redirect(self._build_redirect_url(request))

    def _create_focus_from_olap(self, request):
        """
        Создаёт фокусную категорию из категории OLAP.
        """
        olap_category_id = _parse_positive_int(request.POST.get("olap_category_id"))
        if olap_category_id is None:
            messages.error(request, "Выберите OLAP-категорию для создания фокуса.")
            return redirect(self._build_redirect_url(request))

        olap_category = (
            OlapCategoryDict.objects.filter(id=olap_category_id)
            .only("id", "category_name")
            .first()
        )
        if olap_category is None:
            messages.error(request, "OLAP-категория не найдена.")
            return redirect(self._build_redirect_url(request))

        focus_name = (request.POST.get("focus_name") or "").strip() or (olap_category.category_name or "").strip()
        if not focus_name:
            messages.error(request, "Не удалось определить имя фокусной категории.")
            return redirect(self._build_redirect_url(request))

        raw_code = (request.POST.get("focus_code") or "").strip()
        if raw_code:
            focus_code = raw_code
            if FocusCategory.objects.filter(code=focus_code).exists():
                messages.error(request, f"Фокус с кодом «{focus_code}» уже существует.")
                return redirect(self._build_redirect_url(request))
        else:
            focus_code = self._generate_unique_focus_code(
                base_name=focus_name,
                fallback_prefix=f"focus-olap-{olap_category.id}",
            )

        priority_weight = _parse_positive_int(request.POST.get("priority_weight")) or 1
        tag_code = (request.POST.get("tag_code") or "").strip() or None

        focus = FocusCategory.objects.create(
            code=focus_code,
            name=focus_name,
            source_type=FocusCategory.SourceType.OLAP_DIRECT,
            olap_category=olap_category,
            virtual_category=None,
            is_enabled=True,
            priority_weight=priority_weight,
            tag_code=tag_code,
        )

        rebuild_stats = rebuild_focus_category_nomenclature_resolved(focus_codes=[focus.code])
        messages.success(
            request,
            (
                f"Создан фокус «{focus.name}». "
                f"Связей номенклатуры записано: {rebuild_stats.written_links}."
            ),
        )
        return redirect(self._build_redirect_url(request, selected_focus_id=int(focus.id)))

    def _create_focus_from_virtual(self, request):
        """
        Создаёт фокусную категорию из виртуальной категории.
        """
        virtual_category_id = _parse_positive_int(request.POST.get("virtual_category_id"))
        if virtual_category_id is None:
            messages.error(request, "Выберите виртуальную категорию для создания фокуса.")
            return redirect(self._build_redirect_url(request))

        virtual_category = (
            VirtualCategory.objects.filter(id=virtual_category_id)
            .only("id", "name", "code")
            .first()
        )
        if virtual_category is None:
            messages.error(request, "Виртуальная категория не найдена.")
            return redirect(self._build_redirect_url(request))

        focus_name = (request.POST.get("focus_name") or "").strip() or (virtual_category.name or "").strip()
        if not focus_name:
            messages.error(request, "Не удалось определить имя фокусной категории.")
            return redirect(self._build_redirect_url(request))

        raw_code = (request.POST.get("focus_code") or "").strip()
        if raw_code:
            focus_code = raw_code
            if FocusCategory.objects.filter(code=focus_code).exists():
                messages.error(request, f"Фокус с кодом «{focus_code}» уже существует.")
                return redirect(self._build_redirect_url(request))
        else:
            focus_code = self._generate_unique_focus_code(
                base_name=focus_name,
                fallback_prefix=f"focus-virtual-{virtual_category.id}",
            )

        priority_weight = _parse_positive_int(request.POST.get("priority_weight")) or 1
        tag_code = (request.POST.get("tag_code") or "").strip() or None

        focus = FocusCategory.objects.create(
            code=focus_code,
            name=focus_name,
            source_type=FocusCategory.SourceType.VIRTUAL,
            virtual_category=virtual_category,
            olap_category=None,
            is_enabled=True,
            priority_weight=priority_weight,
            tag_code=tag_code,
        )

        rebuild_stats = rebuild_focus_category_nomenclature_resolved(focus_codes=[focus.code])
        messages.success(
            request,
            (
                f"Создан фокус «{focus.name}». "
                f"Связей номенклатуры записано: {rebuild_stats.written_links}."
            ),
        )
        return redirect(self._build_redirect_url(request, selected_focus_id=int(focus.id)))

    def _set_focus_enabled(self, request):
        """
        Включает или выключает фокусную категорию.
        """
        focus_id = _parse_positive_int(request.POST.get("focus_id"))
        if focus_id is None:
            messages.error(request, "Не указан фокус для изменения статуса.")
            return redirect(self._build_redirect_url(request))

        focus = FocusCategory.objects.filter(id=focus_id).first()
        if focus is None:
            messages.error(request, "Фокусная категория не найдена.")
            return redirect(self._build_redirect_url(request))

        target_enabled = _to_bool_flag(request.POST.get("enabled"))
        if bool(focus.is_enabled) == bool(target_enabled):
            return redirect(self._build_redirect_url(request, selected_focus_id=int(focus.id)))

        focus.is_enabled = bool(target_enabled)
        focus.save(update_fields=["is_enabled", "updated_at"])

        rebuild_stats = rebuild_focus_category_nomenclature_resolved(focus_codes=[focus.code])
        if target_enabled:
            messages.success(
                request,
                f"Фокус «{focus.name}» включён. Связей записано: {rebuild_stats.written_links}.",
            )
        else:
            messages.success(
                request,
                f"Фокус «{focus.name}» отключён. Удалено связей: {rebuild_stats.deleted_links}.",
            )
        return redirect(self._build_redirect_url(request, selected_focus_id=int(focus.id)))

    def _rebuild_focus_resolved(self, request):
        """
        Запускает пересборку предрассчитанных связей focus -> nomenclature.
        """
        focus_id = _parse_positive_int(request.POST.get("focus_id"))
        focus_codes = None
        if focus_id is not None:
            focus = FocusCategory.objects.filter(id=focus_id).only("code").first()
            if focus is None:
                messages.error(request, "Фокусная категория не найдена.")
                return redirect(self._build_redirect_url(request))
            focus_codes = [focus.code]

        rebuild_stats = rebuild_focus_category_nomenclature_resolved(focus_codes=focus_codes)
        messages.success(
            request,
            (
                "Пересборка выполнена: "
                f"scanned={rebuild_stats.scanned_focus_categories}, "
                f"rebuilt={rebuild_stats.rebuilt_focus_categories}, "
                f"written={rebuild_stats.written_links}, "
                f"deleted={rebuild_stats.deleted_links}."
            ),
        )
        return redirect(self._build_redirect_url(request, selected_focus_id=focus_id))

    @staticmethod
    def _build_redirect_url(request, *, selected_focus_id: int | None = None) -> str:
        """
        Формирует URL возврата в экран категорий с сохранением фильтров.
        """
        params = {
            "as_of_date": (request.POST.get("as_of_date") or "").strip(),
            "window_days": (request.POST.get("window_days") or "").strip(),
            "department_id": (request.POST.get("department_id") or "").strip(),
            "selected_focus_id": str(selected_focus_id or _parse_positive_int(request.POST.get("selected_focus_id")) or ""),
        }
        params = {key: value for key, value in params.items() if value}
        base_url = reverse("focus_categories")
        if not params:
            return base_url
        return f"{base_url}?{urlencode(params)}"

    @staticmethod
    def _generate_unique_focus_code(*, base_name: str, fallback_prefix: str) -> str:
        """
        Генерирует уникальный `code` фокусной категории.
        """
        slug = (slugify(base_name) or "").strip("-")
        base = slug or fallback_prefix
        candidate = base
        suffix = 2
        while FocusCategory.objects.filter(code=candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate


def _parse_positive_int(raw_value: str | None) -> int | None:
    """
    Парсит положительное целое число.
    """
    try:
        parsed = int(raw_value or "")
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _to_bool_flag(raw_value: str | None) -> bool:
    """
    Нормализует флаг из формы.
    """
    return (raw_value or "").strip().lower() in {"1", "true", "yes", "on"}

