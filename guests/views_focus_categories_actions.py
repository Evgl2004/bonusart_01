"""
POST-действия экрана «Категории и целевые группы».
"""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views import View

from guests.models import (
    FocusCategory,
    OlapCategoryDict,
    VirtualCategory,
    VirtualCategoryNomenclatureLink,
)
from guests.services.daily_category_fact import rebuild_daily_category_fact_from_raw_lines
from guests.services.olap_catalogs import rebuild_focus_category_nomenclature_resolved
from guests.services.order_fact import rebuild_order_fact_from_raw_lines
from guests.services.window_metrics import rebuild_window_metrics_from_daily_facts


MANUAL_REBUILD_WINDOWS = (7, 14, 30, 60, 180)
MANUAL_REBUILD_MAX_DAYS = max(MANUAL_REBUILD_WINDOWS)
MANUAL_REBUILD_BATCH_SIZE = 2000


class FocusCategoriesActionsView(View):
    """
    Обрабатывает действия управления целевыми категориями.
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        action = (request.POST.get("action") or "").strip()

        if action == "create_focus_from_olap":
            return self._create_focus_from_olap(request)
        if action == "create_focus_from_virtual":
            return self._create_focus_from_virtual(request)
        if action == "create_virtual_category_from_nomenclature":
            return self._create_virtual_category_from_nomenclature(request)
        if action == "set_focus_enabled":
            return self._set_focus_enabled(request)
        if action == "rebuild_focus_resolved":
            return self._rebuild_focus_resolved(request)
        if action == "rebuild_aggregates_tail":
            return self._rebuild_aggregates_tail(request)

        messages.error(request, "Неизвестное действие для экрана категорий.")
        return redirect(self._build_redirect_url(request))

    def _create_focus_from_olap(self, request):
        """
        Создаёт целевую категорию из категории OLAP.
        """
        olap_category_id = _parse_positive_int(request.POST.get("olap_category_id"))
        if olap_category_id is None:
            messages.error(request, "Выберите OLAP-категорию для создания целевой категории.")
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
            messages.error(request, "Не удалось определить имя целевой категории.")
            return redirect(self._build_redirect_url(request))

        raw_code = (request.POST.get("focus_code") or "").strip()
        if raw_code:
            focus_code = raw_code
            if FocusCategory.objects.filter(code=focus_code).exists():
                messages.error(request, f"Целевая категория с кодом «{focus_code}» уже существует.")
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
                f"Создана целевая категория «{focus.name}». "
                f"Связей номенклатуры записано: {rebuild_stats.written_links}. "
                "Для обновления витрин используйте кнопку «Пересчитать итоги за хвост 180 дней»."
            ),
        )
        return redirect(self._build_redirect_url(request, selected_focus_id=int(focus.id)))

    def _create_focus_from_virtual(self, request):
        """
        Создаёт целевую категорию из виртуальной категории.
        """
        virtual_category_id = _parse_positive_int(request.POST.get("virtual_category_id"))
        if virtual_category_id is None:
            messages.error(request, "Выберите виртуальную категорию для создания целевой категории.")
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
            messages.error(request, "Не удалось определить имя целевой категории.")
            return redirect(self._build_redirect_url(request))

        raw_code = (request.POST.get("focus_code") or "").strip()
        if raw_code:
            focus_code = raw_code
            if FocusCategory.objects.filter(code=focus_code).exists():
                messages.error(request, f"Целевая категория с кодом «{focus_code}» уже существует.")
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
                f"Создана целевая категория «{focus.name}». "
                f"Связей номенклатуры записано: {rebuild_stats.written_links}. "
                "Для обновления витрин используйте кнопку «Пересчитать итоги за хвост 180 дней»."
            ),
        )
        return redirect(self._build_redirect_url(request, selected_focus_id=int(focus.id)))

    def _create_virtual_category_from_nomenclature(self, request):
        """
        Создаёт виртуальную категорию из выбранных номенклатур.

        Опционально сразу создаёт и целевую категорию на её основе.
        """
        virtual_name = (request.POST.get("virtual_name") or "").strip()
        if not virtual_name:
            messages.error(request, "Укажите название виртуальной категории.")
            return redirect(self._build_redirect_url(request))

        raw_virtual_code = (request.POST.get("virtual_code") or "").strip()
        if raw_virtual_code and VirtualCategory.objects.filter(code=raw_virtual_code).exists():
            messages.error(request, f"Виртуальная категория с кодом «{raw_virtual_code}» уже существует.")
            return redirect(self._build_redirect_url(request))

        nomenclature_ids = []
        for raw_id in request.POST.getlist("nomenclature_ids"):
            parsed = _parse_positive_int(raw_id)
            if parsed is not None:
                nomenclature_ids.append(parsed)
        nomenclature_ids = sorted(set(nomenclature_ids))
        if not nomenclature_ids:
            messages.error(request, "Выберите хотя бы одну номенклатуру для виртуальной категории.")
            return redirect(self._build_redirect_url(request))

        virtual_code = raw_virtual_code or self._generate_unique_virtual_code(virtual_name)
        virtual_category = VirtualCategory.objects.create(
            code=virtual_code,
            name=virtual_name,
            is_active=True,
        )
        links = [
            VirtualCategoryNomenclatureLink(
                virtual_category=virtual_category,
                nomenclature_id=nomenclature_id,
            )
            for nomenclature_id in nomenclature_ids
        ]
        VirtualCategoryNomenclatureLink.objects.bulk_create(links, batch_size=1000, ignore_conflicts=True)

        selected_focus_id: int | None = None
        if _to_bool_flag(request.POST.get("create_target_category")):
            focus_name = (request.POST.get("focus_name") or "").strip() or virtual_name
            raw_focus_code = (request.POST.get("focus_code") or "").strip()
            if raw_focus_code and FocusCategory.objects.filter(code=raw_focus_code).exists():
                messages.warning(
                    request,
                    f"Код целевой категории «{raw_focus_code}» занят, будет сгенерирован новый.",
                )
                raw_focus_code = ""
            focus_code = raw_focus_code or self._generate_unique_focus_code(
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
            selected_focus_id = int(focus.id)
            rebuild_stats = rebuild_focus_category_nomenclature_resolved(focus_codes=[focus.code])
            messages.success(
                request,
                (
                    f"Созданы виртуальная и целевая категории «{focus.name}». "
                    f"Номенклатур в виртуальной: {len(nomenclature_ids)}. "
                    f"Связей записано: {rebuild_stats.written_links}. "
                    "Для обновления витрин используйте кнопку «Пересчитать итоги за хвост 180 дней»."
                ),
            )
            return redirect(self._build_redirect_url(request, selected_focus_id=selected_focus_id))

        messages.success(
            request,
            (
                f"Создана виртуальная категория «{virtual_category.name}». "
                f"Номенклатур добавлено: {len(nomenclature_ids)}."
            ),
        )
        return redirect(self._build_redirect_url(request))

    def _set_focus_enabled(self, request):
        """
        Включает или выключает целевую категорию.
        """
        focus_id = _parse_positive_int(request.POST.get("focus_id"))
        if focus_id is None:
            messages.error(request, "Не указана целевая категория для изменения статуса.")
            return redirect(self._build_redirect_url(request))

        focus = FocusCategory.objects.filter(id=focus_id).first()
        if focus is None:
            messages.error(request, "Целевая категория не найдена.")
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
                (
                    f"Целевая категория «{focus.name}» включена. "
                    f"Связей записано: {rebuild_stats.written_links}. "
                    "Для обновления витрин используйте кнопку «Пересчитать итоги за хвост 180 дней»."
                ),
            )
        else:
            messages.success(
                request,
                (
                    f"Целевая категория «{focus.name}» отключена. "
                    f"Удалено связей: {rebuild_stats.deleted_links}. "
                    "Для обновления витрин используйте кнопку «Пересчитать итоги за хвост 180 дней»."
                ),
            )
        return redirect(self._build_redirect_url(request, selected_focus_id=int(focus.id)))

    def _rebuild_focus_resolved(self, request):
        """
        Запускает пересборку предрассчитанных связей category -> nomenclature.
        """
        focus_id = _parse_positive_int(request.POST.get("focus_id"))
        focus_codes = None
        if focus_id is not None:
            focus = FocusCategory.objects.filter(id=focus_id).only("code").first()
            if focus is None:
                messages.error(request, "Целевая категория не найдена.")
                return redirect(self._build_redirect_url(request))
            focus_codes = [focus.code]

        rebuild_stats = rebuild_focus_category_nomenclature_resolved(focus_codes=focus_codes)
        messages.success(
            request,
            (
                "Пересборка связей выполнена: "
                f"scanned={rebuild_stats.scanned_focus_categories}, "
                f"rebuilt={rebuild_stats.rebuilt_focus_categories}, "
                f"written={rebuild_stats.written_links}, "
                f"deleted={rebuild_stats.deleted_links}."
            ),
        )
        return redirect(self._build_redirect_url(request, selected_focus_id=focus_id))

    def _rebuild_aggregates_tail(self, request):
        """
        Безопасный ручной пересчёт витрин за «хвост» последних 180 дней.

        Важный принцип:
        1. не запускаем полный пересчёт всей истории из UI;
        2. считаем только контролируемый диапазон;
        3. при выборе заведения ограничиваем финальную витрину по department_id.
        """
        as_of_date = _parse_iso_date(request.POST.get("as_of_date")) or timezone.localdate()
        business_date_from = as_of_date - timedelta(days=MANUAL_REBUILD_MAX_DAYS - 1)
        department_id = (request.POST.get("department_id") or "").strip() or None

        try:
            order_stats = rebuild_order_fact_from_raw_lines(
                business_date_from=business_date_from,
                business_date_to=as_of_date,
                batch_size=MANUAL_REBUILD_BATCH_SIZE,
            )
            daily_stats = rebuild_daily_category_fact_from_raw_lines(
                business_date_from=business_date_from,
                business_date_to=as_of_date,
                batch_size=MANUAL_REBUILD_BATCH_SIZE,
            )
            window_stats = rebuild_window_metrics_from_daily_facts(
                as_of_date=as_of_date,
                window_days=MANUAL_REBUILD_WINDOWS,
                department_id=department_id,
                batch_size=MANUAL_REBUILD_BATCH_SIZE,
            )
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f"Пересчёт не выполнен: {exc}")
            return redirect(self._build_redirect_url(request))

        messages.success(
            request,
            (
                f"Пересчёт витрин выполнен за период {business_date_from}..{as_of_date}. "
                f"Чеки: scanned={order_stats.scanned_raw_lines}, grouped={order_stats.grouped_orders}. "
                f"Дневные итоги: grouped={daily_stats.grouped_rows}, without_mapping={daily_stats.lines_without_focus_mapping}. "
                f"Окна: grouped={window_stats.grouped_rows}."
            ),
        )
        return redirect(self._build_redirect_url(request))

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
            "nomenclature_query": (request.POST.get("nomenclature_query") or "").strip(),
            "nomenclature_group_query": (request.POST.get("nomenclature_group_query") or "").strip(),
            "nomenclature_olap_category_id": str(_parse_positive_int(request.POST.get("nomenclature_olap_category_id")) or ""),
        }
        params = {key: value for key, value in params.items() if value}
        base_url = reverse("focus_categories")
        if not params:
            return base_url
        return f"{base_url}?{urlencode(params)}"

    @staticmethod
    def _generate_unique_focus_code(*, base_name: str, fallback_prefix: str) -> str:
        """
        Генерирует уникальный `code` целевой категории.
        """
        slug = (slugify(base_name) or "").strip("-")
        base = slug or fallback_prefix
        candidate = base
        suffix = 2
        while FocusCategory.objects.filter(code=candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _generate_unique_virtual_code(base_name: str) -> str:
        """
        Генерирует уникальный `code` виртуальной категории.
        """
        slug = (slugify(base_name) or "").strip("-")
        base = slug or "virtual-category"
        candidate = base
        suffix = 2
        while VirtualCategory.objects.filter(code=candidate).exists():
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


def _parse_iso_date(raw_value: str | None) -> date | None:
    """
    Парсит дату формата YYYY-MM-DD.
    """
    if not raw_value:
        return None
    try:
        return date.fromisoformat(str(raw_value).strip())
    except ValueError:
        return None


def _to_bool_flag(raw_value: str | None) -> bool:
    """
    Нормализует флаг из формы.
    """
    return (raw_value or "").strip().lower() in {"1", "true", "yes", "on"}
