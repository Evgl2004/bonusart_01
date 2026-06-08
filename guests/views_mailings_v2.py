"""
Базовый каркас нового UI раздела рассылок (mailings-v2).

Задача этапа:
1. дать единую точку входа для маркетолога;
2. не ломать legacy формы, а использовать их как bridge;
3. показывать ключевые операционные метрики по текущему состоянию рассылок.
"""

from __future__ import annotations

import logging
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
    CouponAutomationConfig,
    CouponAutoscenarioAssignment,
    DispatchTask,
    Guest,
    GuestBotBinding,
    Mailing,
    MailingGuest,
    MessageTemplate,
    NotificationEvent,
    NotificationScenario,
)
from guests.services.template_render import render_message_for_guest
from guests.services.notification_handler_registry import (
    get_registered_schedule_scenario_codes,
    run_registered_schedule_scenarios,
)
from guests.services.coupon_campaign_reporting import build_coupon_campaign_performance_snapshot
from guests.services.coupon_campaign_lifecycle import CouponCampaignLifecycleService
from guests.services.coupon_autoscenarios import (
    CouponAutoscenarioPreviewError,
    build_coupon_autoscenario_execution_plan,
)

MAILINGS_V2_RUN_NOW_MAX_BATCHES = 5
logger = logging.getLogger(__name__)


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

        campaigns = list(campaigns_qs[:100])
        for campaign in campaigns:
            template_obj = getattr(campaign, "template", None)
            display_name, technical_name = _resolve_template_title(template_obj)
            campaign.template_display_name = display_name
            campaign.template_technical_name = technical_name

        context["campaigns_total_filtered"] = campaigns_qs.count()
        context["campaigns"] = campaigns
        context["filters"] = {
            "q": q,
            "only_active": only_active,
            "with_errors": with_errors,
            "show_archived": show_archived,
            "created_from": created_from_raw,
            "created_to": created_to_raw,
        }
        context["current_query_path"] = self.request.get_full_path()
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

    @staticmethod
    def _active_templates_queryset():
        """
        Базовый queryset активных шаблонов для формы кампании.
        """
        return MessageTemplate.objects.filter(is_active=True).order_by("-created_at")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # В v2 по умолчанию показываем только активные шаблоны.
        if "template" in form.fields:
            active_templates = list(self._active_templates_queryset())
            user_templates = [template_obj for template_obj in active_templates if not _is_system_template(template_obj)]
            selected_templates = user_templates if user_templates else active_templates

            selected_template_ids = [template_obj.pk for template_obj in selected_templates]
            template_qs = self._active_templates_queryset().filter(pk__in=selected_template_ids)

            self._only_system_templates_available = bool(active_templates) and not bool(user_templates)
            self._active_templates_count = len(active_templates)

            form.fields["template"].queryset = template_qs
            form.fields["template"].label_from_instance = lambda template_obj: _resolve_template_title(template_obj)[0]

        if "bot_profiles" in form.fields:
            active_bot_profiles_qs = BotProfile.objects.filter(is_active=True).order_by("name", "id")
            form.fields["bot_profiles"].queryset = active_bot_profiles_qs
            self._has_active_bot_profiles = active_bot_profiles_qs.exists()

        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = getattr(self, "object", None)
        context["is_create"] = not bool(mailing and mailing.pk)
        context["legacy_list_url"] = reverse("mailings")
        context["v2_list_url"] = reverse("mailings_v2_campaigns")
        context["only_system_templates_available"] = bool(getattr(self, "_only_system_templates_available", False))
        context["active_templates_count"] = int(getattr(self, "_active_templates_count", 0))
        context["has_active_bot_profiles"] = bool(getattr(self, "_has_active_bot_profiles", False))
        context["bot_profiles_admin_url"] = "/admin/guests/botprofile/"
        form = context.get("form")
        template_queryset = None
        if form is not None and "template" in form.fields:
            template_queryset = form.fields["template"].queryset
        context["template_texts_by_id"] = {
            str(template_obj.id): str(template_obj.message_text or "")
            for template_obj in (template_queryset or MessageTemplate.objects.none())
        }

        if mailing and mailing.pk:
            context["guests_count"] = mailing.guests_rows.count()
            context["campaign_active_tab"] = "params"
            context["status_url"] = reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.pk})
            context["legacy_edit_url"] = reverse("mailing_edit", kwargs={"pk": mailing.pk})
            context["audience_url"] = reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.pk})
            context["runs_url"] = reverse("mailings_v2_campaigns_runs", kwargs={"pk": mailing.pk})
            context["ops_url"] = reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.pk})
            snapshot = _get_workbench_snapshot(self.request, mailing.pk)
            context["workbench_snapshot"] = snapshot
            context["workbench_snapshot_url"] = _build_workbench_url_from_snapshot(snapshot) if snapshot else ""
            context["mailing_row_stats"] = _build_mailing_row_stats(mailing)
            context["dispatch_stats"] = _build_mailing_dispatch_stats(mailing)
        else:
            context["guests_count"] = 0
            context["campaign_active_tab"] = ""
            context["status_url"] = ""
            context["legacy_edit_url"] = ""
            context["audience_url"] = ""
            context["runs_url"] = ""
            context["ops_url"] = ""
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

    def get_initial(self):
        """
        Поддерживает prefill шаблона при переходе из раздела templates.
        """
        initial = super().get_initial()
        today = timezone.localdate()
        period_end = today + timedelta(days=14)
        initial.setdefault("scheduled_date", today.isoformat())
        initial.setdefault("scheduled_time_begin", f"{today.isoformat()}T00:00")
        initial.setdefault("scheduled_time_end", f"{period_end.isoformat()}T23:59")
        initial.setdefault("send_window_begin", "09:00")
        initial.setdefault("send_window_end", "21:00")

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


