"""
Справочник доступных серий купонов для UI рассылочных кампаний.

Форма кампании должна выбирать серию из фактически подтверждённого пула,
а не полагаться на ручной ввод маркетолога.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from guests.models import CouponRegistryEntry
from guests.services.coupon_constants import COUPON_VENUE_GLOBAL_CODE, COUPON_VENUE_GLOBAL_NAME


@dataclass(slots=True)
class _CouponSeriesStat:
    """
    Агрегат доступных купонов по серии для человеко-понятной подписи.
    """

    count: int = 0
    venues: set[str] = field(default_factory=set)


def build_available_coupon_series_choices(
    *,
    existing_series: str | None = None,
) -> tuple[list[tuple[str, str]], set[str]]:
    """
    Возвращает варианты серий, которые можно выбрать в форме кампании.

    В список попадают только активные купоны, уже подтверждённые в iikoCard.
    Если форма редактирует старую кампанию, её текущая серия добавляется
    отдельно, чтобы пользователь мог сохранить параметры без скрытого сброса.
    """
    stats_by_series: dict[str, _CouponSeriesStat] = defaultdict(_CouponSeriesStat)
    rows = (
        CouponRegistryEntry.objects.filter(
            is_active=True,
            pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            iiko_check_status=CouponRegistryEntry.IikoCheckStatus.FOUND,
        )
        .exclude(series__isnull=True)
        .exclude(series="")
        .values("series", "venue_code", "venue_name")
        .order_by("series", "venue_name", "venue_code")
    )

    for row in rows:
        series = str(row.get("series") or "").strip()
        if not series:
            continue

        stat = stats_by_series[series]
        stat.count += 1

        venue_code = str(row.get("venue_code") or "").strip()
        venue_name = str(row.get("venue_name") or "").strip()
        if venue_code == COUPON_VENUE_GLOBAL_CODE:
            stat.venues.add(COUPON_VENUE_GLOBAL_NAME)
        elif venue_name:
            stat.venues.add(venue_name)
        elif venue_code:
            stat.venues.add(venue_code)

    choices: list[tuple[str, str]] = [("", "— Без купонов —")]
    valid_values: set[str] = {""}
    for series in sorted(stats_by_series):
        stat = stats_by_series[series]
        venue_label = _build_venue_label(stat.venues)
        label = f"{series} — {venue_label}, доступно {stat.count}"
        choices.append((series, label))
        valid_values.add(series)

    normalized_existing = str(existing_series or "").strip()
    if normalized_existing and normalized_existing not in valid_values:
        choices.append((normalized_existing, f"{normalized_existing} — уже выбрано в кампании"))
        valid_values.add(normalized_existing)

    return choices, valid_values


def _build_venue_label(venues: set[str]) -> str:
    """
    Формирует краткое описание заведений, где есть доступные купоны серии.
    """
    cleaned = sorted(str(venue or "").strip() for venue in venues if str(venue or "").strip())
    if not cleaned:
        return "заведение не указано"
    if len(cleaned) == 1:
        return cleaned[0]
    return f"несколько заведений: {len(cleaned)}"
