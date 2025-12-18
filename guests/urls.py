from django.urls import path
from .views import (
    GuestListView,
    CategoryListView,
    CategoryUpdateView,
    CategoryDeleteView,
    GuestDetailView,
    # если появится список рассылок, то добавишь:
    MailingListView,
)

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
    path("mailings/", MailingListView.as_view(), name="mailings"),
]
