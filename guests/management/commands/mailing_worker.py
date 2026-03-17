# guests/management/commands/mailing_worker.py

import logging
import time
import traceback
from datetime import datetime, timedelta
from telegram.error import TimedOut

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction

from telegram.error import RetryAfter, Forbidden, BadRequest, TelegramError

from guests.models import Mailing, MailingGuest, MailingChannel ,GuestChannelLink
from guests.services.telegram_bot import TelegramSender
from guests.services.universal_queue import enqueue_mailing_rows_as_dispatch_tasks

logger = logging.getLogger(__name__)

SLEEP_SECONDS = 3

# сколько строк берём из каждой рассылки за 1 проход
BATCH_SIZE = 10

# лимит отправки (строго): не больше N сообщений в секунду (на 1 воркер)
RATE_PER_SEC = 26
WINDOW_SECONDS = 1.0

# через сколько минут возвращаем зависшие IN_PROGRESS обратно в PLANNED
STUCK_TIMEOUT_MINUTES = 3


def _is_universal_dispatch_enabled() -> bool:
    """
    Возвращает признак включения режима F4:
    массовая рассылка ставит задачи в DispatchTask вместо прямой отправки.
    """
    raw_value = getattr(settings, "UNIVERSAL_QUEUE_ENABLE_MAILING_DISPATCH", False)
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value).strip().lower() in ("1", "true", "yes", "on")


class Command(BaseCommand):
    help = "Worker: sends active mailings in background."

    def handle(self, *args, **options):
        print("=== Mailing worker started ===")

        while True:
            try:
                processed = run_iteration()
                if processed == 0:
                    print(f"[worker] nothing to process -> sleep {SLEEP_SECONDS}s\n")
                    time.sleep(SLEEP_SECONDS)
                else:
                    print(f"[worker] processed rows this iteration: {processed}\n")
            except KeyboardInterrupt:
                print("\n=== Mailing worker stopped (KeyboardInterrupt) ===")
                return
            except Exception as e:
                print("[worker] CRASH in iteration:", e)
                traceback.print_exc()
                time.sleep(SLEEP_SECONDS)


def run_iteration() -> int:
    now = timezone.now()
    print(f"[iter] start now={now.isoformat()}")

    # 0) Размораживаем зависшие IN_PROGRESS (если воркер падал/останавливался)
    stuck_qs = MailingGuest.objects.filter(
        status=MailingGuest.Status.IN_PROGRESS,
    )
    stuck_count = stuck_qs.count()
    if stuck_count > 0:
        print(f"[requeue] Found {stuck_count} stuck IN_PROGRESS rows. Reverting to PLANNED.")
        stuck_qs.update(
            status=MailingGuest.Status.PLANNED,
            delivery_status="requeued",
        )

    # 1) Активные рассылки в окне времени
    mailings_qs = (Mailing.objects
                   .filter(is_active=True,
                           scheduled_time_begin__lte=now,
                           scheduled_time_end__gte=now)
                   .order_by("id"))

    mailings_count = mailings_qs.count()
    print(f"[iter] active mailings in time window: {mailings_count}")

    total = 0
    for mailing in mailings_qs:
        print(f"\n[mailing] id={mailing.id} name={getattr(mailing, 'name', None)}")
        total += process_one_mailing(mailing, now)

    return total


