from datetime import datetime, timedelta

from django.utils import timezone
from django.views.generic import ListView, UpdateView, DeleteView, CreateView,TemplateView
from django.db.models import (
    Q, Count, Max, Case, When, Value, IntegerField, Sum
)
from django.shortcuts import redirect,get_object_or_404
from django.urls import reverse_lazy
from django.urls import reverse
from django.db import transaction
from django.views.generic import DetailView
from django.views.decorators.http import require_POST

from django.views import View
from django.contrib import messages

from .models import Guest, GuestCategory, GuestCategoryAssignment, Category, Restaurant, VisitHistory,MessageTemplate
from .models import Mailing, MailingGuest,MailingChannel, GuestChannelLink

from .forms import CategoryForm,MessageTemplateForm,MailingForm
from .services.template_render import render_message_for_guest


class GuestListView(ListView):
    model = Guest
    template_name = "guests/guest_list.html"
    context_object_name = "guests"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset()
        req = self.request

        # --- 1. Поиск по телефону, имени, email ---
        q = (req.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(phone__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(email__icontains=q)
            )

        # --- 2. Фильтр по заведению (можно комбинировать с другими) ---
        restaurant_id = req.GET.get("restaurant_id")
        try:
            restaurant_id = int(restaurant_id) if restaurant_id else None
        except (TypeError, ValueError):
            restaurant_id = None

        if restaurant_id:
            # оставляем только гостей с посещениями этого заведения
            qs = qs.filter(visits__restaurant_id=restaurant_id)

            # считаем общее количество посещений этого заведения
            qs = qs.annotate(
                visit_count=Sum("visits__visit_count")
            )

        # --- 3. Фильтр по категориям (одна или несколько, ГОСТЬ ДОЛЖЕН ИМЕТЬ ВСЕ) ---
        raw_ids = req.GET.getlist("cats")  # список id категорий из GET
        cat_ids: list[int] = []
        for x in raw_ids:
            try:
                if x:
                    cat_ids.append(int(x))
            except ValueError:
                pass

        cat_restaurant_id = req.GET.get("cat_restaurant_id")
        try:
            cat_restaurant_id = int(cat_restaurant_id) if cat_restaurant_id else None
        except (TypeError, ValueError):
            cat_restaurant_id = None

        if cat_ids:
            if cat_restaurant_id:
                guest_ids = (
                    GuestCategoryAssignment.objects
                    .filter(category_id__in=cat_ids, restaurant_id=cat_restaurant_id)
                    .values("guest_id")
                    .annotate(cnt=Count("category_id", distinct=True))
                    .filter(cnt=len(cat_ids))
                    .values_list("guest_id", flat=True)
                )
            else:
                guest_ids = (
                    GuestCategory.objects
                    .filter(category_id__in=cat_ids)
                    .values("guest_id")
                    .annotate(cnt=Count("category_id", distinct=True))
                    .filter(cnt=len(cat_ids))
                    .values_list("guest_id", flat=True)
                )
            qs = qs.filter(id__in=guest_ids)

        # --- 4. Датовые фильтры: зависят от f, но МОЖНО комбинировать с п.2 и п.3 ---
        f = (req.GET.get("f") or "").strip()

        # 4.1 Диапазон дат создания
        if f == "activity_range":
            df = req.GET.get("from")
            dt = req.GET.get("to")
            fmt = "%Y-%m-%d"

            try:
                start = datetime.strptime(df, fmt) if df else None
                end = datetime.strptime(dt, fmt) if dt else None

                if start and end:
                    # включаем конечную дату целиком (до полуночи следующего дня)
                    end = end + timedelta(days=1)
                    qs = qs.filter(created_at__gte=start, created_at__lt=end)
                elif start:
                    qs = qs.filter(created_at__gte=start)
                elif end:
                    qs = qs.filter(created_at__lt=end)
            except ValueError:
                # если ошибка парсинга дат — просто игнорируем фильтр
                pass

        # 4.2 За последние N дней
        elif f == "days_since":
            days = req.GET.get("days")
            try:
                days = int(days)
            except (TypeError, ValueError):
                days = None

            if days is not None and days >= 0:
                since = timezone.now() - timedelta(days=days)
                qs = qs.filter(created_at__gte=since)

        # === 5. Аннотация последнего визита (для сортировки и отображения) ===
        qs = qs.annotate(
            last_visit_sort=Max("visits__visit_date"),
        ).annotate(
            # флаг: есть ли вообще визиты у гостя
            has_visit=Case(
                When(last_visit_sort__isnull=False, then=Value(0)),  # есть визит
                default=Value(1),  # нет визитов
                output_field=IntegerField(),
            )
        )

        # --- 6. Сортировка ---
        allowed = {
            "id": "id",
            "phone": "phone",
            "first_name": "first_name",
            "last_name": "last_name",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "last_visit": "last_visit_sort",
        }

        # по умолчанию — сортировка по последнему визиту
        sort_key = req.GET.get("sort", "last_visit")
        direction = req.GET.get("dir", "desc")

        # особая логика для last_visit: сначала гости с визитами, потом без
        if sort_key == "last_visit":
            if direction == "asc":
                qs = qs.order_by("has_visit", "last_visit_sort")
            else:  # desc
                qs = qs.order_by("has_visit", "-last_visit_sort")
        else:
            sort_field = allowed.get(sort_key, "created_at")
            order = f"-{sort_field}" if direction == "desc" else sort_field
            qs = qs.order_by(order)

        # --- 7. Подгружаем связанные объекты заранее ---
        qs = qs.prefetch_related("visits__restaurant")

        return qs

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")

        # 1) ВЫБРАТЬ ВСЕХ (по текущим фильтрам)
        if action == "select_all":
            guests_qs = self.get_queryset()
            request.session["guests_select_all"] = True
            request.session["guests_selected_count"] = guests_qs.count()
            request.session.modified = True
            return self.get(request, *args, **kwargs)

        # 2) СНЯТЬ ВЫБОР
        elif action == "unselect_all":
            request.session["guests_select_all"] = False
            request.session["guests_selected_count"] = 0
            request.session.modified = True
            return self.get(request, *args, **kwargs)

        # 3) ДОБАВИТЬ В КАТЕГОРИЮ
        elif action == "add_to_category":

            category_id = request.POST.get("category_id")
            restaurant_id = request.POST.get("assign_restaurant_id")

            if not category_id:
                # категорию не выбрали
                return self.get(request, *args, **kwargs)

            try:
                restaurant_id = int(restaurant_id) if restaurant_id else None
            except (TypeError, ValueError):
                restaurant_id = None

            select_all = request.session.get("guests_select_all", False)

            if select_all:
                # режим "выбраны все" — берём всех гостей по текущим фильтрам
                guests_qs = self.get_queryset()
            else:
                # только чекбоксы с текущей страницы
                selected_ids = request.POST.getlist("selected_guests")
                if not selected_ids:
                    return self.get(request, *args, **kwargs)
                guests_qs = Guest.objects.filter(id__in=selected_ids)

            now = timezone.now()

            # создаём связи гость–категория; игнорируем дубли по unique_together
            with transaction.atomic():
                GuestCategory.objects.bulk_create(
                    [
                        GuestCategory(
                            guest_id=guest.id,
                            category_id=int(category_id),
                        )
                        for guest in guests_qs
                    ],
                    ignore_conflicts=True,
                )
                GuestCategoryAssignment.objects.bulk_create(
                    [
                        GuestCategoryAssignment(
                            guest_id=g.id,
                            category_id=int(category_id),
                            restaurant_id=restaurant_id,  # может быть None, если разрешите
                            assigned_at=now,
                        )
                        for g in guests_qs
                    ]
                )
         # 4) ДОБАВИТЬ В Рассылку
        elif action == "add_to_mailing":
            mailing_id = request.POST.get("mailing_id")
            if not mailing_id:
                return self.get(request, *args, **kwargs)

            try:
                mailing_id = int(mailing_id)
            except (TypeError, ValueError):
                return self.get(request, *args, **kwargs)

            mailing = get_object_or_404(Mailing, pk=mailing_id)

            select_all = request.session.get("guests_select_all", False)

            if select_all:
                guests_qs = self.get_queryset()
            else:
                selected_ids = request.POST.getlist("selected_guests")
                if not selected_ids:
                    return self.get(request, *args, **kwargs)
                guests_qs = Guest.objects.filter(id__in=selected_ids)

            now = timezone.now()

            # 1) Активные каналы рассылки
            active_channels = list(mailing.channels.filter(is_active=True))
            if not active_channels:
                # нечего слать — просто сбросим выбор
                request.session["guests_select_all"] = False
                request.session["guests_selected_count"] = 0
                request.session.modified = True
                return self.get(request, *args, **kwargs)

            # 2) Если есть TG-каналы — фильтруем гостей по GuestChannelLink
            tg_channel_ids = [
                ch.id for ch in active_channels
                if ch.channel_kind in (
                    MailingChannel.ChannelKind.PHONE_TELEGRAM,
                    MailingChannel.ChannelKind.PHONE_TELEGRAM_BOT,
                )
            ]

            if tg_channel_ids:
                # ВАЖНО: если select_all=True, guests_qs может быть "сложным" queryset’ом с фильтрами/аннотациями.
                # Поэтому берём список guest_id отдельным запросом (только ids).
                base_guest_ids = list(guests_qs.values_list("id", flat=True))

                if base_guest_ids:
                    eligible_guest_ids = (
                        GuestChannelLink.objects
                        .filter(
                            guest_id__in=base_guest_ids,
                            channel_id__in=tg_channel_ids,
                            is_active=True,
                            is_opt_in=True,
                            is_stop_sending=False,
                        )
                        .exclude(external_chat_id__isnull=True)
                        .exclude(external_chat_id="")
                        .values("guest_id")
                        .annotate(cnt=Count("channel_id", distinct=True))
                        .filter(cnt=len(tg_channel_ids))
                        .values_list("guest_id", flat=True)
                    )
                    guests_qs = Guest.objects.filter(id__in=eligible_guest_ids)
                else:
                    guests_qs = Guest.objects.none()

            # (опционально) если хочешь: для EMAIL-канала можно отфильтровать гостей без email
            # email_required = any(ch.channel_kind == MailingChannel.ChannelKind.EMAIL for ch in active_channels)
            # if email_required:
            #     guests_qs = guests_qs.exclude(email__isnull=True).exclude(email="")

            # 3) Создаём строки MailingGuest (как у тебя было)
            rows = []
            for g in guests_qs.only("id", "phone", "email", "first_name", "last_name"):
                text = render_message_for_guest(mailing.template.message_text, g)
                rows.append(MailingGuest(
                    mailing=mailing,
                    guest=g,
                    phone=g.phone,
                    email=g.email,
                    text_mailing_list=text,
                    scheduled_datetime=mailing.scheduled_time_begin,  # или now, как нужно
                    status=MailingGuest.Status.PLANNED,
                    created_at=now,
                ))

            with transaction.atomic():
                MailingGuest.objects.bulk_create(rows, ignore_conflicts=True, batch_size=1000)

            # после операции сбрасываем режим выбора
            request.session["guests_select_all"] = False
            request.session["guests_selected_count"] = 0
            request.session.modified = True

            return self.get(request, *args, **kwargs)

        # по умолчанию
        return self.get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET

        # --- флаг и количество выбранных гостей из сессии ---
        ctx["select_all"] = self.request.session.get("guests_select_all", False)
        ctx["selected_count"] = self.request.session.get("guests_selected_count", 0)

        # --- параметры фильтрации и поиска ---
        ctx["q"] = g.get("q", "")
        ctx["f"] = g.get("f", "")
        ctx["date_from"] = g.get("from", "")
        ctx["date_to"] = g.get("to", "")
        ctx["days"] = g.get("days", "")
        ctx["cat_restaurant_id"] = self.request.GET.get("cat_restaurant_id", "")

        # id выбранного заведения
        selected_restaurant_id = g.get("restaurant_id", "")
        ctx["selected_restaurant_id"] = selected_restaurant_id

        selected_restaurant = None
        if selected_restaurant_id:
            try:
                selected_restaurant = Restaurant.objects.get(
                    pk=selected_restaurant_id
                )
            except Restaurant.DoesNotExist:
                selected_restaurant = None

        ctx["selected_restaurant"] = selected_restaurant

        # выбранные категории для фильтра (список строковых id)
        filter_category_ids = g.getlist("cats")
        ctx["filter_category_ids"] = filter_category_ids

        # alias для пагинации (селекторы в шаблоне)
        ctx["cats_selected"] = filter_category_ids

        # --- текущая сортировка ---
        cur_sort = g.get("sort", "last_visit")
        cur_dir = g.get("dir", "desc")
        ctx["current_sort"] = cur_sort
        ctx["current_dir"] = cur_dir

        def next_dir(col):
            return "desc" if (cur_sort == col and cur_dir == "asc") else "asc"

        ctx["next_dir"] = {
            "id": next_dir("id"),
            "phone": next_dir("phone"),
            "first_name": next_dir("first_name"),
            "last_name": next_dir("last_name"),
            "created_at": next_dir("created_at"),
            "updated_at": next_dir("updated_at"),
            "last_visit": next_dir("last_visit"),
        }

        # --- подтягиваем категории только для гостей ТЕКУЩЕЙ страницы ---
        guests = ctx["object_list"]
        guest_ids = [guest.id for guest in guests]
        categories_map = {}

        if guest_ids:
            cats = (
                GuestCategory.objects
                .filter(guest_id__in=guest_ids)
                .select_related("category")
            )
            for row in cats:
                categories_map.setdefault(row.guest_id, []).append(row.category)

        for guest in guests:
            guest.categories_list = categories_map.get(guest.id, [])

        # список всех категорий для фильтра и для "Добавить в категорию"
        ctx["categories"] = Category.objects.filter(is_active=True).order_by("name")

        # список заведений для фильтра по посещениям
        ctx["restaurants"] = Restaurant.objects.order_by("name")

        qs_params = self.request.GET.copy()
        if "page" in qs_params:
            qs_params.pop("page")
        ctx["query_string"] = qs_params.urlencode()

        ctx["message_templates"] = MessageTemplate.objects.filter(is_active=True).order_by("-created_at")
        ctx["mailings"] = Mailing.objects.order_by("-created_at")[:200]
        return ctx


# ====== КАТЕГОРИИ ======
class CategoryListView(ListView):
    """
    Страница "Категории":
    - GET: список категорий + форма создания новой
    - POST: создание новой категории
    """
    model = Category
    template_name = "placeholders/categories.html"
    context_object_name = "categories"

    def get_queryset(self):
        return Category.objects.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["form"] = kwargs.get("form") or CategoryForm()
        return ctx

    def post(self, request, *args, **kwargs):
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("categories")  # имя url, см. ниже
        # если ошибка — покажем список + форму с ошибками
        self.object_list = self.get_queryset()
        context = self.get_context_data(form=form)
        return self.render_to_response(context)


class CategoryUpdateView(UpdateView):
    model = Category
    form_class = CategoryForm
    template_name = "placeholders/category_form.html"
    success_url = reverse_lazy("categories")


class CategoryDeleteView(DeleteView):
    model = Category
    template_name = "placeholders/category_confirm_delete.html"
    success_url = reverse_lazy("categories")

class GuestDetailView(DetailView):
    model = Guest
    template_name = "guests/guest_detail.html"
    context_object_name = "guest"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        guest = self.object

        # посещения (история)
        ctx["visits"] = (
            guest.visits
            .select_related("restaurant")
            .order_by("-visit_date")
        )

        # категории гостя
        ctx["guest_categories"] = (
            GuestCategory.objects
            .filter(guest=guest)
            .select_related("category")
            .annotate(
                cnt=Count("guest__visits")
            )
        )

        # (опционально) список активных категорий, если захотите добавлять/снимать прямо из карточки
        ctx["all_categories"] = Category.objects.filter(is_active=True).order_by("name")
        return ctx
class MailingsListView(ListView):
    model = Mailing
    template_name = "mailing/mailings.html"
    context_object_name = "mailings"
    paginate_by = 20

    def get_queryset(self):
        return (
            Mailing.objects
            .select_related("template")
            .order_by("-created_at")
        )
class TemplatesListView(ListView):
    model = MessageTemplate
    template_name = "mailing/mailing_templates.html"
    context_object_name = "templates"

    def get_queryset(self):
        qs = MessageTemplate.objects.order_by("-created_at")

        show_inactive = self.request.GET.get("show_inactive")
        if not show_inactive:
            qs = qs.filter(is_active=True)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["show_inactive"] = self.request.GET.get("show_inactive")
        return ctx
class MessageTemplateCreateView(CreateView):
    model = MessageTemplate
    form_class = MessageTemplateForm
    template_name = "mailing/message_template_form.html"
    success_url = reverse_lazy("message_templates")

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.created_by = "test_user"   # тестово
        obj.save()
        return super().form_valid(form)
class MessageTemplateDetailView(DetailView):
    model = MessageTemplate
    template_name = "mailing/message_template_detail.html"
    context_object_name = "t"
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        ctx["guests"] = Guest.objects.all()[:50]

        guest_id = self.request.GET.get("guest_id")
        if guest_id:
            guest = Guest.objects.get(pk=guest_id)
            ctx["preview_text"] = render_message_for_guest(self.object.message_text, guest)
            ctx["preview_guest"] = guest

        return ctx

class MessageTemplateUpdateView(UpdateView):
    model = MessageTemplate
    form_class = MessageTemplateForm
    template_name = "mailing/message_template_form.html"

    def get_success_url(self):
        return reverse_lazy("message_templates_detail", kwargs={"pk": self.object.pk})

class MailingCreateView(CreateView):
    model = Mailing
    form_class = MailingForm
    template_name = "mailing/mailing_form.html"
    success_url = reverse_lazy("mailings")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # чтобы показывать только активные шаблоны
        if "template" in form.fields:
            form.fields["template"].queryset = MessageTemplate.objects.filter(is_active=True).order_by("-created_at")
        return form

    def form_valid(self, form):
        self.object = form.save(commit=False)

        self.object.is_active = False

        now = timezone.now()

        # если created_at / updated_at не auto_now*
        if hasattr(self.object, "created_at") and not self.object.created_at:
            self.object.created_at = now

        if hasattr(self.object, "updated_at"):
            self.object.updated_at = now

        self.object.save()

        # 🔥 ВАЖНО — сохраняет channels (many-to-many)
        form.save_m2m()

        return redirect(self.success_url)


class MailingUpdateView(UpdateView):
    model = Mailing
    form_class = MailingForm
    template_name = "mailing/mailing_form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["guests_count"] = self.object.guests_rows.count()
        return ctx
    def form_valid(self, form):
        self.object = form.save(commit=False)

        # обновляем updated_at
        if hasattr(self.object, "updated_at"):
            self.object.updated_at = timezone.now()

        self.object.save()

        # 🔥 ВАЖНО — сохраняет выбранные каналы
        form.save_m2m()

        return redirect("mailings")

@require_POST
def mailing_toggle_active(request, pk):
    mailing = get_object_or_404(Mailing, pk=pk)

    # переключаем статус
    mailing.is_active = not mailing.is_active

    # если есть updated_at — обновим
    if hasattr(mailing, "updated_at"):
        mailing.updated_at = timezone.now()

    mailing.save(update_fields=["is_active"] + (["updated_at"] if hasattr(mailing, "updated_at") else []))

    # вернёмся на список рассылок
    return redirect(reverse("mailings"))