from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from guests.models import (
    CouponCampaignAssignment,
    CouponRegistryEntry,
    CouponVtelemaxSyncQueue,
    Mailing,
    MailingGuest,
    VtelemaxRecipientChannel,
    VtelemaxSyncState,
)
from guests.services.coupon_constants import (
    append_coupon_message_footer,
    COUPON_VENUE_GLOBAL_CODE,
    COUPON_VENUE_GLOBAL_NAME,
    is_coupon_global_venue,
)
from guests.services.template_render import render_message_for_guest


def _normalize_phone_e164_ru(raw_value: str | None) -> str | None:
    """
    Нормализует телефон в формат `+7XXXXXXXXXX`.

    Поддерживаемые входы:
    1. `+7XXXXXXXXXX`;
    2. `8XXXXXXXXXX`;
    3. `XXXXXXXXXX`.
    """
    digits = "".join(ch for ch in str(raw_value or "") if ch.isdigit())
    if not digits:
        return None
    if len(digits) == 10:
        return f"+7{digits}"
    if len(digits) == 11 and digits.startswith("8"):
        return f"+7{digits[1:]}"
    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits}"
    return None


def _is_channel_sendable(channel: VtelemaxRecipientChannel | None) -> bool:
    """
    Проверяет, можно ли использовать канал для купонной коммуникации.
    """
    if channel is None:
        return False
    if not bool(channel.is_registered):
        return False
    if not bool(channel.notifications_allowed):
        return False
    if not str(channel.external_id or "").strip():
        return False
    return True


def _resolve_campaign_coupon_series(mailing: Mailing) -> str:
    """
    Возвращает серию купонов кампании в нормализованном виде.
    """
    return str(getattr(mailing, "coupon_series", "") or "").strip()


def _resolve_campaign_coupon_venue_code(mailing: Mailing) -> str:
    """
    Возвращает код заведения купонной кампании в нормализованном виде.
    """
    return str(getattr(mailing, "coupon_venue_code", "") or "").strip()


def _resolve_campaign_coupon_venue_name(mailing: Mailing) -> str:
    """
    Возвращает имя заведения купонной кампании в нормализованном виде.
    """
    return str(getattr(mailing, "coupon_venue_name", "") or "").strip()


def _resolve_campaign_coupon_promo_text(mailing: Mailing) -> str:
    """
    Возвращает текст акции купонной кампании в нормализованном виде.
    """
    return str(getattr(mailing, "coupon_promo_text", "") or "").strip()


def _build_coupon_template_context(
    *,
    coupon_code: str,
    coupon_series: str,
    coupon_venue_code: str,
    coupon_venue_name: str,
    coupon_promo_text: str,
    coupon_expires_at,
) -> dict[str, str]:
    """
    Формирует единый набор переменных купонной кампании для шаблонов.
    """
    return {
        "coupon_code": str(coupon_code or "").strip(),
        "coupon_series": str(coupon_series or "").strip(),
        "coupon_venue_code": str(coupon_venue_code or "").strip(),
        "coupon_venue_name": str(coupon_venue_name or "").strip(),
        "coupon_promo_text": str(coupon_promo_text or "").strip(),
        "coupon_expires_at": coupon_expires_at.strftime("%d.%m.%Y") if coupon_expires_at else "",
    }


@dataclass(slots=True)
class CouponGateIssue:
    """
    Описание проблемы строки кампании на этапе купонного sync-gate.
    """

    row_id: int
    guest_id: int | None
    code: str
    message: str


