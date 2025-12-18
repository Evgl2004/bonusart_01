from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    # админка
    path("admin/", admin.site.urls),

    # все URL'ы из приложения guests
    # /  , /guests/ , /categories/ и т.д.
    path("", include("guests.urls")),
]
