from django.urls import path

from . import views
from .views import (
    CategoryDeleteView,
    CategoryListView,
    CategoryUpdateView,
    GuestDetailView,
    GuestListView,
    MailingsListView,
    MailingCreateView,
    MailingUpdateView,
    MessageTemplateCreateView,
    MessageTemplateDetailView,
    MessageTemplateUpdateView,
    TemplatesListView,
)
from .views_analytics import AnalyticsDashboardView
from .views_mailings_import import MailingImportPhonesView, MailingImportTemplateDownloadView
from .views_mailings_logs import MailingLogsDownloadTxtView, MailingLogsView
from .views_guest_workbench import GuestsWorkbenchView
from .views_navigation import (
    FocusCategoriesWorkbenchView,
    ReportsWorkbenchView,
    SegmentsWorkbenchView,
)


urlpatterns = [
    # Главная страница пока остается списком гостей для совместимости.
    path("", GuestListView.as_view(), name="home"),

    # Новая навигация (пошаговое внедрение интерфейса).
    path("dashboard/", AnalyticsDashboardView.as_view(), name="dashboard"),
    path("segments/", SegmentsWorkbenchView.as_view(), name="segments"),
    path("focus-categories/", FocusCategoriesWorkbenchView.as_view(), name="focus_categories"),
    path("reports/", ReportsWorkbenchView.as_view(), name="reports"),

    # Гости.
    path("guests/workbench/", GuestsWorkbenchView.as_view(), name="guests_workbench"),
    path("guests/", GuestListView.as_view(), name="guests"),
    path("guests/<int:pk>/", GuestDetailView.as_view(), name="guest_detail"),

    # Legacy-раздел категорий: оставляем рабочим по прямому URL.
    path("categories/", CategoryListView.as_view(), name="categories"),
    path("categories/<int:pk>/edit/", CategoryUpdateView.as_view(), name="category_edit"),
    path("categories/<int:pk>/delete/", CategoryDeleteView.as_view(), name="category_delete"),

    # Шаблоны сообщений.
    path("templates/", TemplatesListView.as_view(), name="message_templates"),
    path("templates/create/", MessageTemplateCreateView.as_view(), name="message_templates_create"),
    path("templates/<int:pk>/", MessageTemplateDetailView.as_view(), name="message_templates_detail"),
    path("templates/<int:pk>/edit/", MessageTemplateUpdateView.as_view(), name="message_templates_edit"),

    # Рассылки.
    path("mailings/", MailingsListView.as_view(), name="mailings"),
    path("mailings/create/", MailingCreateView.as_view(), name="mailings_create"),
    path("mailings/<int:pk>/edit/", MailingUpdateView.as_view(), name="mailing_edit"),
    path("mailings/<int:pk>/toggle/", views.mailing_toggle_active, name="mailing_toggle_active"),
    path("mailings/<int:pk>/logs/", MailingLogsView.as_view(), name="mailing_logs"),
    path("mailings/<int:pk>/logs.txt", MailingLogsDownloadTxtView.as_view(), name="mailing_logs_txt"),
    path("mailings/<int:pk>/import-phones/", MailingImportPhonesView.as_view(), name="mailing_import_phones"),
    path("mailings/import-template.xlsx", MailingImportTemplateDownloadView.as_view(), name="mailing_import_template"),

    # Legacy URL дашборда (историческая ссылка).
    path("analytics/", AnalyticsDashboardView.as_view(), name="analytics_dashboard"),
]
