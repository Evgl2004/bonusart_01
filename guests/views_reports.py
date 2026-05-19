"""
Разделы отчётности и реестра купонов.

Назначение модуля:
1. дать оператору отдельный экран реестра купонов с фильтрами и статусами;
2. дать маркетологу отдельный отчёт по купонным кампаниям с выбором кампании;
3. предоставить стабильные URL для дальнейшего развития (добавление графиков и экспортов).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from django.contrib import messages
from django.core.management import CommandError, call_command
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import FileResponse, HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from guests.models import CouponCampaignAssignment, CouponPoolBatch, CouponRegistryEntry, Mailing
from guests.services.coupon_campaign_reporting import build_coupon_campaign_performance_snapshot
from guests.services.coupon_pool import CouponPoolGenerationError, CouponPoolService
from guests.services.coupon_venues import build_coupon_venue_choices


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


def _safe_token(value: str) -> str:
    """
    Нормализует строку для безопасного включения в имя файла.
    """
    token = "".join(ch if ch.isalnum() else "_" for ch in str(value or "").strip())
    return token or "NA"


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
        context["selected_batch"] = (
            CouponPoolBatch.objects.filter(batch_code=batch_code).first()
            if batch_code
            else None
        )
        context["pool_status_choices"] = CouponRegistryEntry.PoolStatus.choices
        context["iiko_check_status_choices"] = CouponRegistryEntry.IikoCheckStatus.choices
        context["coupon_campaign_reports_url"] = reverse("reports_coupon_campaigns")
        context["coupon_ops_url"] = reverse("coupon_registry_ops")
        context["alphabet_mode_choices"] = CouponPoolBatch.AlphabetMode.choices
        context["coupon_venue_choices"], _ = build_coupon_venue_choices()
        context["generate_command_hint"] = (
            "python manage.py generate_coupon_pool --series <SERIES> --venue-code <VENUE_CODE> "
            "--prefix TST- --count 1000 --random-length 12"
        )
        context["verify_command_hint"] = (
            "python manage.py verify_coupon_pool_iiko --series <SERIES> --sample-info-check-limit 50"
        )
        return context


class CouponRegistryOpsView(View):
    """
    POST-операции для экрана реестра купонов.

    Доступные действия:
    1. `generate_pool` — создать пул и экспортировать CSV;
    2. `verify_pool` — запустить проверку загрузки купонов в iikoCard;
    3. `download_csv` — скачать уже сформированный CSV по batch.
    """

    http_method_names = ["post"]

    @staticmethod
    def _resolve_next_url(request) -> str:
        """
        Возвращает безопасный URL возврата после POST-операции.
        """
        default_url = reverse("coupon_registry")
        next_url = str(request.POST.get("next") or "").strip()
        if next_url and url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return next_url
        return default_url

    @staticmethod
    def _redirect_with_query(base_url: str, query: dict[str, str | int]) -> HttpResponseRedirect:
        normalized = {k: str(v) for k, v in query.items() if str(v or "").strip()}
        if normalized:
            return redirect(f"{base_url}?{urlencode(normalized)}")
        return redirect(base_url)

    def post(self, request, *args, **kwargs):
        action = str(request.POST.get("action") or "").strip()
        if action == "generate_pool":
            return self._handle_generate_pool(request)
        if action == "verify_pool":
            return self._handle_verify_pool(request)
        if action == "download_csv":
            return self._handle_download_csv(request)

        messages.error(request, "Неизвестная операция реестра купонов.")
        return redirect(self._resolve_next_url(request))

    def _handle_generate_pool(self, request):
        series = str(request.POST.get("series") or "").strip()
        venue_code = str(request.POST.get("venue_code") or "").strip()
        prefix = str(request.POST.get("prefix") or "").strip().upper()
        batch_code = str(request.POST.get("batch_code") or "").strip()
        generated_by = str(request.POST.get("generated_by") or "").strip()
        export_path_override = str(request.POST.get("export_path") or "").strip()
        alphabet_mode = str(request.POST.get("alphabet_mode") or "").strip()
        include_optional_fields = str(request.POST.get("include_optional_fields") or "").strip() in {"1", "on", "true"}

        count = _parse_positive_int(request.POST.get("count"))
        random_length = _parse_positive_int(request.POST.get("random_length"))

        if not series:
            messages.error(request, "Серия купонов обязательна для генерации.")
            return redirect(self._resolve_next_url(request))
        if not venue_code:
            messages.error(request, "Выберите заведение для генерации пула.")
            return redirect(self._resolve_next_url(request))
        _, venue_map = build_coupon_venue_choices()
        if venue_code not in venue_map:
            messages.error(request, "Выбранное заведение не найдено в справочнике активных заведений.")
            return redirect(self._resolve_next_url(request))
        venue_name = venue_map[venue_code]
        if count is None:
            messages.error(request, "Количество купонов должно быть положительным числом.")
            return redirect(self._resolve_next_url(request))
        if random_length is None:
            messages.error(request, "Длина случайной части должна быть положительным числом.")
            return redirect(self._resolve_next_url(request))
        if alphabet_mode not in {choice[0] for choice in CouponPoolBatch.AlphabetMode.choices}:
            messages.error(request, "Некорректный режим алфавита генерации.")
            return redirect(self._resolve_next_url(request))

        if not generated_by:
            user = getattr(request, "user", None)
            generated_by = str(getattr(user, "username", "") or "").strip() or "ui_operator"

        service = CouponPoolService()
        try:
            result = service.generate_pool(
                series=series,
                prefix=prefix,
                venue_code=venue_code,
                venue_name=venue_name or None,
                count=count,
                random_length=random_length,
                alphabet_mode=alphabet_mode,
                generated_by=generated_by,
                batch_code=batch_code or None,
            )
            if export_path_override:
                export_path = Path(export_path_override)
            else:
                suffix = "series_number_optional" if include_optional_fields else "series_number"
                export_name = (
                    f"iikocard_coupon_import_{_safe_token(series)}_{_safe_token(prefix)}_"
                    f"{count}_{suffix}.csv"
                )
                export_path = Path("tools") / export_name
            csv_path = service.export_batch_csv(
                batch=result.batch,
                output_path=str(export_path),
                include_optional_fields=include_optional_fields,
            )
        except CouponPoolGenerationError as exc:
            messages.error(request, f"Ошибка генерации пула купонов: {exc}")
            return redirect(self._resolve_next_url(request))

        messages.success(
            request,
            (
                f"Пул создан: batch={result.batch.batch_code} (series={result.batch.series}, "
                f"count={result.created_count}, collisions={result.collisions_count}). "
                f"CSV: {csv_path}"
            ),
        )
        return self._redirect_with_query(
            reverse("coupon_registry"),
            {"batch_code": result.batch.batch_code},
        )

    def _handle_verify_pool(self, request):
        series = str(request.POST.get("series") or "").strip()
        batch_code = str(request.POST.get("batch_code") or "").strip()
        sample_info_check_limit = _parse_positive_int(request.POST.get("sample_info_check_limit")) or 2
        page_size = _parse_positive_int(request.POST.get("page_size")) or 500
        max_pages = _parse_positive_int(request.POST.get("max_pages")) or 200

        if not series and not batch_code:
            messages.error(request, "Для проверки укажите серию или batch-код.")
            return redirect(self._resolve_next_url(request))

        try:
            call_command(
                "verify_coupon_pool_iiko",
                series=series,
                batch_code=batch_code,
                sample_info_check_limit=sample_info_check_limit,
                page_size=page_size,
                max_pages=max_pages,
                dry_run=False,
            )
        except CommandError as exc:
            messages.error(request, f"Проверка в iikoCard завершилась с ошибкой: {exc}")
            return redirect(self._resolve_next_url(request))

        if batch_code:
            batch = CouponPoolBatch.objects.filter(batch_code=batch_code).first()
            if batch:
                messages.success(
                    request,
                    (
                        f"Проверка завершена: batch={batch.batch_code}, "
                        f"verification_status={batch.verification_status}, "
                        f"found={batch.verified_found_count}, not_found={batch.verified_not_found_count}."
                    ),
                )
                return self._redirect_with_query(reverse("coupon_registry"), {"batch_code": batch.batch_code})

        messages.success(
            request,
            "Проверка загрузки купонов в iikoCard успешно завершена.",
        )
        return self._redirect_with_query(
            reverse("coupon_registry"),
            {"series": series},
        )

    def _handle_download_csv(self, request):
        batch_code = str(request.POST.get("batch_code") or "").strip()
        if not batch_code:
            messages.error(request, "Укажите batch-код для скачивания CSV.")
            return redirect(self._resolve_next_url(request))

        batch = CouponPoolBatch.objects.filter(batch_code=batch_code).first()
        if batch is None:
            messages.error(request, f"Партия `{batch_code}` не найдена.")
            return redirect(self._resolve_next_url(request))
        if not batch.export_file_path:
            messages.error(
                request,
                (
                    f"У партии `{batch.batch_code}` отсутствует путь к CSV. "
                    "Сначала сформируйте экспорт через генерацию пула."
                ),
            )
            return self._redirect_with_query(reverse("coupon_registry"), {"batch_code": batch.batch_code})

        csv_path = Path(batch.export_file_path).expanduser()
        if not csv_path.is_file():
            messages.error(request, f"CSV-файл не найден по пути: {csv_path}")
            return self._redirect_with_query(reverse("coupon_registry"), {"batch_code": batch.batch_code})

        return FileResponse(
            csv_path.open("rb"),
            as_attachment=True,
            filename=csv_path.name,
            content_type="text/csv",
        )


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
