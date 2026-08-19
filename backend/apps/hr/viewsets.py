"""
HR viewsets.

Two access-control layers, both required
---------------------------------------
* **ABAC (rows).** ``employee``, ``attendance``, ``leave_request``,
  ``leave_balance`` and ``document`` all have ``ScopeRule`` entries and
  matching ``SCOPE_FIELDS``, so ``ScopedQuerysetMixin`` (inherited through
  ``TenantModelViewSet``) narrows every queryset to the actor's own record,
  department subtree or direct reports.
* **Field-level (columns).** Row scope is not enough: a department manager
  legitimately sees their whole team's rows and must still not see their
  salaries. :class:`~apps.hr.serializers.EmployeeSelfSerializer` is chosen for
  every caller without ``hr.employee.read_compensation``.

Configuration resources (job titles, shifts, holidays, leave types) have no
scope rule; ``build_scope_q`` fails closed, so they opt out of ABAC explicitly
via :class:`RbacOnlyQuerysetMixin` rather than returning an empty list to
everybody.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from apps.core.pagination import SmallPagePagination
from apps.core.tenancy_context import get_current_tenant_id
from apps.core.serializers import ReasonRequiredTransitionSerializer, TransitionSerializer
from apps.core.viewsets import TenantModelViewSet, raise_as_api_error
from apps.hr.models import (
    AttendanceRecord,
    Department,
    Employee,
    EmployeeDocument,
    Holiday,
    JobTitle,
    LeaveBalance,
    LeaveRequest,
    LeaveType,
    OvertimeSlip,
    OvertimeType,
    SalaryRevision,
    Shift,
    ShiftAssignment,
)
from apps.hr.serializers import (
    AttendanceRecordSerializer,
    CheckInSerializer,
    DepartmentSerializer,
    EmployeeDocumentSerializer,
    EmployeeSelfSerializer,
    EmployeeSerializer,
    HolidaySerializer,
    JobTitleSerializer,
    LeaveBalanceSerializer,
    LeaveDecisionSerializer,
    LeaveRequestSerializer,
    LeaveTypeSerializer,
    OvertimeSlipSerializer,
    OvertimeTypeSerializer,
    SalaryRevisionSerializer,
    ShiftAssignmentSerializer,
    ShiftSerializer,
    TerminateEmployeeSerializer,
)
from apps.accounting.viewsets import IdempotentActionMixin
from apps.core.exceptions import DomainError
from apps.hr.services.overtime import approve_slip
from apps.iam.permissions import resolve_actor_scope, user_permission_set

#: The permission that separates "may see that this person exists" from "may
#: see what they earn". Referenced in several places, so it is named once.
READ_COMPENSATION = "hr.employee.read_compensation"


def may_read_compensation(request) -> bool:
    return READ_COMPENSATION in user_permission_set(request)


def actor_employee_id(request):
    """The ``Employee`` linked to the calling user, or None.

    An external auditor or a service account has no employee record at all;
    every ``me/`` endpoint therefore 404s for them rather than guessing.
    """
    return resolve_actor_scope(request).employee_id


class RbacOnlyQuerysetMixin:
    """Tenant-scoped and RBAC-guarded, but not ABAC-filtered.

    For HR *configuration* only — shifts, holidays, job titles, leave types.
    Never for a resource that names an employee: those all have scope rules
    and must keep them.
    """

    def get_queryset(self):
        # ``self.queryset.model._default_manager.all()``, never
        # ``self.queryset.all()``. The class attribute was evaluated at import
        # time, with no tenant bound, so ``TenantManager`` failed closed and
        # froze an empty queryset for the life of the process — ``.all()`` on
        # ``.none()`` is still nothing. The symptom is the worst kind: HTTP
        # 200, a well-formed envelope and an empty ``results`` array on every
        # request, with no error anywhere. Re-deriving from the manager runs it
        # inside the request, where the tenant actually is bound. This mirrors
        # ``apps.core.viewsets.TenantViewSetMixin.get_queryset``, which
        # documents the same trap.
        queryset = self.queryset.model._default_manager.all()
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.prefetch_related:
            queryset = queryset.prefetch_related(*self.prefetch_related)
        ordering = getattr(self, "ordering", None)
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset


class CompensationContextMixin:
    """Adds ``may_read_compensation`` to the serializer context."""

    def get_serializer_context(self):
        context = super().get_serializer_context()
        request = getattr(self, "request", None)
        context["may_read_compensation"] = (
            may_read_compensation(request) if request is not None else False
        )
        return context


# ---------------------------------------------------------------------------
# Org structure
# ---------------------------------------------------------------------------

class DepartmentViewSet(TenantModelViewSet):
    """Departments. ``department`` has its own scope rule (own / subtree)."""

    permission_domain = "hr"
    resource = "department"
    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer
    select_related = ("parent", "manager")
    pagination_class = SmallPagePagination
    search_fields = ("code", "name")
    filterset_fields = ("parent", "is_active")
    extra_permissions = {
        "PUT": ["hr.department.update"],
        "PATCH": ["hr.department.update"],
        "DELETE": ["hr.department.archive"],
    }


class JobTitleViewSet(CompensationContextMixin, RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Job titles and their salary bands (bands hidden without compensation
    permission — see :class:`~apps.hr.serializers.JobTitleSerializer`)."""

    permission_domain = "hr"
    resource = "department"
    queryset = JobTitle.objects.all()
    serializer_class = JobTitleSerializer
    select_related = ("department",)
    pagination_class = SmallPagePagination
    search_fields = ("code", "name", "grade")
    filterset_fields = ("department", "is_active")
    extra_permissions = {
        "PUT": ["hr.department.update"],
        "PATCH": ["hr.department.update"],
        "DELETE": ["hr.department.archive"],
    }


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

