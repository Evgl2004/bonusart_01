"""
Действия рабочего экрана гостей (workbench).

На текущем этапе поддерживается сценарий:
1. создание черновика рассылки по текущему отбору гостей.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
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
    AUDIENCE_CHANNEL_GROUP_LEGACY_NO_NEW_BOT,
    build_guest_workbench_payload,
    normalize_audience_channel_group,
    normalize_segment_code,
    normalize_window_days,
)
from guests.services.guest_venue_selection import normalize_venue_selection_mode
from guests.services.mailing_delivery_targets import build_mailing_delivery_plan, normalize_mailing_target_mode
from guests.services.template_render import render_message_for_guest


DEFAULT_MAILING_SEND_WINDOW_BEGIN = time(11, 0)
DEFAULT_MAILING_SEND_WINDOW_END = time(23, 0)


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
            venue_selection_mode=filters["venue_selection_mode"],
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

        mailing_settings = self._extract_mailing_settings(request)
        template = _get_active_message_template(mailing_settings["template_id"])
        if template is None:
            messages.error(
                request,
                "Не найден активный шаблон сообщения. Выберите другой шаблон или включите его в настройках.",
            )
            return redirect(self._build_workbench_redirect_url(request))

        selected_bot_profiles = _get_selected_bot_profiles(mailing_settings["bot_profile_ids"])
        if not selected_bot_profiles:
            messages.error(request, "Выберите хотя бы один активный бот для рассылки.")
            return redirect(self._build_workbench_redirect_url(request))

        if mailing_settings["send_window_begin"] >= mailing_settings["send_window_end"]:
            messages.error(request, "Начало окна отправки должно быть раньше конца окна отправки.")
            return redirect(self._build_workbench_redirect_url(request))

        selected_guest_ids = [int(item["guest_id"]) for item in selected_rows]
        delivery_plan = build_mailing_delivery_plan(
            selected_guest_ids,
            selected_bot_ids=[bot.id for bot in selected_bot_profiles],
            target_mode=mailing_settings["target_mode"],
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
                send_window_begin=mailing_settings["send_window_begin"],
                send_window_end=mailing_settings["send_window_end"],
                target_mode=mailing_settings["target_mode"],
                queue_priority=mailing_settings["queue_priority"],
            )
            mailing.bot_profiles.set(selected_bot_profiles)

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
            mailing_settings=mailing_settings,
            template=template,
            bot_profiles=selected_bot_profiles,
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
        venue_selection_mode = normalize_venue_selection_mode(request.POST.get("venue_selection_mode"))
        segment_code = normalize_segment_code((request.POST.get("segment_code") or "").strip())
        audience_channel_group = normalize_audience_channel_group(request.POST.get("audience_channel_group"))
        if audience_channel_group == AUDIENCE_CHANNEL_GROUP_LEGACY_NO_NEW_BOT:
            segment_code = ""

        focus_category_code = (request.POST.get("focus_category_code") or "").strip()
        if audience_channel_group == AUDIENCE_CHANNEL_GROUP_LEGACY_NO_NEW_BOT:
            focus_category_code = ""
        if focus_category_code and not FocusCategory.objects.filter(
            code=focus_category_code, is_enabled=True
        ).exists():
            focus_category_code = ""

        preset, created = GuestWorkbenchFilterPreset.objects.update_or_create(
            name=preset_name,
            defaults={
                "window_days": window_days,
                "department_id": department_id,
                "venue_selection_mode": venue_selection_mode,
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
            "venue_selection_mode": normalize_venue_selection_mode(
                request.POST.get("venue_selection_mode")
            ),
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
    def _extract_mailing_settings(request) -> dict[str, object]:
        """
        Извлекает параметры создаваемого черновика рассылки.
        """
        return {
            "template_id": _parse_optional_int(request.POST.get("mailing_template_id")),
            "bot_profile_ids": _extract_selected_bot_ids(request),
            "target_mode": normalize_mailing_target_mode(request.POST.get("mailing_target_mode")),
            "queue_priority": _normalize_queue_priority(request.POST.get("mailing_queue_priority")),
            "send_window_begin": _parse_time_value(
                request.POST.get("mailing_send_window_begin"),
                default=DEFAULT_MAILING_SEND_WINDOW_BEGIN,
            ),
            "send_window_end": _parse_time_value(
                request.POST.get("mailing_send_window_end"),
                default=DEFAULT_MAILING_SEND_WINDOW_END,
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
        mailing_settings: dict[str, object],
        template: MessageTemplate,
        bot_profiles: list[BotProfile],
    ) -> None:
        """
        Сохраняет источник аудитории для созданной кампании.

        Постоянная копия нужна, чтобы оператор мог открыть кампанию позднее
        и восстановить, каким фильтром была собрана аудитория.
        """
        payload_filters = payload.get("filters") if isinstance(payload, dict) else {}
        if not isinstance(payload_filters, dict):
            payload_filters = {}

        as_of_date = filters.get("as_of_date")
        as_of_date_value = str(payload_filters.get("as_of_date") or "").strip()
        if not as_of_date_value:
            as_of_date_value = as_of_date.isoformat() if as_of_date else ""
        window_days_value = str(payload_filters.get("window_days") or filters.get("window_days") or "").strip()
        department_id_value = str(payload_filters.get("department_id") or filters.get("department_id") or "").strip()
        venue_selection_mode_value = normalize_venue_selection_mode(
            str(payload_filters.get("venue_selection_mode") or filters.get("venue_selection_mode") or "").strip()
        )
        segment_code_value = str(payload_filters.get("segment_code") or "").strip()
        focus_category_code_value = str(payload_filters.get("focus_category_code") or "").strip()
        audience_channel_group_value = normalize_audience_channel_group(
            str(payload_filters.get("audience_channel_group") or filters.get("audience_channel_group") or "").strip()
        )

        complex_filters_raw = payload_filters.get("complex_filters") or []
        complex_filters: list[dict[str, str]] = []
        for item in complex_filters_raw:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            operator = str(item.get("operator") or "").strip()
            value = str(item.get("value_str") or item.get("value") or "").strip()
            if not (field or operator or value):
                continue
            complex_filters.append(
                {
                    "field": field,
                    "operator": operator,
                    "value": value,
                }
            )

        source_layer = ""
        source_layer = str(payload_filters.get("metrics_layer") or "").strip()

        snapshot = {
            "as_of_date": as_of_date_value,
            "window_days": window_days_value,
            "department_id": department_id_value,
            "venue_selection_mode": venue_selection_mode_value,
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
            "mailing_template_id": int(template.id),
            "mailing_template_name": str(template.name),
            "mailing_target_mode": str(mailing_settings.get("target_mode") or ""),
            "mailing_queue_priority": str(mailing_settings.get("queue_priority") or ""),
            "mailing_send_window_begin": _format_time_value(mailing_settings.get("send_window_begin")),
            "mailing_send_window_end": _format_time_value(mailing_settings.get("send_window_end")),
            "mailing_bot_profile_ids": [int(bot.id) for bot in bot_profiles],
            "mailing_bot_profiles": [
                f"{bot.get_provider_type_display()} ({bot.code})" for bot in bot_profiles
            ],
            "source_layer": source_layer,
            "saved_at": timezone.now().isoformat(),
        }

        all_snapshots = request.session.get("mailings_v2_workbench_snapshots", {})
        if not isinstance(all_snapshots, dict):
            all_snapshots = {}
        all_snapshots[str(mailing_id)] = snapshot
        request.session["mailings_v2_workbench_snapshots"] = all_snapshots
        request.session.modified = True
        Mailing.objects.filter(pk=mailing_id).update(source_filter_snapshot=snapshot)

    @staticmethod
    def _build_workbench_redirect_url(request) -> str:
        """
        Формирует URL возврата в workbench с сохранением фильтров.
        """
        params = {
            "as_of_date": (request.POST.get("as_of_date") or "").strip(),
            "window_days": (request.POST.get("window_days") or "").strip(),
            "department_id": (request.POST.get("department_id") or "").strip(),
            "venue_selection_mode": normalize_venue_selection_mode(
                request.POST.get("venue_selection_mode")
            ),
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


def _parse_optional_int(raw_value: str | None) -> int | None:
    """
    Безопасно читает необязательный идентификатор из формы.
    """
    try:
        value = int(str(raw_value or "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _parse_int_list(raw_values: list[str]) -> list[int]:
    """
    Безопасно читает список идентификаторов из повторяемых полей формы.
    """
    result: list[int] = []
    seen: set[int] = set()
    for raw_value in raw_values:
        value = _parse_optional_int(raw_value)
        if value is None or value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _parse_time_value(raw_value: str | None, *, default: time) -> time:
    """
    Безопасно читает время в формате HH:MM.
    """
    raw = str(raw_value or "").strip()
    if not raw:
        return default
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        return default


def _format_time_value(value: object) -> str:
    """
    Форматирует время для снимка источника аудитории.
    """
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return ""


def _normalize_queue_priority(raw_value: str | None) -> str:
    """
    Нормализует приоритет очереди обычной рассылки.
    """
    normalized = str(raw_value or "").strip().lower()
    allowed_values = {choice[0] for choice in Mailing.QueuePriority.choices}
    if normalized in allowed_values:
        return normalized
    return Mailing.QueuePriority.BULK


def _extract_selected_bot_ids(request) -> list[int]:
    """
    Возвращает выбранные боты. Старые POST-запросы без поля получают все активные боты.
    """
    marker_present = _to_bool_flag(request.POST.get("mailing_bot_profile_ids_present"))
    if marker_present:
        return _parse_int_list(request.POST.getlist("mailing_bot_profile_ids"))
    return list(
        BotProfile.objects.filter(is_active=True)
        .order_by("provider_type", "name", "id")
        .values_list("id", flat=True)
    )


def _get_active_message_template(template_id: int | None) -> MessageTemplate | None:
    """
    Возвращает выбранный активный шаблон или прежний шаблон по умолчанию.
    """
    queryset = MessageTemplate.objects.filter(is_active=True)
    if template_id is not None:
        try:
            return queryset.get(pk=template_id)
        except MessageTemplate.DoesNotExist:
            return None
    return queryset.order_by("-created_at", "name", "id").first()


def _get_selected_bot_profiles(bot_profile_ids: list[int]) -> list[BotProfile]:
    """
    Возвращает активные боты в порядке выбора из формы.
    """
    if not bot_profile_ids:
        return []
    bots_by_id = {
        int(bot.id): bot
        for bot in BotProfile.objects.filter(id__in=bot_profile_ids, is_active=True)
    }
    return [bots_by_id[bot_id] for bot_id in bot_profile_ids if bot_id in bots_by_id]


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
    venue_selection_mode = normalize_venue_selection_mode(filters.get("venue_selection_mode"))

    return (
        "Черновик из workbench: "
        f"as_of={as_of_date}; window={window_days}; venue={venue_selection_mode}; segment={segment_code}; "
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
