"""
Действия рабочего экрана гостей (workbench).

На текущем этапе поддерживается сценарий:
1. создание черновика рассылки по текущему отбору гостей.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View

from guests.models import (
    BotProfile,
    FocusCategory,
    Guest,
    GuestWorkbenchFilterPreset,
    Mailing,
    MailingGuest,
    MessageTemplate,
)
from guests.services.guest_workbench import (
    build_guest_workbench_payload,
    normalize_audience_channel_group,
    normalize_segment_code,
    normalize_window_days,
)
from guests.services.mailing_delivery_targets import build_mailing_delivery_plan
from guests.services.template_render import render_message_for_guest


class GuestsWorkbenchActionsView(View):
    """
    Обрабатывает POST-действия с экрана `guests/workbench`.
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        action = (request.POST.get("action") or "").strip()
        if action == "rename_filter_preset":
            return self._rename_filter_preset(request)
        if action == "delete_filter_preset":
            return self._delete_filter_preset(request)
        if action == "restore_filter_preset":
            return self._restore_filter_preset(request)
        if action == "save_filter_preset":
            return self._save_filter_preset(request)
        if action == "create_mailing_draft":
            return self._create_mailing_draft(request)

        messages.error(request, "Неизвестное действие рабочего экрана.")
        return redirect(self._build_workbench_redirect_url(request))

    def _create_mailing_draft(self, request):
        """
        Создаёт черновик рассылки по текущему отбору гостей.
        """
        filters = self._extract_filters(request)
        payload = build_guest_workbench_payload(
            as_of_date=filters["as_of_date"],
            window_days=filters["window_days"],
            department_id=filters["department_id"],
            segment_code=filters["segment_code"],
            focus_category_code=filters["focus_category_code"],
            audience_channel_group=filters["audience_channel_group"],
            complex_filters=filters["complex_filters"],
            show_all_presets=bool(filters["show_all_presets"]),
            selected_guests_limit=filters["audience_limit"] if filters["audience_limit_enabled"] else None,
        )

        selected_guests = payload.get("selected_guests", {})
        selected_rows = selected_guests.get("rows", [])
        total_selected = int(selected_guests.get("total") or 0)
        is_truncated = bool(selected_guests.get("is_truncated"))
        selected_limit = int(selected_guests.get("limit") or 0)

        if total_selected <= 0 or not selected_rows:
            messages.warning(request, "Для выбранных фильтров не найдено гостей.")
            return redirect(self._build_workbench_redirect_url(request))

        template = MessageTemplate.objects.filter(is_active=True).order_by("-created_at").first()
        if template is None:
            messages.error(
                request,
                "Нет активного шаблона сообщения. Создайте/включите шаблон и повторите.",
            )
            return redirect(self._build_workbench_redirect_url(request))

        selected_guest_ids = [int(item["guest_id"]) for item in selected_rows]
        active_bot_profiles = list(BotProfile.objects.filter(is_active=True).order_by("provider_type", "name", "id"))
        delivery_plan = build_mailing_delivery_plan(
            selected_guest_ids,
            selected_bot_ids=[bot.id for bot in active_bot_profiles],
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
        )
        deliverable_guest_ids = set(delivery_plan.deliverable_guest_ids)
        selected_guest_ids = [guest_id for guest_id in selected_guest_ids if guest_id in deliverable_guest_ids]
        if not selected_guest_ids:
            messages.warning(
                request,
                (
                    "Гости по фильтрам найдены, но среди них нет гостей с доступной доставкой "
                    "через активные боты."
                ),
            )
            return redirect(self._build_workbench_redirect_url(request))

        guests_map = {
            int(guest.id): guest
            for guest in Guest.objects.filter(id__in=selected_guest_ids).only(
                "id", "phone", "email", "first_name", "last_name"
            )
        }
        guests = [guests_map.get(guest_id) for guest_id in selected_guest_ids]
        guests = [guest for guest in guests if guest is not None]

        if not guests:
            messages.warning(request, "Не удалось загрузить данные гостей для создания черновика.")
            return redirect(self._build_workbench_redirect_url(request))

        now = timezone.now()
        scheduled_begin = now + timedelta(minutes=5)
        scheduled_end = scheduled_begin + timedelta(days=1)
        mailing_name = _build_mailing_name(payload)

        with transaction.atomic():
            mailing = Mailing.objects.create(
                name=mailing_name,
                template=template,
                scheduled_date=scheduled_begin.date(),
                scheduled_time_begin=scheduled_begin,
                scheduled_time_end=scheduled_end,
                is_active=False,
                created_at=now,
                updated_at=now,
                send_window_begin=datetime.strptime("11:00", "%H:%M").time(),
                send_window_end=datetime.strptime("23:00", "%H:%M").time(),
                target_mode=Mailing.TargetMode.PRIMARY_ONLY,
                queue_priority=Mailing.QueuePriority.BULK,
            )
            mailing.bot_profiles.set(active_bot_profiles)

            rows = []
            for guest in guests:
                rows.append(
                    MailingGuest(
                        mailing=mailing,
                        guest=guest,
                        phone=guest.phone,
                        email=guest.email,
                        text_mailing_list=render_message_for_guest(template.message_text, guest),
                        scheduled_datetime=scheduled_begin,
                        status=MailingGuest.Status.PLANNED,
                        created_at=now,
                    )
                )

            MailingGuest.objects.bulk_create(rows, ignore_conflicts=True, batch_size=1000)

        self._store_workbench_snapshot_for_mailing(
            request=request,
            mailing_id=mailing.id,
            filters=filters,
            payload=payload,
            selected_total=total_selected,
            selected_rows_count=len(selected_rows),
            delivery_plan=delivery_plan,
        )

        messages.success(
            request,
            _build_mailing_created_message(
                mailing_id=mailing.id,
                guests_count=len(guests),
                total_selected=total_selected,
                is_truncated=is_truncated,
                selected_limit=selected_limit,
                delivery_plan=delivery_plan,
            ),
        )
        return redirect(reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.id}))

    def _save_filter_preset(self, request):
        """
        Сохраняет или обновляет пресет текущих фильтров workbench.
        """
        preset_name = (request.POST.get("preset_name") or "").strip()
        if not preset_name:
            messages.error(request, "Укажите имя пресета перед сохранением.")
            return redirect(self._build_workbench_redirect_url(request))

        window_days = normalize_window_days((request.POST.get("window_days") or "").strip())
        department_id = (request.POST.get("department_id") or "").strip()
        segment_code = normalize_segment_code((request.POST.get("segment_code") or "").strip())
        audience_channel_group = normalize_audience_channel_group(request.POST.get("audience_channel_group"))

        focus_category_code = (request.POST.get("focus_category_code") or "").strip()
        if focus_category_code and not FocusCategory.objects.filter(
            code=focus_category_code, is_enabled=True
        ).exists():
            focus_category_code = ""

        preset, created = GuestWorkbenchFilterPreset.objects.update_or_create(
            name=preset_name,
            defaults={
                "window_days": window_days,
                "department_id": department_id,
                "segment_code": segment_code,
                "focus_category_code": focus_category_code,
                "audience_channel_group": audience_channel_group,
                "is_active": True,
            },
        )

        if created:
            messages.success(request, f"Пресет «{preset.name}» сохранён.")
        else:
            messages.success(request, f"Пресет «{preset.name}» обновлён.")
        return redirect(self._build_workbench_redirect_url(request))

    def _rename_filter_preset(self, request):
        """
        Переименовывает пресет фильтра по его ID.
        """
        preset_id = request.POST.get("preset_id")
        new_name = (request.POST.get("new_name") or "").strip()
        if not preset_id:
            messages.error(request, "Не указан ID пресета для переименования.")
            return redirect(self._build_workbench_redirect_url(request))
        if not new_name:
            messages.error(request, "Укажите новое имя пресета.")
            return redirect(self._build_workbench_redirect_url(request))

        try:
            preset = GuestWorkbenchFilterPreset.objects.get(pk=int(preset_id), is_active=True)
        except (ValueError, GuestWorkbenchFilterPreset.DoesNotExist):
            messages.error(request, "Пресет не найден.")
            return redirect(self._build_workbench_redirect_url(request))

        duplicate_exists = (
            GuestWorkbenchFilterPreset.objects.filter(name=new_name, is_active=True)
            .exclude(pk=preset.pk)
            .exists()
        )
        if duplicate_exists:
            messages.error(request, f"Пресет с именем «{new_name}» уже существует.")
            return redirect(self._build_workbench_redirect_url(request))

        preset.name = new_name
        preset.save(update_fields=["name", "updated_at"])
        messages.success(request, f"Пресет переименован: «{new_name}».")
        return redirect(self._build_workbench_redirect_url(request))

    def _delete_filter_preset(self, request):
        """
        Мягко удаляет пресет фильтра (деактивация).
        """
        preset_id = request.POST.get("preset_id")
        if not preset_id:
            messages.error(request, "Не указан ID пресета для удаления.")
            return redirect(self._build_workbench_redirect_url(request))

        try:
            preset = GuestWorkbenchFilterPreset.objects.get(pk=int(preset_id), is_active=True)
        except (ValueError, GuestWorkbenchFilterPreset.DoesNotExist):
            messages.error(request, "Пресет не найден.")
            return redirect(self._build_workbench_redirect_url(request))

        preset.is_active = False
        preset.save(update_fields=["is_active", "updated_at"])
        messages.success(request, f"Пресет «{preset.name}» удалён.")
        return redirect(self._build_workbench_redirect_url(request))

    def _restore_filter_preset(self, request):
        """
        Восстанавливает ранее деактивированный пресет фильтра.
        """
        preset_id = request.POST.get("preset_id")
        if not preset_id:
            messages.error(request, "Не указан ID пресета для восстановления.")
            return redirect(self._build_workbench_redirect_url(request))

        try:
            preset = GuestWorkbenchFilterPreset.objects.get(pk=int(preset_id), is_active=False)
        except (ValueError, GuestWorkbenchFilterPreset.DoesNotExist):
            messages.error(request, "Пресет не найден.")
            return redirect(self._build_workbench_redirect_url(request))

        preset.is_active = True
        preset.save(update_fields=["is_active", "updated_at"])
        messages.success(request, f"Пресет «{preset.name}» восстановлен.")
        return redirect(self._build_workbench_redirect_url(request))

    @staticmethod
    def _extract_filters(request) -> dict[str, object]:
        """
        Извлекает и нормализует фильтры workbench из POST-формы.
        """
        raw_as_of_date = (request.POST.get("as_of_date") or "").strip()
        return {
            "as_of_date": _parse_iso_date(raw_as_of_date),
            "window_days": (request.POST.get("window_days") or "").strip(),
            "department_id": (request.POST.get("department_id") or "").strip(),
            "segment_code": (request.POST.get("segment_code") or "").strip(),
            "focus_category_code": (request.POST.get("focus_category_code") or "").strip(),
            "audience_channel_group": normalize_audience_channel_group(
                request.POST.get("audience_channel_group")
            ),
            "complex_filters": _extract_complex_filters_from_post(request),
            "show_all_presets": _to_bool_flag(request.POST.get("show_all_presets")),
            "audience_limit_enabled": _to_bool_flag_with_default(
                request.POST.get("audience_limit_enabled"),
                default=True,
            ),
            "audience_limit": _parse_positive_int(
                request.POST.get("audience_limit"),
                default=200,
            ),
        }

    @staticmethod
    def _store_workbench_snapshot_for_mailing(
        request,
        mailing_id: int,
        filters: dict[str, object],
        payload: dict,
        selected_total: int,
        selected_rows_count: int,
        delivery_plan,
    ) -> None:
        """
        Сохраняет в сессии источник аудитории для созданной кампании.
        """
        as_of_date = filters.get("as_of_date")
        as_of_date_value = as_of_date.isoformat() if as_of_date else ""
        window_days_value = str(filters.get("window_days") or "").strip()
        department_id_value = str(filters.get("department_id") or "").strip()
        segment_code_value = str(filters.get("segment_code") or "").strip()
        focus_category_code_value = str(filters.get("focus_category_code") or "").strip()
        audience_channel_group_value = normalize_audience_channel_group(
            str(filters.get("audience_channel_group") or "").strip()
        )

        complex_filters_raw = filters.get("complex_filters") or []
        complex_filters: list[dict[str, str]] = []
        for item in complex_filters_raw:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            operator = str(item.get("operator") or "").strip()
            value = str(item.get("value") or "").strip()
            if not (field or operator or value):
                continue
            complex_filters.append(
                {
                    "field": field,
                    "operator": operator,
                    "value": value,
                }
            )

        payload_filters = payload.get("filters") if isinstance(payload, dict) else {}
        source_layer = ""
        if isinstance(payload_filters, dict):
            source_layer = str(payload_filters.get("metrics_layer") or "").strip()

        snapshot = {
            "as_of_date": as_of_date_value,
            "window_days": window_days_value,
            "department_id": department_id_value,
            "segment_code": segment_code_value,
            "focus_category_code": focus_category_code_value,
            "audience_channel_group": audience_channel_group_value,
            "complex_filters": complex_filters,
            "audience_limit_enabled": bool(filters.get("audience_limit_enabled")),
            "audience_limit": int(filters.get("audience_limit") or 0),
            "selected_total": int(selected_total or 0),
            "selected_rows_count": int(selected_rows_count or 0),
            "delivery_total_guests": int(getattr(delivery_plan, "total_guests", 0) or 0),
            "delivery_available_guests": int(getattr(delivery_plan, "deliverable_guests", 0) or 0),
            "delivery_blocked_without_bot_binding": int(
                getattr(delivery_plan, "blocked_without_bot_binding", 0) or 0
            ),
            "delivery_blocked_without_message_permission": int(
                getattr(delivery_plan, "blocked_without_message_permission", 0) or 0
            ),
            "delivery_legacy_telegram_guests": int(getattr(delivery_plan, "legacy_telegram_guests", 0) or 0),
            "delivery_planned_tasks": int(getattr(delivery_plan, "planned_dispatch_tasks", 0) or 0),
            "source_layer": source_layer,
            "saved_at": timezone.now().isoformat(),
        }

        all_snapshots = request.session.get("mailings_v2_workbench_snapshots", {})
        if not isinstance(all_snapshots, dict):
            all_snapshots = {}
        all_snapshots[str(mailing_id)] = snapshot
        request.session["mailings_v2_workbench_snapshots"] = all_snapshots
        request.session.modified = True

    @staticmethod
    def _build_workbench_redirect_url(request) -> str:
        """
        Формирует URL возврата в workbench с сохранением фильтров.
        """
        params = {
            "as_of_date": (request.POST.get("as_of_date") or "").strip(),
            "window_days": (request.POST.get("window_days") or "").strip(),
            "department_id": (request.POST.get("department_id") or "").strip(),
            "segment_code": (request.POST.get("segment_code") or "").strip(),
            "focus_category_code": (request.POST.get("focus_category_code") or "").strip(),
            "audience_channel_group": normalize_audience_channel_group(
                request.POST.get("audience_channel_group")
            ),
            "show_all_presets": "1" if _to_bool_flag(request.POST.get("show_all_presets")) else "",
        }
        params = {key: value for key, value in params.items() if value}
        complex_filters = _extract_complex_filters_from_post(request)
        if complex_filters:
            params["cf_field"] = [item.get("field") or "" for item in complex_filters]
            params["cf_op"] = [item.get("operator") or "" for item in complex_filters]
            params["cf_value"] = [item.get("value") or "" for item in complex_filters]
        base_url = reverse("guests_workbench")
        if not params:
            return base_url
        return f"{base_url}?{urlencode(params, doseq=True)}"


