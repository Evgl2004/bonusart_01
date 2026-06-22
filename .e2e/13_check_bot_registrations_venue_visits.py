from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.db.models import Count, DateTimeField, Max, Min, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from guests.models import DispatchTask, MailingGuest, OrderFact, VtelemaxRecipientChannel


def parse_date(name: str, default: str) -> date:
    raw_value = (os.environ.get(name) or default).strip()
    return datetime.strptime(raw_value, "%Y-%m-%d").date()


def parse_int(name: str, default: int) -> int:
    raw_value = (os.environ.get(name) or "").strip()
    if not raw_value:
        return default
    return int(raw_value)


def format_dt(value) -> str:
    if not value:
        return "-"
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def format_money(value) -> str:
    amount = value if isinstance(value, Decimal) else Decimal(value or 0)
    return f"{amount:.2f}"


def normalize_phone(value: str | None) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return digits


date_from = parse_date("CHECK_DATE_FROM", "2026-06-20")
date_to = parse_date("CHECK_DATE_TO", "2026-06-21")
venue_code = (os.environ.get("CHECK_VENUE_CODE") or "c9a0df27-11dc-4bee-83a3-f0a5aa16c185").strip()
venue_name = (os.environ.get("CHECK_VENUE_NAME") or "Сами Сусами").strip()
platforms = [
    value.strip().lower()
    for value in (os.environ.get("CHECK_PLATFORMS") or "telegram,max,vk").split(",")
    if value.strip()
]
mailing_id = parse_int("CHECK_MAILING_ID", 10)
sample_limit = parse_int("CHECK_SAMPLE_LIMIT", 50)

if date_from > date_to:
    raise SystemExit("CHECK_DATE_FROM не может быть позже CHECK_DATE_TO")

channels_with_registration_at = VtelemaxRecipientChannel.objects.annotate(
    registration_at=Coalesce("registered_at", "account_created_at", output_field=DateTimeField())
)

registered_any_qs = (
    channels_with_registration_at.filter(
        platform__in=platforms,
        is_registered=True,
        registration_at__date__gte=date_from,
        registration_at__date__lte=date_to,
    )
    .exclude(external_id__isnull=True)
    .exclude(external_id="")
)

registered_optin_qs = registered_any_qs.filter(notifications_allowed=True)

created_qs = VtelemaxRecipientChannel.objects.filter(
    platform__in=platforms,
    account_created_at__isnull=False,
    account_created_at__date__gte=date_from,
    account_created_at__date__lte=date_to,
)

candidate_channels = list(
    registered_any_qs.select_related("guest")
    .order_by("platform", "registration_at", "phone_e164", "id")
)
candidate_guest_ids = sorted({channel.guest_id for channel in candidate_channels if channel.guest_id})
optin_guest_ids = sorted({channel.guest_id for channel in candidate_channels if channel.guest_id and channel.notifications_allowed})

venue_orders_qs = OrderFact.objects.filter(
    business_date__gte=date_from,
    business_date__lte=date_to,
)
if venue_code:
    venue_orders_qs = venue_orders_qs.filter(department_id=venue_code)

candidate_orders_qs = venue_orders_qs.filter(guest_id__in=candidate_guest_ids)
candidate_order_rows = list(
    candidate_orders_qs.values("guest_id")
    .annotate(
        orders_count=Count("id"),
        revenue=Sum("net_sum"),
        first_order_date=Min("business_date"),
        last_order_date=Max("business_date"),
    )
    .order_by("guest_id")
)
orders_by_guest_id = {row["guest_id"]: row for row in candidate_order_rows}

order_dates_by_guest_id: dict[int, set[date]] = defaultdict(set)
for row in candidate_orders_qs.values("guest_id", "business_date"):
    if row["guest_id"] and row["business_date"]:
        order_dates_by_guest_id[row["guest_id"]].add(row["business_date"])

