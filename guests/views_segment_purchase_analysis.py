from __future__ import annotations

from django.views.generic import TemplateView

from guests.services.segment_purchase_analysis import build_segment_purchase_analysis_payload


class SegmentPurchaseAnalysisView(TemplateView):
    template_name = "guests/segment_purchase_analysis.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payload = build_segment_purchase_analysis_payload(
            department_id=self.request.GET.get("department_id"),
            segment_code=self.request.GET.get("segment_code"),
            period_days=self.request.GET.get("period_days"),
            top_limit=self.request.GET.get("top_limit"),
        )
        context["payload"] = payload
        context["selected_department_id"] = payload["filters"]["department_id"]
        context["selected_segment_code"] = payload["filters"]["segment_code"]
        context["selected_period_days"] = int(payload["filters"]["period_days"])
        context["selected_top_limit"] = int(payload["filters"]["top_limit"])
        return context
