"""
Создание черновика обычной рассылки по legacy-гостям Telegram.

Назначение:
1. Собрать гостей, у которых есть старый Telegram-канал vtelemax.
2. Исключить гостей, которые уже прошли новую регистрацию в Telegram/ВК/Макс.
3. Создать обычную кампанию рассылки и строки получателей MailingGuest.

По умолчанию скрипт ничего не записывает в базу. Для создания черновика нужен
явный флаг окружения LEGACY_MAILING_CREATE=1.

Пример проверки без записи:
LEGACY_MAILING_TEMPLATE_ID=8 \
python manage.py shell < .e2e/12_create_legacy_telegram_mailing.py

Пример создания черновика:
LEGACY_MAILING_TEMPLATE_ID=8 LEGACY_MAILING_CREATE=1 \
python manage.py shell < .e2e/12_create_legacy_telegram_mailing.py
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from guests.models import (
    BotProfile,
    Guest,
    GuestBotBinding,
    Mailing,
    MailingGuest,
    MessageTemplate,
    VtelemaxRecipientChannel,
)
from guests.services.mailing_delivery_targets import build_mailing_delivery_plan
from guests.services.template_render import render_message_for_guest


NEW_BOT_PROVIDER_TYPES = ("telegram", "max", "vk")
DEFAULT_WINDOW_BEGIN = "10:00"
DEFAULT_WINDOW_END = "21:00"


def env_value(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def env_flag(name: str) -> bool:
    return env_value(name).lower() in {"1", "true", "yes", "y", "да"}


def env_int(name: str, default: int = 0) -> int:
    raw_value = env_value(name)
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        raise SystemExit(f"Ошибка: {name} должен быть целым числом, сейчас: {raw_value!r}")


def parse_local_datetime(date_value: str, time_value: str):
    local_tz = timezone.get_current_timezone()
    if date_value:
        selected_date = datetime.strptime(date_value, "%Y-%m-%d").date()
    else:
        selected_date = timezone.localdate() + timedelta(days=1)
    selected_time = datetime.strptime(time_value, "%H:%M").time()
    return timezone.make_aware(datetime.combine(selected_date, selected_time), local_tz)


def load_template() -> MessageTemplate:
    template_id = env_int("LEGACY_MAILING_TEMPLATE_ID")
    if template_id <= 0:
        print("Не указан LEGACY_MAILING_TEMPLATE_ID.")
        print("Активные шаблоны:")
        for template in MessageTemplate.objects.filter(is_active=True).order_by("name", "id")[:50]:
            preview = str(template.message_text or "").replace("\n", " ")[:120]
            print(f"  id={template.id} name={template.name} text={preview}")
        raise SystemExit("Укажите LEGACY_MAILING_TEMPLATE_ID и запустите скрипт ещё раз.")

    template = MessageTemplate.objects.filter(id=template_id, is_active=True).first()
    if template is None:
        raise SystemExit(f"Активный шаблон с id={template_id} не найден.")
    return template


def load_telegram_bot() -> BotProfile:
    bot_id = env_int("LEGACY_MAILING_TELEGRAM_BOT_ID")
    if bot_id > 0:
        bot = BotProfile.objects.filter(
            id=bot_id,
            is_active=True,
            provider_type=BotProfile.ProviderType.TELEGRAM,
        ).first()
        if bot is None:
            raise SystemExit(f"Активный Telegram-бот с id={bot_id} не найден.")
        return bot

    bots = list(
        BotProfile.objects.filter(
            is_active=True,
            provider_type=BotProfile.ProviderType.TELEGRAM,
        ).order_by("id")
    )
    if len(bots) == 1:
        return bots[0]

    print("Нужно явно указать LEGACY_MAILING_TELEGRAM_BOT_ID.")
    print("Активные Telegram-боты:")
    for bot in bots:
        print(f"  id={bot.id} code={bot.code} name={bot.name}")
    raise SystemExit("Telegram-бот не выбран однозначно.")


def collect_new_bot_guest_ids() -> set[int]:
    return {
        int(guest_id)
        for guest_id in (
            GuestBotBinding.objects.filter(
                is_active=True,
                bot__is_active=True,
                bot__provider_type__in=NEW_BOT_PROVIDER_TYPES,
            )
            .exclude(external_chat_id__isnull=True)
            .exclude(external_chat_id="")
            .values_list("guest_id", flat=True)
            .distinct()
        )
    }


def collect_legacy_guest_ids(*, exclude_guest_ids: set[int]) -> tuple[list[int], dict[str, int]]:
    base_scope = VtelemaxRecipientChannel.objects.filter(
        guest__isnull=False,
        platform=VtelemaxRecipientChannel.Platform.TELEGRAM,
    )
    with_external_id = base_scope.exclude(external_id__isnull=True).exclude(external_id="")
    registered_scope = with_external_id.filter(is_registered=True)
    sendable_scope = registered_scope.filter(notifications_allowed=True)
    legacy_scope = sendable_scope.exclude(guest_id__in=exclude_guest_ids).order_by(
        "guest_id",
        "-registered_at",
        "-last_synced_at",
        "id",
    )

    guest_ids: list[int] = []
    seen_guest_ids: set[int] = set()
    for channel in legacy_scope.only("guest_id").iterator(chunk_size=2000):
        guest_id = int(channel.guest_id)
        if guest_id in seen_guest_ids:
            continue
        seen_guest_ids.add(guest_id)
        guest_ids.append(guest_id)

    stats = {
        "old_telegram_guests_total": int(base_scope.values("guest_id").distinct().count()),
        "old_telegram_with_external_id": int(with_external_id.values("guest_id").distinct().count()),
        "old_telegram_registered": int(registered_scope.values("guest_id").distinct().count()),
        "old_telegram_sendable": int(sendable_scope.values("guest_id").distinct().count()),
        "excluded_new_bot_registered": int(
            sendable_scope.filter(guest_id__in=exclude_guest_ids).values("guest_id").distinct().count()
        ),
        "legacy_ready": len(guest_ids),
    }
    return guest_ids, stats


def main() -> None:
    template = load_template()
    telegram_bot = load_telegram_bot()

    create_mode = env_flag("LEGACY_MAILING_CREATE")
    allow_duplicate = env_flag("LEGACY_MAILING_ALLOW_DUPLICATE")
    audience_limit = env_int("LEGACY_MAILING_LIMIT")

    window_begin_raw = env_value("LEGACY_MAILING_WINDOW_BEGIN", DEFAULT_WINDOW_BEGIN)
    window_end_raw = env_value("LEGACY_MAILING_WINDOW_END", DEFAULT_WINDOW_END)
    send_begin = parse_local_datetime(env_value("LEGACY_MAILING_DATE"), window_begin_raw)
    send_end = parse_local_datetime(env_value("LEGACY_MAILING_DATE"), window_end_raw)
    if send_end <= send_begin:
        send_end += timedelta(days=1)

    default_name = f"Legacy Telegram: возврат в новый бот {timezone.localtime(send_begin).date().isoformat()}"
    mailing_name = env_value("LEGACY_MAILING_NAME", default_name)[:150]

    new_bot_guest_ids = collect_new_bot_guest_ids()
    legacy_guest_ids, stats = collect_legacy_guest_ids(exclude_guest_ids=new_bot_guest_ids)
    if audience_limit > 0:
        legacy_guest_ids = legacy_guest_ids[:audience_limit]

    delivery_plan = build_mailing_delivery_plan(
        legacy_guest_ids,
        selected_bot_ids=[int(telegram_bot.id)],
        target_mode=Mailing.TargetMode.PRIMARY_ONLY,
    )
    deliverable_guest_ids = list(delivery_plan.deliverable_guest_ids)

    print("=== Черновик рассылки по legacy Telegram ===")
    print(f"mode={'CREATE' if create_mode else 'CHECK_ONLY'}")
    print(f"template_id={template.id}")
    print(f"template_name={template.name}")
    print(f"telegram_bot_id={telegram_bot.id}")
    print(f"telegram_bot_code={telegram_bot.code}")
    print(f"mailing_name={mailing_name}")
    print(f"send_begin={timezone.localtime(send_begin).strftime('%Y-%m-%d %H:%M')}")
    print(f"send_end={timezone.localtime(send_end).strftime('%Y-%m-%d %H:%M')}")
    print(f"send_window={window_begin_raw}-{window_end_raw}")
    print(f"audience_limit={audience_limit or '-'}")

    print("\n=== Аудитория ===")
    for key, value in stats.items():
        print(f"{key}={value}")
    print(f"selected_after_limit={len(legacy_guest_ids)}")
    print(f"deliverable_guests={delivery_plan.deliverable_guests}")
    print(f"planned_dispatch_tasks={delivery_plan.planned_dispatch_tasks}")
    print(f"blocked_without_delivery={len(legacy_guest_ids) - len(deliverable_guest_ids)}")

    print("\n=== Пример первых получателей ===")
    sample_guests = Guest.objects.filter(id__in=deliverable_guest_ids[:10]).order_by("id")
    for guest in sample_guests:
        rendered_text = render_message_for_guest(template.message_text, guest)
        print(f"guest_id={guest.id} phone={guest.phone or '-'} name={guest.first_name or '-'} text={rendered_text[:160]}")

    if not create_mode:
        print("\nЧерновик не создан: это режим проверки без записи.")
        print("Для создания добавьте переменную окружения LEGACY_MAILING_CREATE=1.")
        return

    if not deliverable_guest_ids:
        raise SystemExit("Создание остановлено: нет получателей с доступной доставкой.")

    if Mailing.objects.filter(name=mailing_name).exists() and not allow_duplicate:
        raise SystemExit(
            "Создание остановлено: кампания с таким названием уже существует. "
            "Измените LEGACY_MAILING_NAME или укажите LEGACY_MAILING_ALLOW_DUPLICATE=1."
        )

    guests = list(
        Guest.objects.filter(id__in=deliverable_guest_ids)
        .only("id", "phone", "email", "first_name", "last_name", "birthdate")
        .order_by("id")
    )
    guests_by_id = {int(guest.id): guest for guest in guests}
    ordered_guests = [guests_by_id[guest_id] for guest_id in deliverable_guest_ids if guest_id in guests_by_id]
    now = timezone.now()

    with transaction.atomic():
        mailing = Mailing.objects.create(
            name=mailing_name,
            template=template,
            scheduled_date=timezone.localtime(send_begin).date(),
            scheduled_time_begin=send_begin,
            scheduled_time_end=send_end,
            is_active=False,
            created_at=now,
            updated_at=now,
            send_window_begin=timezone.localtime(send_begin).time().replace(second=0, microsecond=0),
            send_window_end=timezone.localtime(send_end).time().replace(second=0, microsecond=0),
            target_mode=Mailing.TargetMode.PRIMARY_ONLY,
            queue_priority=Mailing.QueuePriority.BULK,
        )
        mailing.bot_profiles.set([telegram_bot])

        rows = [
            MailingGuest(
                mailing=mailing,
                guest=guest,
                phone=guest.phone,
                email=guest.email,
                text_mailing_list=render_message_for_guest(template.message_text, guest),
                scheduled_datetime=send_begin,
                status=MailingGuest.Status.PLANNED,
                created_at=now,
            )
            for guest in ordered_guests
        ]
        MailingGuest.objects.bulk_create(rows, batch_size=1000)

    print("\n=== Черновик создан ===")
    print(f"mailing_id={mailing.id}")
    print(f"rows_created={len(ordered_guests)}")
    print(f"open_url=/mailings-v2/campaigns/{mailing.id}/")
    print(f"status_url=/mailings-v2/campaigns/{mailing.id}/status/")


main()
