"""
Non-viewset sales routes — mounted at ``/api/v1/sales/``.

    GET   /sales/aging/                    sub-ledger AR aging, by due date
    POST  /sales/invoices/refresh-overdue/ recompute the derived OVERDUE flag

Why there are two aging reports, and which one to believe
---------------------------------------------------------
``/api/v1/reporting/ar-aging/`` ages the **ledger**: it sums ``JournalLine``
rows on the AR control accounts, so its total is by construction the AR
balance on the balance sheet. It buckets by *accounting date*, because a
journal line has no due date.

This endpoint ages the **sub-ledger**: it buckets open invoices by
``due_date``, which is what a credit controller actually chases on. Terms of
net-30 mean an invoice raised 20 days ago is not late, and the ledger report
cannot know that.

Both are correct answers to different questions and they will legitimately
differ. Each says so in its response — ``basis: "due_date"`` here,
``basis: "entry_date"`` there — because two aging numbers with no stated basis
is how a collections team ends up calling customers who are not overdue.

Why ``refresh-overdue`` is a POST and not a nightly job only
------------------------------------------------------------
It *is* a nightly job (``as_of`` is a parameter precisely so it can run in the
tenant's own time zone). The endpoint exists because "why is this invoice
still showing as current?" is a support question that must be answerable
without waiting for tomorrow, and because the sweep is idempotent in both
directions — an invoice whose due date was extended, or which has just been
paid, is un-lated by the same call.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Sum
from django.urls import path
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.exceptions import ParseError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.fields import ZERO
from apps.core.tenancy_context import get_current_tenant_id
from apps.iam.permissions import HasPermission, build_scope_q
from apps.sales.models import Invoice

#: Upper bound of each bucket, in days past due. ``None`` is the open-ended
#: tail. Chosen to match the ledger-based aging report's default buckets so the
#: two reports can be read side by side.
DEFAULT_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("current", 0),
    ("1_30", 30),
    ("31_60", 60),
    ("61_90", 90),
    ("90_plus", None),
)

#: Statuses that still contribute to accounts receivable.
OPEN_STATUSES = (
    Invoice.Status.SENT,
    Invoice.Status.PARTIALLY_PAID,
    Invoice.Status.OVERDUE,
)


def _as_of(request):
    raw = request.query_params.get("as_of") or (
        request.data.get("as_of") if hasattr(request.data, "get") else None
    )
    if not raw:
        return timezone.localdate()
    parsed = parse_date(str(raw))
    if parsed is None:
        raise ParseError(
            f"'as_of' must be an ISO date (YYYY-MM-DD); got {raw!r}. Guessing "
            f"a format is how an aging report silently ages to the wrong day."
        )
    return parsed


class ARAgingView(APIView):
    """``GET /sales/aging/`` — open invoices bucketed by days past due.

    Scoped through :func:`apps.iam.permissions.build_scope_q` on ``invoice``,
    the same ``Q`` ``/invoices/`` uses, so a department manager whose invoice
    scope is ``assigned_projects`` sees an aging total that matches the list
    they can open. A report that totals rows the caller cannot drill into is
    worse than no report: it discloses the figure and hides the detail.

    Unpaginated and grouped by customer. An aging report cut in half at an
    arbitrary row is not an aging report — the buckets no longer add up to the
    balance — and the set is bounded by the number of *open* invoices, which is
    what makes returning it whole safe.
    """

    permission_classes = [IsAuthenticated, HasPermission]
    required_permissions = {"*": ["reporting.aging.read", "sales.invoice.read"]}

    def get(self, request):
        as_of = _as_of(request)
        scope = build_scope_q(request.user, "invoice", request=request)
        invoices = (
            Invoice.objects.filter(status__in=OPEN_STATUSES, amount_due__gt=ZERO)
            .filter(scope)
            .select_related("customer")
            .order_by("customer__name", "due_date", "number")
        )

        by_customer: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        bucket_totals: dict[str, Decimal] = {name: ZERO for name, _ in DEFAULT_BUCKETS}
        grand_total = ZERO

        for invoice in invoices.iterator(chunk_size=500):
            days_past_due = (as_of - invoice.due_date).days
            bucket = self._bucket_for(days_past_due)
            amount = invoice.amount_due or ZERO

            key = str(invoice.customer_id)
            row = by_customer.get(key)
            if row is None:
                row = {
                    "customer": key,
                    "customer_code": invoice.customer.code,
                    "customer_name": invoice.customer.name,
                    "currency": invoice.currency,
                    "buckets": {name: ZERO for name, _ in DEFAULT_BUCKETS},
                    "total": ZERO,
                    "invoices": [],
                }
                by_customer[key] = row

            row["buckets"][bucket] += amount
            row["total"] += amount
            row["invoices"].append(
                {
                    "id": str(invoice.id),
                    "number": invoice.number,
                    "issue_date": invoice.issue_date.isoformat(),
                    "due_date": invoice.due_date.isoformat(),
                    "days_past_due": days_past_due,
                    "bucket": bucket,
                    "currency": invoice.currency,
                    # Strings, not JSON numbers: numeric(19, 6) does not
                    # survive an IEEE-754 double, and a total that disagrees
                    # with the ledger by a fraction is a support incident.
                    "amount_due": f"{amount:f}",
                }
            )
            bucket_totals[bucket] += amount
            grand_total += amount

        rows = []
        for row in by_customer.values():
            rows.append(
                {
                    **row,
                    "buckets": {k: f"{v:f}" for k, v in row["buckets"].items()},
                    "total": f"{row['total']:f}",
                }
            )
        rows.sort(key=lambda item: Decimal(item["total"]), reverse=True)

        return Response(
            {
                "basis": "due_date",
                "as_of": as_of.isoformat(),
                "buckets": [name for name, _ in DEFAULT_BUCKETS],
                "customers": rows,
                "bucket_totals": {k: f"{v:f}" for k, v in bucket_totals.items()},
                "total_outstanding": f"{grand_total:f}",
                "note": (
                    "Aged from the sales sub-ledger by due date. "
                    "/api/v1/reporting/ar-aging/ ages the general ledger by "
                    "accounting date and ties to the balance sheet; the two "
                    "legitimately differ wherever payment terms are not "
                    "immediate."
                ),
            }
        )

    @staticmethod
    def _bucket_for(days_past_due: int) -> str:
        for name, upper in DEFAULT_BUCKETS:
            if upper is None:
                return name
            if days_past_due <= upper:
                return name
        return DEFAULT_BUCKETS[-1][0]  # pragma: no cover - unreachable


class RefreshOverdueView(APIView):
    """``POST /sales/invoices/refresh-overdue/`` — recompute the OVERDUE flag.

    Delegates to ``apps.sales.services.invoice_workflow.refresh_overdue_status``,
    which is two bulk ``UPDATE``s rather than a loop over model instances: a
    tenant with 200 000 open invoices would otherwise mean 200 000 round trips
    and a transaction held open for minutes.

    Both directions are applied, and that is the point. Lateness is *derived*,
    so it must be able to go away again — extending a due date or receiving a
    payment un-lates the invoice. A one-way sweep leaves invoices stuck in
    OVERDUE forever, which is the bug that makes people stop trusting the aging
    report.

    ``as_of`` is a parameter rather than ``timezone.localdate()`` inline
    because an invoice due on the 31st is not late in Cairo while it is still
    the 31st in Cairo, whatever the server thinks the date is.
    """

    permission_classes = [IsAuthenticated, HasPermission]
    required_permissions = {"*": ["sales.invoice.update"]}

    def post(self, request):
        from apps.sales.services.invoice_workflow import refresh_overdue_status

        tenant_id = getattr(request, "tenant_id", None) or get_current_tenant_id()
        if tenant_id is None:
            raise ParseError("No organisation is bound to this request.")

        as_of = _as_of(request)
        counts = refresh_overdue_status(tenant_id=tenant_id, as_of=as_of)

        outstanding = (
            Invoice.objects.filter(status__in=OPEN_STATUSES).aggregate(
                total=Sum("amount_due")
            )["total"]
            or ZERO
        )
        return Response(
            {
                "as_of": as_of.isoformat(),
                "became_overdue": counts["became_overdue"],
                "no_longer_overdue": counts["no_longer_overdue"],
                "open_receivables": f"{outstanding:f}",
                "next_run_hint": (as_of + timedelta(days=1)).isoformat(),
            },
            status=status.HTTP_200_OK,
        )


urlpatterns = [
    path("aging/", ARAgingView.as_view(), name="aging"),
    path(
        "invoices/refresh-overdue/",
        RefreshOverdueView.as_view(),
        name="invoices-refresh-overdue",
    ),
]
