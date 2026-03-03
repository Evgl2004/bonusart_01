from datetime import datetime
from django.http import StreamingHttpResponse
from django.views import View
from django.db.models import Count
from django.utils import timezone
from django.shortcuts import get_object_or_404, render

from .models import Mailing, MailingGuest,GuestChannelLink,MailingChannel


class MailingLogsView(View):
    template_name = "mailing/mailing_logs.html"

    def get(self, request, pk: int):
        mailing = get_object_or_404(Mailing, pk=pk)
        qs = MailingGuest.objects.filter(mailing=mailing)

        total = qs.count()
        by_status = dict(
            qs.values("status")
              .annotate(c=Count("id"))
              .values_list("status", "c")
        )

        stats = {
            "total": total,
            "planned": by_status.get(MailingGuest.Status.PLANNED, 0),
            "in_progress": by_status.get(MailingGuest.Status.IN_PROGRESS, 0),
            "done": by_status.get(MailingGuest.Status.DONE, 0),
            "error": by_status.get(MailingGuest.Status.ERROR, 0),
        }

        delivery_stats = list(
            qs.exclude(delivery_status__isnull=True)
              .exclude(delivery_status__exact="")
              .values("delivery_status")
              .annotate(c=Count("id"))
              .order_by("-c")
        )

        last_rows = list(
            qs.select_related("guest")
              .order_by("-id")[:100]
        )

        return render(request, self.template_name, {
            "mailing": mailing,
            "stats": stats,
            "delivery_stats": delivery_stats,
            "last_rows": last_rows,
        })


class MailingLogsDownloadTxtView(View):
    def get(self, request, pk: int):
        mailing = get_object_or_404(Mailing, pk=pk)

        qs = (MailingGuest.objects
              .filter(mailing=mailing)
              .select_related("guest")
              .order_by("id"))

        def line_iter():
            yield f"Mailing #{mailing.id}: {mailing.name}\n"
            yield f"Generated at: {timezone.now().isoformat()}\n"
            yield "-" * 120 + "\n"
            yield "row_id\tguest_id\tstatus\tdelivery_status\tsent_at\texternal_id\tscheduled_datetime\terror\n"

            for r in qs.iterator(chunk_size=2000):
                sent_at = r.sent_at.isoformat() if r.sent_at else ""
                sched = r.scheduled_datetime.isoformat() if r.scheduled_datetime else ""
                err = (r.error_description or "").replace("\t", " ").replace("\n", " ")[:500]
                ext = r.external_id or ""
                ds = r.delivery_status or ""
                yield f"{r.id}\t{r.guest_id}\t{r.status}\t{ds}\t{sent_at}\t{ext}\t{sched}\t{err}\n"

        resp = StreamingHttpResponse(line_iter(), content_type="text/plain; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="mailing_{mailing.id}_log.txt"'
        return resp