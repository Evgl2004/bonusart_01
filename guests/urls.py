from django.urls import path
from . import views
from .views_mailings_logs import MailingLogsView, MailingLogsDownloadTxtView
from .views_mailings_import import MailingImportPhonesView ,MailingImportTemplateDownloadView

from .views import (
    GuestListView,
    CategoryListView,
    CategoryUpdateView,
    CategoryDeleteView,
    GuestDetailView,

    TemplatesListView,
    MessageTemplateCreateView,
    MessageTemplateDetailView,
    MessageTemplateUpdateView,
    #MailingCreateFromGuestsView,
    MailingCreateView,
    MailingsListView,
    MailingUpdateView,

)
from .views_analytics import AnalyticsDashboardView

urlpatterns = [
    # главная страница сайта: список гостей
    path("", GuestListView.as_view(), name="home"),

    # явный URL для гостей (тоже список гостей)
    path("guests/", GuestListView.as_view(), name="guests"),
    path("guests/<int:pk>/", GuestDetailView.as_view(), name="guest_detail"),

    # категории гостей
    path("categories/", CategoryListView.as_view(), name="categories"),
    path("categories/<int:pk>/edit/", CategoryUpdateView.as_view(), name="category_edit"),
    path("categories/<int:pk>/delete/", CategoryDeleteView.as_view(), name="category_delete"),

    #  рассылки
    #path("mailings/", MailingsView.as_view(), name="mailings"),
    path("templates/", TemplatesListView.as_view(), name="message_templates"),
    path("templates/create/", MessageTemplateCreateView.as_view(), name="message_templates_create"),
    path("templates/<int:pk>/", MessageTemplateDetailView.as_view(), name="message_templates_detail"),
    path("templates/<int:pk>/edit/", MessageTemplateUpdateView.as_view(), name="message_templates_edit"),

    #path("mailings/create-from-guests/",MailingCreateFromGuestsView.as_view(),name="mailing_create_from_guests"),
    #path("mailings/create/", MailingCreateView.as_view(), name="mailing_create"),
    path("mailings/", MailingsListView.as_view(), name="mailings"),
    path("mailings/create/", MailingCreateView.as_view(), name="mailings_create"),
    path("mailings/<int:pk>/edit/", MailingUpdateView.as_view(), name="mailing_edit"),
    path("mailings/<int:pk>/toggle/", views.mailing_toggle_active, name="mailing_toggle_active"),
    #path("mailings/create-from-filter/", views.MailingCreateFromFilterView.as_view(), name="mailing_create_from_filter"),

    path("mailings/<int:pk>/logs/", MailingLogsView.as_view(), name="mailing_logs"),
    path("mailings/<int:pk>/logs.txt", MailingLogsDownloadTxtView.as_view(), name="mailing_logs_txt"),

    path("mailings/<int:pk>/import-phones/", MailingImportPhonesView.as_view(), name="mailing_import_phones"),
    path("mailings/import-template.xlsx", MailingImportTemplateDownloadView.as_view(), name="mailing_import_template"),
    path("analytics/", AnalyticsDashboardView.as_view(), name="analytics_dashboard"),

]
