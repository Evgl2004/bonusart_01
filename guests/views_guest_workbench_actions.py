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
    FocusCategory,
    Guest,
    GuestWorkbenchFilterPreset,
    Mailing,
    MailingGuest,
    MessageTemplate,
)
from guests.services.guest_workbench import (
    build_guest_workbench_payload,
    normalize_segment_code,
    normalize_window_days,
)
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
        )

        selected_guests = payload.get("selected_guests", {})
        selected_rows = selected_guests.get("rows", [])
        total_selected = int(selected_guests.get("total") or 0)
        is_truncated = bool(selected_guests.get("is_truncated"))
        selected_limit = int(selected_guests.get("limit") or 0)

        if total_selected <= 0 or not selected_rows:
            messages.warning(request, "Для выбранных фильтров не найдено гостей.")
            return redirect(self._build_workbench_redirect_url(request))

        if is_truncated:
            messages.error(
                request,
                (
                    "Выборка слишком большая для быстрого действия "
                    f"(найдено {total_selected}, показывается {selected_limit}). "
                    "Сузьте фильтры и повторите."
                ),
            )
            return redirect(self._build_workbench_redirect_url(request))

        template = MessageTemplate.objects.filter(is_active=True).order_by("-created_at").first()
        if template is None:
            messages.error(
                request,
                "Нет активного шаблона сообщения. Создайте/включите шаблон и повторите.",
            )
            return redirect(self._build_workbench_redirect_url(request))

        selected_guest_ids = [int(item["guest_id"]) for item in selected_rows]
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

        messages.success(
            request,
            f"Создан черновик рассылки (ID {mailing.id}) по {len(guests)} гостям.",
        )
        return redirect(reverse("mailing_edit", kwargs={"pk": mailing.id}))

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
        }

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
        }
        params = {key: value for key, value in params.items() if value}
        base_url = reverse("guests_workbench")
        if not params:
            return base_url
        return f"{base_url}?{urlencode(params)}"


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


def _build_mailing_name(payload: dict) -> str:
    """
    Формирует понятное имя черновика рассылки по текущим фильтрам workbench.
    """
    filters = payload.get("filters", {})
    as_of_date = (filters.get("as_of_date") or "").strip() or "без даты"
    window_days = str(filters.get("window_days") or "")
    segment_code = (filters.get("segment_code") or "").strip() or "all-segments"
    focus_category_code = (filters.get("focus_category_code") or "").strip() or "all-focus"

    return (
        "Черновик из workbench: "
        f"as_of={as_of_date}; window={window_days}; segment={segment_code}; focus={focus_category_code}"
    )[:150]