mailing_rows_by_guest_id: dict[int, MailingGuest] = {}
mailing_rows_by_phone: dict[str, MailingGuest] = {}
mailing_external_ids: set[str] = set()
dispatch_statuses_by_guest_id: dict[int, Counter] = defaultdict(Counter)
if mailing_id:
    mailing_rows = list(MailingGuest.objects.filter(mailing_id=mailing_id))
    mailing_rows_by_guest_id = {row.guest_id: row for row in mailing_rows if row.guest_id}
    mailing_rows_by_phone = {
        normalize_phone(row.phone): row
        for row in mailing_rows
        if normalize_phone(row.phone)
    }
    mailing_external_ids = {
        str(row.external_id).strip()
        for row in mailing_rows
        if str(row.external_id or "").strip()
    }
    for row in (
        DispatchTask.objects.filter(mailing_guest__mailing_id=mailing_id, guest_id__in=candidate_guest_ids)
        .values("guest_id", "status")
        .annotate(total=Count("id"))
    ):
        if row["guest_id"]:
            dispatch_statuses_by_guest_id[row["guest_id"]][row["status"]] += int(row["total"] or 0)

candidate_guest_ids_with_visit = set(orders_by_guest_id)
same_day_visit_guest_ids = set()
for channel in candidate_channels:
    if not channel.guest_id or not channel.registration_at:
        continue
    registration_day = timezone.localdate(channel.registration_at)
    if registration_day in order_dates_by_guest_id.get(channel.guest_id, set()):
        same_day_visit_guest_ids.add(channel.guest_id)

print("=== Параметры проверки ===")
print(f"Период регистрации/согласия: {date_from.isoformat()} — {date_to.isoformat()}")
print(f"Платформы: {', '.join(platforms)}")
print(f"Заведение для проверки заказов: {venue_name} ({venue_code or 'без фильтра по заведению'})")
print(f"Кампания для связи с рассылкой: {mailing_id or '-'}")
print()

print("=== Как дашборд считает прирост по ботам ===")
print("Всего за день = дата создания канала account_created_at.")
print("С согласием за день = registered_at, если он есть; иначе account_created_at.")
print("В этой проверке строка 'зарегистрированы' = is_registered=True без требования согласия.")
print()

print("=== Прирост каналов по дате создания ===")
created_daily = (
    created_qs.annotate(day=TruncDate("account_created_at"))
    .values("day", "platform")
    .annotate(channels=Count("id"), guests=Count("guest_id", distinct=True))
    .order_by("day", "platform")
)
if created_daily:
    for row in created_daily:
        print(
            f"{row['day']} {row['platform']}: "
            f"каналов={int(row['channels'] or 0)} гостей={int(row['guests'] or 0)}"
        )
else:
    print("Нет каналов, созданных в выбранный период.")
print()

print("=== Прирост зарегистрированных каналов ===")
registered_daily = (
    registered_any_qs.annotate(day=TruncDate("registration_at"))
    .values("day", "platform")
    .annotate(
        channels=Count("id"),
        persons=Count("person_id", distinct=True),
        guests=Count("guest_id", distinct=True),
    )
    .order_by("day", "platform")
)
if registered_daily:
    for row in registered_daily:
        print(
            f"{row['day']} {row['platform']}: "
            f"каналов={int(row['channels'] or 0)} "
            f"персон={int(row['persons'] or 0)} "
            f"гостей={int(row['guests'] or 0)}"
        )
else:
    print("Нет зарегистрированных каналов в выбранный период.")
print()

print("=== Из них зарегистрированные с согласием на рассылку ===")
optin_daily = (
    registered_optin_qs.annotate(day=TruncDate("registration_at"))
    .values("day", "platform")
    .annotate(
        channels=Count("id"),
        persons=Count("person_id", distinct=True),
        guests=Count("guest_id", distinct=True),
    )
    .order_by("day", "platform")
)
if optin_daily:
    for row in optin_daily:
        print(
            f"{row['day']} {row['platform']}: "
            f"каналов={int(row['channels'] or 0)} "
            f"персон={int(row['persons'] or 0)} "
            f"гостей={int(row['guests'] or 0)}"
        )
