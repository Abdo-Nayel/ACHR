"""
Projects serializers.

Budget consumption, profitability and unbilled value are all *derived*: they
are computed from timesheet entries and posted costs, never stored on the
request. A writable "percent consumed" would let a project look under budget
while its timesheets say otherwise, and the timesheets are the ones the
customer gets invoiced from.
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from apps.core.serializers import (
    MoneyField,
    QuantityField,
    RateField,
    TenantScopedSerializer,
)
from apps.projects.models import (
    Project,
    ProjectMember,
    ProjectMilestone,
    ProjectTask,
    TimesheetEntry,
)


def _ratio(numerator: Decimal, denominator: Decimal) -> str:
    """Fraction as a decimal string; ``"0"`` when the denominator is zero.

    Returned as a fraction (0.75), not a percentage (75), because the client
    formats percentages and a number that is sometimes a fraction and
    sometimes a percentage is how a progress bar ends up at 7500%.
    """
    if not denominator:
        return "0"
    return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001")))


class ProjectSerializer(TenantScopedSerializer):
    """A body of work with a budget, a client and a way of being paid for.

    ``budget_consumption`` is computed on read from the materialised
    ``actual_*`` columns the costing service maintains. It is deliberately not
    a stored column: it would need updating on every timesheet approval, and a
    stale percentage is worse than no percentage — people stop checking.
    """

    budget_amount = MoneyField(required=False)
    budget_hours = QuantityField(required=False)
    default_hourly_rate = MoneyField(required=False)
    actual_hours = QuantityField(read_only=True)
    actual_cost_amount = MoneyField(read_only=True)
    invoiced_amount = MoneyField(read_only=True)

    customer_name = serializers.CharField(source="customer.name", read_only=True)
    manager_name = serializers.CharField(source="manager.full_name", read_only=True)
    budget_consumption = serializers.SerializerMethodField()

    server_owned_fields = (
        "status", "actual_hours", "actual_cost_amount", "invoiced_amount",
    )

    class Meta:
        model = Project
        fields = (
            "id", "code", "name", "description", "customer", "customer_name",
            "status", "billing_type", "currency", "budget_amount",
            "budget_hours", "default_hourly_rate", "start_date", "end_date",
            "manager", "manager_name", "is_billable", "actual_hours",
            "actual_cost_amount", "invoiced_amount", "budget_consumption",
            "created_at", "updated_at",
        )

    def get_budget_consumption(self, obj: Project) -> dict[str, str]:
        return {
            "hours_used": str(obj.actual_hours),
            "hours_budget": str(obj.budget_hours),
            "hours_ratio": _ratio(obj.actual_hours, obj.budget_hours),
            "cost_incurred": str(obj.actual_cost_amount),
            "cost_budget": str(obj.budget_amount),
            "cost_ratio": _ratio(obj.actual_cost_amount, obj.budget_amount),
            "amount_invoiced": str(obj.invoiced_amount),
            "currency": obj.currency,
        }


class ProjectMemberSerializer(TenantScopedSerializer):
    """Who is on the project, at what rate.

    ``hourly_rate`` is nullable on purpose: NULL means "use the project's
    default", which is different from 0.00 ("this person is free"), and
    conflating the two silently zeroes a consultant's billable value.
    """

    hourly_rate = MoneyField(required=False, allow_null=True)
    allocation_percent = RateField(required=False)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    project_code = serializers.CharField(source="project.code", read_only=True)

    class Meta:
        model = ProjectMember
        fields = (
            "id", "project", "project_code", "employee", "employee_name",
            "role", "currency", "hourly_rate", "allocation_percent",
            "joined_on", "left_on", "is_active", "created_at",
        )


class ProjectTaskSerializer(TenantScopedSerializer):
    """A unit of work. Status moves through POST sub-resources only."""

    estimated_hours = QuantityField(required=False)
    hourly_rate = MoneyField(required=False, allow_null=True)
    assigned_to_name = serializers.CharField(
        source="assigned_to.full_name", read_only=True
    )
    logged_hours = serializers.SerializerMethodField()

    server_owned_fields = ("status",)

    class Meta:
        model = ProjectTask
        fields = (
            "id", "project", "parent", "code", "name", "description",
            "estimated_hours", "is_billable", "assigned_to", "assigned_to_name",
            "status", "due_date", "currency", "hourly_rate", "sort_order",
            "logged_hours", "created_at", "updated_at",
        )

    def get_logged_hours(self, obj: ProjectTask) -> str:
        total = getattr(obj, "logged_hours_total", None)
        if total is None:
            return "0"
        return str(total)


class TimesheetEntrySerializer(TenantScopedSerializer):
    """One person's hours on one project on one day.

    ``status`` and ``invoice_line`` are server-owned. ``invoice_line`` in
    particular is the once-only billing guard: it is a OneToOne, so the
    database refuses to attach the same entry to two invoice lines, and the
    time-to-invoice action is the only writer.
    """

    hours = QuantityField()
    billable_rate = MoneyField(required=False)
    cost_rate = MoneyField(required=False)
    billable_amount = MoneyField(read_only=True)
    cost_amount = MoneyField(read_only=True)

    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    project_code = serializers.CharField(source="project.code", read_only=True)
    task_name = serializers.CharField(source="task.name", read_only=True)

    server_owned_fields = (
        "status", "submitted_at", "approved_by", "approved_at",
        "rejection_reason", "invoice_line", "invoiced_at",
    )

    class Meta:
        model = TimesheetEntry
        fields = (
            "id", "employee", "employee_name", "project", "project_code",
            "task", "task_name", "work_date", "hours", "description",
            "is_billable", "currency", "billable_rate", "cost_rate",
            "billable_amount", "cost_amount", "status", "submitted_at",
            "approved_by", "approved_at", "rejection_reason", "invoice_line",
            "invoiced_at", "created_at", "updated_at",
        )


class ProjectMilestoneSerializer(TenantScopedSerializer):
    """A billable checkpoint on a fixed-fee or milestone project."""

    amount = MoneyField(required=False)
    percentage_of_budget = RateField(required=False)
    project_code = serializers.CharField(source="project.code", read_only=True)
    is_invoiceable = serializers.BooleanField(read_only=True)

    server_owned_fields = (
        "status", "invoice", "invoiced_at", "accepted_by", "accepted_on",
        "delivered_on",
    )

    class Meta:
        model = ProjectMilestone
        fields = (
            "id", "project", "project_code", "name", "description", "sequence",
            "due_date", "delivered_on", "accepted_on", "currency", "amount",
            "percentage_of_budget", "status", "is_billable", "invoice",
            "invoiced_at", "accepted_by", "is_invoiceable", "created_at",
        )


class BulkApproveSerializer(serializers.Serializer):
    """Body for ``POST /timesheets/bulk-approve/``.

    An explicit id list, never "approve everything matching this filter". A
    filter evaluated server-side can widen between the screen the approver
    read and the request they sent — a new entry submitted in that second gets
    approved by someone who never saw it, which is precisely the control the
    approval step exists to provide.
    """

    ids = serializers.ListField(
        child=serializers.UUIDField(), allow_empty=False, max_length=500
    )
    comment = serializers.CharField(max_length=500, required=False, allow_blank=True)


class CreateInvoiceSerializer(serializers.Serializer):
    """Body for ``POST /projects/{id}/create-invoice``."""

    date_from = serializers.DateField(required=False, allow_null=True)
    date_to = serializers.DateField(required=False, allow_null=True)
    issue_date = serializers.DateField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    #: Roll every entry into one line per task instead of one line per entry.
    group_by_task = serializers.BooleanField(required=False, default=False)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)

    def validate(self, attrs: dict) -> dict:
        start, end = attrs.get("date_from"), attrs.get("date_to")
        if start and end and end < start:
            raise serializers.ValidationError(
                {"date_to": "The period ends before it starts."}
            )
        return attrs


class UnbilledTimeSerializer(serializers.Serializer):
    """Read-only projection of approved, un-invoiced time."""

    employee_id = serializers.UUIDField(read_only=True)
    employee_name = serializers.CharField(read_only=True)
    task_id = serializers.UUIDField(read_only=True, allow_null=True)
    task_name = serializers.CharField(read_only=True)
    hours = QuantityField(read_only=True)
    amount = MoneyField(read_only=True)
    currency = serializers.CharField(read_only=True)