def process_one_mailing(mailing: Mailing, now) -> int:

    # 🔹 1) Проверяем окно отправки (время суток)
    local_now = timezone.localtime(now)
    current_time = local_now.time()

    if not (mailing.send_window_begin <= current_time <= mailing.send_window_end):
        print(
            f"[mailing:{mailing.id}] outside send window "
            f"{mailing.send_window_begin}-{mailing.send_window_end}, skip"
        )
        return 0
    use_universal_dispatch = _is_universal_dispatch_enabled()

    # 2) Активные боты и legacy-каналы рассылки
    selected_bots = list(mailing.bot_profiles.filter(is_active=True).order_by("provider_type", "id"))
    print(f"[mailing:{mailing.id}] selected bot profiles: {len(selected_bots)}")
    for bot in selected_bots:
        print(f"  - bot id={bot.id} provider={bot.provider_type} code={bot.code}")

    channels = list(mailing.channels.filter(is_active=True))
    print(f"[mailing:{mailing.id}] active channels: {len(channels)}")
    for ch in channels:
        print(f"  - channel id={ch.id} kind={ch.channel_kind} token={'YES' if ch.token else 'NO'}")

    if use_universal_dispatch and not selected_bots:
        updated = MailingGuest.objects.filter(
            mailing=mailing,
            status=MailingGuest.Status.PLANNED,
        ).update(
            status=MailingGuest.Status.ERROR,
            delivery_status="no_bot_profiles",
            error_description="No active bot profiles in mailing",
        )
        print(f"[mailing:{mailing.id}] NO BOT PROFILES -> marked ERROR rows: {updated}")
        return 0
    if not use_universal_dispatch and not channels:
        updated = MailingGuest.objects.filter(
            mailing=mailing,
            status=MailingGuest.Status.PLANNED,
        ).update(
            status=MailingGuest.Status.ERROR,
            delivery_status="no_channels",
            error_description="No active channels in mailing",
        )
        print(f"[mailing:{mailing.id}] NO CHANNELS -> marked ERROR rows: {updated}")
        return 0

    # 3) Захват пачки planned -> in_progress
    with transaction.atomic():
        qs = (
            MailingGuest.objects
            .select_for_update()
            .filter(
                mailing=mailing,
                status=MailingGuest.Status.PLANNED,
                scheduled_datetime__lte=now,
            )
            .order_by("id")[:BATCH_SIZE]
        )

        rows = list(qs)
        print(f"[mailing:{mailing.id}] ready planned rows (<=now): {len(rows)}")
        if not rows:
            return 0

        ids = [r.id for r in rows]
        MailingGuest.objects.filter(id__in=ids).update(status=MailingGuest.Status.IN_PROGRESS)
        print(f"[mailing:{mailing.id}] moved to IN_PROGRESS ids={ids}")

    # 4) Режим F4: вместо прямой отправки ставим DispatchTask в универсальную очередь.
    if use_universal_dispatch:
        try:
            summary = enqueue_mailing_rows_as_dispatch_tasks(
                mailing=mailing,
                rows=rows,
                channels=channels,
                now=now,
            )
            logger.info(
                "F4 enqueue mailing_id=%s rows_total=%s rows_queued=%s rows_failed=%s tasks_created=%s tasks_duplicates=%s",
                mailing.id,
                summary.rows_total,
                summary.rows_queued,
                summary.rows_failed,
                summary.tasks_created,
                summary.tasks_duplicates,
            )
            print(
                f"[mailing:{mailing.id}] queued_to_dispatch="
                f"{summary.rows_queued}/{summary.rows_total} failed={summary.rows_failed}"
            )
            return summary.rows_total
        except Exception as enqueue_error:
            # В случае сбоя не оставляем строки в IN_PROGRESS.
            error_text = f"dispatch_enqueue_exception: {str(enqueue_error)[:1800]}"
            for row in rows:
                row.status = MailingGuest.Status.ERROR
                row.delivery_status = "dispatch_enqueue_exception"
                row.error_description = error_text
                row.save(update_fields=["status", "delivery_status", "error_description"])

            logger.exception(
                "Ошибка F4-постановки задач для mailing_id=%s: %s",
                mailing.id,
                enqueue_error,
            )
            print(f"[mailing:{mailing.id}] F4 enqueue failed -> rows marked ERROR")
            return len(rows)

    # 5) Подготовим TelegramSender по каналам (legacy direct-send path)
    telegram_senders: dict[int, TelegramSender] = {}
    for ch in channels:
        if ch.channel_kind in (
            MailingChannel.ChannelKind.PHONE_TELEGRAM_BOT,
            MailingChannel.ChannelKind.PHONE_TELEGRAM,
        ):
            if not ch.token:
                print(f"[mailing:{mailing.id}] ERROR: telegram channel id={ch.id} has NO token")
            else:
                telegram_senders[ch.id] = TelegramSender(ch.token)

    sent_ok = 0
    sent_err = 0

    # 5) Rate limiter (строго N сообщений/сек) — действует на весь mailing внутри этого вызова
    sent_in_window = 0
    window_start = time.time()

    def throttle_if_needed():
        nonlocal sent_in_window, window_start
        if sent_in_window >= RATE_PER_SEC:
            elapsed = time.time() - window_start
            if elapsed < WINDOW_SECONDS:
                sleep_for = WINDOW_SECONDS - elapsed
                print(f"[TG] throttling {sleep_for:.2f}s (rate={RATE_PER_SEC}/sec)")
                time.sleep(sleep_for)
            window_start = time.time()
            sent_in_window = 0

    # 6) Заранее подгрузим GuestChannelLink для всех guest в батче и всех каналов рассылки
    guest_ids = [r.guest_id for r in rows]
    channel_ids = [c.id for c in channels]
    links_qs = GuestChannelLink.objects.filter(
        guest_id__in=guest_ids,
        channel_id__in=channel_ids,
    )
    link_map: dict[tuple[int, int], GuestChannelLink] = {
        (l.guest_id, l.channel_id): l for l in links_qs
    }

    def set_link_error(guest_id: int, channel_id: int, err: str) -> None:
        """Пишем last_error в GuestChannelLink (если линк есть). Не даём воркеру упасть."""
        link = link_map.get((guest_id, channel_id))
        if not link:
            return
        try:
            link.last_error = (err or "")[:2000]
            # updated_at в модели не auto_now, но в БД может быть default; всё равно обновим явно
            link.updated_at = timezone.now()
            link.save(update_fields=["last_error", "updated_at"])
        except Exception:
            # логируем, но не падаем
            print(f"[link] WARNING: cannot save last_error for guest={guest_id} channel={channel_id}")
            traceback.print_exc()

    # 7) Отправка
    for row in rows:
        guest_id = row.guest_id
        print(f"\n[row:{row.id}] start guest_id={guest_id} status was IN_PROGRESS")

        try:
            # отправка по всем каналам
            for ch in channels:
                print(f"[row:{row.id}] channel id={ch.id} kind={ch.channel_kind}")

                link = link_map.get((guest_id, ch.id))
                if not link:
                    raise ValueError(f"No GuestChannelLink for guest_id={guest_id} channel_id={ch.id}")

                # применять флаги теперь нужно отсюда
                if not link.is_active:
                    raise ValueError("GuestChannelLink is_active=False")
                if not link.is_opt_in:
                    raise ValueError("GuestChannelLink is_opt_in=False")
                if getattr(link, "is_stop_sending", False):
                    raise ValueError("GuestChannelLink is_stop_sending=True")

                if ch.channel_kind in (
                    MailingChannel.ChannelKind.PHONE_TELEGRAM_BOT,
                    MailingChannel.ChannelKind.PHONE_TELEGRAM,
                ):
                    chat_id = link.external_chat_id
                    if not chat_id:
                        raise ValueError("GuestChannelLink has no external_chat_id")

                    sender = telegram_senders.get(ch.id)
                    if not sender:
                        raise ValueError("TelegramSender not configured for this channel (token missing?)")

                    throttle_if_needed()

                    print(f"[TG] sending -> chat_id={chat_id} row_id={row.id}")
                    tg_msg = sender.send_message(int(chat_id), row.text_mailing_list)

                    # сохраняем подтверждение Telegram
                    message_id = getattr(tg_msg, "message_id", None) if tg_msg is not None else None
                    msg_date = getattr(tg_msg, "date", None) if tg_msg is not None else None

                    if isinstance(msg_date, datetime):
                        sent_at = msg_date if timezone.is_aware(msg_date) else timezone.make_aware(
                            msg_date, timezone=timezone.utc
                        )
                    else:
                        sent_at = timezone.now()

                    row.external_id = f"tg:{message_id}" if message_id is not None else None
                    row.sent_at = sent_at
                    row.delivery_status = "sent"
                    row.error_description = None
                    row.save(update_fields=["external_id", "sent_at", "delivery_status", "error_description"])

                    sent_in_window += 1

                    print(f"[TG] SUCCESS chat_id={chat_id} message_id={row.external_id} sent_at={row.sent_at}")
                    print(f"[row:{row.id}] SENT telegram -> chat_id={chat_id}")

                elif ch.channel_kind == MailingChannel.ChannelKind.EMAIL:
                    print(f"[row:{row.id}] EMAIL channel not implemented -> skip")
                    # TODO: email позже
                    pass

                else:
                    raise ValueError(f"Unknown channel_kind={ch.channel_kind}")

            # успех строки (по всем каналам)
            row.status = MailingGuest.Status.DONE
            if not getattr(row, "delivery_status", None):
                row.delivery_status = "sent"
            row.save(update_fields=["status", "delivery_status"])
            sent_ok += 1
            print(f"[row:{row.id}] DONE")

        except RetryAfter as e:
            delay = int(getattr(e, "retry_after", 3))
            row.status = MailingGuest.Status.PLANNED
            row.delivery_status = "rate_limited"
            row.error_description = f"RetryAfter {delay}s"
            row.scheduled_datetime = timezone.now() + timedelta(seconds=delay + 1)
            row.save(update_fields=["status", "delivery_status", "error_description", "scheduled_datetime"])
            sent_err += 1
            print(f"[TG] RATE LIMITED -> requeue row:{row.id} for +{delay}s")
            time.sleep(delay)

        except Forbidden as e:
            err = f"Forbidden: {str(e)[:2000]}"
            row.status = MailingGuest.Status.ERROR
            row.delivery_status = "blocked"
            row.error_description = err
            row.save(update_fields=["status", "delivery_status", "error_description"])
            set_link_error(row.guest_id, ch.id, err)
            GuestChannelLink.objects.filter(
                guest_id=row.guest_id,
                channel_id=ch.id,
            ).update(
                is_stop_sending=True,
                is_active=False,
                last_error=err,
                updated_at=timezone.now(),
            )

            sent_err += 1

        except BadRequest as e:
            row.status = MailingGuest.Status.ERROR
            row.delivery_status = "invalid_chat"
            row.error_description = str(e)[:2000]
            row.save(update_fields=["status", "delivery_status", "error_description"])
            for ch in channels:
                set_link_error(guest_id, ch.id, f"BadRequest: {e}")
            sent_err += 1
            print(f"[row:{row.id}] ERROR BadRequest -> {e}")

        except TimedOut as e:
            print(f"[TG] TIMEOUT -> requeue row:{row.id}")

            row.status = MailingGuest.Status.PLANNED
            row.delivery_status = "timeout_retry"
            row.error_description = "Telegram timeout"
            row.scheduled_datetime = timezone.now() + timedelta(seconds=30)

            row.save(update_fields=[
                "status",
                "delivery_status",
                "error_description",
                "scheduled_datetime",
            ])

            sent_err += 1

        except TelegramError as e:
            row.status = MailingGuest.Status.ERROR
            row.delivery_status = "telegram_error"
            row.error_description = str(e)[:2000]
            row.save(update_fields=["status", "delivery_status", "error_description"])
            for ch in channels:
                set_link_error(guest_id, ch.id, f"TelegramError: {e}")
            sent_err += 1
            print(f"[row:{row.id}] ERROR TelegramError -> {e}")
            traceback.print_exc()

        except Exception as e:
            msg = str(e)
            delivery = "error"
            low = msg.lower()

            if "no guestchannellink" in low:
                delivery = "no_link"
            elif "is_opt_in=false" in low:
                delivery = "not_opt_in"
            elif "is_active=false" in low:
                delivery = "inactive_link"
            elif "is_stop_sending=true" in low:
                delivery = "stopped"
            elif "no external_chat_id" in low:
                delivery = "no_chat_id"

            row.status = MailingGuest.Status.ERROR
            row.delivery_status = delivery
            row.error_description = msg[:2000]
            row.save(update_fields=["status", "delivery_status", "error_description"])

            # если ошибка относится к конкретному каналу, лучше было бы писать точнее,
            # но хотя бы запишем её в линки всех активных каналов рассылки
            for ch in channels:
                set_link_error(guest_id, ch.id, msg)

            sent_err += 1
            print(f"[row:{row.id}] ERROR -> {e}")
            traceback.print_exc()

    print(f"\n[mailing:{mailing.id}] batch finished: ok={sent_ok} err={sent_err} total={len(rows)}")
    return len(rows)