def _parse_iso_date(raw_value: str) -> date | None:
    """
    Безопасно парсит дату формата YYYY-MM-DD.
    """
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return None


def _to_bool_flag(raw_value: str | None) -> bool:
    """
    Нормализует флаг из POST-формы (checkbox/select).
    """
    return (raw_value or "").strip().lower() in {"1", "true", "yes", "on"}


def _to_bool_flag_with_default(raw_value: str | None, *, default: bool) -> bool:
    """
    Нормализует флаг, у которого есть поведение по умолчанию.
    """
    if raw_value is None:
        return bool(default)
    return _to_bool_flag(raw_value)


def _parse_positive_int(raw_value: str | None, *, default: int) -> int:
    """
    Безопасно читает положительное целое число из формы.
    """
    try:
        value = int(str(raw_value or "").strip())
    except (TypeError, ValueError):
        return int(default)
    return value if value > 0 else int(default)


def _extract_complex_filters_from_post(request) -> list[dict[str, str]]:
    """
    Извлекает сложные условия фильтра из POST (повторяемые cf_* параметры).
    """
    fields = request.POST.getlist("cf_field")
    operators = request.POST.getlist("cf_op")
    values = request.POST.getlist("cf_value")
    length = max(len(fields), len(operators), len(values), 0)

    result: list[dict[str, str]] = []
    for idx in range(length):
        field = (fields[idx] if idx < len(fields) else "").strip()
        operator = (operators[idx] if idx < len(operators) else "").strip()
        value = (values[idx] if idx < len(values) else "").strip()
        if not field and not operator and not value:
            continue
        result.append({"field": field, "operator": operator, "value": value})
    return result


