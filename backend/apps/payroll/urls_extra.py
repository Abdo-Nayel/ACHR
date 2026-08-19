"""
Non-viewset payroll routes — mounted at ``/api/v1/payroll/``.

    GET  /payroll/me/payslips/   the calling employee's own payslips

Why a ``me/`` route exists when ``/payslips/`` is already scoped
---------------------------------------------------------------
``/payslips/`` *is* narrowed to the caller's own rows by the ``own_record``
scope rule, so this endpoint returns the same set for an ordinary employee.
It exists for the two cases where that equivalence breaks:

* A payroll officer or HR manager whose scope is ``all`` gets every payslip
  from ``/payslips/``. The self-service screen in the app must show *their own*
  pay, not the first page of the company's — and a client that filters by
  ``?employee=<id>`` has to know its own employee id, which it can only get by
  asking, which is one more round trip on every page load.
* An account with no linked ``Employee`` (an external auditor, a service
  account) has no "own" pay at all. This answers 404 and says so, rather than
  returning an empty list that reads as "you were not paid".
"""

from __future__ import annotations

from django.urls import path
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pagination import SmallPagePagination
from apps.iam.permissions import HasPermission, build_scope_q, resolve_actor_scope
from apps.payroll.models import Payslip
from apps.payroll.serializers import PayslipSerializer


class MyPayslipsView(APIView):
    """``GET /payroll/me/payslips/`` — the caller's own pay history.

    The ABAC scope for ``payslip`` is applied *as well as* the employee filter,
    not instead of it. Belt and braces on purpose: this is the one collection
    where a mistake means one employee reading another's salary, and the cost
    of the redundant ``Q`` is nothing.
    """

    permission_classes = [IsAuthenticated, HasPermission]
    required_permissions = {"*": ["payroll.payslip.read"]}

    def get(self, request):
        employee_id = resolve_actor_scope(request).employee_id
        if employee_id is None:
            raise NotFound(
                "This account is not linked to an employee record, so it has no "
                "payslips of its own. External auditors and service accounts "
                "read payroll through /payslips/ and the payroll register."
            )

        scope = build_scope_q(request.user, "payslip", request=request)
        queryset = (
            Payslip.objects.filter(employee_id=employee_id)
            .filter(scope)
            .select_related("run", "employee")
            .prefetch_related("lines", "lines__component")
            .order_by("-run__pay_date", "-created_at")
        )

        paginator = SmallPagePagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = PayslipSerializer(page, many=True, context={"request": request}).data
        return paginator.get_paginated_response(data)


urlpatterns = [
    path("me/payslips/", MyPayslipsView.as_view(), name="my-payslips"),
]
