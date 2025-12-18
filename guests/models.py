from django.db import models
import uuid


class Guest(models.Model):
    id = models.BigAutoField(primary_key=True)
    iiko_id = models.CharField(max_length=50, blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    first_name = models.CharField(max_length=150, blank=True, null=True)
    last_name = models.CharField(max_length=150, blank=True, null=True)
    email = models.CharField(max_length=150, blank=True, null=True)
    birthdate = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    is_stop_sending = models.BooleanField(default=False)
    gender= models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        managed = False  # таблица уже создана в БД
        db_table = "guests"

    def __str__(self):
        return f"{self.first_name or ''} {self.last_name or ''} ({self.phone})"

    # === ВАЖНО: вычисляемые поля для последнего визита ===
    @property
    def last_visit(self):
        """Объект последнего визита (VisitHistory) или None."""
        return (
            self.visits
            .select_related("restaurant")
            .order_by("-visit_date")
            .first()
        )

    @property
    def last_visit_date(self):
        """Дата/время последнего посещения любого заведения."""
        v = self.last_visit
        return v.visit_date if v else None


class Restaurant(models.Model):
    id = models.BigAutoField(primary_key=True)
    iiko_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    name = models.CharField(max_length=255)

    class Meta:
        managed = False
        db_table = "restaurants"

    def __str__(self):
        return self.name


class VisitHistory(models.Model):
    id = models.BigAutoField(primary_key=True)
    guest = models.ForeignKey(
        Guest,
        on_delete=models.CASCADE,
        related_name="visits",      # guest.visits -> все посещения гостя
    )
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="visits",
    )
    visit_date = models.DateTimeField()
    visit_count = models.IntegerField(default=1)

    class Meta:
        managed = False
        db_table = "visit_history"
        unique_together = ("guest", "restaurant")

    def __str__(self):
        return f"{self.guest} -> {self.restaurant} @ {self.visit_date}"
# --- Category (категория) ---
class Category(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=255)
    description = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    # ВАЖНО: теперь даты выставляются автоматически
    created_at = models.DateTimeField(auto_now_add=True)  # была: blank=True, null=True
    updated_at = models.DateTimeField(auto_now=True)  # была: blank=True, null=True

    external_id = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        managed = False
        db_table = "categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # если external_id не задан — сгенерируем автоматически
        if not self.external_id:
            self.external_id = uuid.uuid4().hex  # 32 символа, влезает в max_length=50
        super().save(*args, **kwargs)



# --- GuestCategory (связующая таблица many-to-many) ---
class GuestCategory(models.Model):
    id = models.BigAutoField(primary_key=True)
    guest = models.ForeignKey("Guest", on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    last_assigned_at = models.DateTimeField(null=True, blank=True)
    assign_count = models.IntegerField(default=1)

    class Meta:
        managed = False
        db_table = "guest_categories"
        unique_together = ("guest", "category")

    def __str__(self):
        return f"{self.guest} -> {self.category}"

class GuestCategoryAssignment(models.Model):
    guest = models.ForeignKey("Guest", on_delete=models.CASCADE, db_column="guest_id")
    category = models.ForeignKey("Category", on_delete=models.CASCADE, db_column="category_id")
    restaurant = models.ForeignKey("Restaurant", null=True, on_delete=models.SET_NULL, db_column="restaurant_id")
    assigned_at = models.DateTimeField()

    class Meta:
        db_table = "guest_category_assignments"
        managed = False

