"""
Разделы отчётности и реестра купонов.

Назначение модуля:
1. дать оператору отдельный экран реестра купонов с фильтрами и статусами;
2. дать маркетологу отдельный отчёт по купонным кампаниям с выбором кампании;
3. предоставить стабильные URL для дальнейшего развития (добавление графиков и экспортов).
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.views.generic import TemplateView

from guests.models import CouponCampaignAssignment, CouponPoolBatch, CouponRegistryEntry, Mailing
from guests.services.coupon_campaign_reporting import build_coupon_campaign_performance_snapshot


def _parse_positive_int(value: str | None) -> int | None:
    """
    Возвращает положительное целое число или `None`.

    Используется для безопасного разбора query-параметров вида `campaign_id`.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        return None
    parsed = int(raw)
    return parsed if parsed > 0 else None


class ReportsWorkbenchView(TemplateView):
    """
    Главная точка входа раздела «Отчёты».

    На текущем этапе это компактный хаб с переходами в:
    1. отчёты по купонным кампаниям;
    2. реестр купонов.
    """

    template_name = "reports/hub.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        coupon_campaigns_qs = Mailing.objects.exclude(coupon_series__isnull=True).exclude(coupon_series="")
        context["reports_kpi"] = {
            "coupon_campaigns_total": int(coupon_campaigns_qs.count()),
            "coupon_campaigns_active": int(coupon_campaigns_qs.filter(is_active=True).count()),
            "coupon_registry_total": int(CouponRegistryEntry.objects.count()),
            "coupon_registry_available": int(
                CouponRegistryEntry.objects.filter(
                    is_active=True,
                    pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
                ).count()
            ),
            "coupon_batches_total": int(CouponPoolBatch.objects.count()),
        }
        context["coupon_campaign_reports_url"] = reverse("reports_coupon_campaigns")
        context["coupon_registry_url"] = reverse("coupon_registry")
        return context


class CouponRegistryView(TemplateView):
    """
    Экран «Реестр купонов».

    Показывает:
    1. текущий статус каждого купона;
    2. статус проверки в iikoCard;
    3. связь с кампанией и гостем (если купон назначен).
    """

    template_name = "reports/coupon_registry.html"
    page_size = 100

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        series = str(self.request.GET.get("series") or "").strip()
        batch_code = str(self.request.GET.get("batch_code") or "").strip()
        venue_code = str(self.request.GET.get("venue_code") or "").strip()
        pool_status = str(self.request.GET.get("pool_status") or "").strip()
        iiko_check_status = str(self.request.GET.get("iiko_check_status") or "").strip()
        verified_from_raw = str(self.request.GET.get("verified_from") or "").strip()
        verified_to_raw = str(self.request.GET.get("verified_to") or "").strip()
        campaign_id = _parse_positive_int(self.request.GET.get("campaign_id"))

        verified_from = parse_date(verified_from_raw) if verified_from_raw else None
        verified_to = parse_date(verified_to_raw) if verified_to_raw else None

        coupons_qs = (
            CouponRegistryEntry.objects.select_related("batch")
            .prefetch_related(
                Prefetch(
                    "campaign_assignments",
                    queryset=CouponCampaignAssignment.objects.select_related("campaign", "guest").order_by("-assigned_at"),
                    to_attr="assignments_for_ui",
                )
            )
            .order_by("-id")
        )

        if series:
            coupons_qs = coupons_qs.filter(series__icontains=series)
        if batch_code:
            coupons_qs = coupons_qs.filter(batch__batch_code__icontains=batch_code)
        if venue_code:
            coupons_qs = coupons_qs.filter(venue_code__icontains=venue_code)
        if pool_status:
            coupons_qs = coupons_qs.filter(pool_status=pool_status)
        if iiko_check_status:
            coupons_qs = coupons_qs.filter(iiko_check_status=iiko_check_status)
        if verified_from:
            coupons_qs = coupons_qs.filter(iiko_checked_at__date__gte=verified_from)
        if verified_to:
            coupons_qs = coupons_qs.filter(iiko_checked_at__date__lte=verified_to)
        if campaign_id:
            coupons_qs = coupons_qs.filter(campaign_assignments__campaign_id=campaign_id)

        coupons_qs = coupons_qs.distinct()

        paginator = Paginator(coupons_qs, self.page_size)
        page_obj = paginator.get_page(self.request.GET.get("page"))
        coupons_rows = list(page_obj.object_list)
        for coupon in coupons_rows:
            assignments = getattr(coupon, "assignments_for_ui", [])
            coupon.latest_assignment = assignments[0] if assignments else None

        total_filtered = int(coupons_qs.count())
        # Счётчики считаем явными запросами, чтобы логика была прозрачной для сопровождения.
        available_total = int(
            coupons_qs.filter(
                is_active=True,
                pool_status=CouponRegistryEntry.PoolStatus.VERIFIED_LOADED,
            ).count()
        )
        assigned_total = int(coupons_qs.filter(pool_status=CouponRegistryEntry.PoolStatus.ASSIGNED).count())
        used_total = int(coupons_qs.filter(pool_status=CouponRegistryEntry.PoolStatus.USED).count())
        check_error_total = int(
            coupons_qs.filter(iiko_check_status=CouponRegistryEntry.IikoCheckStatus.CHECK_ERROR).count()
        )

        query_without_page = self.request.GET.copy()
        if "page" in query_without_page:
            query_without_page.pop("page")
        query_tail = query_without_page.urlencode()
        pagination_query = f"&{query_tail}" if query_tail else ""

        context["coupons_page"] = page_obj
        context["coupons_rows"] = coupons_rows
        context["pagination_query"] = pagination_query
        context["registry_stats"] = {
            "total_filtered": total_filtered,
            "available_total": available_total,
            "assigned_total": assigned_total,
            "used_total": used_total,
            "check_error_total": check_error_total,
        }
        context["filters"] = {
            "series": series,
            "batch_code": batch_code,
            "venue_code": venue_code,
            "pool_status": pool_status,
            "iiko_check_status": iiko_check_status,
            "verified_from": verified_from_raw,
            "verified_to": verified_to_raw,
            "campaign_id": campaign_id or "",
        }
        context["pool_status_choices"] = CouponRegistryEntry.PoolStatus.choices
        context["iiko_check_status_choices"] = CouponRegistryEntry.IikoCheckStatus.choices
        context["coupon_campaign_reports_url"] = reverse("reports_coupon_campaigns")
        context["generate_command_hint"] = (
            "python manage.py generate_coupon_pool --series <SERIES> --prefix TST- --count 1000 --random-length 12"
        )
        context["verify_command_hint"] = (
            "python manage.py verify_coupon_pool_iiko --series <SERIES> --sample-size 50"
        )
        return context


