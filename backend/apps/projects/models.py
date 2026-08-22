"""
Projects & time tracking — where labour becomes revenue.

This module answers three questions that every professional-services company
gets wrong at least once:

1. **What did this project cost us?** Hours logged by employees, valued at
   their cost rate, plus expenses. Feeds project profitability.
2. **What can we bill for it?** Hours logged, valued at the *billing* rate,
   filtered to the billable ones and the approved ones.
3. **What have we already billed?** — and this is the one that hurts.
   The link from a timesheet entry to the invoice line it was billed on
   (``TimesheetEntry.invoice_line``) is what stops the same hour being
   invoiced twice. Double-billing a client is not a rounding error; it is a
   credit note, an apology, and a client who audits every subsequent invoice.

Cost rate vs billing rate are deliberately different fields on different
models: the cost rate belongs to the employment relationship (``hr.Employee``)
and is confidential; the billing rate belongs to the commercial relationship
(project, member, or task) and appears on the invoice. Conflating them leaks
salaries into client-facing documents.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.fields import MoneyField, QuantityField, RateField, ZERO
from apps.core.models import Currency, StatusTransitionMixin, TenantScopedModel


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

class Project(StatusTransitionMixin, TenantScopedModel):
    """A body of work with a budget, a client and a way of being paid for.

    ``billing_type`` is not cosmetic — it decides what "revenue" means:

    * ``TIME_AND_MATERIALS`` — revenue is approved billable hours × rate.
      Timesheets drive invoices directly.
    * ``FIXED_FEE`` — revenue is the agreed price regardless of hours. Hours
      are still logged (they are how we learn the quote was too low) but they
      do not generate invoice lines. Recognising revenue from hours here
      would overstate income on an overrunning project.
    * ``MILESTONE`` — revenue is recognised as milestones are accepted; see
      :class:`ProjectMilestone`.
    * ``NON_BILLABLE`` — internal work, R&D, pre-sales. Costs only.

    ``customer`` is nullable because internal projects have no client, and
    the FK is ``PROTECT`` because deleting a customer with project history
    would orphan the revenue those projects produced.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        ON_HOLD = "on_hold", "On hold"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        ARCHIVED = "archived", "Archived"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.ACTIVE, Status.CANCELLED},
        Status.ACTIVE: {Status.ON_HOLD, Status.COMPLETED, Status.CANCELLED},
        Status.ON_HOLD: {Status.ACTIVE, Status.COMPLETED, Status.CANCELLED},
        # Completed is not terminal: final billing regularly reveals work
        # that was never logged, and forcing a new project for it destroys
        # the profitability figure of the original.
        Status.COMPLETED: {Status.ACTIVE, Status.ARCHIVED},
        Status.CANCELLED: {Status.ARCHIVED},
        Status.ARCHIVED: set(),
    }

    class BillingType(models.TextChoices):
        FIXED_FEE = "fixed_fee", "Fixed fee"
        TIME_AND_MATERIALS = "time_and_materials", "Time & materials"
        MILESTONE = "milestone", "Milestone"
        NON_BILLABLE = "non_billable", "Non-billable"

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    customer = models.ForeignKey(
        "sales.Customer", null=True, blank=True,
        on_delete=models.PROTECT, related_name="projects",
    )

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    billing_type = models.CharField(
        max_length=20,
        choices=BillingType.choices,
        default=BillingType.TIME_AND_MATERIALS,
        db_index=True,
    )

    currency = models.CharField(max_length=3, choices=Currency.choices)
    budget_amount = MoneyField()
    budget_hours = QuantityField()
    #: Fallback rate when neither the task, the member nor the employee
    #: carries one. Copied onto each timesheet entry at logging time — see
    #: ``TimesheetEntry.billable_rate`` for why it must not be looked up
    #: later.
    default_hourly_rate = MoneyField()

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    manager = models.ForeignKey(
        "hr.Employee", null=True, blank=True,
        on_delete=models.PROTECT, related_name="managed_projects",
    )
    is_billable = models.BooleanField(default=True)
    #: Denormalised roll-ups, refreshed by the projects service. Reports that
    #: matter (WIP, profitability) recompute from timesheets; these exist so
    #: a project list page is one query.
    actual_hours = QuantityField()
    actual_cost_amount = MoneyField()
    invoiced_amount = MoneyField()

    class Meta(TenantScopedModel.Meta):
        db_table = "projects_project"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_project_code"),
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(start_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="ck_project_date_order",
            ),
            models.CheckConstraint(
                condition=models.Q(budget_amount__gte=0)
                & models.Q(budget_hours__gte=0)
                & models.Q(default_hourly_rate__gte=0),
                name="ck_project_budget_non_negative",
            ),
            # A billable project must have somebody to bill. Catching this
            # at creation is far cheaper than discovering it at month end
            # with 300 unbillable hours logged against it.
            models.CheckConstraint(
                condition=models.Q(is_billable=False)
                | models.Q(billing_type="non_billable")
                | models.Q(customer__isnull=False),
                name="ck_project_billable_has_customer",
            ),
            models.CheckConstraint(
                condition=~models.Q(billing_type="non_billable")
                | models.Q(is_billable=False),
                name="ck_project_non_billable_flag",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status"], name="ix_project_status"),
            models.Index(fields=["tenant", "customer"], name="ix_project_customer"),
            models.Index(fields=["tenant", "manager"], name="ix_project_manager"),
            models.Index(
                fields=["tenant", "start_date", "end_date"], name="ix_project_dates"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.name}"

    @property
    def bills_from_time(self) -> bool:
        return (
            self.is_billable
            and self.billing_type == self.BillingType.TIME_AND_MATERIALS
        )


class ProjectMember(TenantScopedModel):
    """An employee assigned to a project, with their rate on *this* project.

    ``hourly_rate`` overrides the project default. Rates are per assignment
    because the same senior developer is billed at one rate on a retainer and
    another on a fixed-scope build, and because a rate change must apply to
    future work only — which is why the entry copies the rate rather than
    joining to it.

    ``allocation_percent`` is capacity planning: the sum across a person's
    active projects should not exceed 100, and when it does, that is a
    staffing report, not a database error — so it is validated in the
    service, not constrained here.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="members"
    )
    employee = models.ForeignKey(
        "hr.Employee", on_delete=models.PROTECT, related_name="project_memberships"
    )
    role = models.CharField(max_length=80, blank=True)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    hourly_rate = MoneyField(null=True, blank=True)
    #: 0..1 as a fraction, consistent with every other rate in the codebase.
    allocation_percent = RateField()
    joined_on = models.DateField(default=timezone.localdate)
    left_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "projects_project_member"
        ordering = ["project", "employee"]
        constraints = [
            # One *active* membership per person per project. Re-joining after
            # leaving is legal and creates a second, historical row.
            models.UniqueConstraint(
                fields=["project", "employee"],
                condition=models.Q(is_active=True),
                name="uq_project_member_active",
            ),
            models.CheckConstraint(
                condition=models.Q(allocation_percent__gte=0)
                & models.Q(allocation_percent__lte=1),
                name="ck_project_member_allocation",
            ),
            models.CheckConstraint(
                condition=models.Q(hourly_rate__isnull=True)
                | models.Q(hourly_rate__gte=0),
                name="ck_project_member_rate_non_neg",
            ),
            models.CheckConstraint(
                condition=models.Q(left_on__isnull=True)
                | models.Q(left_on__gte=models.F("joined_on")),
                name="ck_project_member_date_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "employee"], name="ix_project_member_emp"),
            models.Index(
                fields=["tenant", "project", "is_active"], name="ix_project_member_proj"
            ),
        ]


class ProjectTask(StatusTransitionMixin, TenantScopedModel):
    """A unit of work within a project; may nest one level or many.

    ``parent`` gives a work-breakdown structure. Time is logged against
    leaf tasks; parent estimates are roll-ups maintained by the service.

    ``is_billable`` here overrides the project's default *downwards* only —
    a non-billable project cannot contain billable tasks (there is nobody to
    bill), but a billable project routinely contains internal tasks such as
    rework, which the client must not be charged for. That asymmetry is
    enforced when a timesheet entry is created, since a CHECK cannot reach
    across to the project row.
    """

    class Status(models.TextChoices):
        TODO = "todo", "To do"
        IN_PROGRESS = "in_progress", "In progress"
        BLOCKED = "blocked", "Blocked"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.TODO: {Status.IN_PROGRESS, Status.BLOCKED, Status.CANCELLED},
        Status.IN_PROGRESS: {Status.BLOCKED, Status.DONE, Status.CANCELLED},
        Status.BLOCKED: {Status.IN_PROGRESS, Status.CANCELLED},
        Status.DONE: {Status.IN_PROGRESS},
        Status.CANCELLED: set(),
    }

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    code = models.CharField(max_length=30, blank=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    estimated_hours = QuantityField()
    is_billable = models.BooleanField(default=True)
    assigned_to = models.ForeignKey(
        "hr.Employee", null=True, blank=True,
        on_delete=models.PROTECT, related_name="assigned_tasks",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.TODO, db_index=True
    )
    due_date = models.DateField(null=True, blank=True)
    #: Task-level rate override; wins over the member and project rates.
    currency = models.CharField(max_length=3, choices=Currency.choices, blank=True)
    hourly_rate = MoneyField(null=True, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta(TenantScopedModel.Meta):
        db_table = "projects_project_task"
        ordering = ["project", "sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "code"],
                condition=~models.Q(code=""),
                name="uq_project_task_code",
            ),
            models.CheckConstraint(
                condition=~models.Q(parent=models.F("id")),
                name="ck_project_task_no_self_parent",
            ),
            models.CheckConstraint(
                condition=models.Q(estimated_hours__gte=0),
                name="ck_project_task_hours_non_neg",
            ),
            models.CheckConstraint(
                condition=models.Q(hourly_rate__isnull=True)
                | models.Q(hourly_rate__gte=0),
                name="ck_project_task_rate_non_neg",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "project", "status"], name="ix_project_task_status"
            ),
            models.Index(fields=["tenant", "assigned_to"], name="ix_project_task_owner"),
            models.Index(fields=["tenant", "parent"], name="ix_project_task_parent"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

class TimesheetEntry(StatusTransitionMixin, TenantScopedModel):
    """One person's hours on one project (and task) on one day.

    Lifecycle: ``DRAFT -> SUBMITTED -> APPROVED -> INVOICED``, with
    ``REJECTED`` looping back to DRAFT. Approval is a control, not
    decoration: approved hours become an invoice to a client and a cost in
    the project's profitability, so the person who logs them must not be the
    person who blesses them.

    THE CRITICAL INVARIANT — an approved entry is invoiced **exactly once**
    -------------------------------------------------------------------------
    Billing the same hour twice is the worst defect this module can produce.
    It is not caught by any total (both invoices look internally consistent),
    it reaches the client before anyone notices, and the remedy is a credit
    note plus a conversation about every other invoice you have ever sent.

    The guard is structural and has two parts:

    1. ``invoice_line`` is a **OneToOneField** to ``sales.InvoiceLine``.
       The database therefore refuses to attach two timesheet entries to the
       same invoice line, and — read the other way — because the field lives
       on *this* row and is set exactly once, an entry can only ever point at
       one invoice line. A second invoicing run cannot create a second link
       without overwriting the first, which the transition map forbids.

    2. ``uq_timesheet_invoiced_once``: a partial unique constraint over
       ``(tenant, id)`` where ``status = 'invoiced'`` **and**
       ``invoice_line IS NOT NULL``, together with
       ``ck_timesheet_invoiced_has_line`` (status INVOICED implies a line)
       and ``ck_timesheet_line_implies_invoiced`` (a line implies status
       INVOICED). The pair makes "invoiced" and "has an invoice line" the
       same fact, so there is no state in which an entry has been billed but
       still looks billable to the next invoicing run — which is the actual
       mechanism of double-billing, far more often than a genuine duplicate.

    The invoicing service additionally selects candidate entries with
    ``status=APPROVED`` **and** ``invoice_line__isnull=True`` inside a
    ``select_for_update()``, so two concurrent invoice runs for the same
    client cannot both pick up the same hours: the second blocks, then sees
    them already INVOICED.

    ``billable_rate`` is *copied* onto the entry when it is logged, never
    joined at invoicing time. A rate rise agreed in June must not silently
    reprice May's unbilled hours; and re-rendering last year's invoice must
    show last year's rate.

    ``0 < hours <= 24`` is a CHECK because a day cannot contain more, and
    every value outside it seen in production has been a data-entry slip (a
    date in the hours box, minutes typed as hours) that would otherwise have
    been billed.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        INVOICED = "invoiced", "Invoiced"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.SUBMITTED},
        Status.SUBMITTED: {Status.APPROVED, Status.REJECTED, Status.DRAFT},
        # Un-approving is allowed only while the hours are still unbilled;
        # the invoicing service holds a row lock so this cannot race with an
        # invoice run.
        Status.APPROVED: {Status.INVOICED, Status.SUBMITTED},
        Status.REJECTED: {Status.DRAFT},
        # Terminal. Once billed, a correction is a credit note plus a new
        # entry — never an edit, because the invoice has left the building.
        Status.INVOICED: set(),
    }

    employee = models.ForeignKey(
        "hr.Employee", on_delete=models.PROTECT, related_name="timesheet_entries"
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="timesheet_entries"
    )
    task = models.ForeignKey(
        ProjectTask, null=True, blank=True,
        on_delete=models.PROTECT, related_name="timesheet_entries",
    )

    work_date = models.DateField(db_index=True)
    hours = QuantityField()
    description = models.CharField(max_length=500, blank=True)

    is_billable = models.BooleanField(default=True, db_index=True)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    #: Snapshot of the rate in force when the work was logged. See docstring.
    billable_rate = MoneyField()
    #: Internal cost of the hour (employee cost rate). Never shown to the
    #: client; drives project profitability only.
    cost_rate = MoneyField()

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)

    #: The time-to-invoice link. OneToOne: one invoice line represents these
    #: hours and no others. Set exactly once, when the entry is billed.
    invoice_line = models.OneToOneField(
        "sales.InvoiceLine", null=True, blank=True,
        on_delete=models.PROTECT, related_name="timesheet_entry",
    )
    invoiced_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "projects_timesheet_entry"
        ordering = ["-work_date", "-created_at"]
        constraints = [
            # A day has 24 hours and no timesheet line is ever legitimately
            # zero. Both bounds catch real slips, not hypothetical ones.
            models.CheckConstraint(
                condition=models.Q(hours__gt=0) & models.Q(hours__lte=24),
                name="ck_timesheet_hours_range",
            ),
            models.CheckConstraint(
                condition=models.Q(billable_rate__gte=0)
                & models.Q(cost_rate__gte=0),
                name="ck_timesheet_rates_non_negative",
            ),
            # "Invoiced" and "has an invoice line" must be the same fact,
            # in both directions. Either half alone leaves a state where the
            # hours look billable but have already been billed.
            models.CheckConstraint(
                condition=~models.Q(status="invoiced")
                | models.Q(invoice_line__isnull=False),
                name="ck_timesheet_invoiced_has_line",
            ),
            models.CheckConstraint(
                condition=models.Q(invoice_line__isnull=True)
                | models.Q(status="invoiced"),
                name="ck_timesheet_line_implies_invoiced",
            ),
            # The billing-once guard, spelled out as an index as well as by
            # the OneToOne: an invoiced row is uniquely identified while
            # invoiced, so no second row can claim the same invoice line and
            # no row can be invoiced without one.
            models.UniqueConstraint(
                fields=["tenant", "invoice_line"],
                condition=models.Q(status="invoiced")
                & models.Q(invoice_line__isnull=False),
                name="uq_timesheet_invoiced_once",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="approved")
                | models.Q(approved_at__isnull=False),
                name="ck_timesheet_approved_at",
            ),
            # Only a billable entry can carry a rate that will be charged.
            models.CheckConstraint(
                condition=models.Q(is_billable=True) | models.Q(invoice_line__isnull=True),
                name="ck_timesheet_non_billable_uninvoiced",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            # The approval queue and the invoicing sweep.
            models.Index(
                fields=["tenant", "status", "work_date"], name="ix_timesheet_status"
            ),
            models.Index(
                fields=["tenant", "employee", "work_date"], name="ix_timesheet_employee"
            ),
            models.Index(
                fields=["tenant", "project", "work_date"], name="ix_timesheet_project"
            ),
            # "What is billable but not yet billed" — the WIP query.
            models.Index(
                fields=["tenant", "project", "is_billable", "status"],
                name="ix_timesheet_wip",
            ),
            models.Index(fields=["tenant", "task"], name="ix_timesheet_task"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.employee_id} {self.work_date} {self.hours}h"

    @property
    def billable_amount(self):
        """Value of these hours to the client. Decimal throughout."""
        return self.hours * self.billable_rate if self.is_billable else ZERO

    @property
    def cost_amount(self):
        return self.hours * self.cost_rate

    @property
    def is_invoiceable(self) -> bool:
        return (
            self.is_billable
            and self.status == self.Status.APPROVED
            and self.invoice_line_id is None
        )

    def clean(self) -> None:
        super().clean()
        # A CHECK cannot reach the project row, so the "a non-billable
        # project cannot have billable hours" rule lives here and in the
        # service that creates entries.
        if self.is_billable and self.project_id and not self.project.is_billable:
            raise ValidationError(
                {"is_billable": "The project is non-billable; its hours cannot be."}
            )
        if self.task_id and self.project_id and self.task.project_id != self.project_id:
            raise ValidationError(
                {"task": "Task belongs to a different project."}
            )
        if self.work_date and self.work_date > timezone.localdate():
            raise ValidationError(
                {"work_date": "Hours cannot be logged for a future date."}
            )


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

class ProjectMilestone(StatusTransitionMixin, TenantScopedModel):
    """A deliverable whose acceptance triggers a billing event.

    On a MILESTONE project this is the revenue trigger: the client accepts
    the deliverable, and *that* — not a date, not a percentage estimate — is
    what makes the amount invoiceable. Tying billing to acceptance rather
    than to a planned date is what keeps revenue recognition defensible when
    a project slips, which every project does.

    ``invoice`` is set once, when the milestone is billed, and the
    ``ACCEPTED -> INVOICED`` transition together with
    ``ck_milestone_invoiced_has_invoice`` gives the same
    bill-exactly-once guarantee as :class:`TimesheetEntry`, for the same
    reason: a milestone billed twice is a credit note and a lost client.
    """

    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        IN_PROGRESS = "in_progress", "In progress"
        DELIVERED = "delivered", "Delivered, awaiting acceptance"
        ACCEPTED = "accepted", "Accepted by client"
        INVOICED = "invoiced", "Invoiced"
        CANCELLED = "cancelled", "Cancelled"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.PLANNED: {Status.IN_PROGRESS, Status.CANCELLED},
        Status.IN_PROGRESS: {Status.DELIVERED, Status.CANCELLED},
        # Rejection sends it back to work; the client did not accept it.
        Status.DELIVERED: {Status.ACCEPTED, Status.IN_PROGRESS, Status.CANCELLED},
        Status.ACCEPTED: {Status.INVOICED},
        Status.INVOICED: set(),
        Status.CANCELLED: set(),
    }

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="milestones"
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    sequence = models.PositiveSmallIntegerField(default=1)

    due_date = models.DateField(null=True, blank=True)
    delivered_on = models.DateField(null=True, blank=True)
    accepted_on = models.DateField(null=True, blank=True)

    currency = models.CharField(max_length=3, choices=Currency.choices)
    amount = MoneyField()
    #: Alternative to a fixed amount: a share of the project's budget, so a
    #: change of scope reprices the remaining milestones consistently.
    percentage_of_budget = RateField()

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PLANNED, db_index=True
    )
    is_billable = models.BooleanField(default=True)
    invoice = models.ForeignKey(
        "sales.Invoice", null=True, blank=True,
        on_delete=models.PROTECT, related_name="project_milestones",
    )
    invoiced_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "projects_project_milestone"
        ordering = ["project", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "sequence"], name="uq_milestone_sequence"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0),
                name="ck_milestone_amount_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(percentage_of_budget__gte=0)
                & models.Q(percentage_of_budget__lte=1),
                name="ck_milestone_percentage_range",
            ),
            # Billed exactly once: invoiced implies an invoice, and an
            # invoice implies invoiced.
            models.CheckConstraint(
                condition=~models.Q(status="invoiced")
                | models.Q(invoice__isnull=False),
                name="ck_milestone_invoiced_has_invoice",
            ),
            models.CheckConstraint(
                condition=models.Q(invoice__isnull=True)
                | models.Q(status="invoiced"),
                name="ck_milestone_invoice_implies_status",
            ),
            # Acceptance is the billing trigger, so it must be dated.
            models.CheckConstraint(
                condition=~models.Q(status__in=["accepted", "invoiced"])
                | models.Q(accepted_on__isnull=False),
                name="ck_milestone_accepted_dated",
            ),
            models.CheckConstraint(
                condition=models.Q(accepted_on__isnull=True)
                | models.Q(delivered_on__isnull=True)
                | models.Q(accepted_on__gte=models.F("delivered_on")),
                name="ck_milestone_date_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "project", "status"], name="ix_milestone_status"
            ),
            models.Index(fields=["tenant", "due_date"], name="ix_milestone_due"),
            # "Accepted but not yet billed" — the milestone billing sweep.
            models.Index(
                fields=["tenant", "status", "is_billable"], name="ix_milestone_billable"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id} #{self.sequence} {self.name}"

    @property
    def is_invoiceable(self) -> bool:
        return (
            self.is_billable
            and self.status == self.Status.ACCEPTED
            and self.invoice_id is None
        )
