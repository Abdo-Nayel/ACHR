"""
Projects viewsets.

ABAC is the point of this module
--------------------------------
An employee may see *their own* timesheet entries and nothing else. That is
not a UI convenience: an employee's hours reveal what they were paid for,
which client they worked on and how long a competitor's project took. The
``employee`` role's ``ScopeRule`` for ``timesheet_entry`` is ``own_record``,
and :class:`apps.iam.permissions.ScopedQuerysetMixin` — inherited through
``TenantModelViewSet`` — compiles it into a ``Q`` on every list, retrieve and
custom action, because ``get_object()`` runs against the scoped queryset too.

The scope resources used here are the ones that actually exist in
``config/permissions.json``: ``project``, ``task`` and ``timesheet_entry``.
``ProjectMember`` and ``ProjectMilestone`` have no scope rule of their own —
and ``build_scope_q`` fails closed — so they are narrowed to the projects the
caller can already see, which is the same answer expressed against a resource
that has a rule.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.fields import ZERO
from apps.core.serializers import (
    ReasonRequiredTransitionSerializer,
    TransitionSerializer,
)
from apps.core.viewsets import TenantModelViewSet, raise_as_api_error
from apps.iam.permissions import assert_not_self_prepared, build_scope_q
from apps.projects.models import (
    Project,
    ProjectMember,
    ProjectMilestone,
    ProjectTask,
    TimesheetEntry,
)
from apps.projects.serializers import (
    BulkApproveSerializer,
    CreateInvoiceSerializer,
    ProjectMemberSerializer,
    ProjectMilestoneSerializer,
    ProjectSerializer,
    ProjectTaskSerializer,
    TimesheetEntrySerializer,
)


def visible_projects(request):
    """Projects the caller may see, per the ``project`` scope rule.

    Used to narrow resources that have no scope rule of their own. Expressing
    "members of projects you can see" in terms of the project scope keeps one
    authorisation answer instead of two that can drift.
    """
    return Project.objects.filter(build_scope_q(request.user, "project", request=request))


class ProjectViewSet(TenantModelViewSet):
    """Projects, their profitability and their time-to-invoice action."""

    permission_domain = "projects"
    resource = "project"
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    select_related = ("customer", "manager")
    search_fields = ("code", "name")
    filterset_fields = ("status", "billing_type", "customer", "manager", "is_billable")
    extra_permissions = {
        "profitability": ["projects.project.read"],
        "unbilled_time": ["projects.project.read"],
        # Turning time into an invoice creates a sales document, so it needs
        # the sales grant as well as the project one. Requiring both is what
        # stops a project manager with no sales access from issuing invoices.
        "create_invoice": ["projects.project.update", "sales.invoice.create"],
        "DELETE": ["projects.project.archive"],
    }

    # -- read actions -------------------------------------------------------

    @action(detail=True, methods=["get"], url_path="profitability")
    def profitability(self, request, pk=None):
        """Revenue, cost and margin for one project.

        Cost is the *cost* rate on timesheets (what the person costs us), not
        the billable rate. Reporting margin against the billable rate would
        make every project look 100% profitable, which is the single most
        common way a professional-services P&L lies.
        """
        project = self.get_object()
        entries = TimesheetEntry.objects.filter(project=project)

        billable = entries.filter(
            is_billable=True,
            status__in=[
                TimesheetEntry.Status.APPROVED, TimesheetEntry.Status.INVOICED,
            ],
        )
        billable_value = sum((e.billable_amount for e in billable), ZERO)
        cost_value = sum((e.cost_amount for e in entries), ZERO)
        hours = entries.aggregate(total=Sum("hours"))["total"] or ZERO

        revenue = Decimal(project.invoiced_amount)
        margin = revenue - Decimal(cost_value)
        return Response(
            {
                "project": str(project.pk),
                "currency": project.currency,
                "hours_logged": str(hours),
                "billable_value": str(billable_value),
                "cost_incurred": str(cost_value),
                "invoiced_amount": str(revenue),
                "margin_amount": str(margin),
                "budget_amount": str(project.budget_amount),
                "budget_hours": str(project.budget_hours),
                # Value approved but not yet invoiced: the number a partner
                # actually chases at month end.
                "unbilled_value": str(
                    sum(
                        (
                            e.billable_amount
                            for e in billable
                            if e.status == TimesheetEntry.Status.APPROVED
                            and e.invoice_line_id is None
                        ),
                        ZERO,
                    )
                ),
            }
        )

    @action(detail=True, methods=["get"], url_path="unbilled-time")
    def unbilled_time(self, request, pk=None):
        """Approved, billable, un-invoiced entries — the invoice preview."""
        project = self.get_object()
        entries = (
            TimesheetEntry.objects.filter(
                project=project,
                status=TimesheetEntry.Status.APPROVED,
                is_billable=True,
                invoice_line__isnull=True,
            )
            .select_related("employee", "task")
            .order_by("work_date", "created_at")
        )
        total_hours = ZERO
        total_amount = ZERO
        rows = []
        for entry in entries:
            total_hours += Decimal(entry.hours)
            total_amount += Decimal(entry.billable_amount)
            rows.append(
                {
                    "id": str(entry.pk),
                    "work_date": entry.work_date.isoformat(),
                    "employee_id": str(entry.employee_id),
                    "employee_name": entry.employee.full_name,
                    "task_id": str(entry.task_id) if entry.task_id else None,
                    "task_name": entry.task.name if entry.task_id else "",
                    "description": entry.description,
                    "hours": str(entry.hours),
                    "billable_rate": str(entry.billable_rate),
                    "amount": str(entry.billable_amount),
                    "currency": entry.currency,
                }
            )
        return Response(
            {
                "project": str(project.pk),
                "currency": project.currency,
                "entry_count": len(rows),
                "total_hours": str(total_hours),
                "total_amount": str(total_amount),
                "entries": rows,
            }
        )

    # -- time to invoice ----------------------------------------------------

    @action(detail=True, methods=["post"], url_path="create-invoice")
    def create_invoice(self, request, pk=None):
        """Turn approved, un-invoiced time into a DRAFT invoice.

        There is no sales service for "invoice this time" (``invoice_workflow``
        starts from an invoice that already exists), so the draft is built
        here — but under the same guarantees the workflow gives:

        * One ``transaction.atomic``. A draft invoice whose lines exist but
          whose timesheet entries were never linked would be billed twice the
          next time somebody runs this.
        * ``select_for_update`` on the entries. Two people clicking "invoice"
          on the same project at the same time otherwise both read the same
          unbilled set and produce two invoices for the same hours.
        * ``TimesheetEntry.invoice_line`` is set inside the lock. It is a
          OneToOne, so even if both of the above failed, the database refuses
          the second attachment — the model enforces once-only billing and
          this code cannot talk it out of that.

        The invoice is left in DRAFT: issuing it (numbering, GL posting) is
        ``POST /invoices/{id}/issue``, and keeping the two separate means a
        reviewer sees the lines before a customer does.
        """
        from apps.projects.services.invoicing import create_invoice_from_time

        project = self.get_object()
        body = CreateInvoiceSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        payload = body.validated_data

        try:
            result = create_invoice_from_time(
                project.pk,
                tenant_id=project.tenant_id,
                user_id=request.user.id,
                issue_date=payload.get("issue_date"),
                due_date=payload.get("due_date"),
                date_from=payload.get("date_from"),
                date_to=payload.get("date_to"),
                group_by_task=payload.get("group_by_task", False),
                notes=payload.get("notes", ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        invoice = result.invoice
        return Response(
            {
                "invoice": str(invoice.pk),
                "status": invoice.status,
                "currency": invoice.currency,
                "line_count": result.line_count,
                "entry_count": result.entry_count,
                "total_amount": str(invoice.total_amount),
            },
            status=status.HTTP_201_CREATED,
        )


class ProjectMemberViewSet(TenantModelViewSet):
    """Project staffing. Narrowed to projects the caller can see."""

    permission_domain = "projects"
    resource = "project"
    queryset = ProjectMember.objects.all()
    serializer_class = ProjectMemberSerializer
    select_related = ("project", "employee")
    filterset_fields = ("project", "employee", "is_active")

    def get_queryset(self):
        # A membership row is a join; it has no ScopeRule of its own, so it
        # inherits the project's. Reading ``self.queryset`` directly rather
        # than ``super()`` avoids ScopedQuerysetMixin compiling a ``project``
        # rule against this model's own columns.
        return (
            self.queryset.select_related(*self.select_related)
            .filter(project__in=visible_projects(self.request))
        )


class ProjectTaskViewSet(TenantModelViewSet):
    """Tasks. ``task`` has its own scope rule (assigned projects)."""

    permission_domain = "projects"
    resource = "task"
    queryset = ProjectTask.objects.all()
    serializer_class = ProjectTaskSerializer
    select_related = ("project", "assigned_to", "parent")
    search_fields = ("code", "name")
    filterset_fields = ("project", "status", "assigned_to", "is_billable")
    extra_permissions = {
        "PATCH": ["projects.task.update"],
        "PUT": ["projects.task.update"],
    }


class ProjectMilestoneViewSet(TenantModelViewSet):
    """Milestones. Narrowed to projects the caller can see."""

    permission_domain = "projects"
    resource = "project"
    queryset = ProjectMilestone.objects.all()
    serializer_class = ProjectMilestoneSerializer
    select_related = ("project", "invoice")
    filterset_fields = ("project", "status", "is_billable")

    def get_queryset(self):
        return (
            self.queryset.select_related(*self.select_related)
            .filter(project__in=visible_projects(self.request))
        )


class TimesheetEntryViewSet(TenantModelViewSet):
    """Timesheets: the ABAC-sensitive resource in this app.

    ``resource = "timesheet_entry"`` drives both halves: the RBAC codenames
    (``projects.timesheet_entry.read`` …) and the ABAC scope rule, which for
    the ``employee`` role is ``own_record``. Nothing in this class re-derives
    "is this my entry?" — the queryset is already narrowed, and every action
    below goes through ``get_object()``.
    """

    permission_domain = "projects"
    resource = "timesheet_entry"
    queryset = TimesheetEntry.objects.all()
    serializer_class = TimesheetEntrySerializer
    select_related = ("employee", "project", "task", "invoice_line")
    filterset_fields = ("employee", "project", "task", "status", "is_billable",
                        "work_date")
    ordering_fields = ("work_date", "created_at", "hours")
    extra_permissions = {
        "submit": ["projects.timesheet_entry.submit"],
        "approve": ["projects.timesheet_entry.approve"],
        "reject": ["projects.timesheet_entry.approve"],
        "bulk_approve": ["projects.timesheet_entry.approve"],
    }

    # -- transitions --------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        """DRAFT -> SUBMITTED. The entry leaves the author's control."""
        entry = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                locked = TimesheetEntry.objects.select_for_update().get(pk=entry.pk)
                locked.assert_can_transition(TimesheetEntry.Status.SUBMITTED)
                locked.status = TimesheetEntry.Status.SUBMITTED
                locked.submitted_at = timezone.now()
                locked.updated_by_id = request.user.id
                locked.save(update_fields=[
                    "status", "submitted_at", "updated_by", "updated_at",
                ])
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return Response(self.get_serializer(locked).data)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """SUBMITTED -> APPROVED. Approved time is what becomes an invoice."""
        entry = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                locked = self._approve_locked(entry.pk, request)
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return Response(self.get_serializer(locked).data)

    @staticmethod
    def _approve_locked(entry_id, request) -> TimesheetEntry:
        locked = TimesheetEntry.objects.select_for_update().get(pk=entry_id)

        # Segregation of duties lives here rather than in the two callers so
        # that `bulk_approve` cannot route around it — a manager approving
        # fifty entries in one call must not be the one path where approving
        # your own is allowed. Raised inside the per-row savepoint, so in the
        # bulk case it is reported as a skipped row and the other forty-nine
        # still land.
        #
        # Approved time becomes a client invoice line, so this is a billing
        # control as much as an HR one. The permission matrix gives
        # Department Manager and Team Lead `exclude_self_prepared` on
        # `timesheet_entry`; this is the only place that parameter is now
        # enforced, since applying it as a queryset filter (the previous
        # behaviour) hid entries from their own author instead.
        assert_not_self_prepared(locked, "timesheet_entry", request)

        locked.assert_can_transition(TimesheetEntry.Status.APPROVED)
        locked.status = TimesheetEntry.Status.APPROVED
        locked.approved_by_id = request.user.id
        locked.approved_at = timezone.now()
        locked.rejection_reason = ""
        locked.updated_by_id = request.user.id
        locked.save(update_fields=[
            "status", "approved_by", "approved_at", "rejection_reason",
            "updated_by", "updated_at",
        ])
        return locked

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        """SUBMITTED -> REJECTED. The reason is mandatory.

        "Rejected" with no reason forces the author to guess, and the guess is
        usually "re-submit unchanged" — which wastes the approver's time twice.
        """
        entry = self.get_object()
        body = ReasonRequiredTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reason = body.validated_reason()

        try:
            with transaction.atomic():
                locked = TimesheetEntry.objects.select_for_update().get(pk=entry.pk)
                locked.assert_can_transition(TimesheetEntry.Status.REJECTED)
                locked.status = TimesheetEntry.Status.REJECTED
                locked.rejection_reason = reason[:255]
                locked.updated_by_id = request.user.id
                locked.save(update_fields=[
                    "status", "rejection_reason", "updated_by", "updated_at",
                ])
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return Response(self.get_serializer(locked).data)

    @action(detail=False, methods=["post"], url_path="bulk-approve")
    def bulk_approve(self, request):
        """Approve many entries in one request.

        The ids are filtered through ``get_queryset()`` first, so an id the
        caller may not see is simply absent from the result — it is reported
        as ``skipped``, never approved, and never 403'd (a 403 would confirm
        the entry exists, which is an enumeration oracle over other people's
        timesheets).

        Each entry is approved in its own savepoint: one illegal transition
        (an entry someone else already invoiced) must not roll back the other
        forty-nine approvals the manager just made.
        """
        body = BulkApproveSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        ids = body.validated_data["ids"]

        visible = set(
            self.get_queryset().filter(pk__in=ids).values_list("pk", flat=True)
        )
        approved, failed = [], []
        for entry_id in ids:
            if entry_id not in visible:
                failed.append({"id": str(entry_id), "error": "not_found"})
                continue
            try:
                with transaction.atomic():
                    self._approve_locked(entry_id, request)
                approved.append(str(entry_id))
            except Exception as exc:  # noqa: BLE001 - reported per row
                failed.append({"id": str(entry_id), "error": str(exc)})

        return Response(
            {
                "approved_count": len(approved),
                "approved": approved,
                "skipped_count": len(failed),
                "skipped": failed,
            },
            status=status.HTTP_200_OK,
        )
