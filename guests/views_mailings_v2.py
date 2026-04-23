"""
Базовый каркас нового UI раздела рассылок (mailings-v2).

Задача этапа:
1. дать единую точку входа для маркетолога;
2. не ломать legacy формы, а использовать их как bridge;
3. показывать ключевые операционные метрики по текущему состоянию рассылок.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import CreateView, TemplateView, UpdateView

from guests.forms import MailingForm
from guests.models import DispatchTask, Mailing, MailingGuest, MessageTemplate, NotificationScenario


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

        campaigns_qs = (
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
            .order_by("-created_at")
        )

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
        context["kpi"] = {
            "campaigns_total": campaigns_qs.count(),
            "campaigns_active": campaigns_qs.filter(is_active=True).count(),
            "campaigns_recently_updated": campaigns_qs.filter(updated_at__gte=recently_updated_threshold).count(),
            "templates_active": MessageTemplate.objects.filter(is_active=True).count(),
            "scenarios_active": NotificationScenario.objects.filter(is_active=True).count(),
            "dispatch_total": int(dispatch_stats.get("total") or 0),
            "dispatch_pending": int(dispatch_stats.get("pending") or 0)
            + int(dispatch_stats.get("queued") or 0)
            + int(dispatch_stats.get("in_progress") or 0),
            "dispatch_done": int(dispatch_stats.get("done") or 0),
            "dispatch_failed": int(dispatch_stats.get("failed") or 0),
        }

        context["campaigns"] = campaigns_qs[:25]
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
                "secondary_url": reverse("message_templates_create"),
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
            context["mailing_import_report"] = self.request.session.pop("mailing_import_report", None)
            context["mailing_import_error"] = self.request.session.pop("mailing_import_error", None)
        else:
            context["guests_count"] = 0
            context["legacy_edit_url"] = ""
            context["audience_url"] = ""
            context["mailing_import_report"] = None
            context["mailing_import_error"] = None
        return context


class MailingsV2CampaignCreateView(_MailingsV2CampaignFormMixin, CreateView):
    """
    Создание кампании в новом UI.

    Логика сохранения соответствует текущей legacy-форме.
    """

    def form_valid(self, form):
        self.object = form.save(commit=False)
        self.object.is_active = False

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
        return context


class MailingsV2TemplatesView(TemplateView):
    """
    Каркас раздела шаблонов в новом контуре.

    На этапе bridge используется текущий backend CRUD шаблонов.
    """

    template_name = "mailing_v2/templates_hub.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        template_rows = (
            MessageTemplate.objects.annotate(
                mailings_total=Count("mailings", distinct=True),
                scenarios_total=Count("notification_scenarios", distinct=True),
            )
            .order_by("-updated_at")
        )
        context["templates_total"] = template_rows.count()
        context["templates_active"] = template_rows.filter(is_active=True).count()
        context["templates"] = template_rows[:50]
        return context


class MailingsV2MonitorView(TemplateView):
    """
    Каркас мониторинга задач доставки.

    Показывает агрегаты из DispatchTask по текущему состоянию очереди.
    """

    template_name = "mailing_v2/monitor_hub.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scope = DispatchTask.objects.filter(
            Q(mailing_guest__isnull=False) | Q(notification_scenario__isnull=False)
        )
        status_rows = scope.values("status").annotate(total=Count("id")).order_by("status")
        provider_rows = scope.values("provider_type").annotate(total=Count("id")).order_by("-total")
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