else:
    print("Нет зарегистрированных каналов с согласием в выбранный период.")
print()

print("=== Сводка зарегистрированных гостей с согласием ===")
print(f"Каналов: {len(candidate_channels)}")
print(f"Уникальных гостей с привязкой к локальной базе: {len(candidate_guest_ids)}")
print(f"Уникальных гостей с согласием: {len(optin_guest_ids)}")
print(f"Уникальных гостей без согласия: {len(set(candidate_guest_ids) - set(optin_guest_ids))}")
print(f"Каналов без связи с локальным гостем: {sum(1 for channel in candidate_channels if not channel.guest_id)}")
print()

venue_total = venue_orders_qs.aggregate(
    orders_count=Count("id"),
    guests_count=Count("guest_id", distinct=True),
    revenue=Sum("net_sum"),
)
print("=== Заказы выбранного заведения за этот же период ===")
print(f"Всего заказов: {int(venue_total['orders_count'] or 0)}")
print(f"Уникальных гостей в заказах: {int(venue_total['guests_count'] or 0)}")
print(f"Выручка нетто: {format_money(venue_total['revenue'])}")
print()

print("=== Пересечение регистрации и заказов в заведении ===")
optin_guest_ids_with_visit = set(optin_guest_ids) & candidate_guest_ids_with_visit
print(f"Зарегистрированных гостей, которые были в заведении в период: {len(candidate_guest_ids_with_visit)}")
print(f"Из них с согласием на рассылку: {len(optin_guest_ids_with_visit)}")
print(f"Гостей, у которых заказ был в день регистрации: {len(same_day_visit_guest_ids)}")
if candidate_guest_ids:
    share = len(candidate_guest_ids_with_visit) * 100 / len(candidate_guest_ids)
    print(f"Доля гостей с заказом в заведении: {share:.1f}%")
else:
    print("Доля гостей с заказом в заведении: 0.0%")
print()

if mailing_id:
    print(f"=== Связь с рассылкой #{mailing_id} ===")
    candidate_phone_keys = {
        normalize_phone(getattr(channel.guest, "phone", None) or channel.phone_e164)
        for channel in candidate_channels
    }
    candidate_external_ids = {str(channel.external_id or "").strip() for channel in candidate_channels if channel.external_id}
    by_guest_count = len(set(candidate_guest_ids) & set(mailing_rows_by_guest_id))
    by_phone_count = len({key for key in candidate_phone_keys if key in mailing_rows_by_phone})
    by_external_id_count = len(candidate_external_ids & mailing_external_ids)
    print(f"Гостей из регистраций, найденных в аудитории рассылки по guest_id: {by_guest_count}")
    print(f"Гостей из регистраций, найденных в аудитории рассылки по телефону: {by_phone_count}")
    print(f"Каналов из регистраций, найденных в файле рассылки по Telegram ID: {by_external_id_count}")
    mailing_rows_for_candidates = [
        row
        for row in mailing_rows_by_guest_id.values()
        if row.guest_id in candidate_guest_ids
    ]
    mailing_statuses = Counter(row.status for row in mailing_rows_for_candidates)
    delivery_statuses = Counter((row.delivery_status or "-") for row in mailing_rows_for_candidates)
    print(f"Статусы строк рассылки: {dict(mailing_statuses)}")
    print(f"Статусы доставки строк: {dict(delivery_statuses)}")
    sent_before_registration = 0
    for channel in candidate_channels:
        mailing_row = mailing_rows_by_guest_id.get(channel.guest_id)
        if mailing_row and mailing_row.sent_at and channel.registration_at and mailing_row.sent_at <= channel.registration_at:
            sent_before_registration += 1
    print(
        "Каналов, где отправка рассылки была раньше регистрации/согласия: "
        f"{sent_before_registration}"
    )
    print()

