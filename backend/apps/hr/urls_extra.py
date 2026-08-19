"""
Non-viewset HR routes — mounted at ``/api/v1/hr/``.

    GET  /hr/me/   the calling user's own employee record and leave balances

Why the client never sends its own employee id
----------------------------------------------
Same argument as ``/tenancy/current/``: the id is derivable server-side from
an already-authenticated, already-membership-checked request, and a client
that constructs the URL from a value it stores locally is a client that can be
pointed at the wrong record by a stale cache or a shared device.

The record is served through
:class:`~apps.hr.serializers.EmployeeSelfSerializer` unless the caller holds
``hr.employee.read_compensation`` — even for their *own* record. That looks
over-cautious and is not: this payload is what a self-service screen caches,
and salary in a cached payload is salary on a shared laptop. A caller who is
entitled to compensation data gets the full serializer; everyone else gets
directory fields, which is all a profile screen renders anyway.
"""

from __future__ import annotations

from django.urls import path
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.hr.models import Employee, LeaveBalance
from apps.hr.serializers import (
    EmployeeSelfSerializer,
    EmployeeSerializer,
    LeaveBalanceSerializer,
)
from apps.iam.permissions import HasPermission, resolve_actor_scope, user_permission_set


class MyEmployeeView(APIView):
    """``GET /hr/me/`` — everything a self-service home screen needs, in one call."""

    permission_classes = [IsAuthenticated, HasPermission]
    #: Deliberately empty, and this is the one place in the module where that
    #: is not a mistake.
    #:
    #: The ``Employee`` role does **not** hold ``hr.employee.read`` — that
    #: codename is "may read the staff directory", which is exactly what a
    #: self-service user must not have. Guarding ``/hr/me/`` with it would lock
    #: out every person the endpoint exists for, and the workaround somebody
    #: would then reach for is granting the directory permission to everyone,
    #: which is far worse than this.
    #:
    #: What makes it safe is that there is no parameter. The record is resolved
    #: from ``resolve_actor_scope(request).employee_id`` — the employee linked
    #: to the membership the tenant middleware already validated — so there is
    #: no id a caller could substitute and no row this can be pointed at.
    #: ``HasPermission`` treats an empty list as "authenticated is enough"; a
    #: *missing* table would still deny.
    required_permissions: dict = {"*": []}

    def get(self, request):
        employee_id = resolve_actor_scope(request).employee_id
        if employee_id is None:
            # 404 rather than an empty object: an external auditor or a service
            # account has no employee record at all, and an empty body would
            # read as "your record is blank".
            raise NotFound(
                "This account is not linked to an employee record. Ask an "
                "administrator to connect the login to a person in HR."
            )

        employee = (
            Employee.objects.select_related("department", "job_title", "manager")
            .filter(pk=employee_id)
            .first()
        )
        if employee is None:
            raise NotFound("Your employee record is not visible in this workspace.")

        may_read_compensation = (
            "hr.employee.read_compensation" in user_permission_set(request)
        )
        serializer_class = (
            EmployeeSerializer if may_read_compensation else EmployeeSelfSerializer
        )

        balances = (
            LeaveBalance.objects.filter(
                employee_id=employee_id, year=timezone.localdate().year
            )
            .select_related("leave_type")
            .order_by("leave_type__name")
        )

        return Response(
            {
                "employee": serializer_class(
                    employee, context={"request": request}
                ).data,
                "includes_compensation": may_read_compensation,
                "leave_balances": LeaveBalanceSerializer(
                    balances, many=True, context={"request": request}
                ).data,
            }
        )


urlpatterns = [
    path("me/", MyEmployeeView.as_view(), name="me"),
]