class EmployeeViewSet(CompensationContextMixin, TenantModelViewSet):
    """Employee records.

    ``get_serializer_class`` is where the redaction happens. Write responses
    are re-serialised through the *read* serializer so that a caller who may
    update a phone number but may not read compensation does not receive the
    salary back in the 200 body of their own PATCH.
    """

    permission_domain = "hr"
    resource = "employee"
    queryset = Employee.objects.all()
    serializer_class = EmployeeSerializer
    select_related = ("department", "job_title", "manager", "work_schedule")
    search_fields = ("employee_code", "first_name", "last_name", "work_email")
    filterset_fields = ("department", "job_title", "manager", "status",
                        "employment_type")
    extra_permissions = {
        "leave_balances": ["hr.leave_balance.read"],
        "documents": ["hr.document.read"],
        "terminate": ["hr.employee.terminate"],
        "DELETE": ["hr.employee.terminate"],
    }

    def _read_serializer_class(self):
        return (
            EmployeeSerializer
            if may_read_compensation(self.request)
            else EmployeeSelfSerializer
        )

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            # The redacted serializer is read-only by construction, so writes
            # always bind against the full one; the compensation columns are
            # refused inside the serializer for callers without the grant.
            return EmployeeSerializer
        return self._read_serializer_class()

    def _read_response(self, instance, status_code=status.HTTP_200_OK):
        serializer = self._read_serializer_class()(
            instance, context=self.get_serializer_context()
        )
        return Response(serializer.data, status=status_code)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return self._read_response(serializer.instance, status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return self._read_response(serializer.instance)

    # -- sub-resources ------------------------------------------------------

    @action(detail=True, methods=["get"], url_path="leave-balances")
    def leave_balances(self, request, pk=None):
        """This employee's balances, current year first."""
        employee = self.get_object()
        balances = (
            LeaveBalance.objects.filter(employee=employee)
            .select_related("leave_type")
            .order_by("-year", "leave_type__code")
        )
        year = request.query_params.get("year")
        if year:
            balances = balances.filter(year=year)
        return Response(
            LeaveBalanceSerializer(
                balances, many=True, context=self.get_serializer_context()
            ).data
        )

    @action(detail=True, methods=["get"], url_path="documents")
    def documents(self, request, pk=None):
        """This employee's documents.

        Confidential documents (medical certificates, disciplinary letters,
        salary agreements) are filtered out unless the caller is an HR role or
        the employee themselves. A manager who may list their team's documents
        is exactly the caller this filter exists for.
        """
        employee = self.get_object()
        documents = EmployeeDocument.objects.filter(employee=employee)
        is_self = actor_employee_id(request) == employee.id
        if not (may_read_compensation(request) or is_self):
            documents = documents.filter(is_confidential=False)
        return Response(
            EmployeeDocumentSerializer(
                documents.order_by("-created_at"),
                many=True,
                context=self.get_serializer_context(),
            ).data
        )

    @action(detail=True, methods=["post"], url_path="terminate")
    def terminate(self, request, pk=None):
        """Record a leaving. ACTIVE/ON_LEAVE/SUSPENDED -> TERMINATED/RESIGNED.

        A POST sub-resource rather than a status field because termination has
        consequences the column alone does not carry: the date drives
        end-of-service, the final payslip proration and the statutory filing,
        and ``ck_hr_employee_terminated_has_date`` refuses the row without it.
        """
        employee = self.get_object()
        body = TerminateEmployeeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        payload = body.validated_data

        try:
            with transaction.atomic():
                locked = Employee.objects.select_for_update().get(pk=employee.pk)
                locked.assert_can_transition(payload["outcome"])
                locked.status = payload["outcome"]
                locked.termination_date = payload["termination_date"]
                locked.termination_reason = payload["reason"]
                locked.updated_by_id = request.user.id
                locked.save(update_fields=[
                    "status", "termination_date", "termination_reason",
                    "updated_by", "updated_at",
                ])
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return self._read_response(locked)


class EmployeeDocumentViewSet(TenantModelViewSet):
    """Documents on employee records. ABAC resource: ``document``."""

    permission_domain = "hr"
    resource = "document"
    queryset = EmployeeDocument.objects.all()
    serializer_class = EmployeeDocumentSerializer
    select_related = ("employee", "uploaded_by")
    filterset_fields = ("employee", "document_type", "is_confidential")
    extra_permissions = {
        "POST": ["hr.document.manage"],
        "PUT": ["hr.document.manage"],
        "PATCH": ["hr.document.manage"],
        "DELETE": ["hr.document.manage"],
    }

    def get_queryset(self):
        queryset = super().get_queryset()
        request = getattr(self, "request", None)
        if request is None:
            return queryset
        if may_read_compensation(request):
            return queryset
        # Same rule as the employee sub-resource: your own confidential
        # documents are yours to read; other people's are not.
        own = actor_employee_id(request)
        if own is not None:
            return queryset.filter(Q(is_confidential=False) | Q(employee_id=own))
        return queryset.filter(is_confidential=False)


class SalaryRevisionViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Pay-change history.

    Guarded end to end by ``hr.employee.read_compensation``: this table *is*
    compensation data, so there is no redacted variant to fall back to, and a
    caller without the grant sees nothing at all rather than a row count that
    tells them a colleague got a raise last month.
    """

    permission_domain = "hr"
    resource = "employee"
    queryset = SalaryRevision.objects.all()
    serializer_class = SalaryRevisionSerializer
    select_related = ("employee", "new_job_title", "new_department")
    filterset_fields = ("employee", "change_type", "effective_date")
    required_permissions = {
        "GET": [READ_COMPENSATION],
        "HEAD": [READ_COMPENSATION],
        "OPTIONS": [READ_COMPENSATION],
        "POST": [READ_COMPENSATION, "hr.employee.update"],
        "PUT": [READ_COMPENSATION, "hr.employee.update"],
        "PATCH": [READ_COMPENSATION, "hr.employee.update"],
        "DELETE": [READ_COMPENSATION, "hr.employee.update"],
    }

    def perform_create(self, serializer):
        # ``previous_salary`` is taken from the employee row, never from the
        # client: a revision that misstates the old salary makes every
        # historical payslip unexplainable.
        employee = serializer.validated_data["employee"]
        serializer.save(
            previous_salary=employee.base_salary,
            currency=employee.salary_currency,
        )


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

class ShiftViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Working patterns. Attendance configuration, not employee data."""

    permission_domain = "hr"
    resource = "attendance"
    queryset = Shift.objects.all()
    serializer_class = ShiftSerializer
    pagination_class = SmallPagePagination
    search_fields = ("code", "name")
    filterset_fields = ("is_active",)


class HolidayViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Public and company holidays."""

    permission_domain = "hr"
    resource = "attendance"
    queryset = Holiday.objects.all()
    serializer_class = HolidaySerializer
    select_related = ("applies_to_department",)
    pagination_class = SmallPagePagination
    filterset_fields = ("date", "is_paid", "applies_to_department")


class AttendanceRecordViewSet(TenantModelViewSet):
    """Attendance, including the mobile check-in/check-out pair."""

    permission_domain = "hr"
    resource = "attendance"
    queryset = AttendanceRecord.objects.all()
    serializer_class = AttendanceRecordSerializer
    select_related = ("employee", "scheduled_shift")
    filterset_fields = ("employee", "work_date", "status", "source")
    ordering_fields = ("work_date", "check_in_at")
    extra_permissions = {
        "check_in": ["hr.attendance.create"],
        "check_out": ["hr.attendance.create"],
    }

    # -- helpers ------------------------------------------------------------

    def _target_employee_id(self, request, body):
        """Whose attendance is being recorded.

        Defaults to the caller's own employee record. Naming somebody else
        requires ``hr.attendance.update`` — the grant a supervisor holds for
        correcting a terminal misread. Without it, a request that names
        another employee is refused rather than silently redirected to the
        caller, because silently recording your own attendance when you meant
        to record a colleague's produces two wrong rows.
        """
        requested = body.validated_data.get("employee")
        own = actor_employee_id(request)
        if requested is None:
            if own is None:
                raise NotFound(
                    "Your user account is not linked to an employee record, so "
                    "attendance cannot be recorded for you."
                )
            return own
        if str(requested) != str(own):
            if "hr.attendance.update" not in user_permission_set(request):
                raise PermissionDenied(
                    "Recording attendance for another employee requires "
                    "hr.attendance.update."
                )
        return requested

    @staticmethod
    def _worked_hours(record: AttendanceRecord) -> tuple[Decimal, Decimal]:
        """(worked, overtime) in hours, to six decimal places.

        Break minutes come from the scheduled shift, not from the device: a
        client that reported its own unpaid break would be reporting on the
        thing it is being measured by.
        """
        if not (record.check_in_at and record.check_out_at):
            return Decimal("0.000000"), Decimal("0.000000")
        seconds = (record.check_out_at - record.check_in_at).total_seconds()
        hours = Decimal(str(seconds / 3600))
        shift = record.scheduled_shift
        if shift is not None and shift.break_minutes:
            hours -= Decimal(shift.break_minutes) / Decimal(60)
        if hours < 0:
            hours = Decimal(0)
        hours = hours.quantize(Decimal("0.000001"))

        overtime = Decimal("0.000000")
        if shift is not None and shift.overtime_after_hours:
            excess = hours - Decimal(shift.overtime_after_hours)
            if excess > 0:
                overtime = excess.quantize(Decimal("0.000001"))
        return hours, overtime

    # -- actions ------------------------------------------------------------

    @action(detail=False, methods=["post"], url_path="check-in")
    def check_in(self, request):
        """Start the working day. Idempotent per employee per day.

        ``uq_hr_attendance_day`` allows exactly one record per (employee,
        work_date), which is what makes this safe to retry: a mobile client on
        a bad connection sends the same check-in three times, and all three
        return the same record with ``already_checked_in: true`` rather than
        three rows or a 500 from the unique index.

        The *first* check-in time wins. Overwriting it on a retry would move
        somebody's arrival later — quietly turning a punctual employee into a
        late one — and the retry is exactly when the client cannot tell
        whether the first request landed.
        """
        body = CheckInSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        employee_id = self._target_employee_id(request, body)
        work_date = timezone.localdate()

        with transaction.atomic():
            record, created = AttendanceRecord.objects.get_or_create(
                employee_id=employee_id,
                work_date=work_date,
                defaults={
                    "tenant_id": (
                        getattr(request, "tenant_id", None) or get_current_tenant_id()
                    ),
                    "check_in_at": timezone.now(),
                    "check_in_location": body.location(),
                    "source": body.validated_data["source"],
                    "notes": body.validated_data.get("notes", ""),
                    "created_by_id": request.user.id,
                    "updated_by_id": request.user.id,
                },
            )
            already = not created and record.check_in_at is not None
            if not already:
                record = (
                    AttendanceRecord.objects.select_for_update().get(pk=record.pk)
                )
                if record.check_in_at is None:
                    record.check_in_at = timezone.now()
                    record.check_in_location = body.location()
                    record.source = body.validated_data["source"]
                    record.updated_by_id = request.user.id
                    record.save(update_fields=[
                        "check_in_at", "check_in_location", "source",
                        "updated_by", "updated_at",
                    ])

        return Response(
            {
                "already_checked_in": already,
                "attendance": self.get_serializer(record).data,
            },
            status=status.HTTP_200_OK if already else status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="check-out")
    def check_out(self, request):
        """End the working day. Idempotent: a repeat returns the same record.

        The *last* check-out would be the intuitive choice, but a retry cannot
        be told apart from a genuine second departure, so the first one is
        kept and a correction goes through ``hr.attendance.update`` where it
        leaves an audit trail.
        """
        body = CheckInSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        employee_id = self._target_employee_id(request, body)
        work_date = timezone.localdate()

        with transaction.atomic():
            record = (
                AttendanceRecord.objects.select_for_update()
                .select_related("scheduled_shift")
                .filter(employee_id=employee_id, work_date=work_date)
                .first()
            )
            if record is None:
                raise NotFound(
                    "There is no check-in for today, so there is nothing to "
                    "check out of."
                )
            already = record.check_out_at is not None
            if not already:
                record.check_out_at = timezone.now()
                record.check_out_location = body.location()
                worked, overtime = self._worked_hours(record)
                record.worked_hours = worked
                record.overtime_hours = overtime
                record.updated_by_id = request.user.id
                record.save(update_fields=[
                    "check_out_at", "check_out_location", "worked_hours",
                    "overtime_hours", "updated_by", "updated_at",
                ])

        return Response(
            {
                "already_checked_out": already,
                "attendance": self.get_serializer(record).data,
            }
        )


# ---------------------------------------------------------------------------
# Leave
# ---------------------------------------------------------------------------

class LeaveTypeViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Leave policy configuration."""

    permission_domain = "hr"
    resource = "leave_type"
    queryset = LeaveType.objects.all()
    serializer_class = LeaveTypeSerializer
    pagination_class = SmallPagePagination
    search_fields = ("code", "name")
    filterset_fields = ("is_active", "is_paid", "affects_payroll")
    extra_permissions = {
        "POST": ["hr.leave_type.manage"],
        "PUT": ["hr.leave_type.manage"],
        "PATCH": ["hr.leave_type.manage"],
        "DELETE": ["hr.leave_type.manage"],
    }


class LeaveBalanceViewSet(TenantModelViewSet):
    """Entitlements. Read-mostly: every day column is service-owned.

    Creating and updating balances is not a client operation — accrual,
    consumption and carry-over all happen in ``apps.hr.services.leave`` under
    a row lock. The only legitimate manual change is an adjustment, which is
    gated on the sensitive ``hr.leave_balance.adjust`` permission.
    """

    permission_domain = "hr"
    resource = "leave_balance"
    queryset = LeaveBalance.objects.all()
    serializer_class = LeaveBalanceSerializer
    select_related = ("employee", "leave_type")
    filterset_fields = ("employee", "leave_type", "year")
    extra_permissions = {
        "POST": ["hr.leave_balance.adjust"],
        "PUT": ["hr.leave_balance.adjust"],
        "PATCH": ["hr.leave_balance.adjust"],
        "DELETE": ["hr.leave_balance.adjust"],
    }


class LeaveRequestViewSet(TenantModelViewSet):
    """Leave requests. Every transition delegates to ``apps.hr.services.leave``.

    Nothing in this class implements policy: eligibility, overlap detection,
    the balance lock and the manager -> HR approval chain all live in the
    service, which is also what the accrual cron and the mobile offline outbox
    call. A second implementation here would be a second thing to keep in step
    with the labour code.
    """

    permission_domain = "hr"
    resource = "leave_request"
    queryset = LeaveRequest.objects.all()
    serializer_class = LeaveRequestSerializer
    select_related = ("employee", "leave_type", "current_approver")
    prefetch_related = ("approvals",)
    filterset_fields = ("employee", "leave_type", "status", "start_date")
    extra_permissions = {
        "submit": ["hr.leave_request.create"],
        "approve": ["hr.leave_request.approve"],
        "reject": ["hr.leave_request.reject"],
        "cancel": ["hr.leave_request.cancel"],
    }

    def _run(self, func, *args, **kwargs) -> LeaveRequest:
        try:
            with transaction.atomic():
                return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        """DRAFT -> the approval chain. Validates eligibility and balance."""
        from apps.hr.services import leave

        instance = self.get_object()
        result = self._run(leave.submit_request, instance, user_id=request.user.id)
        return Response(self.get_serializer(result).data)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """Record one approval step and advance the chain.

        The service checks the approver's authority itself — it is called from
        places that have no request — so this action does not second-guess it.
        """
        from apps.hr.services import leave

        instance = self.get_object()
        body = LeaveDecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        result = self._run(
            leave.approve_step,
            instance,
            request.user,
            comment=body.validated_data.get("comment", ""),
        )
        return Response(self.get_serializer(result).data)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        """Refuse the request and release the days it was holding."""
        from apps.hr.services import leave

        instance = self.get_object()
        body = ReasonRequiredTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        result = self._run(
            leave.reject, instance, request.user, reason=body.validated_reason()
        )
        return Response(self.get_serializer(result).data)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """Withdraw the request. Legal even after approval — plans change, and
        this is the only path that returns the days to the balance."""
        from apps.hr.services import leave

        instance = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        result = self._run(
            leave.cancel,
            instance,
            user_id=request.user.id,
            reason=body.validated_reason(),
        )
        return Response(self.get_serializer(result).data)


class ShiftAssignmentViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Which shift an employee works, over a date range.

    Attendance *configuration*, so it sits under ``hr.attendance`` rather than
    ``hr.employee``: a scheduler who rosters the night shift needs this and
    has no business reading compensation.
    """

    permission_domain = "hr"
    resource = "attendance"
    queryset = ShiftAssignment.objects.all()
    serializer_class = ShiftAssignmentSerializer
    select_related = ("employee", "shift")
    pagination_class = SmallPagePagination
    filterset_fields = ("employee", "shift", "start_date")
    search_fields = ("employee__employee_code", "location")
    ordering_fields = ("start_date", "created_at")
    ordering = ("-start_date",)


class OvertimeTypeViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Overtime categories and their multipliers. Reference data."""

    permission_domain = "hr"
    resource = "attendance"
    queryset = OvertimeType.objects.all()
    serializer_class = OvertimeTypeSerializer
    select_related = ("component",)
    pagination_class = SmallPagePagination
    filterset_fields = ("is_active",)
    search_fields = ("code", "name")
    ordering = ("code",)


class OvertimeSlipViewSet(IdempotentActionMixin, TenantModelViewSet):
    """Overtime claims, from draft to approved.

    ``submit`` and ``approve`` are separate sub-resources carrying separate
    permissions, for the same reason expense claims do: everyone who works
    overtime needs to claim it, and almost nobody should certify it. A
    writable ``status`` would collapse those into one authority.
    """

    permission_domain = "hr"
    resource = "attendance"
    queryset = OvertimeSlip.objects.all()
    serializer_class = OvertimeSlipSerializer
    select_related = ("employee", "overtime_type", "overtime_type__component",
                      "approved_by", "payroll_run")
    filterset_fields = ("employee", "overtime_type", "status", "work_date")
    search_fields = ("employee__employee_code", "notes")
    ordering_fields = ("work_date", "amount", "created_at")
    ordering = ("-work_date", "-created_at")
    extra_permissions = {
        "submit": ["hr.attendance.create"],
        "approve": ["hr.attendance.approve"],
        "reject": ["hr.attendance.approve"],
    }

    def _actor_id(self):
        return getattr(getattr(self.request, "user", None), "id", None)

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        """DRAFT -> SUBMITTED. The claim is now somebody else's to judge."""
        slip = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        def run(_key):
            try:
                slip.assert_can_transition(OvertimeSlip.Status.SUBMITTED)
            except ValueError as exc:
                raise DomainError(str(exc)) from exc
            slip.status = OvertimeSlip.Status.SUBMITTED
            slip.updated_by_id = self._actor_id()
            slip.save(update_fields=["status", "updated_by", "updated_at"])
            return slip

        return self.run_idempotent(request, transition="submit", run=run)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """SUBMITTED -> APPROVED, pricing the slip as it goes.

        The pricing happens here and not at claim time because the rate is
        derived from the salary in force on the day worked, and approval is
        the moment the figure becomes something the company owes.
        """
        slip = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        def run(_key):
            try:
                return approve_slip(slip, user_id=self._actor_id())
            except ValueError as exc:
                raise DomainError(str(exc)) from exc

        return self.run_idempotent(request, transition="approve", run=run)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        """SUBMITTED -> REJECTED. The reason is mandatory.

        ``ck_hr_overtime_rejected_has_reason`` agrees at the database level: a
        rejection with no reason leaves the claimant guessing at what would
        pass, and the guess is usually "resubmit unchanged".
        """
        slip = self.get_object()
        body = ReasonRequiredTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reason = body.validated_reason()

        def run(_key):
            try:
                slip.assert_can_transition(OvertimeSlip.Status.REJECTED)
            except ValueError as exc:
                raise DomainError(str(exc)) from exc
            slip.status = OvertimeSlip.Status.REJECTED
            slip.rejected_reason = reason[:255]
            slip.updated_by_id = self._actor_id()
            slip.save(update_fields=[
                "status", "rejected_reason", "updated_by", "updated_at",
            ])
            return slip

        return self.run_idempotent(request, transition="reject", run=run)