print("=== Список для сверки: зарегистрировались в ботах и были в заведении ===")
matched_channels = [
    channel
    for channel in candidate_channels
    if channel.guest_id in candidate_guest_ids_with_visit
]
if matched_channels:
    for channel in matched_channels[:sample_limit]:
        order_info = orders_by_guest_id.get(channel.guest_id) or {}
        mailing_row = mailing_rows_by_guest_id.get(channel.guest_id)
        dispatch_counter = dispatch_statuses_by_guest_id.get(channel.guest_id, Counter())
        guest_phone = getattr(channel.guest, "phone", None) or channel.phone_e164 or "-"
        by_phone_row = mailing_rows_by_phone.get(normalize_phone(guest_phone))
        registration_day = timezone.localdate(channel.registration_at) if channel.registration_at else None
        same_day = "да" if registration_day in order_dates_by_guest_id.get(channel.guest_id, set()) else "нет"
        in_file_by_guest = "да" if channel.guest_id in mailing_rows_by_guest_id else "нет"
        in_file_by_phone = "да" if by_phone_row else "нет"
        in_file_by_external_id = "да" if str(channel.external_id or "").strip() in mailing_external_ids else "нет"
        print(
            f"guest_id={channel.guest_id} phone={guest_phone} platform={channel.platform} "
            f"registered_at={format_dt(channel.registration_at)} "
            f"consent={'да' if channel.notifications_allowed else 'нет'} "
            f"orders={int(order_info.get('orders_count') or 0)} "
            f"revenue={format_money(order_info.get('revenue'))} "
            f"first_order={order_info.get('first_order_date') or '-'} "
            f"last_order={order_info.get('last_order_date') or '-'} "
            f"same_day={same_day} "
            f"in_legacy_file_by_guest={in_file_by_guest} "
            f"in_legacy_file_by_phone={in_file_by_phone} "
            f"in_legacy_file_by_telegram_id={in_file_by_external_id} "
            f"mailing_status={(mailing_row.status if mailing_row else '-')} "
            f"delivery_status={(mailing_row.delivery_status if mailing_row else '-')} "
            f"dispatch={dict(dispatch_counter)}"
        )
else:
    print("Совпадений не найдено.")
print()

print("=== Список для сверки: зарегистрировались в ботах, но заказа в заведении нет ===")
not_matched_channels = [
    channel
    for channel in candidate_channels
    if channel.guest_id and channel.guest_id not in candidate_guest_ids_with_visit
]
if not_matched_channels:
    for channel in not_matched_channels[:sample_limit]:
        mailing_row = mailing_rows_by_guest_id.get(channel.guest_id)
        guest_phone = getattr(channel.guest, "phone", None) or channel.phone_e164 or "-"
        by_phone_row = mailing_rows_by_phone.get(normalize_phone(guest_phone))
        in_file_by_guest = "да" if channel.guest_id in mailing_rows_by_guest_id else "нет"
        in_file_by_phone = "да" if by_phone_row else "нет"
        in_file_by_external_id = "да" if str(channel.external_id or "").strip() in mailing_external_ids else "нет"
        print(
            f"guest_id={channel.guest_id} phone={guest_phone} platform={channel.platform} "
            f"registered_at={format_dt(channel.registration_at)} "
            f"consent={'да' if channel.notifications_allowed else 'нет'} "
            f"in_legacy_file_by_guest={in_file_by_guest} "
            f"in_legacy_file_by_phone={in_file_by_phone} "
            f"in_legacy_file_by_telegram_id={in_file_by_external_id} "
            f"mailing_status={(mailing_row.status if mailing_row else '-')} "
            f"delivery_status={(mailing_row.delivery_status if mailing_row else '-')}"
        )
else:
    print("Нет таких гостей.")
