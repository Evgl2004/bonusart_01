import re
from io import BytesIO

from django.views import View
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.http import HttpResponse
from openpyxl import load_workbook, Workbook

from .forms import MailingImportPhonesForm
from .models import GuestBotBinding, Guest, Mailing, MailingGuest
from guests.services.template_render import render_message_for_guest


def normalize_phone(raw: str) -> str | None:
    """
    Приводим телефон к единому виду для поиска.
    Простейшая нормализация: берём цифры, оставляем последние 10.
    """
    if not raw:
        return None
    digits = re.sub(r"\D+", "", str(raw))
    if not digits:
        return None

    # часто в РФ номера 11 цифр (7/8 + 10 цифр)
    if len(digits) >= 10:
        digits10 = digits[-10:]
        return digits10  # храним/сравниваем по последним 10 цифрам

    return None


def read_phones_from_xlsx(file_obj) -> list[str]:
    wb = load_workbook(filename=file_obj, read_only=True, data_only=True)
    ws = wb.active

    phones: list[str] = []
    # читаем все строки/ячейки, 1 столбец достаточно
    for row in ws.iter_rows(min_row=1, values_only=True):
        if not row:
            continue
        val = row[0]
        p = normalize_phone(val)
        if p:
            phones.append(p)

    return phones


def resolve_next_url(request, fallback_url: str) -> str:
    """
    Безопасно вернуть URL для redirect после импорта.

    Позволяет new UI передавать `next`, не ломая legacy поведение.
    """
    next_url = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return fallback_url


class MailingImportPhonesView(View):
    """
    POST: загрузить Excel, сопоставить телефоны с Guest, добавить в MailingGuest.
    """
    def post(self, request, pk: int):
        mailing = get_object_or_404(Mailing, pk=pk)
        fallback_url = reverse("mailing_edit", args=[mailing.id])
        redirect_url = resolve_next_url(request, fallback_url)
        form = MailingImportPhonesForm(request.POST, request.FILES)

        if not form.is_valid():
            # возвращаем на форму редактирования (или куда тебе удобнее)
            request.session["mailing_import_error"] = str(form.errors)
            return redirect(redirect_url)

        phones_raw = read_phones_from_xlsx(form.cleaned_data["file"])

        total_loaded = len(phones_raw)
        phones_unique = sorted(set(phones_raw))

        # 1) находим гостей по телефону
        # ВАЖНО: в БД у тебя phone может быть с +7, пробелами и т.п.
        # Поэтому делаем "поиск по цифрам": берём гостей, у кого phone содержит последние 10.
        # Это не супер быстро на миллионах, но для начала ок.
        guests_found = []
        not_found = []

        # Быстрее: одним запросом не получится идеально из-за normalize,
        # поэтому делаем так: берём всех гостей по совпадению "окончания".
        # (Если у тебя Postgres — можно сделать regexp_replace на уровне SQL, но это усложнение.)
        candidates = Guest.objects.filter(phone__isnull=False).exclude(phone__exact="")

        phone_to_guest = {}
        for g in candidates.iterator(chunk_size=5000):
            gp = normalize_phone(g.phone)
            if gp:
                phone_to_guest.setdefault(gp, g)

        for p in phones_unique:
            g = phone_to_guest.get(p)
            if g:
                guests_found.append(g)
            else:
                not_found.append(p)

        found_count = len(guests_found)

        # 2) выясняем, кто уже есть в рассылке (у тебя уникальность mailing+guest)
        found_ids = [g.id for g in guests_found]
        already_ids = set(
            MailingGuest.objects.filter(mailing=mailing, guest_id__in=found_ids)
            .values_list("guest_id", flat=True)
        )

        to_add = [g for g in guests_found if g.id not in already_ids]

        # 2.5) Оставляем гостей только с активными привязками к ботам,
        # выбранным в этой рассылке.
        selected_bot_ids = list(
            mailing.bot_profiles.filter(is_active=True).values_list("id", flat=True)
        )
        if not selected_bot_ids:
            to_add = []
        elif to_add:
            to_add_ids = [g.id for g in to_add]
            eligible_bindings = (
                GuestBotBinding.objects
                .filter(
                    guest_id__in=to_add_ids,
                    bot_id__in=selected_bot_ids,
                    is_active=True,
                    is_opt_in=True,
                    is_stop_sending=False,
                )
                .exclude(external_chat_id__isnull=True)
                .exclude(external_chat_id="")
            )

            if mailing.target_mode == Mailing.TargetMode.PRIMARY_ONLY:
                eligible_ids = set(
                    eligible_bindings.filter(is_primary=True).values_list("guest_id", flat=True)
                )
            else:
                eligible_ids = set(eligible_bindings.values_list("guest_id", flat=True).distinct())

            to_add = [g for g in to_add if g.id in eligible_ids]

        # 3) создаём строки MailingGuest
        now = timezone.now()
        template_text = mailing.template.message_text
        scheduled_dt = mailing.scheduled_time_begin  # логика: отправлять можно не раньше начала окна

        rows = []
        for g in to_add:
            rendered_text = render_message_for_guest(template_text, g)
            rows.append(MailingGuest(
                mailing=mailing,
                guest=g,
                phone=g.phone,
                email=g.email,
                text_mailing_list=rendered_text,
                scheduled_datetime=scheduled_dt,
                status=MailingGuest.Status.PLANNED,
                created_at=now,
            ))

        MailingGuest.objects.bulk_create(rows)

        added_count = len(rows)
        already_count = len(already_ids)
        not_found_count = len(not_found)

        # 4) сохраняем отчёт (проще всего в session, чтобы показать на странице)
        request.session["mailing_import_report"] = {
            "total_loaded": total_loaded,
            "unique_phones": len(phones_unique),
            "found": found_count,
            "added": added_count,
            "already": already_count,
            "not_found": not_found_count,
        }

        return redirect(redirect_url)


class MailingImportTemplateDownloadView(View):
    """
    Скачать шаблон Excel с одним столбцом phone.
    """
    def get(self, request):
        wb = Workbook()
        ws = wb.active
        ws.title = "phones"
        ws["A1"] = "phone"
        ws["A2"] = "+79991234567"

        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)

        resp = HttpResponse(
            bio.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = 'attachment; filename="mailing_phones_template.xlsx"'
        return resp
