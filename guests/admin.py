from django.contrib import admin

from .models import Guest, Restaurant, VisitHistory, Category, GuestCategory

@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "iiko_id")

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "is_active")

@admin.register(VisitHistory)
class VisitHistoryAdmin(admin.ModelAdmin):
    list_display = ("id", "guest", "restaurant", "visit_date")

@admin.register(GuestCategory)
class GuestCategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "guest", "category")

