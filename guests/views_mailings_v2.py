"""
Базовый каркас нового UI раздела рассылок (mailings-v2).

Задача этапа:
1. дать единую точку входа для маркетолога;
2. не ломать legacy формы, а использовать их как bridge;
3. показывать ключевые операционные метрики по текущему состоянию рассылок.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DetailView, TemplateView, UpdateView

from guests.forms import MailingForm, MessageTemplateForm
from guests.management.commands import mailing_worker as mailing_worker_cmd
from guests.models import (
    BotProfile,
    DispatchTask,
    Guest,
    GuestBotBinding,
    Mailing,
    MailingGuest,
    MessageTemplate,
    NotificationScenario,
)

MAILINGS_V2_RUN_NOW_MAX_BATCHES = 5


class MailingsV2CampaignsHubView(TemplateView):
    """
    Главный экран mailings-v2.

    Пока выступает как маршрутизатор и operational overview:
    1. сводка по кампаниям;
    2. быстрые переходы в текущие рабочие формы;
    3. стартовая точка для поэтапного перевода UX из legacy.
    """

    template_name = "mailing_v2/campaigns_hub.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        all_campaigns_qs = (
            Mailing.objects.select_related("template")
            .annotate(
                recipients_total=Count("guests_rows", distinct=True),
                recipients_done=Count(
                    "guests_rows",
                    filter=Q(guests_rows__status=MailingGuest.Status.DONE),
                    distinct=True,
                ),
                recipients_error=Count(
                    "guests_rows",
                    filter=Q(guests_rows__status=MailingGuest.Status.ERROR),
                    distinct=True,
                ),
            )
            .order_by("-updated_at", "-id")
        )

        q = (self.request.GET.get("q") or "").strip()
        only_active = bool(self.request.GET.get("only_active"))
        with_errors = bool(self.request.GET.get("with_errors"))
        show_archived = bool(self.request.GET.get("show_archived"))
        created_from_raw = (self.request.GET.get("created_from") or "").strip()
        created_to_raw = (self.request.GET.get("created_to") or "").strip()

        created_from = parse_date(created_from_raw) if created_from_raw else None
        created_to = parse_date(created_to_raw) if created_to_raw else None

        campaigns_qs = all_campaigns_qs
        if not show_archived:
            campaigns_qs = campaigns_qs.filter(is_archived=False)
        if only_active:
            campaigns_qs = campaigns_qs.filter(is_active=True)
        if with_errors:
            campaigns_qs = campaigns_qs.filter(recipients_error__gt=0)
        if created_from:
            campaigns_qs = campaigns_qs.filter(created_at__date__gte=created_from)
        if created_to:
            campaigns_qs = campaigns_qs.filter(created_at__date__lte=created_to)
        if q:
            search_q = Q(name__icontains=q) | Q(template__name__icontains=q)
            if q.isdigit():
                search_q = search_q | Q(id=int(q))
            campaigns_qs = campaigns_qs.filter(search_q)

        dispatch_scope = DispatchTask.objects.filter(
            Q(mailing_guest__isnull=False) | Q(notification_scenario__isnull=False)
        )
        dispatch_stats = dispatch_scope.aggregate(
            total=Count("id"),
            pending=Count("id", filter=Q(status=DispatchTask.Status.PENDING)),
            queued=Count("id", filter=Q(status=DispatchTask.Status.QUEUED)),
            in_progress=Count("id", filter=Q(status=DispatchTask.Status.IN_PROGRESS)),
            done=Count("id", filter=Q(status=DispatchTask.Status.DONE)),
            failed=Count("id", filter=Q(status=DispatchTask.Status.FAILED)),
        )

        recently_updated_threshold = timezone.now() - timedelta(days=7)
        kpi_scope = all_campaigns_qs.filter(is_archived=False)
        context["kpi"] = {
            "campaigns_total": kpi_scope.count(),
            "campaigns_active": kpi_scope.filter(is_active=True).count(),
            "campaigns_recently_updated": kpi_scope.filter(updated_at__gte=recently_updated_threshold).count(),
            "campaigns_archived": all_campaigns_qs.filter(is_archived=True).count(),
            "templates_active": MessageTemplate.objects.filter(is_active=True).count(),
            "scenarios_active": NotificationScenario.objects.filter(is_active=True).count(),
            "dispatch_total": int(dispatch_stats.get("total") or 0),
            "dispatch_pending": int(dispatch_stats.get("pending") or 0)
            + int(dispatch_stats.get("queued") or 0)
            + int(dispatch_stats.get("in_progress") or 0),
            "dispatch_done": int(dispatch_stats.get("done") or 0),
            "dispatch_failed": int(dispatch_stats.get("failed") or 0),
        }

        context["campaigns_total_filtered"] = campaigns_qs.count()
        context["campaigns"] = campaigns_qs[:100]
        context["filters"] = {
            "q": q,
            "only_active": only_active,
            "with_errors": with_errors,
            "show_archived": show_archived,
            "created_from": created_from_raw,
            "created_to": created_to_raw,
        }
        context["current_query_path"] = self.request.get_full_path()
        context["flow_sections"] = [
            {
                "title": "Кампании",
                "description": "Создание, запуск и контроль ручных рассылок.",
                "primary_label": "Открыть кампании",
                "primary_url": reverse("mailings_v2_campaigns"),
                "secondary_label": "Создать кампанию",
                "secondary_url": reverse("mailings_v2_campaigns_new"),
            },
            {
                "title": "Шаблоны",
                "description": "Управление текстами, переменными и превью сообщений.",
                "primary_label": "Открыть шаблоны",
                "primary_url": reverse("mailings_v2_templates"),
                "secondary_label": "Создать шаблон",
                "secondary_url": reverse("mailings_v2_templates_new"),
            },
            {
                "title": "Монитор",
                "description": "Статусы доставки, ошибки и ретраи по отправкам.",
                "primary_label": "Открыть монитор",
                "primary_url": reverse("mailings_v2_monitor"),
                "secondary_label": "Логи кампаний",
                "secondary_url": reverse("mailings"),
            },
            {
                "title": "Автосценарии",
                "description": "Сценарные отправки и регулярные проверки условий.",
                "primary_label": "Открыть сценарии",
                "primary_url": reverse("mailings_v2_scenarios"),
                "secondary_label": "Открыть admin",
                "secondary_url": "/admin/guests/notificationscenario/",
            },
        ]
        return context


class _MailingsV2CampaignFormMixin:
    """
    Общая логика формы кампании в новом UI.

    Используем текущую backend-модель и форму без смены контракта.
    """

    model = Mailing
    form_class = MailingForm
    template_name = "mailing_v2/campaign_form.html"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # В v2 по умолчанию показываем только активные шаблоны.
        if "template" in form.fields:
            form.fields["template"].queryset = MessageTemplate.objects.filter(is_active=True).order_by("-created_at")
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = getattr(self, "object", None)
        context["is_create"] = not bool(mailing and mailing.pk)
        context["legacy_list_url"] = reverse("mailings")
        context["v2_list_url"] = reverse("mailings_v2_campaigns")

        if mailing and mailing.pk:
            context["guests_count"] = mailing.guests_rows.count()
            context["legacy_edit_url"] = reverse("mailing_edit", kwargs={"pk": mailing.pk})
            context["audience_url"] = reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.pk})
            context["runs_url"] = reverse("mailings_v2_campaigns_runs", kwargs={"pk": mailing.pk})
            context["ops_url"] = reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.pk})
            context["mailing_import_report"] = self.request.session.pop("mailing_import_report", None)
            context["mailing_import_error"] = self.request.session.pop("mailing_import_error", None)
            context["mailing_ops_dry_run_report"] = self.request.session.pop("mailing_ops_dry_run_report", None)
            context["mailing_ops_run_now_report"] = self.request.session.pop("mailing_ops_run_now_report", None)
            snapshot = _get_workbench_snapshot(self.request, mailing.pk)
            context["workbench_snapshot"] = snapshot
            context["workbench_snapshot_url"] = _build_workbench_url_from_snapshot(snapshot) if snapshot else ""
            context["mailing_row_stats"] = _build_mailing_row_stats(mailing)
            context["dispatch_stats"] = _build_mailing_dispatch_stats(mailing)
        else:
            context["guests_count"] = 0
            context["legacy_edit_url"] = ""
            context["audience_url"] = ""
            context["runs_url"] = ""
            context["ops_url"] = ""
            context["mailing_import_report"] = None
            context["mailing_import_error"] = None
            context["mailing_ops_dry_run_report"] = None
            context["mailing_ops_run_now_report"] = None
            context["workbench_snapshot"] = None
            context["workbench_snapshot_url"] = ""
            context["mailing_row_stats"] = _empty_mailing_row_stats()
            context["dispatch_stats"] = _empty_dispatch_stats()
        return context


class MailingsV2CampaignCreateView(_MailingsV2CampaignFormMixin, CreateView):
    """
    Создание кампании в новом UI.

    Логика сохранения соответствует текущей legacy-форме.
    """

    def form_valid(self, form):
        self.object = form.save(commit=False)
        self.object.is_active = False
        self.object.is_archived = False

        now = timezone.now()
        if hasattr(self.object, "created_at") and not self.object.created_at:
            self.object.created_at = now
        if hasattr(self.object, "updated_at"):
            self.object.updated_at = now

        self.object.save()
        form.save_m2m()
        messages.success(self.request, f"Кампания создана (ID {self.object.id}).")
        return redirect("mailings_v2_campaigns_edit", pk=self.object.pk)


class MailingsV2CampaignUpdateView(_MailingsV2CampaignFormMixin, UpdateView):
    """
    Редактирование кампании в новом UI.
    """

    def form_valid(self, form):
        self.object = form.save(commit=False)
        if hasattr(self.object, "updated_at"):
            self.object.updated_at = timezone.now()
        self.object.save()
        form.save_m2m()
        messages.success(self.request, "Изменения кампании сохранены.")
        return redirect("mailings_v2_campaigns_edit", pk=self.object.pk)


class MailingsV2CampaignOpsView(View):
    """
    Операционные POST-действия для кампании в mailings-v2.

    Поддерживает:
    1. безопасный старт/пауза кампании;
    2. возврат error/in_progress строк в planned;
    3. ручной retry задач dispatch со статусом failed.
    """

    http_method_names = ["post"]

    @staticmethod
    def _resolve_next_url(request, default_url: str) -> str:
        next_url = (request.POST.get("next") or "").strip()
        if next_url and url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return next_url
        return default_url

    def post(self, request, *args, **kwargs):
        mailing = get_object_or_404(Mailing, pk=kwargs["pk"])
        action = (request.POST.get("action") or "").strip()
        list_url = reverse("mailings_v2_campaigns")
        edit_url = reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.pk})

        if mailing.is_archived and action in {
            "toggle_active",
            "retry_failed_rows",
            "requeue_in_progress_rows",
            "retry_failed_dispatch",
            "dry_run_campaign",
            "run_now_campaign",
        }:
            messages.error(request, "Архивная кампания недоступна для операционных действий.")
            return redirect(self._resolve_next_url(request, edit_url))

        if action == "toggle_active":
            mailing.is_active = not bool(mailing.is_active)
            if hasattr(mailing, "updated_at"):
                mailing.updated_at = timezone.now()
            mailing.save(update_fields=["is_active"] + (["updated_at"] if hasattr(mailing, "updated_at") else []))
            if mailing.is_active:
                messages.success(request, f"Кампания #{mailing.id} запущена.")
            else:
                messages.success(request, f"Кампания #{mailing.id} поставлена на паузу.")
            return redirect(self._resolve_next_url(request, edit_url))

        if action == "retry_failed_rows":
            updated = MailingGuest.objects.filter(
                mailing=mailing,
                status=MailingGuest.Status.ERROR,
            ).update(
                status=MailingGuest.Status.PLANNED,
                delivery_status="retry_requested",
                error_description=None,
                scheduled_datetime=timezone.now(),
            )
            if updated > 0:
                messages.success(request, f"Возвращено в planned строк: {updated}.")
            else:
                messages.info(request, "Строк со статусом error не найдено.")
            return redirect(self._resolve_next_url(request, edit_url))

        if action == "requeue_in_progress_rows":
            updated = MailingGuest.objects.filter(
                mailing=mailing,
                status=MailingGuest.Status.IN_PROGRESS,
            ).update(
                status=MailingGuest.Status.PLANNED,
                delivery_status="requeued_from_ui",
            )
            if updated > 0:
                messages.success(request, f"Возвращено из in_progress в planned строк: {updated}.")
            else:
                messages.info(request, "Зависших строк in_progress не найдено.")
            return redirect(self._resolve_next_url(request, edit_url))

        if action == "retry_failed_dispatch":
            now = timezone.now()
            updated = DispatchTask.objects.filter(
                mailing_guest__mailing=mailing,
                status=DispatchTask.Status.FAILED,
            ).update(
                status=DispatchTask.Status.PENDING,
                enqueued_at=None,
                queue_name=None,
                started_at=None,
                finished_at=None,
                last_error=None,
                available_at=now,
                updated_at=now,
                attempt=0,
            )
            if updated > 0:
                messages.success(request, f"Dispatch-задач переведено в pending: {updated}.")
            else:
                messages.info(request, "Dispatch-задач со статусом failed не найдено.")
            return redirect(self._resolve_next_url(request, edit_url))

        if action == "dry_run_campaign":
            report = _build_mailing_dry_run_report(mailing=mailing, now=timezone.now())
            request.session["mailing_ops_dry_run_report"] = report
            request.session.modified = True
            messages.info(
                request,
                (
                    f"Dry-run: ready={report['ready_rows']} "
                    f"targetable={report['ready_rows_with_targets']} "
                    f"blocked={report['ready_rows_without_targets']}."
                ),
            )
            return redirect(self._resolve_next_url(request, edit_url))

        if action == "run_now_campaign":
            report = _run_mailing_now(
                mailing=mailing,
                now=timezone.now(),
                max_batches=MAILINGS_V2_RUN_NOW_MAX_BATCHES,
            )
            request.session["mailing_ops_run_now_report"] = report
            request.session.modified = True
            processed_rows = int(report.get("processed_rows_total") or 0)
            if processed_rows > 0:
                messages.success(
                    request,
                    (
                        f"Run-now: обработано строк {processed_rows}, "
                        f"батчей {report['processed_batches']}, "
                        f"лимит-достигнут={report['reached_batch_limit']}."
                    ),
                )
            else:
                messages.info(
                    request,
                    (
                        f"Run-now: строки не обработаны "
                        f"(schedule_open={report['schedule_window_open']}, "
                        f"send_open={report['send_window_open']}, "
                        f"ready={report['ready_rows_before']})."
                    ),
                )
            return redirect(self._resolve_next_url(request, edit_url))

        if action == "archive_campaign":
            if mailing.is_archived:
                messages.info(request, f"Кампания #{mailing.id} уже в архиве.")
            else:
                now = timezone.now()
                mailing.is_archived = True
                mailing.is_active = False
                if hasattr(mailing, "updated_at"):
                    mailing.updated_at = now
                    mailing.save(update_fields=["is_archived", "is_active", "updated_at"])
                else:
                    mailing.save(update_fields=["is_archived", "is_active"])
                messages.success(request, f"Кампания #{mailing.id} перенесена в архив.")
            return redirect(self._resolve_next_url(request, list_url))

        if action == "duplicate_campaign":
            now = timezone.now()
            with transaction.atomic():
                duplicate = Mailing.objects.create(
                    name=f"{mailing.name} (копия)",
                    template=mailing.template,
                    scheduled_date=mailing.scheduled_date,
                    scheduled_time_begin=mailing.scheduled_time_begin,
                    scheduled_time_end=mailing.scheduled_time_end,
                    is_active=False,
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                    send_window_begin=mailing.send_window_begin,
                    send_window_end=mailing.send_window_end,
                    target_mode=mailing.target_mode,
                    queue_priority=mailing.queue_priority,
                )
                duplicate.bot_profiles.set(mailing.bot_profiles.all())
                source_rows = mailing.guests_rows.values(
                    "guest_id",
                    "phone",
                    "email",
                    "text_mailing_list",
                    "scheduled_datetime",
                )
                duplicate_rows = [
                    MailingGuest(
                        mailing=duplicate,
                        guest_id=row["guest_id"],
                        phone=row["phone"],
                        email=row["email"],
                        text_mailing_list=row["text_mailing_list"],
                        scheduled_datetime=row["scheduled_datetime"],
                        status=MailingGuest.Status.PLANNED,
                        error_description=None,
                        external_id=None,
                        sent_at=None,
                        delivery_status="duplicated_from_campaign",
                        created_at=now,
                    )
                    for row in source_rows
                ]
                if duplicate_rows:
                    MailingGuest.objects.bulk_create(duplicate_rows, batch_size=1000)

            messages.success(
                request,
                f"Кампания #{mailing.id} продублирована: создана #{duplicate.id}, строк аудитории={len(duplicate_rows)}.",
            )
            return redirect("mailings_v2_campaigns_edit", pk=duplicate.pk)

        messages.error(request, "Неизвестное действие кампании.")
        return redirect(self._resolve_next_url(request, edit_url))


class MailingsV2CampaignAudienceView(TemplateView):
    """
    Просмотр аудитории выбранной кампании.

    Экран нужен как промежуточная валидация состава перед отправкой.
    """

    template_name = "mailing_v2/campaign_audience.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])
        rows_qs = (
            MailingGuest.objects.filter(mailing=mailing)
            .select_related("guest")
            .order_by("-id")
        )
        context["mailing"] = mailing
        context["rows"] = rows_qs[:300]
        context["stats"] = rows_qs.aggregate(
            total=Count("id"),
            planned=Count("id", filter=Q(status=MailingGuest.Status.PLANNED)),
            in_progress=Count("id", filter=Q(status=MailingGuest.Status.IN_PROGRESS)),
            done=Count("id", filter=Q(status=MailingGuest.Status.DONE)),
            error=Count("id", filter=Q(status=MailingGuest.Status.ERROR)),
        )
        snapshot = _get_workbench_snapshot(self.request, mailing.pk)
        context["workbench_snapshot"] = snapshot
        context["workbench_snapshot_url"] = _build_workbench_url_from_snapshot(snapshot) if snapshot else ""
        return context


class MailingsV2CampaignRunsView(TemplateView):
    """
    Экран запусков/истории по конкретной кампании.

    Даёт операционный срез по двум связанным слоям:
    1. строки получателей (`MailingGuest`);
    2. задачи доставки (`DispatchTask`).
    """

    template_name = "mailing_v2/campaign_runs.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])

        query = (self.request.GET.get("q") or "").strip()
        selected_row_status = (self.request.GET.get("row_status") or "").strip()
        selected_task_status = (self.request.GET.get("task_status") or "").strip()
        selected_provider_type = (self.request.GET.get("provider_type") or "").strip()

        valid_row_statuses = {value for value, _ in MailingGuest.Status.choices}
        valid_task_statuses = {value for value, _ in DispatchTask.Status.choices}
        valid_providers = {value for value, _ in BotProfile.ProviderType.choices}

        rows_scope = MailingGuest.objects.filter(mailing=mailing).select_related("guest")
        if selected_row_status in valid_row_statuses:
            rows_scope = rows_scope.filter(status=selected_row_status)
        else:
            selected_row_status = ""

        tasks_scope = DispatchTask.objects.filter(mailing_guest__mailing=mailing).select_related(
            "mailing_guest",
            "guest",
            "bot_profile",
        )
        if selected_task_status in valid_task_statuses:
            tasks_scope = tasks_scope.filter(status=selected_task_status)
        else:
            selected_task_status = ""

        if selected_provider_type in valid_providers:
            tasks_scope = tasks_scope.filter(provider_type=selected_provider_type)
        else:
            selected_provider_type = ""

        if query:
            rows_scope = rows_scope.filter(
                Q(phone__icontains=query)
                | Q(delivery_status__icontains=query)
                | Q(error_description__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(guest__first_name__icontains=query)
                | Q(guest__last_name__icontains=query)
            )
            tasks_scope = tasks_scope.filter(
                Q(external_chat_id__icontains=query)
                | Q(last_error__icontains=query)
                | Q(message_text__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(mailing_guest__phone__icontains=query)
            )

        rows_filtered_total = rows_scope.count()
        tasks_filtered_total = tasks_scope.count()

        rows = list(rows_scope.order_by("-id")[:200])
        tasks = list(tasks_scope.order_by("-id")[:200])

        timeline = _build_dispatch_timeline(tasks_scope.order_by("-updated_at")[:60])

        context["mailing"] = mailing
        context["rows"] = rows
        context["tasks"] = tasks
        context["timeline"] = timeline
        context["rows_filtered_total"] = rows_filtered_total
        context["tasks_filtered_total"] = tasks_filtered_total
        context["row_stats"] = _build_mailing_row_stats(mailing)
        context["task_stats"] = _build_mailing_dispatch_stats(mailing)
        context["row_status_choices"] = list(MailingGuest.Status.choices)
        context["task_status_choices"] = list(DispatchTask.Status.choices)
        context["provider_choices"] = list(BotProfile.ProviderType.choices)
        context["selected_row_status"] = selected_row_status
        context["selected_task_status"] = selected_task_status
        context["selected_provider_type"] = selected_provider_type
        context["query"] = query
        return context


class MailingsV2TemplatesView(TemplateView):
    """
    Каркас раздела шаблонов в новом контуре.

    На этапе bridge используется текущий backend CRUD шаблонов.
    """

    template_name = "mailing_v2/templates_hub.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        show_inactive = bool(self.request.GET.get("show_inactive"))
        query = (self.request.GET.get("q") or "").strip()

        template_rows = MessageTemplate.objects.annotate(
            mailings_total=Count("mailings", distinct=True),
            scenarios_total=Count("notification_scenarios", distinct=True),
        ).order_by("-updated_at")

        if not show_inactive:
            template_rows = template_rows.filter(is_active=True)

        if query:
            template_rows = template_rows.filter(
                Q(name__icontains=query) | Q(description__icontains=query) | Q(message_text__icontains=query)
            )

        context["templates_total"] = MessageTemplate.objects.count()
        context["templates_active"] = MessageTemplate.objects.filter(is_active=True).count()
        context["templates"] = template_rows[:100]
        context["show_inactive"] = show_inactive
        context["query"] = query
        return context


class MailingsV2TemplateCreateView(CreateView):
    """
    Создание шаблона в новом контуре.
    """

    model = MessageTemplate
    form_class = MessageTemplateForm
    template_name = "mailing_v2/template_form.html"

    def form_valid(self, form):
        obj = form.save(commit=False)
        # На этом этапе сохраняем совместимость с текущим backend-контрактом.
        obj.created_by = "mailings_v2_user"
        obj.save()
        messages.success(self.request, f"Шаблон создан (ID {obj.id}).")
        return redirect("mailings_v2_templates_detail", pk=obj.pk)


class MailingsV2TemplateDetailView(DetailView):
    """
    Детальная карточка шаблона с предпросмотром на госте.
    """

    model = MessageTemplate
    template_name = "mailing_v2/template_detail.html"
    context_object_name = "template_obj"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["guests"] = Guest.objects.all()[:50]

        guest_id = self.request.GET.get("guest_id")
        if guest_id:
            guest = Guest.objects.filter(pk=guest_id).first()
            if guest:
                from guests.services.template_render import render_message_for_guest

                context["preview_text"] = render_message_for_guest(self.object.message_text, guest)
                context["preview_guest"] = guest
                context["selected_guest_id"] = str(guest.id)
        return context


class MailingsV2TemplateUpdateView(UpdateView):
    """
    Редактирование шаблона в новом контуре.
    """

    model = MessageTemplate
    form_class = MessageTemplateForm
    template_name = "mailing_v2/template_form.html"

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, "Шаблон сохранен.")
        return redirect("mailings_v2_templates_detail", pk=self.object.pk)


class MailingsV2MonitorView(TemplateView):
    """
    Каркас мониторинга задач доставки.

    Показывает агрегаты из DispatchTask по текущему состоянию очереди.
    """

    template_name = "mailing_v2/monitor_hub.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base_scope = DispatchTask.objects.filter(
            Q(mailing_guest__isnull=False) | Q(notification_scenario__isnull=False)
        )
        scope = base_scope.select_related(
            "mailing_guest__mailing",
            "notification_scenario",
            "bot_profile",
            "guest",
        )

        selected_mailing_id = (self.request.GET.get("mailing_id") or "").strip()
        selected_status = (self.request.GET.get("status") or "").strip()
        selected_provider = (self.request.GET.get("provider_type") or "").strip()
        selected_scenario_id = (self.request.GET.get("scenario_id") or "").strip()

        if selected_mailing_id.isdigit():
            scope = scope.filter(mailing_guest__mailing_id=int(selected_mailing_id))
        else:
            selected_mailing_id = ""

        valid_statuses = {value for value, _ in DispatchTask.Status.choices}
        if selected_status in valid_statuses:
            scope = scope.filter(status=selected_status)
        else:
            selected_status = ""

        valid_providers = {value for value, _ in BotProfile.ProviderType.choices}
        if selected_provider in valid_providers:
            scope = scope.filter(provider_type=selected_provider)
        else:
            selected_provider = ""

        if selected_scenario_id.isdigit():
            scope = scope.filter(notification_scenario_id=int(selected_scenario_id))
        else:
            selected_scenario_id = ""

        status_rows = scope.values("status").annotate(total=Count("id")).order_by("status")
        provider_rows = scope.values("provider_type").annotate(total=Count("id")).order_by("-total")

        recent_rows = scope.order_by("-id")[:200]

        campaigns = Mailing.objects.order_by("-created_at")[:200]
        scenarios = NotificationScenario.objects.order_by("code")[:200]

        dispatch_pending = scope.filter(
            Q(status=DispatchTask.Status.PENDING)
            | Q(status=DispatchTask.Status.QUEUED)
            | Q(status=DispatchTask.Status.IN_PROGRESS)
        ).count()

        context["campaigns"] = campaigns
        context["scenarios"] = scenarios
        context["status_choices"] = list(DispatchTask.Status.choices)
        context["provider_choices"] = list(BotProfile.ProviderType.choices)
        context["selected_mailing_id"] = selected_mailing_id
        context["selected_status"] = selected_status
        context["selected_provider_type"] = selected_provider
        context["selected_scenario_id"] = selected_scenario_id
        context["recent_rows"] = recent_rows
        context["dispatch_pending"] = dispatch_pending
        context["status_rows"] = status_rows
        context["provider_rows"] = provider_rows
        context["total_tasks"] = scope.count()
        context["failed_tasks"] = scope.filter(status=DispatchTask.Status.FAILED).count()
        return context


class MailingsV2ScenariosView(TemplateView):
    """
    Каркас раздела автосценариев.

    Отображает текущий перечень сценариев и их базовые показатели.
    """

    template_name = "mailing_v2/scenarios_hub.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scenarios = (
            NotificationScenario.objects.select_related("template")
            .annotate(events_total=Count("events", distinct=True))
            .order_by("code")
        )
        context["scenarios"] = scenarios
        context["scenarios_total"] = scenarios.count()
        context["scenarios_active"] = scenarios.filter(is_active=True).count()
        return context


def _is_time_in_window(current_time, window_begin, window_end) -> bool:
    """
    Проверяет вхождение времени в окно отправки.

    Повторяет текущую логику `mailing_worker.process_one_mailing`,
    чтобы dry-run и run-now давали одинаковый результат.
    """
    return window_begin <= current_time <= window_end


def _build_mailing_dry_run_report(mailing: Mailing, now) -> dict[str, object]:
    """
    Формирует dry-run отчёт по готовности кампании к немедленному запуску.
    """
    local_now = timezone.localtime(now)
    current_time = local_now.time()
    selected_bot_ids = list(
        mailing.bot_profiles.filter(is_active=True).values_list("id", flat=True).order_by("id")
    )

    rows_scope = MailingGuest.objects.filter(mailing=mailing)
    ready_scope = rows_scope.filter(status=MailingGuest.Status.PLANNED, scheduled_datetime__lte=now)
    ready_rows = int(ready_scope.count())

    ready_rows_with_targets = 0
    ready_rows_without_targets = 0
    if ready_rows > 0 and selected_bot_ids:
        targetable_guest_ids_qs = (
            GuestBotBinding.objects.filter(
                guest_id__in=ready_scope.values_list("guest_id", flat=True),
                bot_id__in=selected_bot_ids,
                is_active=True,
                is_opt_in=True,
                is_stop_sending=False,
                bot__is_active=True,
            )
            .exclude(external_chat_id__isnull=True)
            .exclude(external_chat_id="")
            .values_list("guest_id", flat=True)
            .distinct()
        )
        ready_rows_with_targets = int(ready_scope.filter(guest_id__in=targetable_guest_ids_qs).count())
        ready_rows_without_targets = max(ready_rows - ready_rows_with_targets, 0)

    report = {
        "generated_at": now.isoformat(),
        "mailing_id": int(mailing.id),
        "mailing_is_active": bool(mailing.is_active),
        "schedule_window_open": bool(mailing.scheduled_time_begin <= now <= mailing.scheduled_time_end),
        "send_window_open": bool(_is_time_in_window(current_time, mailing.send_window_begin, mailing.send_window_end)),
        "selected_bots_total": int(mailing.bot_profiles.count()),
        "selected_bots_active": int(len(selected_bot_ids)),
        "rows_total": int(rows_scope.count()),
        "planned_rows_total": int(rows_scope.filter(status=MailingGuest.Status.PLANNED).count()),
        "ready_rows": ready_rows,
        "future_rows": int(rows_scope.filter(status=MailingGuest.Status.PLANNED, scheduled_datetime__gt=now).count()),
        "in_progress_rows": int(rows_scope.filter(status=MailingGuest.Status.IN_PROGRESS).count()),
        "done_rows": int(rows_scope.filter(status=MailingGuest.Status.DONE).count()),
        "error_rows": int(rows_scope.filter(status=MailingGuest.Status.ERROR).count()),
        "ready_rows_with_targets": int(ready_rows_with_targets),
        "ready_rows_without_targets": int(ready_rows_without_targets),
    }
    return report


def _run_mailing_now(mailing: Mailing, now, max_batches: int) -> dict[str, object]:
    """
    Выполняет ограниченный one-shot запуск кампании через существующий producer-путь.
    """
    report_before = _build_mailing_dry_run_report(mailing=mailing, now=now)
    processed_rows_total = 0
    processed_batches = 0
    reached_batch_limit = False

    if report_before["send_window_open"] and report_before["ready_rows"] > 0:
        for _ in range(max_batches):
            processed = int(mailing_worker_cmd.process_one_mailing(mailing=mailing, now=now) or 0)
            if processed <= 0:
                break
            processed_rows_total += processed
            processed_batches += 1
        if processed_batches >= max_batches:
            reached_batch_limit = True

    report_after = _build_mailing_dry_run_report(mailing=mailing, now=timezone.now())
    return {
        "generated_at": timezone.now().isoformat(),
        "mailing_id": int(mailing.id),
        "schedule_window_open": bool(report_before["schedule_window_open"]),
        "send_window_open": bool(report_before["send_window_open"]),
        "ready_rows_before": int(report_before["ready_rows"]),
        "ready_rows_after": int(report_after["ready_rows"]),
        "processed_batches": int(processed_batches),
        "processed_rows_total": int(processed_rows_total),
        "batch_size": int(mailing_worker_cmd.BATCH_SIZE),
        "max_batches": int(max_batches),
        "reached_batch_limit": bool(reached_batch_limit),
    }


def _get_workbench_snapshot(request, mailing_id: int) -> dict | None:
    """
    Достаёт и нормализует снимок фильтров Workbench для кампании из сессии.
    """
    all_snapshots = request.session.get("mailings_v2_workbench_snapshots", {})
    if not isinstance(all_snapshots, dict):
        return None
    raw_snapshot = all_snapshots.get(str(mailing_id))
    if not isinstance(raw_snapshot, dict):
        return None

    snapshot = {
        "as_of_date": str(raw_snapshot.get("as_of_date") or "").strip(),
        "window_days": str(raw_snapshot.get("window_days") or "").strip(),
        "department_id": str(raw_snapshot.get("department_id") or "").strip(),
        "segment_code": str(raw_snapshot.get("segment_code") or "").strip(),
        "focus_category_code": str(raw_snapshot.get("focus_category_code") or "").strip(),
        "selected_total": int(raw_snapshot.get("selected_total") or 0),
        "selected_rows_count": int(raw_snapshot.get("selected_rows_count") or 0),
        "source_layer": str(raw_snapshot.get("source_layer") or "").strip(),
        "saved_at": str(raw_snapshot.get("saved_at") or "").strip(),
        "complex_filters": [],
    }

    complex_filters_raw = raw_snapshot.get("complex_filters") or []
    if isinstance(complex_filters_raw, list):
        for item in complex_filters_raw:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            operator = str(item.get("operator") or "").strip()
            value = str(item.get("value") or "").strip()
            if not (field or operator or value):
                continue
            snapshot["complex_filters"].append(
                {
                    "field": field,
                    "operator": operator,
                    "value": value,
                }
            )
    return snapshot


def _build_workbench_url_from_snapshot(snapshot: dict) -> str:
    """
    Собирает URL перехода в Workbench по сохранённому snapshot фильтров.
    """
    params = {}
    for key in ("as_of_date", "window_days", "department_id", "segment_code", "focus_category_code"):
        value = str(snapshot.get(key) or "").strip()
        if value:
            params[key] = value

    complex_filters = snapshot.get("complex_filters") or []
    if isinstance(complex_filters, list) and complex_filters:
        params["cf_field"] = [str(item.get("field") or "").strip() for item in complex_filters]
        params["cf_op"] = [str(item.get("operator") or "").strip() for item in complex_filters]
        params["cf_value"] = [str(item.get("value") or "").strip() for item in complex_filters]

    base_url = reverse("guests_workbench")
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params, doseq=True)}"


def _empty_mailing_row_stats() -> dict[str, int]:
    return {
        "total": 0,
        "planned": 0,
        "in_progress": 0,
        "done": 0,
        "error": 0,
    }


def _empty_dispatch_stats() -> dict[str, int]:
    return {
        "total": 0,
        "pending": 0,
        "queued": 0,
        "in_progress": 0,
        "done": 0,
        "failed": 0,
        "canceled": 0,
    }


def _build_mailing_row_stats(mailing: Mailing) -> dict[str, int]:
    """
    Сводка по статусам строк получателей для конкретной кампании.
    """
    stats = mailing.guests_rows.aggregate(
        total=Count("id"),
        planned=Count("id", filter=Q(status=MailingGuest.Status.PLANNED)),
        in_progress=Count("id", filter=Q(status=MailingGuest.Status.IN_PROGRESS)),
        done=Count("id", filter=Q(status=MailingGuest.Status.DONE)),
        error=Count("id", filter=Q(status=MailingGuest.Status.ERROR)),
    )
    result = _empty_mailing_row_stats()
    for key in result.keys():
        result[key] = int(stats.get(key) or 0)
    return result


def _build_mailing_dispatch_stats(mailing: Mailing) -> dict[str, int]:
    """
    Сводка по статусам dispatch-задач, связанных с кампанией.
    """
    scope = DispatchTask.objects.filter(mailing_guest__mailing=mailing)
    stats = scope.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(status=DispatchTask.Status.PENDING)),
        queued=Count("id", filter=Q(status=DispatchTask.Status.QUEUED)),
        in_progress=Count("id", filter=Q(status=DispatchTask.Status.IN_PROGRESS)),
        done=Count("id", filter=Q(status=DispatchTask.Status.DONE)),
        failed=Count("id", filter=Q(status=DispatchTask.Status.FAILED)),
        canceled=Count("id", filter=Q(status=DispatchTask.Status.CANCELED)),
    )
    result = _empty_dispatch_stats()
    for key in result.keys():
        result[key] = int(stats.get(key) or 0)
    return result


def _build_dispatch_timeline(tasks: list[DispatchTask]) -> list[dict[str, object]]:
    """
    Формирует компактный таймлайн по последним задачам dispatch.
    """
    timeline: list[dict[str, object]] = []
    for task in tasks:
        event_time = task.finished_at or task.started_at or task.enqueued_at or task.created_at
        timeline.append(
            {
                "task_id": int(task.id),
                "status": str(task.status),
                "provider_type": str(task.provider_type),
                "guest_phone": str(task.guest.phone) if task.guest and task.guest.phone else "",
                "event_time": event_time,
                "message": (task.last_error or "")[:200],
            }
        )
    timeline.sort(key=lambda item: item["event_time"] or timezone.now(), reverse=True)
    return timeline[:60]
