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
from django.db.models import Count, F, Max, Q
from django.http import QueryDict
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
    NotificationEvent,
    NotificationScenario,
)
from guests.services.notification_handler_registry import (
    get_registered_schedule_scenario_codes,
    run_registered_schedule_scenarios,
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
        context["mailings_v2_flow"] = _build_mailings_v2_flow(active_area="campaigns")
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
            context["campaign_active_tab"] = "overview"
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
            context["wizard_state"] = _build_campaign_wizard_state(
                mailing=mailing,
                active_tab=context["campaign_active_tab"],
            )
        else:
            context["guests_count"] = 0
            context["campaign_active_tab"] = ""
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
            context["wizard_state"] = _build_campaign_wizard_state(
                mailing=None,
                active_tab="overview",
            )
        context["mailings_v2_flow"] = _build_mailings_v2_flow(active_area="campaigns")
        return context


class MailingsV2CampaignCreateView(_MailingsV2CampaignFormMixin, CreateView):
    """
    Создание кампании в новом UI.

    Логика сохранения соответствует текущей legacy-форме.
    """

    def get_initial(self):
        """
        Поддерживает prefill шаблона при переходе из раздела templates.
        """
        initial = super().get_initial()
        template_id_raw = str(self.request.GET.get("template_id") or "").strip()
        if template_id_raw.isdigit():
            template = MessageTemplate.objects.filter(pk=int(template_id_raw), is_active=True).first()
            if template:
                initial["template"] = template.pk
                if not initial.get("name"):
                    initial["name"] = f"Кампания: {template.name}"
        return initial

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
        context["campaign_active_tab"] = "audience"
        context["wizard_state"] = _build_campaign_wizard_state(
            mailing=mailing,
            active_tab=context["campaign_active_tab"],
        )
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
        context["campaign_active_tab"] = "runs"
        context["wizard_state"] = _build_campaign_wizard_state(
            mailing=mailing,
            active_tab=context["campaign_active_tab"],
        )
        return context


class MailingsV2CampaignJobsView(TemplateView):
    """
    Экран заданий отправки по конкретной кампании.

    Фокус:
    1. операционный срез по DispatchTask;
    2. фильтры по статусу/провайдеру/очереди;
    3. агрегаты для быстрой диагностики по результатам доставки.
    """

    template_name = "mailing_v2/campaign_jobs.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])

        query = (self.request.GET.get("q") or "").strip()
        selected_task_status = (self.request.GET.get("task_status") or "").strip()
        selected_provider_type = (self.request.GET.get("provider_type") or "").strip()
        selected_queue_name = (self.request.GET.get("queue_name") or "").strip()

        valid_task_statuses = {value for value, _ in DispatchTask.Status.choices}
        valid_providers = {value for value, _ in BotProfile.ProviderType.choices}

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

        if selected_queue_name:
            tasks_scope = tasks_scope.filter(queue_name=selected_queue_name)

        if query:
            tasks_scope = tasks_scope.filter(
                Q(external_chat_id__icontains=query)
                | Q(last_error__icontains=query)
                | Q(message_text__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(mailing_guest__phone__icontains=query)
            )

        tasks_filtered_total = int(tasks_scope.count())
        tasks = list(tasks_scope.order_by("-updated_at", "-id")[:250])

        provider_status_rows = list(
            tasks_scope.values("provider_type", "status").annotate(total=Count("id")).order_by("provider_type", "status")
        )
        queue_rows = list(
            tasks_scope.values("queue_name").annotate(total=Count("id")).order_by("-total", "queue_name")[:30]
        )
        top_errors = list(
            tasks_scope.filter(status=DispatchTask.Status.FAILED)
            .exclude(last_error__isnull=True)
            .exclude(last_error__exact="")
            .values("provider_type", "last_error")
            .annotate(total=Count("id"))
            .order_by("-total", "provider_type", "last_error")[:25]
        )
        delivery_feedback_rows = list(
            MailingGuest.objects.filter(mailing=mailing)
            .exclude(delivery_status__isnull=True)
            .exclude(delivery_status__exact="")
            .values("delivery_status")
            .annotate(total=Count("id"))
            .order_by("-total", "delivery_status")[:25]
        )

        context["mailing"] = mailing
        context["query"] = query
        context["selected_task_status"] = selected_task_status
        context["selected_provider_type"] = selected_provider_type
        context["selected_queue_name"] = selected_queue_name
        context["task_status_choices"] = list(DispatchTask.Status.choices)
        context["provider_choices"] = list(BotProfile.ProviderType.choices)
        context["queue_name_choices"] = list(
            DispatchTask.objects.filter(mailing_guest__mailing=mailing)
            .exclude(queue_name__isnull=True)
            .exclude(queue_name__exact="")
            .values_list("queue_name", flat=True)
            .distinct()
            .order_by("queue_name")
        )
        context["tasks"] = tasks
        context["tasks_filtered_total"] = tasks_filtered_total
        context["task_stats"] = _build_mailing_dispatch_stats(mailing)
        context["provider_status_rows"] = provider_status_rows
        context["queue_rows"] = queue_rows
        context["top_errors"] = top_errors
        context["delivery_feedback_rows"] = delivery_feedback_rows
        context["campaign_active_tab"] = "jobs"
        context["wizard_state"] = _build_campaign_wizard_state(
            mailing=mailing,
            active_tab=context["campaign_active_tab"],
        )
        return context


class MailingsV2CampaignErrorsView(TemplateView):
    """
    Экран ошибок кампании в mailings-v2.

    Показывает две проблемные зоны:
    1. error-строки аудитории (`MailingGuest`);
    2. failed-задачи доставки (`DispatchTask`).
    """

    template_name = "mailing_v2/campaign_errors.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])

        query = (self.request.GET.get("q") or "").strip()
        selected_delivery_status = (self.request.GET.get("delivery_status") or "").strip()
        selected_provider_type = (self.request.GET.get("provider_type") or "").strip()

        delivery_status_choices = list(
            MailingGuest.objects.filter(mailing=mailing)
            .exclude(delivery_status__isnull=True)
            .exclude(delivery_status__exact="")
            .values_list("delivery_status", flat=True)
            .distinct()
            .order_by("delivery_status")
        )
        if selected_delivery_status and selected_delivery_status not in set(delivery_status_choices):
            selected_delivery_status = ""

        valid_providers = {value for value, _ in BotProfile.ProviderType.choices}
        if selected_provider_type not in valid_providers:
            selected_provider_type = ""

        row_error_codes = {
            "dispatch_no_targets",
            "dispatch_no_bot_profiles",
            "dispatch_enqueue_error",
            "dispatch_enqueue_exception",
            "retry_requested",
            "requeued_from_ui",
        }
        row_errors_scope = MailingGuest.objects.filter(mailing=mailing).filter(
            Q(status=MailingGuest.Status.ERROR)
            | Q(error_description__isnull=False)
            | Q(delivery_status__in=row_error_codes)
        ).select_related("guest")
        row_errors_scope = row_errors_scope.exclude(
            Q(error_description__isnull=True)
            & (Q(delivery_status__isnull=True) | Q(delivery_status__exact=""))
            & ~Q(status=MailingGuest.Status.ERROR)
        )
        if selected_delivery_status:
            row_errors_scope = row_errors_scope.filter(delivery_status=selected_delivery_status)
        if query:
            row_errors_scope = row_errors_scope.filter(
                Q(phone__icontains=query)
                | Q(delivery_status__icontains=query)
                | Q(error_description__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(guest__first_name__icontains=query)
                | Q(guest__last_name__icontains=query)
            )

        failed_dispatch_scope = DispatchTask.objects.filter(
            mailing_guest__mailing=mailing,
            status=DispatchTask.Status.FAILED,
        ).select_related("mailing_guest", "guest", "bot_profile")
        if selected_provider_type:
            failed_dispatch_scope = failed_dispatch_scope.filter(provider_type=selected_provider_type)
        if query:
            failed_dispatch_scope = failed_dispatch_scope.filter(
                Q(external_chat_id__icontains=query)
                | Q(last_error__icontains=query)
                | Q(message_text__icontains=query)
                | Q(guest__phone__icontains=query)
                | Q(mailing_guest__phone__icontains=query)
            )

        row_errors_total = row_errors_scope.count()
        failed_dispatch_total = failed_dispatch_scope.count()

        context["mailing"] = mailing
        context["query"] = query
        context["selected_delivery_status"] = selected_delivery_status
        context["selected_provider_type"] = selected_provider_type
        context["delivery_status_choices"] = delivery_status_choices
        context["provider_choices"] = list(BotProfile.ProviderType.choices)
        context["row_errors_total"] = row_errors_total
        context["failed_dispatch_total"] = failed_dispatch_total
        context["row_error_groups"] = (
            row_errors_scope.values("delivery_status")
            .annotate(total=Count("id"))
            .order_by("-total", "delivery_status")[:20]
        )
        context["dispatch_error_groups"] = (
            failed_dispatch_scope.values("provider_type", "last_error")
            .annotate(total=Count("id"))
            .order_by("-total", "provider_type", "last_error")[:20]
        )
        context["row_errors"] = list(row_errors_scope.order_by("-id")[:200])
        context["failed_dispatch"] = list(failed_dispatch_scope.order_by("-updated_at", "-id")[:200])
        context["current_query_path"] = self.request.get_full_path()
        context["row_stats"] = _build_mailing_row_stats(mailing)
        context["task_stats"] = _build_mailing_dispatch_stats(mailing)
        context["campaign_active_tab"] = "errors"
        context["wizard_state"] = _build_campaign_wizard_state(
            mailing=mailing,
            active_tab=context["campaign_active_tab"],
        )
        return context


class MailingsV2CampaignLogsView(TemplateView):
    """
    Экран логов кампании в mailings-v2.

    Даёт комбинированный журнал:
    1. изменения строк аудитории (`MailingGuest`);
    2. события dispatch-задач (`DispatchTask`).
    """

    template_name = "mailing_v2/campaign_logs.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])

        query = (self.request.GET.get("q") or "").strip()
        selected_row_status = (self.request.GET.get("row_status") or "").strip()
        selected_task_status = (self.request.GET.get("task_status") or "").strip()

        valid_row_statuses = {value for value, _ in MailingGuest.Status.choices}
        valid_task_statuses = {value for value, _ in DispatchTask.Status.choices}

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
        tasks = list(tasks_scope.order_by("-updated_at", "-id")[:200])
        timeline = _build_mailing_log_timeline(rows=rows[:120], tasks=tasks[:120])

        context["mailing"] = mailing
        context["query"] = query
        context["selected_row_status"] = selected_row_status
        context["selected_task_status"] = selected_task_status
        context["row_status_choices"] = list(MailingGuest.Status.choices)
        context["task_status_choices"] = list(DispatchTask.Status.choices)
        context["rows"] = rows
        context["tasks"] = tasks
        context["timeline"] = timeline
        context["rows_filtered_total"] = rows_filtered_total
        context["tasks_filtered_total"] = tasks_filtered_total
        context["row_stats"] = _build_mailing_row_stats(mailing)
        context["task_stats"] = _build_mailing_dispatch_stats(mailing)
        context["campaign_active_tab"] = "logs"
        context["wizard_state"] = _build_campaign_wizard_state(
            mailing=mailing,
            active_tab=context["campaign_active_tab"],
        )
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
        context["mailings_v2_flow"] = _build_mailings_v2_flow(active_area="templates")
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
        context["mailings_v2_flow"] = _build_mailings_v2_flow(active_area="templates")
        context["campaign_prefill_url"] = (
            f"{reverse('mailings_v2_campaigns_new')}?{urlencode({'template_id': self.object.id})}"
        )

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

    @staticmethod
    def _build_redirect_url(*, return_query: str) -> str:
        """
        Собирает URL возврата на monitor с сохранением активных фильтров.
        """
        base_url = reverse("mailings_v2_monitor")
        safe_query = str(return_query or "").strip()
        if not safe_query:
            return base_url
        return f"{base_url}?{safe_query}"

    @staticmethod
    def _normalize_filters(params) -> dict[str, str]:
        """
        Нормализует входные фильтры monitor из QueryDict/словаря.
        """
        selected_mailing_id = str(params.get("mailing_id") or "").strip()
        selected_status = str(params.get("status") or "").strip()
        selected_provider = str(params.get("provider_type") or "").strip()
        selected_scenario_id = str(params.get("scenario_id") or "").strip()

        if not selected_mailing_id.isdigit():
            selected_mailing_id = ""
        if not selected_scenario_id.isdigit():
            selected_scenario_id = ""

        valid_statuses = {value for value, _ in DispatchTask.Status.choices}
        if selected_status not in valid_statuses:
            selected_status = ""

        valid_providers = {value for value, _ in BotProfile.ProviderType.choices}
        if selected_provider not in valid_providers:
            selected_provider = ""

        return {
            "mailing_id": selected_mailing_id,
            "status": selected_status,
            "provider_type": selected_provider,
            "scenario_id": selected_scenario_id,
        }

    @classmethod
    def _build_filtered_scope(cls, params):
        """
        Возвращает queryset DispatchTask по фильтрам monitor.
        """
        scope = DispatchTask.objects.filter(
            Q(mailing_guest__isnull=False) | Q(notification_scenario__isnull=False)
        ).select_related(
            "mailing_guest__mailing",
            "notification_scenario",
            "bot_profile",
            "guest",
        )
        filters = cls._normalize_filters(params)

        if filters["mailing_id"]:
            scope = scope.filter(mailing_guest__mailing_id=int(filters["mailing_id"]))
        if filters["status"]:
            scope = scope.filter(status=filters["status"])
        if filters["provider_type"]:
            scope = scope.filter(provider_type=filters["provider_type"])
        if filters["scenario_id"]:
            scope = scope.filter(notification_scenario_id=int(filters["scenario_id"]))

        return scope, filters

    def post(self, request, *args, **kwargs):
        """
        Быстрые операционные действия по задачам dispatch на monitor-экране.

        Поддерживает:
        1. retry failed -> pending (сброс попыток);
        2. requeue pending/queued -> pending с available_at=now.
        """
        action = str(request.POST.get("action") or "").strip()
        return_query = str(request.POST.get("return_query") or "").strip()
        redirect_url = self._build_redirect_url(return_query=return_query)

        filter_params = QueryDict(return_query, mutable=False) if return_query else request.POST
        scope, filters = self._build_filtered_scope(filter_params)
        now = timezone.now()

        if action == "retry_failed_tasks":
            candidates = scope.filter(status=DispatchTask.Status.FAILED)
            updated = candidates.update(
                status=DispatchTask.Status.PENDING,
                enqueued_at=None,
                queue_name=None,
                started_at=None,
                finished_at=None,
                available_at=now,
                updated_at=now,
                attempt=0,
                last_error=None,
            )
            request.session["mailings_v2_monitor_ops_report"] = {
                "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
                "action": "retry_failed_tasks",
                "updated_tasks": int(updated),
                "filters": filters,
            }
            if updated > 0:
                messages.success(request, f"Переведено failed -> pending задач: {updated}.")
            else:
                messages.info(request, "Под выбранный фильтр failed-задачи не найдены.")
            return redirect(redirect_url)

        if action == "requeue_waiting_tasks":
            candidates = scope.filter(status__in=[DispatchTask.Status.PENDING, DispatchTask.Status.QUEUED])
            updated = candidates.update(
                status=DispatchTask.Status.PENDING,
                enqueued_at=None,
                queue_name=None,
                started_at=None,
                finished_at=None,
                available_at=now,
                updated_at=now,
            )
            request.session["mailings_v2_monitor_ops_report"] = {
                "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
                "action": "requeue_waiting_tasks",
                "updated_tasks": int(updated),
                "filters": filters,
            }
            if updated > 0:
                messages.success(request, f"Переоткрыто pending/queued задач: {updated}.")
            else:
                messages.info(request, "Под выбранный фильтр pending/queued задачи не найдены.")
            return redirect(redirect_url)

        messages.error(request, "Неизвестное действие monitor.")
        return redirect(redirect_url)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scope, filters = self._build_filtered_scope(self.request.GET)

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

        retry_candidates = scope.filter(
            status=DispatchTask.Status.FAILED,
            attempt__lt=F("max_attempts"),
        ).count()
        retry_exhausted = scope.filter(
            status=DispatchTask.Status.FAILED,
            attempt__gte=F("max_attempts"),
        ).count()
        retry_in_queue = scope.filter(
            status__in=[DispatchTask.Status.PENDING, DispatchTask.Status.QUEUED],
            attempt__gt=0,
        ).count()
        retry_attempted = scope.filter(attempt__gt=0).count()
        max_attempt_observed = int(scope.aggregate(max_attempt=Max("attempt")).get("max_attempt") or 0)

        context["campaigns"] = campaigns
        context["scenarios"] = scenarios
        context["status_choices"] = list(DispatchTask.Status.choices)
        context["provider_choices"] = list(BotProfile.ProviderType.choices)
        context["selected_mailing_id"] = filters["mailing_id"]
        context["selected_status"] = filters["status"]
        context["selected_provider_type"] = filters["provider_type"]
        context["selected_scenario_id"] = filters["scenario_id"]
        context["recent_rows"] = recent_rows
        context["dispatch_pending"] = dispatch_pending
        context["status_rows"] = status_rows
        context["provider_rows"] = provider_rows
        context["total_tasks"] = scope.count()
        context["failed_tasks"] = scope.filter(status=DispatchTask.Status.FAILED).count()
        context["retry_candidates"] = int(retry_candidates)
        context["retry_exhausted"] = int(retry_exhausted)
        context["retry_in_queue"] = int(retry_in_queue)
        context["retry_attempted"] = int(retry_attempted)
        context["max_attempt_observed"] = max_attempt_observed
        context["return_query"] = self.request.GET.urlencode()
        context["monitor_ops_report"] = self.request.session.pop("mailings_v2_monitor_ops_report", None)
        context["mailings_v2_flow"] = _build_mailings_v2_flow(active_area="monitor")
        return context


class MailingsV2ScenariosView(TemplateView):
    """
    Каркас раздела автосценариев.

    Отображает текущий перечень сценариев и их базовые показатели.
    """

    template_name = "mailing_v2/scenarios_hub.html"

    @staticmethod
    def _parse_limit_per_scenario(raw_value: str, *, default: int = 500) -> int:
        """
        Нормализует лимит обработки одного сценария для ручного запуска.
        """
        try:
            parsed = int(str(raw_value or "").strip())
        except (TypeError, ValueError):
            return int(default)
        if parsed <= 0:
            return int(default)
        return min(parsed, 5000)

    @staticmethod
    def _build_redirect_url(*, return_query: str) -> str:
        """
        Собирает URL возврата на экран сценариев с сохранением фильтров.
        """
        base_url = reverse("mailings_v2_scenarios")
        safe_query = str(return_query or "").strip()
        if not safe_query:
            return base_url
        return f"{base_url}?{safe_query}"

    def post(self, request, *args, **kwargs):
        """
        Обрабатывает ручной one-shot запуск плановых сценариев.
        """
        action = str(request.POST.get("action") or "").strip()
        return_query = str(request.POST.get("return_query") or "").strip()
        redirect_url = self._build_redirect_url(return_query=return_query)

        if action != "run_schedule_once":
            messages.error(request, "Неизвестное действие для экрана сценариев.")
            return redirect(redirect_url)

        scenario_code = str(request.POST.get("scenario_code") or "").strip()
        limit_per_scenario = self._parse_limit_per_scenario(request.POST.get("limit_per_scenario"))
        scenario_codes = [scenario_code] if scenario_code else None
        stats = run_registered_schedule_scenarios(
            scenario_codes=scenario_codes,
            limit_per_scenario=limit_per_scenario,
        )

        report_rows: list[dict[str, int | str]] = []
        total_created_tasks = 0
        for code, stat in stats.items():
            created_tasks = int(getattr(stat, "created_tasks", 0) or 0)
            row = {
                "scenario_code": str(code),
                "scanned_guests": int(getattr(stat, "scanned_guests", 0) or 0),
                "matched_guests": int(getattr(stat, "matched_guests", 0) or 0),
                "created_tasks": created_tasks,
                "skipped_without_coupon": int(getattr(stat, "skipped_without_coupon", 0) or 0),
                "skipped_duplicate_or_no_targets": int(
                    getattr(stat, "skipped_duplicate_or_no_targets", 0) or 0
                ),
            }
            report_rows.append(row)
            total_created_tasks += created_tasks

        request.session["mailings_v2_scenarios_run_report"] = {
            "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
            "limit_per_scenario": int(limit_per_scenario),
            "selected_scenario_code": scenario_code,
            "rows": report_rows,
            "total_created_tasks": int(total_created_tasks),
        }

        if scenario_code:
            messages.success(
                request,
                (
                    f"Сценарий '{scenario_code}' обработан: "
                    f"создано задач={total_created_tasks}, лимит={limit_per_scenario}."
                ),
            )
        else:
            messages.success(
                request,
                (
                    "Плановые сценарии обработаны вручную: "
                    f"создано задач={total_created_tasks}, лимит={limit_per_scenario}."
                ),
            )
        return redirect(redirect_url)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        since_24h = now - timedelta(hours=24)

        query = str(self.request.GET.get("q") or "").strip()
        selected_trigger_type = str(self.request.GET.get("trigger_type") or "").strip()
        show_inactive = bool(self.request.GET.get("show_inactive"))
        only_system = bool(self.request.GET.get("only_system"))
        with_errors = bool(self.request.GET.get("with_errors"))

        scenarios_scope = NotificationScenario.objects.select_related("template").annotate(
            events_total=Count("events", distinct=True),
            events_24h=Count("events", filter=Q(events__created_at__gte=since_24h), distinct=True),
            events_error_24h=Count(
                "events",
                filter=Q(
                    events__created_at__gte=since_24h,
                    events__status=NotificationEvent.Status.ERROR,
                ),
                distinct=True,
            ),
            tasks_24h=Count("dispatch_tasks", filter=Q(dispatch_tasks__created_at__gte=since_24h), distinct=True),
            tasks_failed_24h=Count(
                "dispatch_tasks",
                filter=Q(
                    dispatch_tasks__created_at__gte=since_24h,
                    dispatch_tasks__status=DispatchTask.Status.FAILED,
                ),
                distinct=True,
            ),
            last_event_at=Max("events__created_at"),
        )

        if not show_inactive:
            scenarios_scope = scenarios_scope.filter(is_active=True)
        if only_system:
            scenarios_scope = scenarios_scope.filter(is_system=True)

        valid_trigger_types = {value for value, _ in NotificationScenario.TriggerType.choices}
        if selected_trigger_type in valid_trigger_types:
            scenarios_scope = scenarios_scope.filter(trigger_type=selected_trigger_type)
        else:
            selected_trigger_type = ""

        if with_errors:
            scenarios_scope = scenarios_scope.filter(
                Q(events_error_24h__gt=0) | Q(tasks_failed_24h__gt=0)
            )

        if query:
            scenarios_scope = scenarios_scope.filter(
                Q(code__icontains=query)
                | Q(name__icontains=query)
                | Q(description__icontains=query)
                | Q(template__name__icontains=query)
            )

        scenarios_scope = scenarios_scope.order_by("code")
        scenario_ids = scenarios_scope.values_list("id", flat=True)

        context["scenarios"] = scenarios_scope[:200]
        context["scenarios_total"] = scenarios_scope.count()
        context["scenarios_active"] = scenarios_scope.filter(is_active=True).count()
        context["events_24h_total"] = NotificationEvent.objects.filter(
            scenario_id__in=scenario_ids,
            created_at__gte=since_24h,
        ).count()
        context["tasks_24h_total"] = DispatchTask.objects.filter(
            notification_scenario_id__in=scenario_ids,
            created_at__gte=since_24h,
        ).count()
        context["tasks_failed_24h_total"] = DispatchTask.objects.filter(
            notification_scenario_id__in=scenario_ids,
            created_at__gte=since_24h,
            status=DispatchTask.Status.FAILED,
        ).count()
        context["schedule_scenario_codes"] = list(get_registered_schedule_scenario_codes())
        context["trigger_type_choices"] = list(NotificationScenario.TriggerType.choices)
        context["selected_trigger_type"] = selected_trigger_type
        context["show_inactive"] = show_inactive
        context["only_system"] = only_system
        context["with_errors"] = with_errors
        context["query"] = query
        context["return_query"] = self.request.GET.urlencode()
        context["scenarios_run_report"] = self.request.session.pop("mailings_v2_scenarios_run_report", None)
        context["mailings_v2_flow"] = _build_mailings_v2_flow(active_area="scenarios")
        return context


def _build_mailings_v2_flow(*, active_area: str) -> dict[str, object]:
    """
    Формирует единый bridge-флоу для экранов mailings-v2.

    Шаги:
    1. гипотеза и отбор в workbench;
    2. подготовка шаблона;
    3. настройка и запуск кампании;
    4. мониторинг результата и разбор проблем.
    """
    rank_map = {
        "workbench": 1,
        "templates": 2,
        "campaigns": 3,
        "monitor": 4,
        "scenarios": 4,
    }
    current_rank = int(rank_map.get(str(active_area or ""), 3))

    monitor_url = reverse("mailings_v2_monitor")
    scenarios_url = reverse("mailings_v2_scenarios")
    step4_url = scenarios_url if active_area == "scenarios" else monitor_url

    steps = [
        {
            "number": 1,
            "title": "Гипотеза в Workbench",
            "description": "Соберите сегмент и создайте черновик рассылки из отбора.",
            "url": reverse("guests_workbench"),
            "cta": "Открыть Workbench",
        },
        {
            "number": 2,
            "title": "Шаблон сообщения",
            "description": "Подготовьте текст и проверьте предпросмотр на реальном госте.",
            "url": reverse("mailings_v2_templates"),
            "cta": "Открыть шаблоны",
        },
        {
            "number": 3,
            "title": "Кампания и запуск",
            "description": "Проверьте аудиторию, выполните dry-run и запускайте кампанию.",
            "url": reverse("mailings_v2_campaigns"),
            "cta": "Открыть кампании",
        },
        {
            "number": 4,
            "title": "Мониторинг и обратная связь",
            "description": "Контролируйте dispatch, retry, ошибки и корректируйте следующий запуск.",
            "url": step4_url,
            "cta": "Открыть мониторинг",
        },
    ]

    for step in steps:
        number = int(step["number"])
        if number < current_rank:
            step["status"] = "done"
        elif number == current_rank:
            step["status"] = "current"
        else:
            step["status"] = "todo"

    subtitle_map = {
        "templates": "Сейчас вы на шаге подготовки контента. После шаблона переходите к запуску кампании.",
        "campaigns": "Сейчас вы на шаге запуска. После старта контролируйте отработку на экране мониторинга.",
        "monitor": "Сейчас вы на шаге контроля и обратной связи по отправкам.",
        "scenarios": "Сейчас вы на шаге контроля автосценариев и их операционного запуска.",
    }

    return {
        "title": "Маршрут маркетолога",
        "subtitle": subtitle_map.get(
            active_area,
            "Единый сценарий: от гипотезы до запуска и операционного контроля.",
        ),
        "steps": steps,
    }


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


def _build_campaign_wizard_state(*, mailing: Mailing | None, active_tab: str) -> dict[str, object]:
    """
    Формирует состояние мастер-флоу кампании (3 шага) для нового UI.

    Шаги:
    1. параметры кампании;
    2. аудитория;
    3. проверка и запуск.
    """
    if not mailing or not mailing.pk:
        return {
            "current_step": 1,
            "summary": "Шаг 1 из 3: заполните параметры кампании и сохраните черновик.",
            "cta_label": "Сохранить шаг 1",
            "cta_url": "",
            "steps": [
                {"number": 1, "title": "Параметры", "status": "current", "url": ""},
                {"number": 2, "title": "Аудитория", "status": "todo", "url": ""},
                {"number": 3, "title": "Проверка и запуск", "status": "todo", "url": ""},
            ],
        }

    rows_total = int(mailing.guests_rows.count())
    has_audience = rows_total > 0
    has_dispatch_activity = DispatchTask.objects.filter(mailing_guest__mailing=mailing).exists()
    has_send_results = mailing.guests_rows.filter(status__in=[MailingGuest.Status.DONE, MailingGuest.Status.ERROR]).exists()

    if not has_audience:
        recommended_step = 2
    elif has_dispatch_activity or has_send_results or mailing.is_active:
        recommended_step = 3
    else:
        recommended_step = 3

    tab_to_step = {
        "overview": recommended_step,
        "audience": 2,
        "runs": 3,
        "jobs": 3,
        "errors": 3,
        "logs": 3,
    }
    current_step = int(tab_to_step.get(active_tab or "overview", recommended_step))

    step1_url = reverse("mailings_v2_campaigns_edit", kwargs={"pk": mailing.pk})
    step2_url = reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.pk})
    step3_url = reverse("mailings_v2_campaigns_runs", kwargs={"pk": mailing.pk})

    if current_step <= 1:
        summary = "Шаг 1 из 3: проверьте шаблон, окна отправки, режим и выбранных ботов."
        cta_label = "Перейти к аудитории"
        cta_url = step2_url
    elif current_step == 2:
        summary = (
            f"Шаг 2 из 3: соберите аудиторию. Сейчас строк в кампании: {rows_total}."
            " Когда состав готов, переходите к запуску."
        )
        cta_label = "Перейти к запуску"
        cta_url = step3_url
    else:
        summary = (
            "Шаг 3 из 3: выполните dry-run, проверьте задания/ошибки и запустите кампанию."
        )
        cta_label = "Открыть экран запусков"
        cta_url = step3_url

    steps = []
    for number, title, url in (
        (1, "Параметры", step1_url),
        (2, "Аудитория", step2_url),
        (3, "Проверка и запуск", step3_url),
    ):
        if number < current_step:
            status = "done"
        elif number == current_step:
            status = "current"
        else:
            status = "todo"
        steps.append(
            {
                "number": number,
                "title": title,
                "status": status,
                "url": url,
            }
        )

    return {
        "current_step": current_step,
        "summary": summary,
        "cta_label": cta_label,
        "cta_url": cta_url,
        "steps": steps,
    }


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


def _build_mailing_log_timeline(
    *,
    rows: list[MailingGuest],
    tasks: list[DispatchTask],
) -> list[dict[str, object]]:
    """
    Собирает общий таймлайн изменений по строкам аудитории и dispatch-задачам.
    """
    timeline: list[dict[str, object]] = []

    for row in rows:
        event_time = row.sent_at or row.updated_at or row.created_at
        timeline.append(
            {
                "kind": "row",
                "event_time": event_time,
                "status": str(row.status),
                "phone": str(row.phone or (row.guest.phone if row.guest and row.guest.phone else "")),
                "title": f"Строка #{row.id}",
                "message": str(row.error_description or row.delivery_status or ""),
            }
        )

    for task in tasks:
        event_time = task.finished_at or task.started_at or task.enqueued_at or task.created_at
        timeline.append(
            {
                "kind": "dispatch",
                "event_time": event_time,
                "status": str(task.status),
                "phone": str(task.guest.phone) if task.guest and task.guest.phone else "",
                "title": f"Dispatch #{task.id}",
                "message": str(task.last_error or task.message_text or "")[:240],
            }
        )

    timeline.sort(key=lambda item: item["event_time"] or timezone.now(), reverse=True)
    return timeline[:120]
