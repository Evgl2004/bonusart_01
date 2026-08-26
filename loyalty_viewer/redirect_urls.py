"""Закрытый перечень маршрутов отдельной службы переходов."""

from django.urls import path

from guests.views_tracked_links import tracked_link_health, tracked_link_redirect


urlpatterns = [
    path(
        "r/v1/<str:public_token>",
        tracked_link_redirect,
        name="tracked_link_redirect",
    ),
    path(
        "internal/health",
        tracked_link_health,
        name="tracked_link_health",
    ),
]