def _build_mailing_name(payload: dict) -> str:
    """
    Формирует понятное имя черновика рассылки по текущим фильтрам workbench.
    """
    filters = payload.get("filters", {})
    as_of_date = (filters.get("as_of_date") or "").strip() or "без даты"
    window_days = str(filters.get("window_days") or "")
    segment_code = (filters.get("segment_code") or "").strip() or "all-segments"
    focus_category_code = (filters.get("focus_category_code") or "").strip() or "all-focus"
    audience_channel_group = (filters.get("audience_channel_group") or "").strip() or "all-audience"

    return (
        "Черновик из workbench: "
        f"as_of={as_of_date}; window={window_days}; segment={segment_code}; "
        f"focus={focus_category_code}; audience={audience_channel_group}"
    )[:150]


def _build_mailing_created_message(
    *,
    mailing_id: int,
    guests_count: int,
    total_selected: int,
    is_truncated: bool,
    selected_limit: int,
    delivery_plan,
) -> str:
    """
    Формирует понятное сообщение после создания черновика рассылки.
    """
    base = f"Создан черновик рассылки (ID {mailing_id}) по {guests_count} гостям."
    if is_truncated:
        base = (
            f"{base} Всего по отбору найдено {total_selected}; "
            f"применён лимит {selected_limit}."
        )
    skipped = int(total_selected or 0) - int(guests_count or 0)
    if skipped > 0:
        base = f"{base} Пропущено без доступной доставки: {skipped}."
    planned_tasks = int(getattr(delivery_plan, "planned_dispatch_tasks", 0) or 0)
    if planned_tasks:
        base = f"{base} Задач доставки при запуске: {planned_tasks}."
    return base
