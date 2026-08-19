"""
Payroll: what each person is paid, why, and how that reaches the ledger.

Payroll sits between two systems that both consider themselves authoritative,
and its job is to be the auditable bridge:

    hr.Employee / hr.SalaryRevision / hr.AttendanceRecord / hr.LeaveRequest
        -> payroll.PayrollRun -> payroll.Payslip -> accounting.JournalEntry

Three rules shape every model in this file:

1. **A payslip is a financial document, not a view.** Once issued it must
   reproduce byte-identically forever, so it stores snapshots rather than
   joins (see :class:`Payslip`).
2. **The ledger is written exactly once**, at the ``APPROVED -> POSTED``
   transition, through ``accounting.services.posting.post_entry()`` with an
   idempotency key (see :class:`PayrollRun`).
3. **Nothing in this module is a float.** Every amount is a Decimal in a
   ``numeric`` column; a cent of drift here is a failed trial balance and a
   rejected statutory filing.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models

from apps.core.fields import MoneyField, QuantityField, RateField, ZERO
from apps.core.models import (
    Currency,
    ImmutableFinancialModel,
    TenantScopedModel,
)


# ---------------------------------------------------------------------------
# Component definitions (the tenant's pay structure)
# ---------------------------------------------------------------------------

class PayrollComponent(TenantScopedModel):
    """The definition of one earning, deduction or employer contribution.

    This is the tenant's *pay structure*, expressed as data. Housing
    allowance, transport allowance, overtime, income tax, social insurance,
    loan repayment and end-of-service accrual are all rows here rather than
    branches in the engine — a tenant in another country must be configurable,
    not forkable.

    Why ``sequence`` matters
    ------------------------
    Components are evaluated in ``sequence`` order, and the order is not
    cosmetic: it is an evaluation dependency graph flattened into an integer.

    * A ``PERCENTAGE_OF_BASE`` allowance depends only on the base salary and
      can be computed first.
    * A ``PERCENTAGE_OF_GROSS`` deduction (a union fee of 1% of gross, say)
      depends on the *total of every earning*, so it must run after all
      earnings — including the percentage-of-base ones — have been added.
    * A ``FORMULA`` component may reference the running totals of both.

    Running a percentage-of-gross deduction before the last allowance is
    added produces a number that is quietly too small, on every payslip, every
    month, until an employee recomputes it by hand. The convention is
    earnings in 100–499, statutory deductions in 500–799, voluntary
    deductions in 800–999.

    Formula safety
    --------------
    ``formula_expression`` is tenant-supplied text. It is evaluated by
    ``apps.payroll.services.engine.evaluate_formula()``, a restricted
    ``ast``-walking evaluator with a whitelist of node types and a whitelist
    of names drawn from the payslip context. It is **never** passed to
    ``eval()``: ``eval()`` on a string a customer can edit is remote code
    execution against the payroll host, which holds every salary and bank
    account in the database.
    """

    class ComponentType(models.TextChoices):
        EARNING = "earning", "Earning"
        DEDUCTION = "deduction", "Employee deduction"
        EMPLOYER_CONTRIBUTION = "employer_contribution", "Employer contribution"
        #: Shown on the payslip, changes nothing (leave balance, ticket value).
        INFORMATIONAL = "informational", "Informational only"

    class CalculationType(models.TextChoices):
        FIXED = "fixed", "Fixed amount"
        PERCENTAGE_OF_BASE = "percentage_of_base", "Percentage of base salary"
        PERCENTAGE_OF_GROSS = "percentage_of_gross", "Percentage of gross"
        FORMULA = "formula", "Restricted formula"
        PER_UNIT = "per_unit", "Rate per unit (overtime hours, pieces)"

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    component_type = models.CharField(
        max_length=24, choices=ComponentType.choices, db_index=True
    )
    calculation_type = models.CharField(
        max_length=20, choices=CalculationType.choices, default=CalculationType.FIXED
    )
    #: Used by FIXED and as the default unit price for PER_UNIT.
    amount = MoneyField()
    #: Fraction, not percent: 0.100000 is 10%. Same convention as
    #: ``accounting.TaxRate.rate`` so the two never have to be reconciled.
    rate = RateField()
    formula_expression = models.CharField(max_length=500, blank=True)
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.EGP
    )

    #: Whether this earning enters the income-tax base. Housing allowance is
    #: often taxable while a per-diem is not; getting this wrong understates
    #: withholding and the tenant, not the employee, pays the penalty.
    is_taxable = models.BooleanField(default=True)
    is_subject_to_social_insurance = models.BooleanField(default=True)
    #: False for INFORMATIONAL rows: they print but do not move the net.
    affects_net = models.BooleanField(default=True)
    sequence = models.PositiveSmallIntegerField(
        default=500, help_text="Evaluation order; see the class docstring."
    )

    #: Where this component lands in the GL. An earning debits an expense; a
    #: deduction credits a liability until it is remitted.
    expense_account = models.ForeignKey(
        "accounting.Account",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="payroll_expense_components",
    )
    liability_account = models.ForeignKey(
        "accounting.Account",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="payroll_liability_components",
    )
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "payroll_component"
        ordering = ["sequence", "code"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_pay_component_code"),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0) & models.Q(rate__gte=0),
                name="ck_pay_component_non_negative",
            ),
            # A percentage above 100% of gross is always a data-entry error
            # (a misplaced decimal turning 5% into 500%) and would produce a
            # negative net that the engine would then refuse to post.
            models.CheckConstraint(
                condition=models.Q(rate__lte=1), name="ck_pay_component_rate_fraction",
            ),
            # A FORMULA component with no formula silently evaluates to zero.
            models.CheckConstraint(
                condition=~models.Q(calculation_type="formula")
                | ~models.Q(formula_expression=""),
                name="ck_pay_component_formula_present",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "is_active", "sequence"], name="ix_pay_comp_eval_order"
            ),
            models.Index(fields=["tenant", "component_type"], name="ix_pay_comp_type"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.name}"

    @property
    def is_deduction(self) -> bool:
        return self.component_type == self.ComponentType.DEDUCTION


class EmployeePayrollProfile(TenantScopedModel):
    """Per-employee payroll settings that are not part of the HR record.

    Separate from :class:`hr.Employee` because these are *payroll* facts with
    a different audience and a different permission (``payroll.profile.read``,
    not ``hr.employee.read``): a department manager may see their team's HR
    record and must not see their tax exemption or their loan deductions.

    ``is_exempt_from_tax`` and ``is_exempt_from_social_insurance`` are
    explicit booleans rather than the absence of a component, so that a
    missing configuration looks different from a deliberate exemption.
    """

    employee = models.OneToOneField(
        "hr.Employee", on_delete=models.PROTECT, related_name="payroll_profile"
    )
    #: Overrides Employee.pay_frequency when a specific contract differs.
    pay_frequency = models.CharField(max_length=10, blank=True)
    is_exempt_from_tax = models.BooleanField(default=False)
    is_exempt_from_social_insurance = models.BooleanField(default=False)
    #: Annual personal allowance/relief applied before the tax brackets.
    tax_exemption_amount = MoneyField()
    #: Number of dependants; feeds country-specific relief formulas.
    dependants_count = models.PositiveSmallIntegerField(default=0)
    #: The salary figure social insurance is computed on, which in several
    #: jurisdictions is a capped "insurable wage" rather than actual pay.
    insurable_wage = MoneyField()
    payment_method = models.CharField(max_length=20, default="bank_transfer")
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.EGP
    )
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "payroll_employee_profile"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "employee"], name="uq_pay_profile_employee"
            ),
            models.CheckConstraint(
                condition=models.Q(tax_exemption_amount__gte=0)
                & models.Q(insurable_wage__gte=0),
                name="ck_pay_profile_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_pay_profile_active"),
        ]


class EmployeeComponent(TenantScopedModel):
    """Assigns a :class:`PayrollComponent` to one employee, with overrides.

    The component defines the *rule*; this row says the rule applies to this
    person, optionally with a different amount or rate ("standard transport
    allowance, but 800 for site staff").

    ``effective_from`` / ``effective_to`` exist for the same reason
    :class:`hr.SalaryRevision` exists: a payroll re-run for March must see the
    assignments that were live in March. Assignments are therefore *ended*
    (``effective_to`` set) rather than deleted, and the engine selects rows
    overlapping the pay period.
    """

    employee = models.ForeignKey(
        "hr.Employee", on_delete=models.PROTECT, related_name="payroll_components"
    )
    component = models.ForeignKey(
        PayrollComponent, on_delete=models.PROTECT, related_name="assignments"
    )
    #: NULL = inherit the component's value. Zero is a meaningful override
    #: (suspend an allowance without ending the assignment), which is why
    #: these are nullable rather than defaulting to ZERO.
    amount_override = MoneyField(null=True, blank=True, default=None)
    rate_override = RateField(null=True, blank=True, default=None)
    quantity = QuantityField(default=ZERO, help_text="Units for PER_UNIT components.")
    effective_from = models.DateField(db_index=True)
    effective_to = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "payroll_employee_component"
        ordering = ["employee", "component"]
        constraints = [
            # One live assignment of a component per employee at a time. Two
            # overlapping rows would apply the allowance twice.
            models.UniqueConstraint(
                fields=["tenant", "employee", "component", "effective_from"],
                name="uq_pay_emp_component_from",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gte=models.F("effective_from")),
                name="ck_pay_emp_component_date_order",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_override__isnull=True)
                | models.Q(amount_override__gte=0),
                name="ck_pay_emp_component_amount",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "employee", "effective_from"], name="ix_pay_empcomp_lookup"
            ),
        ]


class TaxBracket(TenantScopedModel):
    """One slab of a progressive income-tax scale.

    Marginal, not average
    ---------------------
    Each bracket taxes only the portion of income that falls *inside* it. For
    a scale of 0% to 15 000, 10% to 30 000 and 20% above, an annual taxable
    income of 50 000 is taxed::

        (15 000 - 0)      * 0.00 =     0
        (30 000 - 15 000) * 0.10 = 1 500
        (50 000 - 30 000) * 0.20 = 4 000
                                   -----
                                   5 500

    not ``50 000 * 0.20 = 10 000``. Applying the top rate to the whole income
    is the classic payroll bug: it overtaxes every employee who crosses a
    threshold and produces the "a raise made my net pay drop" complaint, which
    is impossible under a correct marginal calculation.

    ``fixed_deduction`` supports jurisdictions that publish their scale as
    "20% of income minus 2 500" — an algebraically equivalent form of the same
    marginal calculation which is easier to reconcile against the official
    tables when they are published that way.

    ``upper_bound`` NULL means infinity: the top bracket has no ceiling.
    Modelling it as a very large number instead would eventually be exceeded
    by a hyperinflating currency, and would make the "is this the top
    bracket?" test a magic-number comparison.

    Brackets are versioned by ``effective_from`` and never edited: a payroll
    re-run for a prior year must apply that year's scale.
    """

    country = models.CharField(max_length=2, help_text="ISO-3166 alpha-2.")
    effective_from = models.DateField(db_index=True)
    effective_to = models.DateField(null=True, blank=True)
    lower_bound = MoneyField()
    #: NULL = unbounded (top slab).
    upper_bound = MoneyField(null=True, blank=True, default=None)
    rate = RateField(help_text="Fraction, e.g. 0.225000 for 22.5%.")
    fixed_deduction = MoneyField()
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.EGP
    )
    #: The period the bounds are expressed in. Scales are published annually
    #: but payroll runs monthly, so the engine annualises taxable income,
    #: applies the scale, then divides — storing the basis makes that
    #: conversion explicit instead of an assumption.
    is_annual_basis = models.BooleanField(default=True)
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta(TenantScopedModel.Meta):
        db_table = "payroll_tax_bracket"
        ordering = ["country", "-effective_from", "sequence", "lower_bound"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "country", "effective_from", "lower_bound"],
                name="uq_pay_tax_bracket_slab",
            ),
            models.CheckConstraint(
                condition=models.Q(upper_bound__isnull=True)
                | models.Q(upper_bound__gt=models.F("lower_bound")),
                name="ck_pay_tax_bracket_bound_order",
            ),
            models.CheckConstraint(
                condition=models.Q(rate__gte=0) & models.Q(rate__lte=1),
                name="ck_pay_tax_bracket_rate_fraction",
            ),
            models.CheckConstraint(
                condition=models.Q(lower_bound__gte=0)
                & models.Q(fixed_deduction__gte=0),
                name="ck_pay_tax_bracket_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "country", "effective_from"], name="ix_pay_bracket_lookup"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        ceiling = self.upper_bound if self.upper_bound is not None else "∞"
        return f"{self.country} {self.lower_bound}–{ceiling} @ {self.rate}"


# ---------------------------------------------------------------------------
# Runs and payslips
# ---------------------------------------------------------------------------

class PayrollRun(TenantScopedModel):
    """One payroll cycle for one period — the unit of calculation, approval,
    posting and payment.

    Lifecycle and approval chain
    ----------------------------
    ::

        DRAFT
          -> CALCULATING          (engine started; run is locked to writers)
          -> CALCULATED           (payslips exist, nothing has left the company)
          -> PENDING_APPROVAL     (submitted by the payroll officer)
          -> APPROVED             (signed off by a *different* user)
          -> POSTED               (general ledger written — exactly once)
          -> PAID                 (bank file disbursed, cash has moved)

    ``CANCELLED`` is reachable from every state up to and including APPROVED.
    It is **not** reachable from POSTED: once the ledger is written the only
    correction is a reversing journal entry, because deleting a posted payroll
    would silently change a filed period. That asymmetry is the whole reason
    POSTED is a distinct state from PAID.

    Segregation of duties: the user who runs the calculation must not be the
    user who approves it (enforced in
    ``apps.payroll.services.engine.approve_run``). ``calculated_by`` is stored
    for exactly that comparison, and both are written to the audit log.

    GL posting happens exactly once
    -------------------------------
    The *only* moment payroll touches the ledger is the ``APPROVED -> POSTED``
    transition, via
    ``apps.payroll.services.engine.post_run_to_ledger()``, which builds one
    balanced :class:`accounting.JournalEntryDraft` for the whole run and hands
    it to ``post_entry()`` with::

        idempotency_key = f"payroll:{run.id}"

    That key is enforced by ``uq_entry_idempotency`` on
    ``accounting_journal_entry``. A retried Celery task, a double-clicked
    button or a replayed webhook therefore returns the *existing* entry rather
    than posting salary expense twice. The ``journal_entry`` OneToOne on this
    model is the second, independent guard: the database will not let two runs
    claim the same entry, and a run that already has one is refused by the
    service before it builds a draft.

    ``locked`` freezes payslip editing independently of status, for the window
    between calculation and approval when HR is reviewing figures.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CALCULATING = "calculating", "Calculating"
        CALCULATED = "calculated", "Calculated"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        APPROVED = "approved", "Approved"
        POSTED = "posted", "Posted to ledger"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.CALCULATING, Status.CANCELLED},
        # Back to DRAFT when the engine fails half-way: the partial payslips
        # are discarded and the run is re-calculable.
        Status.CALCULATING: {Status.CALCULATED, Status.DRAFT, Status.CANCELLED},
        # Re-calculation is legal until someone has approved it.
        Status.CALCULATED: {Status.PENDING_APPROVAL, Status.CALCULATING, Status.CANCELLED},
        Status.PENDING_APPROVAL: {Status.APPROVED, Status.CALCULATED, Status.CANCELLED},
        Status.APPROVED: {Status.POSTED, Status.CANCELLED},
        # Terminal as far as this row is concerned: after POSTED the only
        # correction is a reversing journal entry.
        Status.POSTED: {Status.PAID},
        Status.PAID: set(),
        Status.CANCELLED: set(),
    }

    class Frequency(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        BIWEEKLY = "biweekly", "Bi-weekly"
        WEEKLY = "weekly", "Weekly"
        OFF_CYCLE = "off_cycle", "Off-cycle"

    name = models.CharField(max_length=150)
    period_start = models.DateField()
    period_end = models.DateField()
    #: The date the money reaches employees; drives the GL entry date and the
    #: bank file value date. Frequently in the following month, which is why
    #: it is not derived from period_end.
    pay_date = models.DateField(db_index=True)
    frequency = models.CharField(
        max_length=10, choices=Frequency.choices, default=Frequency.MONTHLY
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    #: Restricts the run to one department subtree (for staged approvals in
    #: large tenants). NULL = the whole company.
    department = models.ForeignKey(
        "hr.Department", null=True, blank=True, on_delete=models.PROTECT,
        related_name="payroll_runs",
    )

    #: Control totals, materialised from the payslips at the end of the
    #: calculation. Reports aggregate the payslips; these exist so the run
    #: list does not, and so the posting service can assert against them.
    employee_count = models.PositiveIntegerField(default=0)
    total_gross = MoneyField()
    total_deductions = MoneyField()
    total_employer_cost = MoneyField()
    total_net = MoneyField()
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.EGP
    )

    #: NULL until POSTED. OneToOne, so the database itself guarantees that no
    #: two runs share a ledger entry and no run is posted twice.
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="payroll_run",
    )

    calculated_at = models.DateTimeField(null=True, blank=True)
    #: Stored for the segregation-of-duties check in approve_run().
    calculated_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="approved_payroll_runs",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    locked = models.BooleanField(
        default=False, help_text="Freezes payslip edits while the run is under review."
    )
    notes = models.CharField(max_length=500, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "payroll_run"
        ordering = ["-period_start"]
        constraints = [
            # Running the same period twice is the most expensive mistake in
            # this module: it doubles salary expense in the GL and, if the bank
            # file goes out, pays everybody twice. The database refuses.
            # Off-cycle corrections use frequency=OFF_CYCLE, which is what
            # makes that legitimate case expressible without weakening this.
            models.UniqueConstraint(
                fields=["tenant", "period_start", "period_end", "frequency"],
                name="uq_pay_run_period",
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="ck_pay_run_period_order",
            ),
            models.CheckConstraint(
                condition=models.Q(total_gross__gte=0)
                & models.Q(total_deductions__gte=0)
                & models.Q(total_net__gte=0)
                & models.Q(total_employer_cost__gte=0),
                name="ck_pay_run_totals_non_negative",
            ),
            # POSTED without a ledger entry means the books and payroll
            # disagree; that state must be unreachable, not merely unlikely.
            models.CheckConstraint(
                condition=~models.Q(status__in=["posted", "paid"])
                | models.Q(journal_entry__isnull=False),
                name="ck_pay_run_posted_has_entry",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="approved")
                | models.Q(approved_by__isnull=False),
                name="ck_pay_run_approved_has_approver",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status"], name="ix_pay_run_status"),
            models.Index(
                fields=["tenant", "period_start", "period_end"], name="ix_pay_run_period"
            ),
            models.Index(fields=["tenant", "pay_date"], name="ix_pay_run_pay_date"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} ({self.period_start}..{self.period_end})"

    @property
    def idempotency_key(self) -> str:
        """The key that makes GL posting exactly-once. Derived, never stored,
        so it cannot drift from the run it identifies."""
        return f"payroll:{self.id}"

    @property
    def is_editable(self) -> bool:
        return not self.locked and self.status in {
            self.Status.DRAFT, self.Status.CALCULATED
        }

    def assert_can_transition(self, new_status: str) -> None:
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal payroll run transition {self.status} -> {new_status}. "
                f"A posted run is corrected with a reversing journal entry, "
                f"never by moving it backwards."
            )


class Payslip(ImmutableFinancialModel):
    """One employee's pay for one run — a statutory document, frozen on issue.

    Why the employee is snapshotted into JSONB
    ------------------------------------------
    ``employee_snapshot`` denormalises the employee's name, Arabic name,
    employee code, job title, department, department path, base salary,
    bank details (masked) and tax identifiers *as they were on the pay date*.
    That duplication is mandatory, not laziness:

    * A payslip is a legal document that must reproduce **identically in five
      years**. If it renders by joining to ``hr_employee``, then a marriage
      (name change), a promotion (title change), a re-organisation
      (department renamed or merged) or a GDPR erasure request silently
      rewrites documents that were already issued, filed and, in a dispute,
      submitted as evidence.
    * The department shown on a payslip is the one the cost was charged to at
      the time. Moving an employee between departments must not retroactively
      move last year's payroll expense.
    * Regenerating a PDF must be deterministic. With a snapshot the render is
      a pure function of this row; without it, it is a function of the current
      state of the whole HR module.

    The FK to ``employee`` is kept as well, but for *navigation and reporting*
    ("show me this person's payslips"), never as the source of printed values.

    Why the net identity is not a CHECK constraint
    ----------------------------------------------
    The true invariant is::

        net_amount == gross_amount
                      - income_tax_amount
                      - social_insurance_employee
                      - other_deductions

    That is expressible in SQL, but only as long as no jurisdiction ever adds
    a fifth deduction column — and encoding a moving definition of "total
    deductions" in a constraint means a schema migration on every regulatory
    change, applied to a table with millions of immutable rows. Instead the
    database enforces the parts that never change (``net >= 0``,
    ``gross >= 0``, every component non-negative) and the exact identity is
    asserted twice at the point of creation: once in the payroll engine, with
    Decimals, before the payslip is written, and once by the GL balance check
    when the run is posted — ``sum(debits) == sum(credits)`` cannot hold if
    any payslip's net is inconsistent with its gross and deductions.
    """

    class PaymentStatus(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        IN_BANK_FILE = "in_bank_file", "In bank file"
        PAID = "paid", "Paid"
        FAILED = "failed", "Payment failed"
        HELD = "held", "Held"

    #: CASCADE would be wrong even for a draft run: a payslip is a financial
    #: document and its parent run is deleted only if it was never calculated.
    run = models.ForeignKey(
        PayrollRun, on_delete=models.PROTECT, related_name="payslips"
    )
    employee = models.ForeignKey(
        "hr.Employee", on_delete=models.PROTECT, related_name="payslips"
    )
    #: See the class docstring. This, not the FK, is what the PDF renders.
    employee_snapshot = models.JSONField(default=dict, blank=True)

    #: Days in the period the employee was scheduled to work.
    working_days = QuantityField(default=ZERO)
    #: Days actually paid (worked + paid leave + holidays). The proration
    #: numerator: gross is scaled by paid_days / working_days.
    paid_days = QuantityField(default=ZERO)
    leave_days_unpaid = QuantityField(default=ZERO)
    overtime_hours = QuantityField(default=ZERO)

    gross_amount = MoneyField()
    #: Gross minus non-taxable earnings and pre-tax reliefs. Stored because
    #: the tax authority asks for it directly and because recomputing it
    #: requires the tax rules that were in force at the time.
    taxable_amount = MoneyField()
    income_tax_amount = MoneyField()
    social_insurance_employee = MoneyField()
    #: Employer's share: not a deduction from the employee, but a cost to the
    #: company. It never touches net pay and is posted as its own expense.
    social_insurance_employer = MoneyField()
    other_deductions = MoneyField()
    net_amount = MoneyField()
    currency = models.CharField(max_length=3, choices=Currency.choices)

    payment_status = models.CharField(
        max_length=14, choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID, db_index=True,
    )
    payment_reference = models.CharField(max_length=100, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    #: Object-storage key for the rendered PDF; regenerable from the snapshot.
    pdf_key = models.CharField(max_length=255, blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "payroll_payslip"
        ordering = ["run", "employee"]
        constraints = [
            # One payslip per employee per run. Without it a retried
            # calculation adds a second slip and the run's total gross —
            # and therefore the GL posting — is overstated.
            models.UniqueConstraint(
                fields=["tenant", "run", "employee"], name="uq_pay_payslip_run_employee"
            ),
            models.CheckConstraint(
                condition=models.Q(gross_amount__gte=0) & models.Q(net_amount__gte=0),
                name="ck_pay_payslip_gross_net_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(income_tax_amount__gte=0)
                & models.Q(social_insurance_employee__gte=0)
                & models.Q(social_insurance_employer__gte=0)
                & models.Q(other_deductions__gte=0)
                & models.Q(taxable_amount__gte=0),
                name="ck_pay_payslip_components_non_negative",
            ),
            # Net can never exceed gross; if it does, a deduction was applied
            # with the wrong sign.
            models.CheckConstraint(
                condition=models.Q(net_amount__lte=models.F("gross_amount")),
                name="ck_pay_payslip_net_lte_gross",
            ),
            models.CheckConstraint(
                condition=models.Q(paid_days__gte=0)
                & models.Q(working_days__gte=0)
                & models.Q(overtime_hours__gte=0),
                name="ck_pay_payslip_days_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "run"], name="ix_pay_slip_run"),
            models.Index(fields=["tenant", "employee"], name="ix_pay_slip_employee"),
            models.Index(
                fields=["tenant", "payment_status"], name="ix_pay_slip_payment"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"Payslip {self.employee_id} / {self.run_id}"

    @property
    def total_deductions(self) -> object:
        """The deductions that reduce net pay. The employer's social insurance
        share is deliberately excluded — it is a company cost, not a
        deduction, and including it here would understate net pay."""
        return (
            self.income_tax_amount
            + self.social_insurance_employee
            + self.other_deductions
        )

    @property
    def employer_cost(self) -> object:
        return self.gross_amount + self.social_insurance_employer


class PayslipLine(ImmutableFinancialModel):
    """One component's contribution to one payslip — the "why" behind the net.

    The line, like its parent, snapshots its component
    (``component_snapshot``: code, name, type, calculation type, rate,
    accounts). A tenant that renames "Transport allowance" to "Mobility
    allowance", changes its rate or deactivates it must not alter what last
    year's payslips say they paid.

    ``calculation_note`` records the arithmetic in human-readable form
    ("1 200.00 x 0.15 = 180.00", "overtime 12.5h x 62.50"). It is what an HR
    officer reads out to an employee who is querying their pay, and what makes
    a formula component debuggable in production without re-running the
    engine.
    """

    #: CASCADE is permitted here: lines are child rows of a payslip and have
    #: no independent existence. A payslip itself cannot be deleted
    #: (ImmutableFinancialModel), so this cascade is effectively unreachable
    #: outside a DRAFT-run cleanup.
    payslip = models.ForeignKey(
        Payslip, on_delete=models.CASCADE, related_name="lines"
    )
    component = models.ForeignKey(
        PayrollComponent, null=True, blank=True, on_delete=models.PROTECT,
        related_name="payslip_lines",
    )
    component_snapshot = models.JSONField(default=dict, blank=True)
    sequence = models.PositiveSmallIntegerField(default=0)
    quantity = QuantityField(default=ZERO)
    rate = RateField()
    amount = MoneyField()
    is_taxable = models.BooleanField(default=True)
    calculation_note = models.CharField(max_length=255, blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "payroll_payslip_line"
        ordering = ["payslip", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["payslip", "sequence"], name="uq_pay_slip_line_sequence"
            ),
            # Amounts are magnitudes; direction comes from the component type,
            # exactly as debit/credit are separate columns in the ledger.
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="ck_pay_slip_line_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "payslip"], name="ix_pay_slip_line_parent"),
            models.Index(fields=["tenant", "component"], name="ix_pay_slip_line_comp"),
        ]


# ---------------------------------------------------------------------------
# Disbursement
# ---------------------------------------------------------------------------

class PayrollBankFile(TenantScopedModel):
    """A batch payment instruction sent to the bank for one run.

    Separate from the run because the two fail independently: a run can be
    approved and posted while the bank file is rejected for a malformed IBAN,
    and a run can legitimately produce several files (one per paying bank, or
    a re-issue after a rejection). Modelling the file as fields on the run
    would make the second attempt overwrite the evidence of the first.

    ``total_amount`` and ``employee_count`` are recorded here as the figures
    *actually transmitted*, which is what gets reconciled against the bank
    statement — not what the run says it intended to pay.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        GENERATED = "generated", "Generated"
        SENT = "sent", "Sent to bank"
        CONFIRMED = "confirmed", "Confirmed by bank"
        REJECTED = "rejected", "Rejected by bank"
        CANCELLED = "cancelled", "Cancelled"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.GENERATED, Status.CANCELLED},
        Status.GENERATED: {Status.SENT, Status.CANCELLED},
        Status.SENT: {Status.CONFIRMED, Status.REJECTED},
        Status.CONFIRMED: set(),
        # A rejected file is re-issued as a new file, preserving the evidence
        # of what the bank refused and why.
        Status.REJECTED: set(),
        Status.CANCELLED: set(),
    }

    run = models.ForeignKey(
        PayrollRun, on_delete=models.PROTECT, related_name="bank_files"
    )
    reference = models.CharField(max_length=64, blank=True)
    #: Bank-specific layout identifier (SEPA pain.001, CSV, EG-ACH...).
    file_format = models.CharField(max_length=30, default="csv")
    file_key = models.CharField(max_length=255, blank=True)
    #: Hash of the transmitted bytes: proves what was sent if the bank and the
    #: company disagree about the contents of a batch.
    sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    total_amount = MoneyField()
    employee_count = models.PositiveIntegerField(default=0)
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.EGP
    )
    source_bank_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT,
        related_name="payroll_bank_files",
    )
    value_date = models.DateField(null=True, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    bank_response = models.JSONField(default=dict, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "payroll_bank_file"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_amount__gte=0),
                name="ck_pay_bank_file_amount_non_negative",
            ),
            models.UniqueConstraint(
                fields=["tenant", "reference"],
                condition=~models.Q(reference=""),
                name="uq_pay_bank_file_reference",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "run"], name="ix_pay_bankfile_run"),
            models.Index(fields=["tenant", "status"], name="ix_pay_bankfile_status"),
        ]

    def assert_can_transition(self, new_status: str) -> None:
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal bank file transition {self.status} -> {new_status}."
            )


class SalaryDisbursement(TenantScopedModel):
    """One employee's line inside a :class:`PayrollBankFile`.

    Bank rejections are per-line, not per-file: twenty-nine transfers settle
    and one bounces on a closed account. Without a row per employee there is
    nowhere to record that, and the failed payslip silently stays marked as
    paid.

    The IBAN is copied here rather than read from the employee at settlement
    time, for the same reason payslips snapshot: the file must show the
    account the money was actually sent to, even after the employee updates
    their bank details.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        SETTLED = "settled", "Settled"
        FAILED = "failed", "Failed"
        RETURNED = "returned", "Returned by bank"

    bank_file = models.ForeignKey(
        PayrollBankFile, on_delete=models.CASCADE, related_name="disbursements"
    )
    payslip = models.ForeignKey(
        Payslip, on_delete=models.PROTECT, related_name="disbursements"
    )
    employee = models.ForeignKey(
        "hr.Employee", on_delete=models.PROTECT, related_name="disbursements"
    )
    amount = MoneyField()
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.EGP
    )
    #: Snapshot of the destination account at transmission time. PII: masked
    #: in every serializer and log.
    beneficiary_iban = models.CharField(max_length=64, blank=True)
    beneficiary_bank = models.CharField(max_length=150, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    settled_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    payment_reference = models.CharField(max_length=100, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "payroll_salary_disbursement"
        ordering = ["bank_file", "employee"]
        constraints = [
            # One instruction per payslip per file: a duplicate line pays the
            # same net twice out of the same batch.
            models.UniqueConstraint(
                fields=["tenant", "bank_file", "payslip"],
                name="uq_pay_disbursement_slip",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0),
                name="ck_pay_disbursement_non_negative",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="failed") | ~models.Q(failure_reason=""),
                name="ck_pay_disbursement_failure_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "bank_file"], name="ix_pay_disb_file"),
            models.Index(fields=["tenant", "employee"], name="ix_pay_disb_employee"),
            models.Index(fields=["tenant", "status"], name="ix_pay_disb_status"),
        ]


# ---------------------------------------------------------------------------
# Salary structures — the reusable package layer
# ---------------------------------------------------------------------------

class SalaryStructure(TenantScopedModel):
    """A named package of components: "Senior Engineer", "Field Driver".

    ``EmployeeComponent`` already attaches components to one employee at a
    time, and that is the right primitive — but it is the only one, so a
    company with ninety drivers on the same package holds that package ninety
    times. Raising the transport allowance means ninety edits, and the failure
    mode is not that it is slow: it is that edit sixty-three gets missed and
    nobody finds out until a driver asks why their colleague earns more.

    A structure is a *template*, not a grant. Assigning it (see
    :class:`SalaryStructureAssignment`) is what gives an employee the
    components, and the assignment is dated — so a mid-year promotion is two
    assignments, not an overwrite, and last March's payslip can still be
    explained by the package that was in force in March.
    """

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    description = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "payroll_salary_structure"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_payroll_structure_code"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.code} — {self.name}"


class SalaryStructureLine(TenantScopedModel):
    """One component inside a structure, and how it is sized.

    Two ways to size it, and exactly one may be used per line:

    ``amount``
        A flat figure. Housing allowance of 2 000 a month.
    ``percentage_of_base``
        A fraction of the assignment's ``base_salary``. Transport at 10% of
        base, so the package scales when someone is promoted without anyone
        re-deriving the number.

    The exclusivity is a database constraint rather than a convention because
    a line carrying both is genuinely ambiguous — there is no defensible rule
    for which wins, and picking one silently is how a payroll ends up 2 000
    out per person per month.
    """

    structure = models.ForeignKey(
        SalaryStructure, on_delete=models.CASCADE, related_name="lines"
    )
    component = models.ForeignKey(
        PayrollComponent, on_delete=models.PROTECT, related_name="structure_lines"
    )
    amount = MoneyField(default=ZERO)
    percentage_of_base = RateField(default=0)
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta(TenantScopedModel.Meta):
        db_table = "payroll_salary_structure_line"
        ordering = ["structure", "sequence", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["structure", "component"],
                name="uq_payroll_structure_component",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0)
                & models.Q(percentage_of_base__gte=0),
                name="ck_payroll_structure_line_non_negative",
            ),
            # Exactly one sizing method. Both set is ambiguous; neither set is
            # a line worth nothing, which is a data-entry slip rather than an
            # intention.
            models.CheckConstraint(
                condition=(
                    models.Q(amount__gt=0, percentage_of_base=0)
                    | models.Q(amount=0, percentage_of_base__gt=0)
                ),
                name="ck_payroll_structure_line_one_sizing",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.structure.code}/{self.component.code}"

    def resolve(self, base_salary: Decimal) -> Decimal:
        """The line's value for a given base salary."""
        if self.percentage_of_base > ZERO:
            return base_salary * self.percentage_of_base
        return self.amount


class SalaryStructureAssignment(TenantScopedModel):
    """An employee on a structure, from a date, at a base salary.

    Dated rather than current-only, and never edited in place. Payroll for a
    past period must resolve the package that was in force *then*: an
    assignment that was overwritten on promotion makes every prior payslip
    unexplainable, and payslips are the documents employees dispute.

    ``base_salary`` lives here rather than on the structure because the
    package is shared and the salary is not — two engineers on the same
    structure are paid differently, and that difference is the entire point of
    a base figure.
    """

    employee = models.ForeignKey(
        "hr.Employee", on_delete=models.PROTECT, related_name="salary_structures"
    )
    structure = models.ForeignKey(
        SalaryStructure, on_delete=models.PROTECT, related_name="assignments"
    )
    from_date = models.DateField(db_index=True)
    to_date = models.DateField(null=True, blank=True)
    base_salary = MoneyField()
    currency = models.CharField(max_length=3, choices=Currency.choices)
    notes = models.CharField(max_length=255, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "payroll_salary_structure_assignment"
        ordering = ["-from_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "employee", "from_date"],
                name="uq_payroll_assignment_start",
            ),
            models.CheckConstraint(
                condition=models.Q(to_date__isnull=True)
                | models.Q(to_date__gte=models.F("from_date")),
                name="ck_payroll_assignment_dates",
            ),
            models.CheckConstraint(
                condition=models.Q(base_salary__gt=0),
                name="ck_payroll_assignment_base_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "employee", "-from_date"],
                         name="ix_payroll_asg_emp"),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.employee} on {self.structure.code} from {self.from_date}"

    def covers(self, on_date) -> bool:
        if on_date < self.from_date:
            return False
        return self.to_date is None or on_date <= self.to_date