@dataclass(slots=True)
class CouponGateReport:
    """
    Сводка выполнения купонного этапа перед постановкой в DispatchTask.
    """

    coupon_mode: bool = False
    coupon_series: str = ""
    coupon_venue_code: str = ""
    coupon_venue_name: str = ""
    rows_total: int = 0
    rows_ready: int = 0
    rows_blocked: int = 0
    existing_assignments: int = 0
    created_assignments: int = 0
    required_new_assignments: int = 0
    available_coupons_before: int = 0
    queue_events_created: int = 0
    sync_ok: int = 0
    sync_error: int = 0
    global_blockers: list[str] = field(default_factory=list)
    issues: list[CouponGateIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """
        Сериализует отчёт для UI/session-логики.
        """
        return {
            "coupon_mode": self.coupon_mode,
            "coupon_series": self.coupon_series,
            "coupon_venue_code": self.coupon_venue_code,
            "coupon_venue_name": self.coupon_venue_name,
            "rows_total": self.rows_total,
            "rows_ready": self.rows_ready,
            "rows_blocked": self.rows_blocked,
            "existing_assignments": self.existing_assignments,
            "created_assignments": self.created_assignments,
            "required_new_assignments": self.required_new_assignments,
            "available_coupons_before": self.available_coupons_before,
            "queue_events_created": self.queue_events_created,
            "sync_ok": self.sync_ok,
            "sync_error": self.sync_error,
            "issues_by_code": self.issues_by_code(),
            "global_blockers": list(self.global_blockers),
            "issues": [
                {
                    "row_id": issue.row_id,
                    "guest_id": issue.guest_id,
                    "code": issue.code,
                    "message": issue.message,
                }
                for issue in self.issues
            ],
        }

    def issues_by_code(self) -> dict[str, int]:
        """
        Возвращает агрегат проблем по кодам для UI/логов.
        """
        counts = Counter(issue.code for issue in self.issues if issue.code)
        return {str(code): int(count) for code, count in counts.items()}


class CouponCampaignGateService:
    """
    Подготавливает купонные назначения и выполняет pre-send sync-gate.

    Назначение:
    1. гарантировать, что для строки кампании есть купон (1 гость = 1 купон);
    2. обновить персонализированный текст строки с реальным кодом купона;
    3. проверить, что по строке есть валидный vtelemax-канал;
    4. сформировать технические события синхронизации (`CouponVtelemaxSyncQueue`).
    """

    def __init__(self) -> None:
        self.max_sync_age_minutes = max(
            1,
            int(getattr(settings, "VTELEMAX_COUPON_SYNC_GATE_MAX_SYNC_AGE_MINUTES", 120)),
        )
        self.require_fresh_vtelemax_sync = bool(
            getattr(settings, "VTELEMAX_COUPON_SYNC_GATE_REQUIRE_FRESH_STATE", True)
        )

    def prepare_rows_for_dispatch(
        self,
        *,
        mailing: Mailing,
        rows: list[MailingGuest],
        now=None,
        dry_run: bool = False,
    ) -> tuple[list[MailingGuest], CouponGateReport]:
        """
        Готовит строки кампании к отправке с купонным контролем.

        Возвращает:
        1. `ready_rows` — строки, прошедшие gate;
        2. `report` — подробная диагностика по блокировкам.
        """
        report = CouponGateReport(rows_total=len(rows))
        if not rows:
            return [], report

        coupon_series = _resolve_campaign_coupon_series(mailing)
        if not coupon_series:
            report.rows_ready = len(rows)
            return rows, report

        coupon_venue_code = _resolve_campaign_coupon_venue_code(mailing)
        coupon_venue_name = _resolve_campaign_coupon_venue_name(mailing)
        coupon_promo_text = _resolve_campaign_coupon_promo_text(mailing)
        coupon_venue_is_global = is_coupon_global_venue(coupon_venue_code)
        if coupon_venue_is_global and not coupon_venue_name:
            coupon_venue_name = COUPON_VENUE_GLOBAL_NAME

        report.coupon_mode = True
        report.coupon_series = coupon_series
        report.coupon_venue_code = coupon_venue_code
        report.coupon_venue_name = coupon_venue_name
        now = now or timezone.now()

        if not coupon_venue_code:
            report.global_blockers.append(
                "Для купонной кампании не задано заведение. Укажите заведение в параметрах кампании."
            )
            return self._finalize_ready_rows(rows=rows, ready_guest_ids=set(), report=report)

        if not coupon_promo_text:
            report.global_blockers.append(
                "Для купонной кампании не задан текст акции. Заполните поле текста акции перед запуском."
            )
            return self._finalize_ready_rows(rows=rows, ready_guest_ids=set(), report=report)

        guest_ids = [int(row.guest_id) for row in rows if row.guest_id]
        row_by_guest_id = {int(row.guest_id): row for row in rows if row.guest_id}

        assignments = list(
            CouponCampaignAssignment.objects.filter(
                campaign=mailing,
                guest_id__in=guest_ids,
            )
            .select_related("guest", "coupon")
            .order_by("id")
        )
        assignment_by_guest_id: dict[int, CouponCampaignAssignment] = {
            int(assignment.guest_id): assignment
            for assignment in assignments
            if assignment.guest_id
        }
        report.existing_assignments = len(assignment_by_guest_id)

        missing_guest_ids = [guest_id for guest_id in guest_ids if guest_id not in assignment_by_guest_id]
        report.required_new_assignments = len(missing_guest_ids)

        if missing_guest_ids:
            available_qs = CouponRegistryEntry.objects.filter(
                series=coupon_series,
                is_active=True,
                pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            )
            if coupon_venue_is_global:
                available_qs = available_qs.filter(
                    Q(venue_code=COUPON_VENUE_GLOBAL_CODE) | Q(venue_code__isnull=True) | Q(venue_code="")
                )
            else:
                available_qs = available_qs.filter(venue_code=coupon_venue_code)
            available_qs = available_qs.order_by("id")
            report.available_coupons_before = int(available_qs.count())

            reserve_rows = list(available_qs.select_for_update()[: len(missing_guest_ids)])
            if len(reserve_rows) < len(missing_guest_ids):
                blocker = (
                    f"Недостаточно купонов серии `{coupon_series}` для заведения `{coupon_venue_code}`: "
                    f"нужно={len(missing_guest_ids)}, "
                    f"доступно={len(reserve_rows)}."
                )
                report.global_blockers.append(blocker)
                for guest_id in missing_guest_ids:
                    row = row_by_guest_id.get(guest_id)
                    if row is None:
                        continue
                    report.issues.append(
                        CouponGateIssue(
                            row_id=int(row.id),
                            guest_id=int(row.guest_id) if row.guest_id else None,
                            code="coupon_pool_insufficient",
                            message="Купон не назначен: в пуле недостаточно подтвержденных купонов.",
                        )
                    )
                return self._finalize_ready_rows(rows=rows, ready_guest_ids=set(), report=report)

            channels_by_guest_id = self._build_channels_map(guest_ids=guest_ids)
            created_assignments = 0
            created_events = 0
            for index, guest_id in enumerate(missing_guest_ids):
                row = row_by_guest_id.get(guest_id)
                if row is None:
                    continue
                coupon = reserve_rows[index]
                primary_channel = self._pick_preferred_channel(channels_by_guest_id.get(guest_id, []))
                phone_e164 = (
                    str(primary_channel.phone_e164 or "").strip()
                    if primary_channel and primary_channel.phone_e164
                    else _normalize_phone_e164_ru(getattr(row.guest, "phone", None))
                )
                lifetime_expires_at = getattr(mailing, "scheduled_time_end", None)
                assignment_venue_code = str(coupon.venue_code or coupon_venue_code or "").strip()
                assignment_venue_name = str(coupon.venue_name or coupon_venue_name or "").strip()
                rendered_promo_text = render_message_for_guest(
                    coupon_promo_text,
                    row.guest,
                    extra_context=_build_coupon_template_context(
                        coupon_code=coupon.code,
                        coupon_series=coupon.series,
                        coupon_venue_code=assignment_venue_code,
                        coupon_venue_name=assignment_venue_name,
                        coupon_promo_text=coupon_promo_text,
                        coupon_expires_at=lifetime_expires_at,
                    ),
                )
                assignment = CouponCampaignAssignment.objects.create(
                    campaign=mailing,
                    guest=row.guest,
                    coupon=coupon,
                    person_id=primary_channel.person_id if primary_channel else None,
                    phone_e164=phone_e164 or None,
                    coupon_series=coupon.series,
                    coupon_code=coupon.code,
                    venue_code=assignment_venue_code or None,
                    venue_name=assignment_venue_name or None,
                    promo_text=rendered_promo_text or None,
                    assigned_at=now,
                    lifetime_expires_at=lifetime_expires_at,
                    status=CouponCampaignAssignment.Status.RESERVED,
                    vtelemax_sync_status=CouponCampaignAssignment.VtelemaxSyncStatus.PENDING,
                )
                created_assignments += 1
                assignment_by_guest_id[int(guest_id)] = assignment

                if not dry_run:
                    coupon.is_active = False
                    coupon.pool_status = CouponRegistryEntry.PoolStatus.ASSIGNED
                    coupon.assigned_at = now
                    coupon.save(update_fields=["is_active", "pool_status", "assigned_at", "updated_at"])
                    created_events += self._upsert_sync_queue_event(
                        assignment=assignment,
                        now=now,
                        status=CouponVtelemaxSyncQueue.Status.PENDING,
                        last_error=None,
                    )

            report.created_assignments = created_assignments
            report.queue_events_created += created_events

        if report.available_coupons_before == 0:
            fallback_qs = CouponRegistryEntry.objects.filter(
                series=coupon_series,
                is_active=True,
                pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            )
            if coupon_venue_is_global:
                fallback_qs = fallback_qs.filter(
                    Q(venue_code=COUPON_VENUE_GLOBAL_CODE) | Q(venue_code__isnull=True) | Q(venue_code="")
                )
            else:
                fallback_qs = fallback_qs.filter(venue_code=coupon_venue_code)
            report.available_coupons_before = int(fallback_qs.count())

        # Проверка свежести синка получателей vtelemax.
        if self.require_fresh_vtelemax_sync and bool(getattr(settings, "VTELEMAX_SYNC_ENABLED", False)):
            v_state = VtelemaxSyncState.objects.filter(key="vtelemax_recipients").first()
            if v_state is None or v_state.last_status != VtelemaxSyncState.Status.SUCCESS or not v_state.last_success_at:
                report.global_blockers.append(
                    "Нет успешного синка получателей vtelemax. Выполните sync_vtelemax_recipients перед запуском."
                )
            else:
                sync_age = now - v_state.last_success_at
                if sync_age > timedelta(minutes=self.max_sync_age_minutes):
                    report.global_blockers.append(
                        (
                            "Синк получателей vtelemax устарел: "
                            f"{int(sync_age.total_seconds() // 60)} мин. "
                            f"Допустимо не более {self.max_sync_age_minutes} мин."
                        )
                    )

        channels_by_guest_id = self._build_channels_map(guest_ids=guest_ids)
        sync_events_by_assignment_id = self._build_latest_sync_events_map(
            assignment_ids=[int(assignment.id) for assignment in assignment_by_guest_id.values() if assignment.id]
        )
        ready_guest_ids: set[int] = set()
        text_rows_to_update: list[MailingGuest] = []
        queue_events_created = 0

        for row in rows:
            guest_id = int(row.guest_id) if row.guest_id else None
            if not guest_id:
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=None,
                        code="missing_guest",
                        message="Строка рассылки не содержит guest_id.",
                    )
                )
                continue

            assignment = assignment_by_guest_id.get(guest_id)
            if assignment is None:
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="assignment_not_found",
                        message="Для гостя не найдено назначение купона.",
                    )
                )
                continue

            assignment_venue_code = str(assignment.venue_code or "").strip()
            if not assignment_venue_code and not dry_run:
                assignment.venue_code = coupon_venue_code
                assignment.venue_name = assignment.venue_name or coupon_venue_name or None
                assignment.promo_text = assignment.promo_text or coupon_promo_text or None
                assignment.save(update_fields=["venue_code", "venue_name", "promo_text", "updated_at"])
                assignment_venue_code = coupon_venue_code
            assignment_venue_mismatch = False
            if coupon_venue_is_global:
                assignment_venue_mismatch = bool(
                    assignment_venue_code and not is_coupon_global_venue(assignment_venue_code)
                )
            elif assignment_venue_code and assignment_venue_code != coupon_venue_code:
                assignment_venue_mismatch = True

            if assignment_venue_mismatch:
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="coupon_venue_mismatch",
                        message=(
                            "Назначенный купон относится к другому заведению: "
                            f"`{assignment_venue_code}` вместо `{coupon_venue_code}`."
                        ),
                    )
                )
                if not dry_run:
                    assignment.status = CouponCampaignAssignment.Status.ERROR
                    assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.ERROR
                    assignment.vtelemax_sync_error = "Несовпадение заведения купона и кампании."
                    assignment.save(
                        update_fields=[
                            "status",
                            "vtelemax_sync_status",
                            "vtelemax_sync_error",
                            "updated_at",
                        ]
                    )
                    queue_events_created += self._upsert_sync_queue_event(
                        assignment=assignment,
                        now=now,
                        status=CouponVtelemaxSyncQueue.Status.ERROR,
                        last_error="Несовпадение заведения купона и кампании.",
                    )
                continue

            valid_channel = self._pick_sendable_channel(channels_by_guest_id.get(guest_id, []))
            if valid_channel is None:
                report.sync_error += 1
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="vtelemax_channel_invalid",
                        message=(
                            "Нет валидного канала vtelemax "
                            "(is_registered=true, notifications_allowed=true, external_id заполнен)."
                        ),
                    )
                )
                if not dry_run:
                    assignment.vtelemax_sync_status = CouponCampaignAssignment.VtelemaxSyncStatus.ERROR
                    assignment.vtelemax_sync_error = "Не найден валидный канал получателя в vtelemax."
                    assignment.vtelemax_synced_at = None
                    assignment.save(
                        update_fields=[
                            "vtelemax_sync_status",
                            "vtelemax_sync_error",
                            "vtelemax_synced_at",
                            "updated_at",
                        ]
                    )
                    queue_events_created += self._upsert_sync_queue_event(
                        assignment=assignment,
                        now=now,
                        status=CouponVtelemaxSyncQueue.Status.ERROR,
                        last_error="Не найден валидный канал получателя в vtelemax.",
                    )
                continue

            sync_event = sync_events_by_assignment_id.get(int(assignment.id)) if assignment.id else None
            if sync_event is None:
                report.sync_error += 1
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="coupon_sync_event_missing",
                        message=(
                            "Для назначения не найдено событие синхронизации купона в очереди vtelemax. "
                            "Требуется повторная постановка sync-события."
                        ),
                    )
                )
                if not dry_run:
                    queue_events_created += self._upsert_sync_queue_event(
                        assignment=assignment,
                        now=now,
                        status=CouponVtelemaxSyncQueue.Status.PENDING,
                        last_error=None,
                    )
                continue

            if sync_event.status == CouponVtelemaxSyncQueue.Status.ERROR:
                report.sync_error += 1
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="coupon_sync_event_error",
                        message=(
                            "Событие синхронизации купона завершилось ошибкой. "
                            f"Последняя ошибка: {str(sync_event.last_error or 'неизвестно')[:300]}"
                        ),
                    )
                )
                continue

            if sync_event.status == CouponVtelemaxSyncQueue.Status.PENDING:
                report.sync_error += 1
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="coupon_sync_event_pending",
                        message=(
                            "Событие синхронизации купона ещё не подтверждено vtelemax "
                            "(ожидает обработки очередью)."
                        ),
                    )
                )
                continue

            if sync_event.status == CouponVtelemaxSyncQueue.Status.SENT:
                report.sync_error += 1
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="coupon_sync_event_sent_wait_ack",
                        message="Событие синхронизации отправлено в vtelemax, но ещё не получен ACK.",
                    )
                )
                continue

            if sync_event.status != CouponVtelemaxSyncQueue.Status.ACKED:
                report.sync_error += 1
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="coupon_sync_event_unknown_status",
                        message=f"Событие синхронизации в неизвестном статусе: `{sync_event.status}`.",
                    )
                )
                continue

            if assignment.vtelemax_sync_status == CouponCampaignAssignment.VtelemaxSyncStatus.ERROR:
                report.sync_error += 1
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="coupon_sync_status_error",
                        message=(
                            "Назначение купона помечено ошибкой синхронизации. "
                            f"Ошибка: {str(assignment.vtelemax_sync_error or 'неизвестно')[:300]}"
                        ),
                    )
                )
                continue

            if assignment.vtelemax_sync_status != CouponCampaignAssignment.VtelemaxSyncStatus.OK:
                report.sync_error += 1
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="coupon_sync_status_pending",
                        message="Назначение купона ещё не переведено в статус `ok` после синхронизации.",
                    )
                )
                continue

            if assignment.vtelemax_synced_at is None:
                report.sync_error += 1
                report.issues.append(
                    CouponGateIssue(
                        row_id=int(row.id),
                        guest_id=guest_id,
                        code="coupon_sync_synced_at_missing",
                        message="Для назначения отсутствует время подтверждённой синхронизации (vtelemax_synced_at).",
                    )
                )
                continue

            report.sync_ok += 1
            ready_guest_ids.add(guest_id)

            if not dry_run:
                rendered_promo_text = render_message_for_guest(
                    coupon_promo_text,
                    row.guest,
                    extra_context=_build_coupon_template_context(
                        coupon_code=assignment.coupon_code,
                        coupon_series=assignment.coupon_series,
                        coupon_venue_code=assignment.venue_code or coupon_venue_code,
                        coupon_venue_name=assignment.venue_name or coupon_venue_name,
                        coupon_promo_text=assignment.promo_text or coupon_promo_text,
                        coupon_expires_at=assignment.lifetime_expires_at,
                    ),
                )
                assignment.person_id = valid_channel.person_id
                assignment.phone_e164 = str(valid_channel.phone_e164 or "").strip() or assignment.phone_e164
                assignment.venue_code = assignment.venue_code or coupon_venue_code
                assignment.venue_name = assignment.venue_name or coupon_venue_name or None
                assignment.promo_text = rendered_promo_text or assignment.promo_text or coupon_promo_text or None
                assignment.save(
                    update_fields=[
                        "person_id",
                        "phone_e164",
                        "venue_code",
                        "venue_name",
                        "promo_text",
                        "updated_at",
                    ]
                )

                # Персонализируем текст строки реальным кодом купона.
                rendered = render_message_for_guest(
                    mailing.template.message_text,
                    row.guest,
                    extra_context={
                        "coupon_code": assignment.coupon_code,
                        "coupon_series": assignment.coupon_series,
                        "coupon_venue_code": assignment.venue_code or coupon_venue_code,
                        "coupon_venue_name": assignment.venue_name or coupon_venue_name,
                        "coupon_promo_text": assignment.promo_text or coupon_promo_text,
                        "coupon_expires_at": (
                            assignment.lifetime_expires_at.strftime("%d.%m.%Y")
                            if assignment.lifetime_expires_at
                            else ""
                        ),
                    },
                )
                rendered = append_coupon_message_footer(rendered)
                if row.text_mailing_list != rendered:
                    row.text_mailing_list = rendered
                    text_rows_to_update.append(row)

        if not dry_run and text_rows_to_update:
            MailingGuest.objects.bulk_update(text_rows_to_update, fields=["text_mailing_list"], batch_size=500)

        report.queue_events_created += queue_events_created
        return self._finalize_ready_rows(rows=rows, ready_guest_ids=ready_guest_ids, report=report)

    @staticmethod
    def _build_channels_map(*, guest_ids: list[int]) -> dict[int, list[VtelemaxRecipientChannel]]:
        channels_map: dict[int, list[VtelemaxRecipientChannel]] = {}
        if not guest_ids:
            return channels_map

        queryset = (
            VtelemaxRecipientChannel.objects.filter(guest_id__in=guest_ids)
            .order_by("guest_id", "platform", "id")
        )
        for channel in queryset:
            if not channel.guest_id:
                continue
            channels_map.setdefault(int(channel.guest_id), []).append(channel)
        return channels_map

    @staticmethod
    def _build_latest_sync_events_map(
        *,
        assignment_ids: list[int],
    ) -> dict[int, CouponVtelemaxSyncQueue]:
        """
        Возвращает последнее событие sync-очереди для каждого назначения купона.
        """
        if not assignment_ids:
            return {}

        events = (
            CouponVtelemaxSyncQueue.objects.filter(
                assignment_id__in=assignment_ids,
                direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
            )
            .order_by("assignment_id", "-id")
        )
        mapping: dict[int, CouponVtelemaxSyncQueue] = {}
        for event in events:
            if not event.assignment_id:
                continue
            assignment_id = int(event.assignment_id)
            if assignment_id in mapping:
                continue
            mapping[assignment_id] = event
        return mapping

    @staticmethod
    def _pick_sendable_channel(channels: list[VtelemaxRecipientChannel]) -> VtelemaxRecipientChannel | None:
        for channel in channels:
            if _is_channel_sendable(channel):
                return channel
        return None

    @staticmethod
    def _pick_preferred_channel(channels: list[VtelemaxRecipientChannel]) -> VtelemaxRecipientChannel | None:
        # Приоритет: сначала валидный sendable, иначе первый доступный.
        sendable = CouponCampaignGateService._pick_sendable_channel(channels)
        if sendable is not None:
            return sendable
        return channels[0] if channels else None

    @staticmethod
    def _upsert_sync_queue_event(
        *,
        assignment: CouponCampaignAssignment,
        now,
        status: str,
        last_error: str | None,
    ) -> int:
        """
        Обновляет или создаёт техническое событие sync-очереди.

        Возвращает 1, если событие создано заново, иначе 0.
        """
        payload = {
            "campaign_id": int(assignment.campaign_id),
            "guest_id": int(assignment.guest_id) if assignment.guest_id else None,
            "person_id": str(assignment.person_id) if assignment.person_id else None,
            "phone_e164": assignment.phone_e164,
            "coupon_series": assignment.coupon_series,
            "coupon_code": assignment.coupon_code,
            "venue_code": assignment.venue_code,
            "venue_name": assignment.venue_name,
            "promo_text": assignment.promo_text,
            "status": assignment.status,
            "vtelemax_sync_status": assignment.vtelemax_sync_status,
        }

        existing = (
            CouponVtelemaxSyncQueue.objects.filter(
                assignment=assignment,
                direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
            )
            .order_by("-id")
            .first()
        )
        if existing is None:
            CouponVtelemaxSyncQueue.objects.create(
                direction=CouponVtelemaxSyncQueue.Direction.ASSIGNMENTS,
                assignment=assignment,
                payload_json=payload,
                status=status,
                attempts=1 if status in (CouponVtelemaxSyncQueue.Status.SENT, CouponVtelemaxSyncQueue.Status.ACKED) else 0,
                next_retry_at=now,
                last_error=last_error,
                sent_at=now if status in (CouponVtelemaxSyncQueue.Status.SENT, CouponVtelemaxSyncQueue.Status.ACKED) else None,
                ack_at=now if status == CouponVtelemaxSyncQueue.Status.ACKED else None,
            )
            return 1

        existing.payload_json = payload
        existing.status = status
        existing.last_error = last_error
        existing.sent_at = (
            now
            if status in (CouponVtelemaxSyncQueue.Status.SENT, CouponVtelemaxSyncQueue.Status.ACKED)
            else None
        )
        existing.ack_at = now if status == CouponVtelemaxSyncQueue.Status.ACKED else None
        if status in (CouponVtelemaxSyncQueue.Status.SENT, CouponVtelemaxSyncQueue.Status.ACKED):
            existing.attempts = int(existing.attempts or 0) + 1
        elif status == CouponVtelemaxSyncQueue.Status.PENDING:
            existing.attempts = 0
        existing.next_retry_at = now
        existing.save(
            update_fields=[
                "payload_json",
                "status",
                "last_error",
                "sent_at",
                "ack_at",
                "attempts",
                "next_retry_at",
                "updated_at",
            ]
        )
        return 0

    @staticmethod
    def _finalize_ready_rows(
        *,
        rows: list[MailingGuest],
        ready_guest_ids: set[int],
        report: CouponGateReport,
    ) -> tuple[list[MailingGuest], CouponGateReport]:
        blocked_guest_ids = {
            int(issue.guest_id)
            for issue in report.issues
            if issue.guest_id is not None
        }
        if report.global_blockers:
            blocked_guest_ids = blocked_guest_ids.union(
                {int(row.guest_id) for row in rows if row.guest_id}
            )

        if not ready_guest_ids:
            ready_rows = [
                row
                for row in rows
                if row.guest_id and int(row.guest_id) not in blocked_guest_ids and not report.global_blockers
            ]
        else:
            ready_rows = [
                row
                for row in rows
                if row.guest_id and int(row.guest_id) in ready_guest_ids and int(row.guest_id) not in blocked_guest_ids
            ]

        report.rows_ready = len(ready_rows)
        report.rows_blocked = max(report.rows_total - report.rows_ready, 0)
        return ready_rows, report
