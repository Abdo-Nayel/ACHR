"""
Non-viewset accounting routes — mounted at ``/api/v1/accounting/``.

    GET  /accounting/period-for/   which fiscal period a date falls in, and
                                   whether anything may still be posted to it

Why this is a route and not a client-side calculation
-----------------------------------------------------
"Can I post to 31 December?" is answered by the *server*: it depends on the
fiscal calendar, on ``FiscalPeriod.status`` and — for a soft-closed period —
on whether the caller holds ``accounting.period.post_to_soft_closed``. A client
that derives the answer from a cached calendar will get it wrong the moment
somebody closes a period in another tab, and the user finds out only when a
posting they have already spent five minutes entering is rejected.
"""

from __future__ import annotations

from django.urls import path
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounting.models import FiscalPeriod
from apps.accounting.serializers import FiscalPeriodSerializer
from apps.iam.permissions import HasPermission, user_permission_set


class PeriodForDateView(APIView):
    """``GET /accounting/period-for/?date=YYYY-MM-DD``.

    Returns the period containing the date, its status, and a boolean saying
    whether *this caller* may post into it — the last part is why the answer
    cannot be cached per tenant.
    """

    permission_classes = [IsAuthenticated, HasPermission]
    required_permissions = {"*": ["accounting.fiscal_period.read"]}

    def get(self, request):
        raw = request.query_params.get("date")
        if raw:
            on_date = parse_date(raw)
            if on_date is None:
                raise ParseError(
                    f"'date' must be an ISO date (YYYY-MM-DD); got {raw!r}."
                )
        else:
            on_date = timezone.localdate()

        period = (
            FiscalPeriod.objects.select_related("fiscal_year")
            .filter(start_date__lte=on_date, end_date__gte=on_date)
            .first()
        )
        if period is None:
            # Not an error in the client's request: the calendar simply does
            # not cover that date yet. Saying so names the fix (create the
            # fiscal year) instead of leaving a posting to fail later.
            raise NotFound(
                f"No fiscal period covers {on_date.isoformat()}. Create the "
                f"fiscal year before posting into it — an entry with no period "
                f"cannot be closed, reported on, or locked."
            )

        held = user_permission_set(request)
        status_value = period.status
        may_post = status_value == FiscalPeriod.Status.OPEN or (
            status_value == FiscalPeriod.Status.SOFT_CLOSED
            and "accounting.period.post_to_soft_closed" in held
        )

        return Response(
            {
                "date": on_date.isoformat(),
                "period": FiscalPeriodSerializer(
                    period, context={"request": request}
                ).data,
                "may_post": may_post,
                "reason": (
                    ""
                    if may_post
                    else (
                        f"Period {period.name} is "
                        f"{period.get_status_display().lower()}. Post to the "
                        f"next open period, or ask someone holding "
                        f"accounting.period.post_to_soft_closed to enter it."
                    )
                ),
            }
        )


urlpatterns = [
    path("period-for/", PeriodForDateView.as_view(), name="period-for"),
]