class CouponCampaignReportsView(TemplateView):
    """
    Раздел отчётов «Купонные кампании».

    Доступны два сценария:
    1. выбор кампании из списка и просмотр KPI;
    2. прямой вход по query `campaign_id` (например, из карточки кампании).
    """

    template_name = "reports/coupon_campaigns.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        query = str(self.request.GET.get("q") or "").strip()
        campaign_id = _parse_positive_int(self.request.GET.get("campaign_id"))

        campaigns_qs = (
            Mailing.objects.exclude(coupon_series__isnull=True)
            .exclude(coupon_series="")
            .order_by("-scheduled_date", "-id")
        )
        if query:
            campaign_filter = Q(name__icontains=query) | Q(coupon_series__icontains=query)
            if query.isdigit():
                campaign_filter = campaign_filter | Q(id=int(query))
            campaigns_qs = campaigns_qs.filter(campaign_filter)

        campaigns = list(campaigns_qs[:200])
        campaigns_map = {campaign.id: campaign for campaign in campaigns}

        selected_campaign = None
        report_dict = None
        report_error = ""
        if campaign_id:
            selected_campaign = campaigns_map.get(campaign_id)
            if selected_campaign is None:
                selected_campaign = (
                    Mailing.objects.exclude(coupon_series__isnull=True)
                    .exclude(coupon_series="")
                    .filter(id=campaign_id)
                    .first()
                )

            if selected_campaign:
                try:
                    report_dict = build_coupon_campaign_performance_snapshot(mailing=selected_campaign).to_dict()
                except Exception as exc:  # noqa: BLE001
                    report_error = (
                        "Не удалось построить купонный отчёт. "
                        "Проверьте логи сервиса и корректность данных кампании."
                    )
                    context["report_error_debug"] = str(exc)
            else:
                report_error = "Кампания не найдена или не является купонной."

        context["campaigns"] = campaigns
        context["filters"] = {
            "q": query,
            "campaign_id": campaign_id or "",
        }
        context["selected_campaign"] = selected_campaign
        context["coupon_campaign_report"] = report_dict
        context["coupon_campaign_report_error"] = report_error
        context["registry_url"] = reverse("coupon_registry")
        context["back_to_reports_url"] = reverse("reports")
        if selected_campaign:
            status_url = reverse("mailings_v2_campaigns_status", kwargs={"pk": selected_campaign.id})
            context["campaign_status_url"] = status_url
            context["registry_for_campaign_url"] = f"{reverse('coupon_registry')}?{urlencode({'campaign_id': selected_campaign.id})}"
        else:
            context["campaign_status_url"] = ""
            context["registry_for_campaign_url"] = ""
        return context