class MailingsV2CampaignStatusView(TemplateView):
    """
    Экран статуса и операционного управления кампанией.

    Сводит в одном месте:
    1. ключевые счётчики аудитории и dispatch;
    2. переходы к операционным экранам;
    3. управляющие действия (запуск/пауза/retry/dry-run/run-now).
    """

    template_name = "mailing_v2/campaign_status.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mailing = get_object_or_404(Mailing, pk=self.kwargs["pk"])
        context["mailing"] = mailing
        context["v2_list_url"] = reverse("mailings_v2_campaigns")
        context["campaign_active_tab"] = "status"
        context["audience_url"] = reverse("mailings_v2_campaigns_audience", kwargs={"pk": mailing.pk})
        context["runs_url"] = reverse("mailings_v2_campaigns_runs", kwargs={"pk": mailing.pk})
        context["jobs_url"] = reverse("mailings_v2_campaigns_jobs", kwargs={"pk": mailing.pk})
        context["errors_url"] = reverse("mailings_v2_campaigns_errors", kwargs={"pk": mailing.pk})
        context["logs_url"] = reverse("mailings_v2_campaigns_logs", kwargs={"pk": mailing.pk})
        context["ops_url"] = reverse("mailings_v2_campaigns_ops", kwargs={"pk": mailing.pk})
        context["legacy_logs_url"] = reverse("mailing_logs", kwargs={"pk": mailing.pk})
        context["legacy_logs_txt_url"] = reverse("mailing_logs_txt", kwargs={"pk": mailing.pk})
        context["coupon_report_url"] = (
            f"{reverse('reports_coupon_campaigns')}?{urlencode({'campaign_id': mailing.id})}"
        )
        context["guests_count"] = mailing.guests_rows.count()
        context["mailing_row_stats"] = _build_mailing_row_stats(mailing)
        context["dispatch_stats"] = _build_mailing_dispatch_stats(mailing)
        context["mailing_ops_dry_run_report"] = self.request.session.pop("mailing_ops_dry_run_report", None)
        context["mailing_ops_run_now_report"] = self.request.session.pop("mailing_ops_run_now_report", None)

        coupon_campaign_report = None
        coupon_campaign_report_error = ""
        if str(getattr(mailing, "coupon_series", "") or "").strip():
            try:
                coupon_campaign_report = build_coupon_campaign_performance_snapshot(
                    mailing=mailing
                ).to_dict()
            except Exception as err:  # noqa: BLE001
                logger.exception(
                    "Coupon campaign report build failed: campaign_id=%s error=%s",
                    mailing.id,
                    err,
                )
                coupon_campaign_report_error = (
                    "Не удалось построить купонный отчёт. Проверьте логи сервиса."
                )
        context["coupon_campaign_report"] = coupon_campaign_report
        context["coupon_campaign_report_error"] = coupon_campaign_report_error
        return context


class MailingsV2CampaignOpsView(View):
    """
    Операционные POST-действия для кампании в mailings-v2.

    Поддерживает:
    1. безопасный старт/пауза кампании;
    2. возврат error/in_progress строк в planned;
    3. ручной retry задач dispatch со статусом failed.
    4. безопасную отмену кампании с освобождением неотправленных купонов.
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
        status_url = reverse("mailings_v2_campaigns_status", kwargs={"pk": mailing.pk})

        if mailing.is_archived and action in {
            "toggle_active",
            "retry_failed_rows",
            "requeue_in_progress_rows",
            "retry_failed_dispatch",
            "dry_run_campaign",
            "run_now_campaign",
            "cancel_campaign",
        }:
            messages.error(request, "Архивная кампания недоступна для операционных действий.")
            return redirect(self._resolve_next_url(request, status_url))

        if action == "toggle_active":
            mailing.is_active = not bool(mailing.is_active)
            if hasattr(mailing, "updated_at"):
                mailing.updated_at = timezone.now()
            mailing.save(update_fields=["is_active"] + (["updated_at"] if hasattr(mailing, "updated_at") else []))
            if mailing.is_active:
                messages.success(request, f"Кампания #{mailing.id} запущена.")
            else:
                messages.success(request, f"Кампания #{mailing.id} поставлена на паузу.")
            return redirect(self._resolve_next_url(request, status_url))

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
            return redirect(self._resolve_next_url(request, status_url))

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
            return redirect(self._resolve_next_url(request, status_url))

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
            return redirect(self._resolve_next_url(request, status_url))

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
            return redirect(self._resolve_next_url(request, status_url))

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
            return redirect(self._resolve_next_url(request, status_url))

        if action == "cancel_campaign":
            if mailing.is_archived:
                messages.info(request, f"Кампания #{mailing.id} уже в архиве.")
                return redirect(self._resolve_next_url(request, status_url))

            lifecycle_service = CouponCampaignLifecycleService()
            stats = lifecycle_service.cancel_campaign(
                mailing=mailing,
                reason="campaign_canceled_by_operator",
                now=timezone.now(),
                dry_run=False,
            )
            payload = stats.to_dict()
            messages.success(
                request,
                (
                    f"Кампания #{mailing.id} остановлена. "
                    f"Строк отменено={payload['rows_canceled']}, "
                    f"dispatch отменено={payload['dispatch_tasks_canceled']}, "
                    f"купонов подготовлено к освобождению={payload.get('assignments_release_pending', 0)}."
                ),
            )
            return redirect(self._resolve_next_url(request, status_url))

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
                    coupon_series=mailing.coupon_series,
                    coupon_venue_code=mailing.coupon_venue_code,
                    coupon_venue_name=mailing.coupon_venue_name,
                    coupon_promo_text=mailing.coupon_promo_text,
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
        return redirect(self._resolve_next_url(request, status_url))


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
        rows = list(rows_qs[:300])
        for row in rows:
            row.status_label = _localize_mailing_row_status(row.status)
            row.delivery_status_label = _localize_delivery_status(row.delivery_status)
        context["mailing"] = mailing
        context["rows"] = rows
        context["stats"] = rows_qs.aggregate(
            total=Count("id"),
            planned=Count("id", filter=Q(status=MailingGuest.Status.PLANNED)),
            in_progress=Count("id", filter=Q(status=MailingGuest.Status.IN_PROGRESS)),
            done=Count("id", filter=Q(status=MailingGuest.Status.DONE)),
            error=Count("id", filter=Q(status=MailingGuest.Status.ERROR)),
        )
        context["mailing_import_report"] = self.request.session.pop("mailing_import_report", None)
        context["mailing_import_error"] = self.request.session.pop("mailing_import_error", None)
        snapshot = _get_workbench_snapshot(self.request, mailing.pk)
        context["workbench_snapshot"] = snapshot
        context["workbench_snapshot_url"] = _build_workbench_url_from_snapshot(snapshot) if snapshot else ""
        context["campaign_active_tab"] = "audience"
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
        template_kind = str(self.request.GET.get("template_kind") or "all").strip().lower()
        if template_kind not in {"all", "user", "system"}:
            template_kind = "all"

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

        system_filter_q = Q(created_by__iexact="system") | (
            Q(name__startswith="SYSTEM_") & Q(name__endswith="_TEMPLATE")
        )
        if template_kind == "system":
            template_rows = template_rows.filter(system_filter_q)
        elif template_kind == "user":
            template_rows = template_rows.exclude(system_filter_q)

        templates = list(template_rows[:100])
        for template_obj in templates:
            display_name, technical_name = _resolve_template_title(template_obj)
            template_obj.display_name = display_name
            template_obj.technical_name = technical_name
            template_obj.is_system_template = _is_system_template(template_obj)

        context["templates_total"] = MessageTemplate.objects.count()
        context["templates_active"] = MessageTemplate.objects.filter(is_active=True).count()
        context["templates"] = templates
        context["show_inactive"] = show_inactive
        context["query"] = query
        context["template_kind"] = template_kind
        return context


class MailingsV2TemplateCreateView(CreateView):
    """
    Создание шаблона в новом контуре.
    """

    model = MessageTemplate
    form_class = MessageTemplateForm
    template_name = "mailing_v2/template_form.html"

    @staticmethod
    def _build_new_template_preview_source() -> MessageTemplate:
        """
        Возвращает временный объект шаблона для предпросмотра на форме создания.
        """
        return MessageTemplate()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_guest_id = str(self.request.GET.get("guest_id") or "").strip()
        form = context.get("form")
        message_text_override = ""
        if form is not None:
            message_text_override = str(form.data.get("message_text") or form.initial.get("message_text") or "")

        context.update(
            _build_template_preview_state(
                template_obj=self._build_new_template_preview_source(),
                selected_guest_id=selected_guest_id,
                message_text_override=message_text_override,
            )
        )
        context["preview_requested"] = bool(selected_guest_id)
        return context

    def post(self, request, *args, **kwargs):
        if str(request.POST.get("action") or "").strip() == "preview":
            self.object = None
            form = self.get_form()
            selected_guest_id = str(request.POST.get("preview_guest_id") or "").strip()
            message_text_override = str(request.POST.get("message_text") or "")

            context = self.get_context_data(form=form)
            context["selected_guest_id"] = selected_guest_id
            context.update(
                _build_template_preview_state(
                    template_obj=self._build_new_template_preview_source(),
                    selected_guest_id=selected_guest_id,
                    message_text_override=message_text_override,
                )
            )
            context["preview_requested"] = True
            return self.render_to_response(context)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        obj = form.save(commit=False)
        # На этом этапе сохраняем совместимость с текущим backend-контрактом.
        obj.created_by = "mailings_v2_user"
        obj.save()
        messages.success(self.request, f"Шаблон создан (ID {obj.id}).")
        return redirect("mailings_v2_templates_edit", pk=obj.pk)


class MailingsV2TemplateDetailView(DetailView):
    """
    Детальная карточка шаблона с предпросмотром на госте.
    """

    model = MessageTemplate
    template_name = "mailing_v2/template_detail.html"
    context_object_name = "template_obj"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        display_name, technical_name = _resolve_template_title(self.object)
        context["template_display_name"] = display_name
        context["template_technical_name"] = technical_name
        context["template_is_system"] = _is_system_template(self.object)
        context["campaign_prefill_url"] = (
            f"{reverse('mailings_v2_campaigns_new')}?{urlencode({'template_id': self.object.id})}"
        )

        preview_context = _build_template_preview_state(
            template_obj=self.object,
            selected_guest_id=str(self.request.GET.get("guest_id") or "").strip(),
            message_text_override=self.object.message_text,
        )
        context.update(preview_context)
        return context


class MailingsV2TemplateUpdateView(UpdateView):
    """
    Редактирование шаблона в новом контуре.
    """

    model = MessageTemplate
    form_class = MessageTemplateForm
    template_name = "mailing_v2/template_form.html"

    def _build_editor_context(self) -> dict[str, object]:
        display_name, technical_name = _resolve_template_title(self.object)
        return {
            "template_display_name": display_name,
            "template_technical_name": technical_name,
            "template_is_system": _is_system_template(self.object),
            "campaign_prefill_url": f"{reverse('mailings_v2_campaigns_new')}?{urlencode({'template_id': self.object.id})}",
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self._build_editor_context())

        selected_guest_id = str(self.request.GET.get("guest_id") or "").strip()
        preview_context = _build_template_preview_state(
            template_obj=self.object,
            selected_guest_id=selected_guest_id,
            message_text_override=self.object.message_text,
        )
        context.update(preview_context)
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if str(request.POST.get("action") or "").strip() == "preview":
            form = self.get_form()
            selected_guest_id = str(request.POST.get("preview_guest_id") or "").strip()
            message_text_override = str(request.POST.get("message_text") or "")

            context = self.get_context_data(form=form, object=self.object)
            context.update(self._build_editor_context())
            context["selected_guest_id"] = selected_guest_id
            context.update(
                _build_template_preview_state(
                    template_obj=self.object,
                    selected_guest_id=selected_guest_id,
                    message_text_override=message_text_override,
                )
            )
            context["preview_requested"] = True
            return self.render_to_response(context)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, "Шаблон сохранен.")
        return redirect("mailings_v2_templates_edit", pk=self.object.pk)


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
                "action_label": "Перезапуск ошибочных задач",
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
                "action_label": "Повторная постановка ожидающих задач",
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
    def _parse_positive_int(raw_value: str, *, default: int, max_value: int) -> int:
        """
        Нормализует пользовательский лимит для безопасного предпросмотра.
        """
        try:
            parsed = int(str(raw_value or "").strip())
        except (TypeError, ValueError):
            return int(default)
        if parsed <= 0:
            return int(default)
        return min(parsed, max_value)

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
        selected_coupon_scenario_code = str(
            self.request.GET.get("coupon_scenario_code") or ""
        ).strip()
        coupon_scan_limit = self._parse_positive_int(
            self.request.GET.get("coupon_scan_limit"),
            default=5000,
            max_value=100000,
        )
        coupon_sample_limit = self._parse_positive_int(
            self.request.GET.get("coupon_sample_limit"),
            default=20,
            max_value=100,
        )

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

        scenarios = list(scenarios_scope[:200])
        for scenario in scenarios:
            template_obj = getattr(scenario, "template", None)
            display_name, technical_name = _resolve_template_title(template_obj)
            scenario.template_display_name = display_name
            scenario.template_technical_name = technical_name

        coupon_configs = list(
            CouponAutomationConfig.objects.select_related("scenario", "scenario__template")
            .annotate(
                runs_total=Count("runs", distinct=True),
                assignments_total=Count("assignments", distinct=True),
                assignments_sent=Count(
                    "assignments",
                    filter=Q(assignments__status=CouponAutoscenarioAssignment.Status.SENT),
                    distinct=True,
                ),
                assignments_used=Count(
                    "assignments",
                    filter=Q(
                        assignments__status__in=[
                            CouponAutoscenarioAssignment.Status.USED,
                            CouponAutoscenarioAssignment.Status.USED_AFTER_CAMPAIGN,
                        ]
                    ),
                    distinct=True,
                ),
                assignments_error=Count(
                    "assignments",
                    filter=Q(assignments__status=CouponAutoscenarioAssignment.Status.ERROR),
                    distinct=True,
                ),
                last_run_at=Max("runs__created_at"),
            )
            .order_by("scenario__code")[:100]
        )
        for config in coupon_configs:
            template_obj = getattr(config.scenario, "template", None)
            display_name, technical_name = _resolve_template_title(template_obj)
            config.template_display_name = display_name
            config.template_technical_name = technical_name

        coupon_plan = None
        coupon_plan_error = ""
        if selected_coupon_scenario_code:
            try:
                plan = build_coupon_autoscenario_execution_plan(
                    scenario_code=selected_coupon_scenario_code,
                    scan_limit=coupon_scan_limit,
                )
                coupon_plan = plan.as_dict()
                coupon_plan["sample_plan_items"] = coupon_plan.get("plan_items", [])[
                    :coupon_sample_limit
                ]
            except CouponAutoscenarioPreviewError as exc:
                coupon_plan_error = str(exc)

        context["scenarios"] = scenarios
        context["coupon_autoscenario_configs"] = coupon_configs
        context["coupon_execution_mode_choices"] = list(CouponAutomationConfig.ExecutionMode.choices)
        context["selected_coupon_scenario_code"] = selected_coupon_scenario_code
        context["coupon_scan_limit"] = coupon_scan_limit
        context["coupon_sample_limit"] = coupon_sample_limit
        context["coupon_plan"] = coupon_plan
        context["coupon_plan_error"] = coupon_plan_error
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
        return context


SYSTEM_TEMPLATE_NAME_MAP = {
    "SYSTEM_BALANCE_CHANGED_TEMPLATE": "Системный шаблон: изменение баланса",
    "SYSTEM_INACTIVE_7D_TEMPLATE": "Системный шаблон: неактивные 7 дней",
    "SYSTEM_INACTIVE_30D_COUPON_TEMPLATE": "Системный шаблон: неактивные 30 дней + купон",
    "SYSTEM_MEAT_LOVER_30D_TEMPLATE": "Системный шаблон: любитель мяса 30 дней",
}


def _is_system_template(template_obj: MessageTemplate | None) -> bool:
    """
    Определяет, относится ли шаблон к системным.

    На текущем этапе используем совместимый эвристический признак:
    1. `created_by == "system"`;
    2. техническое имя формата `SYSTEM_*_TEMPLATE`.
    """
    if template_obj is None:
        return False

    created_by = str(getattr(template_obj, "created_by", "") or "").strip().lower()
    if created_by == "system":
        return True

    raw_name = str(getattr(template_obj, "name", "") or "").strip().upper()
    return raw_name.startswith("SYSTEM_") and raw_name.endswith("_TEMPLATE")


def _resolve_template_title(template_obj: MessageTemplate | None) -> tuple[str, str]:
    """
    Возвращает пару названий шаблона:
    1. display_name — человеко-понятный заголовок;
    2. technical_name — техническое имя (если отличается от display).
    """
    if template_obj is None:
        return "", ""

    raw_name = str(getattr(template_obj, "name", "") or "").strip()
    if not raw_name:
        return "Шаблон без названия", ""

    mapped_name = SYSTEM_TEMPLATE_NAME_MAP.get(raw_name)
    if mapped_name:
        return mapped_name, raw_name

    if raw_name.startswith("SYSTEM_") and raw_name.endswith("_TEMPLATE"):
        normalized = raw_name.removeprefix("SYSTEM_").removesuffix("_TEMPLATE").strip("_")
        words = [w for w in normalized.split("_") if w]
        pretty_name = " ".join(word.capitalize() if not word.isdigit() else word for word in words)
        if pretty_name:
            return f"Системный шаблон: {pretty_name}", raw_name

    return raw_name, ""


def _build_guest_display_name(guest: Guest) -> str:
    """
    Возвращает компактное человеко-понятное имя гостя для селекторов.
    """
    first_name = str(getattr(guest, "first_name", "") or "").strip()
    last_name = str(getattr(guest, "last_name", "") or "").strip()
    fio = " ".join(part for part in [first_name, last_name] if part)
    if fio:
        return fio

    phone = str(getattr(guest, "phone", "") or "").strip()
    if phone:
        return phone

    return f"Гость #{guest.id}"


def _build_template_preview_context(*, template_obj: MessageTemplate, guest: Guest) -> dict[str, object]:
    """
    Формирует расширенный контекст предпросмотра шаблона.

    Приоритет:
    1. payload/coupon последнего NotificationEvent для связки guest+template;
    2. fallback-расчёт days_without_visits по последнему визиту гостя.
    """
    context: dict[str, object] = {}
    latest_event = None
    if getattr(template_obj, "pk", None):
        latest_event = (
            NotificationEvent.objects.filter(
                guest=guest,
                scenario__template=template_obj,
            )
            .order_by("-event_at", "-id")
            .first()
        )

    if latest_event and isinstance(latest_event.payload, dict):
        for key, value in latest_event.payload.items():
            if isinstance(key, str):
                context[key] = value

    if latest_event and latest_event.coupon_code and not context.get("coupon_code"):
        context["coupon_code"] = str(latest_event.coupon_code)

    if "days_without_visits" not in context:
        last_visit_date = getattr(guest, "last_visit_date", None)
        if last_visit_date is not None:
            if hasattr(last_visit_date, "date"):
                last_visit_date = last_visit_date.date()
            try:
                context["days_without_visits"] = max((timezone.localdate() - last_visit_date).days, 0)
            except Exception:
                context["days_without_visits"] = ""
        else:
            context["days_without_visits"] = ""

    if "coupon_code" not in context:
        context["coupon_code"] = ""

    return context


def _build_template_preview_state(
    *,
    template_obj: MessageTemplate,
    selected_guest_id: str,
    message_text_override: str | None,
) -> dict[str, object]:
    """
    Собирает состояние предпросмотра шаблона для detail/edit экрана.

    Возвращает:
    1. список гостей для выбора;
    2. текущий выбранный guest_id;
    3. итоговый предпросмотр текста;
    4. подпись выбранного гостя.
    """
    safe_selected_guest_id = str(selected_guest_id or "").strip()

    guests = list(Guest.objects.order_by("-updated_at", "-id")[:300])
    for guest in guests:
        guest.display_name = _build_guest_display_name(guest)

    selected_guest: Guest | None = None
    if safe_selected_guest_id.isdigit():
        selected_guest = next(
            (guest for guest in guests if guest.id == int(safe_selected_guest_id)),
            None,
        )
        if selected_guest is None:
            selected_guest = Guest.objects.filter(id=int(safe_selected_guest_id)).first()
            if selected_guest is not None:
                selected_guest.display_name = _build_guest_display_name(selected_guest)
                guests.insert(0, selected_guest)

    preview_text = ""
    preview_guest_display_name = ""
    if selected_guest is not None:
        preview_guest_display_name = str(
            getattr(selected_guest, "display_name", "") or _build_guest_display_name(selected_guest)
        )
        preview_context = _build_template_preview_context(template_obj=template_obj, guest=selected_guest)
        message_text = (
            message_text_override
            if message_text_override is not None
            else str(getattr(template_obj, "message_text", "") or "")
        )
        preview_text = render_message_for_guest(
            message_text,
            selected_guest,
            extra_context=preview_context,
        )

    return {
        "guests": guests,
        "selected_guest_id": safe_selected_guest_id,
        "preview_text": preview_text,
        "preview_guest_display_name": preview_guest_display_name,
    }


def _build_mailings_v2_flow(*, active_area: str) -> dict[str, object]:
    """
    Формирует единый bridge-флоу для экранов mailings-v2.

    Шаги:
    1. гипотеза и отбор в workbench;
    2. подготовка шаблона;
    3. настройка и запуск кампании;
    4. мониторинг результата и разбор проблем.
    """
    monitor_url = reverse("mailings_v2_monitor")
    scenarios_url = reverse("mailings_v2_scenarios")
    step4_url = scenarios_url if active_area == "scenarios" else monitor_url

    steps = [
        {
            "number": 1,
            "title": "Гипотеза в рабочем экране гостей",
            "description": "Соберите сегмент и создайте черновик рассылки из отбора.",
            "help": "Вы формируете бизнес-гипотезу в экране «Гости»: выбираете фильтры и сохраняете отбор в черновик рассылки.",
            "url": reverse("guests_workbench"),
            "cta": "Открыть экран «Гости»",
        },
        {
            "number": 2,
            "title": "Шаблон сообщения",
            "description": "Подготовьте текст и проверьте предпросмотр на реальном госте.",
            "help": "На этом шаге создаётся или редактируется текст сообщения, а также проверяется итоговый вид сообщения для конкретного гостя.",
            "url": reverse("mailings_v2_templates"),
            "cta": "Открыть шаблоны",
            "secondary_url": reverse("mailings_v2_templates_new"),
            "secondary_cta": "Создать шаблон",
        },
        {
            "number": 3,
            "title": "Кампания и запуск",
            "description": "Проверьте аудиторию, выполните dry-run и запускайте кампанию.",
            "help": "Здесь настраиваются параметры запуска, состав аудитории и операционные действия: dry-run, run-now, запуск/пауза и повторы.",
            "url": reverse("mailings_v2_campaigns_new"),
            "cta": "Создать кампанию",
        },
        {
            "number": 4,
            "title": "Мониторинг и обратная связь",
            "description": "Контролируйте dispatch, retry, ошибки и корректируйте следующий запуск.",
            "help": "В мониторинге вы видите статусы доставки, ошибки и результаты отправок, чтобы улучшать следующий запуск.",
            "url": step4_url,
            "cta": "Открыть мониторинг",
        },
    ]

    return {
        "title": "Маршрут маркетолога",
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


def _is_coupon_sync_gate_ack_wait_report(gate_report: dict[str, object]) -> bool:
    """
    Проверяет, что batch run-now остановился только из-за ожидания ACK купона от vtelemax.
    """
    if not bool(gate_report.get("coupon_mode")):
        return False
    if int(gate_report.get("rows_total") or 0) <= 0:
        return False
    if int(gate_report.get("rows_ready") or 0) > 0:
        return False
    if int(gate_report.get("rows_blocked") or 0) <= 0:
        return False

    global_blockers = gate_report.get("global_blockers") or []
    if global_blockers:
        return False

    issues_by_code = gate_report.get("issues_by_code") or {}
    if not isinstance(issues_by_code, dict) or not issues_by_code:
        return False

    soft_block_codes = {
        str(code)
        for code in getattr(mailing_worker_cmd, "COUPON_GATE_SOFT_BLOCK_CODES", set())
    }
    return all(str(code) in soft_block_codes for code in issues_by_code)


def _run_mailing_now(mailing: Mailing, now, max_batches: int) -> dict[str, object]:
    """
    Выполняет ограниченный one-shot запуск кампании через существующий producer-путь.
    """
    report_before = _build_mailing_dry_run_report(mailing=mailing, now=now)
    processed_rows_total = 0
    processed_batches = 0
    reached_batch_limit = False
    stopped_on_coupon_sync_gate_wait = False
    gate_reports: list[dict[str, object]] = []

    if report_before["send_window_open"] and report_before["ready_rows"] > 0:
        for _ in range(max_batches):
            gate_reports_before = len(gate_reports)
            processed = int(
                mailing_worker_cmd.process_one_mailing(
                    mailing=mailing,
                    now=now,
                    gate_reports_collector=gate_reports,
                )
                or 0
            )
            if processed <= 0:
                break
            processed_rows_total += processed
            processed_batches += 1
            new_gate_reports = gate_reports[gate_reports_before:]
            if new_gate_reports and all(
                _is_coupon_sync_gate_ack_wait_report(gate_report)
                for gate_report in new_gate_reports
            ):
                stopped_on_coupon_sync_gate_wait = True
                break
        if processed_batches >= max_batches and not stopped_on_coupon_sync_gate_wait:
            reached_batch_limit = True

    report_after = _build_mailing_dry_run_report(mailing=mailing, now=timezone.now())
    coupon_gate_blocked_reasons: dict[str, int] = {}
    coupon_gate_blocked_rows = 0
    for gate_report in gate_reports:
        coupon_gate_blocked_rows += int(gate_report.get("rows_blocked") or 0)
        issues = gate_report.get("issues") or []
        if isinstance(issues, list):
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                message = str(issue.get("message") or "").strip()
                reason = message or "Причина не указана"
                coupon_gate_blocked_reasons[reason] = int(coupon_gate_blocked_reasons.get(reason, 0) + 1)
        global_blockers = gate_report.get("global_blockers") or []
        if isinstance(global_blockers, list):
            for blocker in global_blockers:
                reason = str(blocker or "").strip()
                if not reason:
                    continue
                coupon_gate_blocked_reasons[reason] = int(coupon_gate_blocked_reasons.get(reason, 0) + 1)
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
        "stopped_on_coupon_sync_gate_wait": bool(stopped_on_coupon_sync_gate_wait),
        "coupon_mode": bool(getattr(mailing, "coupon_series", None)),
        "coupon_series": str(getattr(mailing, "coupon_series", "") or "").strip(),
        "coupon_venue_code": str(getattr(mailing, "coupon_venue_code", "") or "").strip(),
        "coupon_venue_name": str(getattr(mailing, "coupon_venue_name", "") or "").strip(),
        "coupon_gate_blocked_rows": coupon_gate_blocked_rows,
        "coupon_gate_blocked_reasons": coupon_gate_blocked_reasons,
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


_MAILING_ROW_STATUS_LABELS_RU: dict[str, str] = {
    MailingGuest.Status.PLANNED: "запланировано",
    MailingGuest.Status.IN_PROGRESS: "в обработке",
    MailingGuest.Status.DONE: "успешно",
    MailingGuest.Status.ERROR: "ошибка",
}

_DELIVERY_STATUS_LABELS_RU: dict[str, str] = {
    "pending": "ожидает",
    "queued": "в очереди",
    "in_progress": "в обработке",
    "done": "доставлено",
    "success": "доставлено",
    "delivered": "доставлено",
    "failed": "ошибка",
    "error": "ошибка",
    "dispatch_no_targets": "нет целей отправки",
    "dispatch_no_bot_profiles": "нет активных ботов",
    "dispatch_enqueue_error": "ошибка постановки в очередь",
    "dispatch_enqueue_exception": "исключение при постановке в очередь",
    "retry_requested": "запрошен повтор",
    "requeued_from_ui": "повторно поставлено из UI",
    "duplicated_from_campaign": "скопировано из исходной кампании",
}


def _localize_mailing_row_status(value: str | None) -> str:
    status = (str(value or "")).strip()
    if not status:
        return "—"
    return _MAILING_ROW_STATUS_LABELS_RU.get(status, status)


def _localize_delivery_status(value: str | None) -> str:
    status = (str(value or "")).strip()
    if not status:
        return "—"
    return _DELIVERY_STATUS_LABELS_RU.get(status, status)


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
        # У модели MailingGuest нет updated_at, поэтому используем доступные временные поля.
        event_time = row.sent_at or row.created_at or row.scheduled_datetime
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
