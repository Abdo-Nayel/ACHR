"""
Human Resources: the org chart, the people in it, their time and their leave.

This module records *HR facts*. It deliberately does not record *authorisation*
facts (that is ``apps.iam``) and does not record *money movements* (that is
``apps.accounting``). The three are linked by narrow, explicit foreign keys:

    iam.TenantMembership.employee  -> hr.Employee     (a login that is a person)
    iam.RoleAssignment.department  -> hr.Department   (ABAC scope)
    hr.Employee.default_cost_center -> accounting.Account
    payroll.Payslip.employee       -> hr.Employee     (snapshotted, see payroll)

Two structural decisions are load-bearing and are explained at length in the
docstrings below because getting them wrong is expensive and irreversible:

1. ``Employee`` is not ``iam.User`` (see :class:`Employee`).
2. Compensation lives in an append-only :class:`SalaryRevision` history, not
   in a mutable column that payroll reads (see :class:`SalaryRevision`).
"""

from __future__ import annotations

from django.db import models

from apps.core.fields import MoneyField, QuantityField, RateField, ZERO
from apps.core.models import Currency, TenantScopedModel


# ---------------------------------------------------------------------------
# Org chart
# ---------------------------------------------------------------------------

class Department(TenantScopedModel):
    """A node in the tenant's organisational chart.

    Departments are the unit of *cost attribution* (salary expense is posted
    per department cost centre) and the unit of *authorisation scope* (an HR
    manager assigned to "Engineering" may act on Engineering and everything
    beneath it — ``iam.ScopeRule.Strategy.DEPARTMENT_SUBTREE``).

    Why ``path`` exists
    -------------------
    ``parent`` alone gives a correct hierarchy but a slow one. Answering
    "every employee in this manager's subtree" from ``parent`` requires a
    recursive CTE, and that CTE runs on *every* authorisation check — i.e.
    on every list endpoint, for every request, for every tenant. At a few
    thousand departments that is a recursive scan in the hot path of the
    permission layer.

    ``path`` is a materialised path built from department codes, always
    slash-delimited and slash-terminated::

        "/root/eng/backend/"

    The subtree query then collapses to a single indexed prefix scan::

        Department.objects.filter(path__startswith=manager_dept.path)

    which PostgreSQL answers from ``ix_hr_dept_path`` (a B-tree supports
    ``LIKE 'prefix%'`` range scans when the pattern is left-anchored) in
    O(log n + matches) with no recursion at all. The trailing slash is not
    cosmetic: without it ``/eng/`` would also match a sibling ``/engineering/``.

    ``path`` is derived state and is rebuilt by
    ``apps.hr.services.org.rebuild_paths()`` whenever a department is moved.
    It is never edited by hand.
    """

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children",
    )
    #: PROTECT, not CASCADE: deleting a department must not silently delete the
    #: sub-tree (and with it the cost-centre history of everyone in it).
    manager = models.ForeignKey(
        "hr.Employee",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="managed_departments",
        help_text="String reference: Employee is defined below and points back here.",
    )
    #: Cost centre this department's payroll expense is charged to. NULL falls
    #: back to the tenant's default salary expense account at posting time.
    cost_center_account = models.ForeignKey(
        "accounting.Account",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="hr_departments",
    )
    #: Materialised ancestry — see the class docstring. Maintained by service
    #: code, never by a view or a serializer.
    path = models.CharField(
        max_length=500,
        blank=True,
        db_index=True,
        help_text="Slash-delimited, slash-terminated ancestry, e.g. /root/eng/backend/",
    )
    #: Cached depth, so the UI can indent a flat list without walking parents.
    depth = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_department"
        ordering = ["path", "code"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_hr_department_code"),
            models.CheckConstraint(
                condition=~models.Q(parent=models.F("id")),
                name="ck_hr_department_no_self_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "path"], name="ix_hr_dept_path"),
            models.Index(fields=["tenant", "parent"], name="ix_hr_dept_parent"),
            models.Index(fields=["tenant", "is_active"], name="ix_hr_dept_active"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.name}"

    def build_path(self) -> str:
        """Recompute this node's path from its ancestors.

        Kept as a pure function of ``parent`` so a repair job can rebuild the
        whole column deterministically after a bad import.
        """
        prefix = self.parent.path if self.parent_id and self.parent.path else "/"
        return f"{prefix}{self.code}/"

    @property
    def subtree_prefix(self) -> str:
        """The value to use in ``path__startswith`` for a subtree query."""
        return self.path or self.build_path()


class JobTitle(TenantScopedModel):
    """A position definition: what a role is called and what it may be paid.

    Also called a "Position" in some HRIS vocabularies. It is a *template*,
    not an assignment — the assignment is ``Employee.job_title``. Keeping the
    salary band here (rather than only on the employee) is what lets HR
    validate "this offer is below the band for a Senior Engineer" at hire
    time and lets compensation reviews report band penetration.

    The band is advisory, not a database constraint on Employee.base_salary:
    real organisations make out-of-band exceptions, and a hard constraint
    would simply be worked around by mislabelling the title, which destroys
    the reporting value of the band.
    """

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.PROTECT, related_name="job_titles",
    )
    #: Free-form pay grade / job level ("L4", "Grade 7"). Ordinal meaning is
    #: tenant-specific, so it is not an enum.
    grade = models.CharField(max_length=20, blank=True)
    min_salary = MoneyField()
    max_salary = MoneyField()
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.EGP
    )
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_job_title"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_hr_job_title_code"),
            models.CheckConstraint(
                condition=models.Q(min_salary__gte=0) & models.Q(max_salary__gte=0),
                name="ck_hr_job_title_band_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(max_salary__gte=models.F("min_salary")),
                name="ck_hr_job_title_band_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "department"], name="ix_hr_title_dept"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

class Gender(models.TextChoices):
    MALE = "male", "Male"
    FEMALE = "female", "Female"
    UNSPECIFIED = "unspecified", "Unspecified"


class Employee(TenantScopedModel):
    """A person employed by the tenant — an HR fact, not a login.

    Why Employee and iam.User are separate models
    ---------------------------------------------
    It is tempting to add ``salary`` and ``hire_date`` to the user table and
    be done. That is a design error with four independent failure modes, and
    every one of them shows up in production:

    1. **Not every employee has a login.** Factory floor, drivers, cleaners
       and seasonal staff are clocked in by a supervisor. If Employee *is*
       User, payroll cannot pay someone who has no email address, so someone
       invents ``ahmed.driver@localhost`` and the identity table fills with
       credentials that must never authenticate.
    2. **Not every login is an employee.** External auditors, the outsourced
       accountant, the tax consultant and platform admins all need accounts
       and must never appear on a payroll register or an org chart.
    3. **A User is global; an Employee is tenant-scoped.** One accountant
       serves five client companies with one login (see
       :class:`iam.TenantMembership`). Merging the tables would either force
       five logins or leak one company's HR data into another's.
    4. **Lifecycles diverge.** A terminated employee's HR record must survive
       for the statutory retention period (payslips, tax filings, end-of-
       service settlements) while their login must die the same afternoon.
       One row cannot be both retained and revoked.

    The link is therefore a nullable one-way join on
    ``iam.TenantMembership.employee``: identity points at HR, HR does not
    depend on identity.

    PII
    ---
    ``national_id``, ``bank_account_iban``, ``tax_id`` and
    ``social_insurance_number`` are personal data. They are stored as-is in
    this model definition, but the columns are subject to **field-level
    encryption** (pgcrypto, key held in KMS, applied in migration
    ``hr/0003_pii_encryption``) and are excluded from every default
    serializer, log record and CSV export. Reading them requires
    ``hr.employee.read_pii``, which is a ``is_sensitive`` permission and is
    written to ``tenancy.TenantAuditLog`` on each access. Do not add them to
    a ``__str__``, an index that could leak by timing, or an error message.
    """

    class EmploymentType(models.TextChoices):
        FULL_TIME = "full_time", "Full time"
        PART_TIME = "part_time", "Part time"
        CONTRACT = "contract", "Contract"
        INTERN = "intern", "Intern"
        TEMPORARY = "temporary", "Temporary"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ON_LEAVE = "on_leave", "On leave"
        SUSPENDED = "suspended", "Suspended"
        TERMINATED = "terminated", "Terminated"
        RESIGNED = "resigned", "Resigned"

    #: TERMINATED and RESIGNED are terminal. Re-hiring someone is a *new*
    #: Employee row with a new employee_code: their service period, seniority
    #: accrual and end-of-service entitlement restart, and silently reviving
    #: the old row would carry the previous leave balance and hire date into
    #: the new contract — a real and expensive payroll bug.
    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.ACTIVE: {Status.ON_LEAVE, Status.SUSPENDED, Status.TERMINATED, Status.RESIGNED},
        Status.ON_LEAVE: {Status.ACTIVE, Status.SUSPENDED, Status.TERMINATED, Status.RESIGNED},
        Status.SUSPENDED: {Status.ACTIVE, Status.TERMINATED, Status.RESIGNED},
        Status.TERMINATED: set(),
        Status.RESIGNED: set(),
    }

    class PayFrequency(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        BIWEEKLY = "biweekly", "Bi-weekly"
        WEEKLY = "weekly", "Weekly"

    class MaritalStatus(models.TextChoices):
        SINGLE = "single", "Single"
        MARRIED = "married", "Married"
        DIVORCED = "divorced", "Divorced"
        WIDOWED = "widowed", "Widowed"
        UNSPECIFIED = "unspecified", "Unspecified"

    #: Human-facing identifier printed on payslips and used by biometric
    #: terminals. Unique *per tenant* — a global unique index would let one
    #: customer probe another's headcount by trying codes.
    employee_code = models.CharField(max_length=30)

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    #: Full legal name in Arabic. Kept as one field rather than split: Arabic
    #: legal names are a father/grandfather chain that does not decompose into
    #: first/last, and government forms want the chain verbatim.
    arabic_name = models.CharField(max_length=200, blank=True)

    #: PII — see class docstring. National identity number / iqama / CPR.
    national_id = models.CharField(max_length=64, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(
        max_length=12, choices=Gender.choices, default=Gender.UNSPECIFIED
    )
    marital_status = models.CharField(
        max_length=12, choices=MaritalStatus.choices, default=MaritalStatus.UNSPECIFIED
    )

    personal_email = models.EmailField(blank=True)
    #: Not unique and not an authentication credential — see the class
    #: docstring. It is a contact address that happens to look like a login.
    work_email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    #: {"line1", "line2", "city", "governorate", "postal_code", "country"}.
    #: JSONB because address shape is country-specific and a schema change
    #: per country is not worth a migration.
    address = models.JSONField(default=dict, blank=True)

    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="employees"
    )
    job_title = models.ForeignKey(
        JobTitle, null=True, blank=True, on_delete=models.PROTECT, related_name="employees"
    )
    #: PROTECT: you may not delete a manager who still has reports; the org
    #: chart must never contain a dangling reporting line, because the leave
    #: approval chain walks it.
    manager = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="direct_reports"
    )
    work_schedule = models.ForeignKey(
        "hr.WorkSchedule",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="employees",
        help_text="Default working pattern; attendance is scored against it.",
    )

    employment_type = models.CharField(
        max_length=12,
        choices=EmploymentType.choices,
        default=EmploymentType.FULL_TIME,
        db_index=True,
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )

    hire_date = models.DateField(db_index=True)
    probation_end_date = models.DateField(null=True, blank=True)
    termination_date = models.DateField(null=True, blank=True)
    termination_reason = models.CharField(max_length=255, blank=True)

    #: Current contractual salary. Read by HR screens and by the offer/band
    #: checks. **Payroll does not read this column** — it reads the salary
    #: that was effective during the pay period from :class:`SalaryRevision`.
    base_salary = MoneyField()
    salary_currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.EGP
    )
    pay_frequency = models.CharField(
        max_length=10, choices=PayFrequency.choices, default=PayFrequency.MONTHLY
    )

    #: PII — see class docstring.
    bank_account_iban = models.CharField(max_length=64, blank=True)
    bank_name = models.CharField(max_length=150, blank=True)
    tax_id = models.CharField(max_length=64, blank=True)
    social_insurance_number = models.CharField(max_length=64, blank=True)

    #: Overrides the department's cost centre for this individual (a shared
    #: services engineer charged to a specific project account, for example).
    default_cost_center = models.ForeignKey(
        "accounting.Account",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="hr_employees",
    )
    #: Object-storage key, not a file blob: photos are served through a signed
    #: URL so that access is authorised per request rather than by URL guess.
    photo_key = models.CharField(max_length=255, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_employee"
        ordering = ["employee_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "employee_code"], name="uq_hr_employee_code"
            ),
            models.CheckConstraint(
                condition=models.Q(termination_date__isnull=True)
                | models.Q(termination_date__gte=models.F("hire_date")),
                name="ck_hr_employee_termination_after_hire",
            ),
            # A terminated employee without a leaving date breaks end-of-service
            # calculation, the final payslip proration and the statutory filing.
            models.CheckConstraint(
                condition=~models.Q(status="terminated")
                | models.Q(termination_date__isnull=False),
                name="ck_hr_employee_terminated_has_date",
            ),
            models.CheckConstraint(
                condition=models.Q(base_salary__gte=0),
                name="ck_hr_employee_salary_non_negative",
            ),
            # Self-management makes the approval chain an infinite loop.
            models.CheckConstraint(
                condition=~models.Q(manager=models.F("id")),
                name="ck_hr_employee_no_self_manager",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status"], name="ix_hr_emp_status"),
            models.Index(fields=["tenant", "department", "status"], name="ix_hr_emp_dept"),
            models.Index(fields=["tenant", "manager"], name="ix_hr_emp_manager"),
            models.Index(fields=["tenant", "hire_date"], name="ix_hr_emp_hire_date"),
            models.Index(fields=["tenant", "job_title"], name="ix_hr_emp_job_title"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.employee_code} — {self.first_name} {self.last_name}"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_payable(self) -> bool:
        """Whether this employee should appear in a payroll run at all.

        SUSPENDED is deliberately excluded: suspension is usually unpaid or
        partially paid and always requires an explicit HR decision, so the
        engine refuses to guess.
        """
        return self.status in {self.Status.ACTIVE, self.Status.ON_LEAVE}

    def assert_can_transition(self, new_status: str) -> None:
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal employee status transition {self.status} -> {new_status}. "
                f"Re-hiring a terminated employee requires a new employee record."
            )


class EmployeeDocument(TenantScopedModel):
    """A file attached to an employee's HR record.

    The binary never lives in PostgreSQL: ``file_key`` is an object-storage
    key and access is granted through a short-lived signed URL. ``sha256`` is
    stored so that a document produced in a labour dispute can be proved to
    be byte-identical to the one uploaded on the recorded date.

    ``is_confidential`` marks documents that only HR roles may read (medical
    certificates, disciplinary letters, salary agreements). It is enforced by
    the ABAC policy for the ``employee_document`` resource, not merely by the
    UI — a manager holding ``hr.employee_document.read`` scoped to their
    department still may not open a confidential row.
    """

    class DocumentType(models.TextChoices):
        CONTRACT = "contract", "Employment contract"
        ID = "id", "Identity document"
        CERTIFICATE = "certificate", "Certificate / qualification"
        VISA = "visa", "Visa / work permit"
        PASSPORT = "passport", "Passport"
        MEDICAL = "medical", "Medical"
        OTHER = "other", "Other"

    #: CASCADE is correct here and only here in this module: a document has no
    #: meaning without its employee, and HR record deletion (a GDPR erasure
    #: request) must take the attachments with it. Employee rows themselves are
    #: archived rather than deleted in normal operation.
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="documents"
    )
    document_type = models.CharField(
        max_length=16, choices=DocumentType.choices, db_index=True
    )
    title = models.CharField(max_length=200)
    file_key = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    #: NULL = does not expire. The compliance report ("visas expiring in the
    #: next 60 days") is a range scan on ``ix_hr_doc_expiry``.
    expiry_date = models.DateField(null=True, blank=True)
    is_confidential = models.BooleanField(default=False)
    uploaded_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_employee_document"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expiry_date__isnull=True)
                | models.Q(issue_date__isnull=True)
                | models.Q(expiry_date__gte=models.F("issue_date")),
                name="ck_hr_document_date_order",
            ),
            models.CheckConstraint(
                condition=models.Q(size_bytes__gte=0), name="ck_hr_document_size",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "employee"], name="ix_hr_doc_employee"),
            # Drives the expiring-documents report; (tenant, expiry_date) so the
            # scan is a per-tenant range and not a full-table filter.
            models.Index(fields=["tenant", "expiry_date"], name="ix_hr_doc_expiry"),
        ]


class SalaryRevision(TenantScopedModel):
    """Append-only compensation and employment history.

    Why this is a table and not an UPDATE on ``Employee.base_salary``
    ----------------------------------------------------------------
    Payroll must be **reproducible for any historical period**. Three
    scenarios make a mutable salary column untenable:

    * A March payroll is re-run in June (a correction, an audit request, a
      retro-active bonus). If March's salary was overwritten by an April
      raise, the re-run silently produces different numbers than the payslip
      the employee was actually paid, and the GL no longer reconciles.
    * A raise is approved on the 20th with effect from the 1st. Payroll needs
      "what was effective on 2026-03-31", not "what is in the column now".
    * A labour inspector asks when a salary changed, who approved it and why.
      An UPDATE answers none of those questions; this table answers all three.

    ``Employee.base_salary`` is therefore a *cache of the latest revision* for
    HR screens. The payroll engine reads
    ``SalaryRevision.effective_on(employee, period_end)`` and never the column.

    Rows are never updated or deleted. A mistake is corrected by a new
    revision with a correcting ``reason``.

    ``previous_*`` fields are denormalised copies rather than a join to the
    prior row: it makes "show me every raise over 20%" a single scan and it
    survives the (illegal but occasionally attempted) deletion of an earlier
    row.
    """

    class ChangeType(models.TextChoices):
        HIRE = "hire", "Initial salary on hire"
        RAISE = "raise", "Salary increase"
        CUT = "cut", "Salary decrease"
        PROMOTION = "promotion", "Promotion"
        TRANSFER = "transfer", "Department transfer"
        CORRECTION = "correction", "Correction of a previous revision"

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="salary_revisions"
    )
    change_type = models.CharField(
        max_length=12, choices=ChangeType.choices, default=ChangeType.RAISE
    )
    #: The date the new figure starts applying. Payroll selects the row with
    #: the greatest effective_date <= period_end.
    effective_date = models.DateField(db_index=True)
    previous_salary = MoneyField()
    new_salary = MoneyField()
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.EGP
    )
    #: Employment (non-salary) history, captured on the same row because a
    #: promotion is usually both at once and splitting them makes "what was
    #: this person's title in March" a two-table reconstruction.
    previous_job_title = models.ForeignKey(
        JobTitle, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    new_job_title = models.ForeignKey(
        JobTitle, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    previous_department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    new_department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=500, blank=True)
    approved_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_salary_revision"
        ordering = ["-effective_date", "-created_at"]
        constraints = [
            # One revision per employee per effective date: two rows on the same
            # date make "the salary effective on D" ambiguous, and the engine
            # would pick one arbitrarily.
            models.UniqueConstraint(
                fields=["tenant", "employee", "effective_date"],
                name="uq_hr_salary_revision_date",
            ),
            models.CheckConstraint(
                condition=models.Q(new_salary__gte=0) & models.Q(previous_salary__gte=0),
                name="ck_hr_salary_revision_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            # The engine's lookup: (employee, effective_date DESC) LIMIT 1.
            models.Index(
                fields=["tenant", "employee", "-effective_date"], name="ix_hr_salrev_lookup"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.employee_id} {self.effective_date}: {self.new_salary}"

    def delete(self, *args, **kwargs):  # pragma: no cover - guard rail
        raise PermissionError(
            "SalaryRevision is append-only; historical payroll depends on it. "
            "Record a CORRECTION revision instead."
        )


# ---------------------------------------------------------------------------
# Working patterns
# ---------------------------------------------------------------------------

class Shift(TenantScopedModel):
    """A named daily working window (08:00–16:00 with a 60-minute break).

    Times are stored as wall-clock ``TimeField`` in the tenant's timezone, not
    as UTC datetimes: "the morning shift starts at 08:00" must stay 08:00
    across a DST change, whereas a UTC instant would drift by an hour.

    ``expected_hours_per_day`` is stored rather than derived from
    start/end/break because night shifts cross midnight and because some
    contracts pay a nominal 8 hours for a 7.5-hour attendance window.
    ``overtime_after_hours`` is the daily threshold beyond which worked time is
    scored as overtime.
    """

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    start_time = models.TimeField()
    end_time = models.TimeField()
    #: True when end_time <= start_time, i.e. the shift ends the next calendar
    #: day. Stored explicitly so attendance scoring does not have to infer it.
    crosses_midnight = models.BooleanField(default=False)
    break_minutes = models.PositiveSmallIntegerField(default=0)
    expected_hours_per_day = QuantityField(default=ZERO)
    overtime_after_hours = QuantityField(default=ZERO)
    #: Grace period before a check-in is scored as late. Without it every
    #: employee is "late" by 30 seconds and the report is noise.
    late_grace_minutes = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_shift"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_hr_shift_code"),
            models.CheckConstraint(
                condition=models.Q(expected_hours_per_day__gte=0)
                & models.Q(expected_hours_per_day__lte=24),
                name="ck_hr_shift_expected_hours_range",
            ),
            models.CheckConstraint(
                condition=models.Q(overtime_after_hours__gte=0),
                name="ck_hr_shift_overtime_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_hr_shift_active"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class WorkSchedule(TenantScopedModel):
    """A named weekly working pattern assigned to employees.

    ``working_days`` is an array of ISO weekday numbers (1 = Monday … 7 =
    Sunday) stored as JSONB rather than seven booleans: the working week is
    Sunday–Thursday in much of the Gulf, Monday–Friday in Europe and
    Saturday–Wednesday in a few places, and a fixed-column model quietly
    assumes one of them.

    A day not in ``working_days`` is scored ``WEEKEND`` in attendance and is
    excluded from the "working days" divisor used to prorate a partial-month
    salary — which is exactly why this belongs in the data model and not in
    a constant.
    """

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    default_shift = models.ForeignKey(
        Shift, null=True, blank=True, on_delete=models.PROTECT, related_name="schedules"
    )
    #: e.g. [7, 1, 2, 3, 4] for a Sunday-to-Thursday week.
    working_days = models.JSONField(default=list, blank=True)
    expected_hours_per_week = QuantityField(default=ZERO)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_work_schedule"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_hr_work_schedule_code"
            ),
            # Exactly one default per tenant, as a partial unique index — the
            # alternative (application code that "usually" clears the old flag)
            # eventually leaves two defaults and a coin-flip assignment.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_default=True),
                name="uq_hr_schedule_one_default",
            ),
            models.CheckConstraint(
                condition=models.Q(expected_hours_per_week__gte=0),
                name="ck_hr_schedule_hours_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_hr_schedule_active"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class Holiday(TenantScopedModel):
    """A non-working public or company holiday.

    A holiday inside a leave request's date range is not deducted from the
    leave balance — the employee was not going to work that day anyway. That
    single rule is why holidays are tenant data (and optionally department
    data: a factory and a head office observe different closures) rather than
    a hard-coded national calendar.

    ``is_recurring`` marks fixed-date annual holidays (1 January). Lunar
    holidays are not recurring in the Gregorian calendar and are created per
    year once announced, which is also when their exact dates become known.
    """

    name = models.CharField(max_length=150)
    date = models.DateField(db_index=True)
    is_recurring = models.BooleanField(default=False)
    #: NULL = the whole tenant observes it.
    applies_to_department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.CASCADE, related_name="holidays"
    )
    is_paid = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_holiday"
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "date", "applies_to_department"],
                name="uq_hr_holiday_date_dept",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "date"], name="ix_hr_holiday_date"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.date} {self.name}"


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

class AttendanceRecord(TenantScopedModel):
    """One employee's attendance for one calendar day.

    Why UNIQUE (tenant, employee, work_date)
    ----------------------------------------
    This is the single most important constraint in the attendance model.
    Biometric terminals fire duplicate events, mobile clients retry on a flaky
    connection, and supervisors re-enter a day they think was missed. Without
    the unique index each duplicate becomes a second attendance row for the
    same day, and the payroll engine — which counts rows to derive *paid
    days* — pays that day twice. It is a silent, self-inflicted overpayment
    that nobody notices until the bank file is larger than the accrual.

    With the constraint the duplicate is an ``IntegrityError`` that the
    check-in service turns into an idempotent update of the existing row.

    **A mid-shift break is not a second record.** Prayer breaks, lunch and
    site-to-site travel are :class:`AttendanceBreak` child rows. Modelling
    them as extra AttendanceRecord rows is the obvious shortcut and it breaks
    the constraint above, so the child table exists precisely to make the
    correct thing also the easy thing.

    ``worked_hours`` is computed at checkout and stored: the report must not
    change if the shift definition is edited afterwards, and aggregating
    check-in/check-out pairs on read is a needless per-request cost.
    """

    class Status(models.TextChoices):
        PRESENT = "present", "Present"
        ABSENT = "absent", "Absent"
        LATE = "late", "Late"
        HALF_DAY = "half_day", "Half day"
        ON_LEAVE = "on_leave", "On leave"
        HOLIDAY = "holiday", "Holiday"
        WEEKEND = "weekend", "Weekend"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual entry"
        BIOMETRIC = "biometric", "Biometric terminal"
        MOBILE_GPS = "mobile_gps", "Mobile app (GPS)"
        WEB = "web", "Web check-in"

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="attendance_records"
    )
    work_date = models.DateField(db_index=True)
    check_in_at = models.DateTimeField(null=True, blank=True)
    check_out_at = models.DateTimeField(null=True, blank=True)
    #: The shift the day was *scheduled* against, copied at check-in. Keeping
    #: the FK (rather than re-deriving from the employee's current schedule)
    #: is what makes a historical lateness report stable after a roster change.
    scheduled_shift = models.ForeignKey(
        Shift, null=True, blank=True, on_delete=models.PROTECT, related_name="attendance"
    )

    worked_hours = QuantityField(default=ZERO)
    overtime_hours = QuantityField(default=ZERO)
    late_minutes = models.PositiveIntegerField(default=0)
    early_leave_minutes = models.PositiveIntegerField(default=0)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PRESENT, db_index=True
    )
    source = models.CharField(
        max_length=12, choices=Source.choices, default=Source.MANUAL
    )
    #: {"lat": "30.044420", "lng": "31.235712", "accuracy_m": 12}. Coordinates
    #: are stored as strings/Decimals in JSONB, never as floats, and are
    #: retained only as long as the geofencing policy requires.
    check_in_location = models.JSONField(default=dict, blank=True)
    check_out_location = models.JSONField(default=dict, blank=True)
    notes = models.CharField(max_length=500, blank=True)
    #: Manual entries and overtime must be countersigned; the payroll engine
    #: only counts overtime from approved rows.
    approved_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_attendance_record"
        ordering = ["-work_date"]
        constraints = [
            # See the class docstring: this is what stops a double check-in
            # creating two payable days.
            models.UniqueConstraint(
                fields=["tenant", "employee", "work_date"], name="uq_hr_attendance_day"
            ),
            models.CheckConstraint(
                condition=models.Q(check_out_at__isnull=True)
                | models.Q(check_in_at__isnull=True)
                | models.Q(check_out_at__gte=models.F("check_in_at")),
                name="ck_hr_attendance_time_order",
            ),
            models.CheckConstraint(
                condition=models.Q(worked_hours__gte=0)
                & models.Q(worked_hours__lte=24)
                & models.Q(overtime_hours__gte=0),
                name="ck_hr_attendance_hours_range",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            # The payroll engine's hot query: one employee, one period.
            models.Index(
                fields=["tenant", "employee", "work_date"], name="ix_hr_att_emp_date"
            ),
            models.Index(fields=["tenant", "work_date"], name="ix_hr_att_date"),
            models.Index(fields=["tenant", "status"], name="ix_hr_att_status"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.employee_id} {self.work_date} {self.status}"


class AttendanceBreak(TenantScopedModel):
    """A paused interval inside one attendance day.

    Exists so that "left the building at 12:30, came back at 13:15" does not
    have to be expressed as a second :class:`AttendanceRecord` — which the
    one-record-per-day unique constraint forbids, and which would double-count
    the day for payroll. Unpaid break minutes are subtracted from
    ``worked_hours`` when the day is closed out.
    """

    class Kind(models.TextChoices):
        MEAL = "meal", "Meal"
        PRAYER = "prayer", "Prayer"
        REST = "rest", "Rest"
        PERSONAL = "personal", "Personal"
        OTHER = "other", "Other"

    #: CASCADE: a break has no meaning without its parent day.
    attendance = models.ForeignKey(
        AttendanceRecord, on_delete=models.CASCADE, related_name="breaks"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OTHER)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(default=0)
    is_paid = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_attendance_break"
        ordering = ["started_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ended_at__isnull=True)
                | models.Q(ended_at__gte=models.F("started_at")),
                name="ck_hr_break_time_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "attendance"], name="ix_hr_break_parent"),
        ]


# ---------------------------------------------------------------------------
# Leave
# ---------------------------------------------------------------------------

class LeaveType(TenantScopedModel):
    """A category of leave and the policy that governs it.

    Every field here is a rule the leave service reads at runtime rather than
    a branch in code: labour law differs per country and per tenant, and
    encoding "maternity leave is 90 days for female employees with 10 months
    of service" as an ``if`` makes the product unsellable in the next country.

    ``affects_payroll`` distinguishes leave that changes pay (unpaid leave
    reduces paid days) from leave that does not (annual leave is fully paid
    and payroll-neutral). ``deduction_account`` names the GL account that an
    unpaid-leave deduction is credited to when one is produced.
    """

    class AccrualMethod(models.TextChoices):
        NONE = "none", "No accrual (fixed entitlement)"
        MONTHLY = "monthly", "Accrues monthly"
        ANNUAL = "annual", "Granted annually"
        PER_HOUR_WORKED = "per_hour_worked", "Accrues per hour worked"

    class GenderRestriction(models.TextChoices):
        NONE = "none", "Any"
        MALE = "male", "Male only"
        FEMALE = "female", "Female only"

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    is_paid = models.BooleanField(default=True)

    accrual_method = models.CharField(
        max_length=16, choices=AccrualMethod.choices, default=AccrualMethod.ANNUAL
    )
    #: Days added per accrual event (per month for MONTHLY, per year for
    #: ANNUAL, per hour worked for PER_HOUR_WORKED — hence a Quantity, and
    #: hence fractional: 1.75 days/month is 21 days/year.
    accrual_rate_days = QuantityField(default=ZERO)
    #: Cap on the balance. 0 = uncapped. Prevents an untaken-leave liability
    #: growing without bound on the balance sheet.
    max_balance_days = QuantityField(default=ZERO)
    allow_negative_balance = models.BooleanField(
        default=False, help_text="Permit advancing leave the employee has not yet accrued."
    )
    carry_over_limit_days = QuantityField(default=ZERO)
    #: e.g. sick leave over 2 consecutive days needs a medical certificate.
    #: 0 = never required.
    requires_attachment_after_days = models.PositiveSmallIntegerField(default=0)
    gender_restriction = models.CharField(
        max_length=8, choices=GenderRestriction.choices, default=GenderRestriction.NONE
    )
    min_service_months = models.PositiveSmallIntegerField(default=0)
    #: Minimum notice the employee must give. Enforced at submit time.
    min_notice_days = models.PositiveSmallIntegerField(default=0)
    affects_payroll = models.BooleanField(
        default=False, help_text="True when days taken change gross pay (unpaid leave)."
    )
    deduction_account = models.ForeignKey(
        "accounting.Account",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="hr_leave_types",
    )
    requires_hr_approval = models.BooleanField(
        default=True, help_text="Second approval step after the direct manager."
    )
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_leave_type"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_hr_leave_type_code"),
            models.CheckConstraint(
                condition=models.Q(accrual_rate_days__gte=0)
                & models.Q(max_balance_days__gte=0)
                & models.Q(carry_over_limit_days__gte=0),
                name="ck_hr_leave_type_days_non_negative",
            ),
            # Unpaid leave that does not affect payroll is a contradiction that
            # would silently pay people for unpaid days.
            models.CheckConstraint(
                condition=models.Q(is_paid=True) | models.Q(affects_payroll=True),
                name="ck_hr_leave_type_unpaid_affects_payroll",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_hr_leave_type_active"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class LeaveBalance(TenantScopedModel):
    """An employee's entitlement ledger for one leave type in one year.

    Why ``available_days`` is stored rather than computed on read
    ------------------------------------------------------------
    The obvious design is to derive availability with
    ``SUM(accrued) - SUM(taken)`` over the request history. It fails for three
    reasons:

    1. **Races.** Two managers approving two requests concurrently both read
       "5 days available", both approve 4 days, and the employee ends up 3
       days overdrawn. A derived value cannot be locked; a stored one can.
       Every mutation takes ``SELECT ... FOR UPDATE`` on this row, which
       serialises the approvals and makes the second one fail correctly.
    2. **Cost.** Availability is displayed on every leave screen and checked
       on every submission. Re-aggregating an employee's whole history for a
       widget is work repeated thousands of times a day.
    3. **Auditability.** Opening balance, accrual, carry-over and manual
       adjustment are distinct business events with different approvers. A
       derived total cannot answer "why is it 12.5 and not 14".

    The stored value is maintained transactionally alongside the events that
    change it, and a nightly reconciliation task recomputes it from source to
    prove the two agree.

    Invariant: ``available = opening + accrued + carried_over + adjusted - taken``.
    It is asserted in the service layer; it is not a CHECK constraint only
    because ``adjusted_days`` is legitimately signed.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="leave_balances"
    )
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.PROTECT, related_name="balances"
    )
    year = models.PositiveSmallIntegerField(db_index=True)

    opening_days = QuantityField(default=ZERO)
    accrued_days = QuantityField(default=ZERO)
    taken_days = QuantityField(default=ZERO)
    carried_over_days = QuantityField(default=ZERO)
    #: Signed: HR may add days (goodwill) or remove them (correction).
    adjusted_days = QuantityField(default=ZERO)
    available_days = QuantityField(default=ZERO)
    #: Days on submitted-but-not-yet-approved requests. Held back so an
    #: employee cannot spend the same balance twice while approval is pending.
    pending_days = QuantityField(default=ZERO)
    last_accrued_on = models.DateField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_leave_balance"
        ordering = ["-year"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "employee", "leave_type", "year"],
                name="uq_hr_leave_balance_year",
            ),
            models.CheckConstraint(
                condition=models.Q(accrued_days__gte=0)
                & models.Q(taken_days__gte=0)
                & models.Q(carried_over_days__gte=0)
                & models.Q(pending_days__gte=0),
                name="ck_hr_leave_balance_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "employee", "year"], name="ix_hr_leave_bal_emp"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.employee_id} {self.leave_type_id} {self.year}: {self.available_days}"

    def recomputed_available(self):
        """The invariant, as a single expression, for the reconciliation job."""
        return (
            self.opening_days
            + self.accrued_days
            + self.carried_over_days
            + self.adjusted_days
            - self.taken_days
        )


class LeaveRequest(TenantScopedModel):
    """An employee's application to be absent, and its approval state.

    Overlap prevention
    ------------------
    Two approved leave requests must not cover the same day for the same
    employee: the payroll engine would deduct the day twice and the balance
    would be double-charged. A ``UniqueConstraint`` cannot express this — it
    compares equality of columns, and "these two date ranges intersect" is not
    an equality. PostgreSQL *can* express it, with an exclusion constraint,
    which is added in the raw migration ``hr/0004_leave_overlap_exclusion``::

        CREATE EXTENSION IF NOT EXISTS btree_gist;

        ALTER TABLE hr_leave_request
            ADD CONSTRAINT ex_hr_leave_request_no_overlap
            EXCLUDE USING gist (
                tenant_id   WITH =,
                employee_id WITH =,
                daterange(start_date, end_date, '[]') WITH &&
            )
            WHERE (status IN ('submitted', 'pending_manager', 'pending_hr', 'approved'));

    Notes on that SQL:

    * ``btree_gist`` is required because ``tenant_id`` and ``employee_id`` are
      scalars (uuid) and gist has no native equality operator class for them.
    * ``'[]'`` makes the range inclusive of both endpoints — a one-day leave
      is ``[D, D]`` and must still collide with itself.
    * The ``WHERE`` clause is what makes the constraint usable: cancelled and
      rejected requests, and drafts being edited, are free to overlap. Only
      live ones are exclusive.
    * The constraint is enforced by the database, so a Celery worker, a data
      import or a future service cannot bypass it. The application still
      checks first, to produce a readable error instead of an IntegrityError.

    Half days
    ---------
    ``half_day_start`` / ``half_day_end`` deduct 0.5 from ``total_days`` at
    each end of the range. They intentionally do not carry a time: which half
    of the day is an operational detail between employee and manager, and
    modelling it as a time range would demand a shift-aware calculation for
    zero payroll benefit.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        PENDING_MANAGER = "pending_manager", "Pending manager approval"
        PENDING_HR = "pending_hr", "Pending HR approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    #: APPROVED -> CANCELLED is permitted on purpose: plans change, and the
    #: cancellation path is what returns the days to the balance. It is *not*
    #: a deletion — the row and its approval trail survive.
    #: REJECTED and CANCELLED are terminal; re-applying creates a new request
    #: so that the history shows both attempts.
    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.SUBMITTED, Status.CANCELLED},
        Status.SUBMITTED: {Status.PENDING_MANAGER, Status.PENDING_HR,
                           Status.APPROVED, Status.REJECTED, Status.CANCELLED},
        Status.PENDING_MANAGER: {Status.PENDING_HR, Status.APPROVED,
                                 Status.REJECTED, Status.CANCELLED},
        Status.PENDING_HR: {Status.APPROVED, Status.REJECTED, Status.CANCELLED},
        Status.APPROVED: {Status.CANCELLED},
        Status.REJECTED: set(),
        Status.CANCELLED: set(),
    }

    #: Statuses that hold days against the balance and participate in the
    #: exclusion constraint above. Kept next to the map so the two cannot
    #: drift apart unnoticed.
    BLOCKING_STATUSES: set[str] = {
        Status.SUBMITTED, Status.PENDING_MANAGER, Status.PENDING_HR, Status.APPROVED,
    }

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="leave_requests"
    )
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.PROTECT, related_name="requests"
    )
    start_date = models.DateField(db_index=True)
    end_date = models.DateField()
    half_day_start = models.BooleanField(default=False)
    half_day_end = models.BooleanField(default=False)
    #: Working days only — weekends and holidays inside the range are already
    #: excluded when this is computed, which is why it is stored: recomputing
    #: it later, after the holiday calendar has been amended, would change an
    #: already-approved request.
    total_days = QuantityField(default=ZERO)
    reason = models.CharField(max_length=500, blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    #: Who the request is currently waiting on. Denormalised from the approval
    #: chain so "my pending approvals" is an indexed lookup, not a walk of the
    #: org chart per request.
    current_approver = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="pending_leave_requests",
    )
    attachment_key = models.CharField(max_length=255, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=500, blank=True)
    #: Set when the days have been debited from the balance, so a retry of the
    #: approval path cannot debit twice.
    balance_applied = models.BooleanField(default=False)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_leave_request"
        ordering = ["-start_date"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="ck_hr_leave_request_date_order",
            ),
            # A zero-day leave request is either a bug in the day calculation
            # (the whole range fell on weekends) or an attempt to game the
            # approval workflow. Both should fail loudly.
            models.CheckConstraint(
                condition=models.Q(total_days__gt=0),
                name="ck_hr_leave_request_days_positive",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="rejected")
                | ~models.Q(rejection_reason=""),
                name="ck_hr_leave_request_rejection_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status"], name="ix_hr_leave_req_status"),
            # Payroll: approved unpaid leave for one employee inside a period.
            models.Index(
                fields=["tenant", "employee", "start_date"], name="ix_hr_leave_req_emp"
            ),
            models.Index(
                fields=["tenant", "current_approver", "status"],
                name="ix_hr_leave_req_approver",
            ),
            models.Index(
                fields=["tenant", "start_date", "end_date"], name="ix_hr_leave_req_range"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.employee_id} {self.start_date}..{self.end_date} ({self.status})"

    def assert_can_transition(self, new_status: str) -> None:
        """The only sanctioned way to change ``status``.

        Views and serializers never assign ``.status`` directly; every path
        goes through ``apps.hr.services.leave``, which calls this first. That
        is what keeps "approved after being cancelled" from being reachable.
        """
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal leave request transition {self.status} -> {new_status}."
            )

    @property
    def is_blocking(self) -> bool:
        return self.status in self.BLOCKING_STATUSES


class LeaveApproval(TenantScopedModel):
    """One step in a leave request's approval chain — the audit trail.

    Kept as rows rather than as a status history blob because the questions
    asked of it are relational: "how long does HR take to decide", "which
    manager approved this", "was the segregation-of-duties rule respected".

    Rows are append-only. A manager who changes their mind does not edit their
    approval; the request is cancelled and re-submitted, and both decisions
    remain visible.
    """

    class Decision(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SKIPPED = "skipped", "Skipped (no approver at this level)"
        DELEGATED = "delegated", "Delegated"

    request = models.ForeignKey(
        LeaveRequest, on_delete=models.CASCADE, related_name="approvals"
    )
    #: 1 = direct manager, 2 = HR, 3+ = escalations. Explicit rather than
    #: implied by created_at so that a re-ordered chain stays reconstructable.
    step_order = models.PositiveSmallIntegerField(default=1)
    approver = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="leave_approvals",
    )
    decision = models.CharField(
        max_length=10, choices=Decision.choices, default=Decision.PENDING, db_index=True
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    comment = models.CharField(max_length=500, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_leave_approval"
        ordering = ["request", "step_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "step_order"], name="uq_hr_leave_approval_step"
            ),
            models.CheckConstraint(
                condition=models.Q(decision="pending")
                | models.Q(decided_at__isnull=False),
                name="ck_hr_leave_approval_decided_at",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "approver", "decision"], name="ix_hr_appr_actor"),
        ]


# ---------------------------------------------------------------------------
# Shift assignment
# ---------------------------------------------------------------------------

class ShiftAssignment(TenantScopedModel):
    """Which shift an employee works, over a date range.

    ``WorkSchedule.default_shift`` already answers "what shift does this
    employee normally work". This answers the question that actually drives
    payroll: "what shift were they on *that day*". A night-shift rotation, a
    two-week cover for a colleague on leave, a temporary move to another site
    — none of them are the employee's default, and without a dated assignment
    the only record of them is a manager's memory.

    That matters here rather than only in HR because overtime is priced
    against the scheduled shift: hours beyond a shift that ended at 17:00 and
    hours beyond one that ended at 02:00 are different amounts of money.

    Open-ended by design (``end_date`` NULL = current). Rotations are usually
    written before anyone knows when they end, and forcing a placeholder date
    means either a fake far-future value or a row nobody creates.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="shift_assignments"
    )
    shift = models.ForeignKey(
        Shift, on_delete=models.PROTECT, related_name="assignments"
    )
    #: Free text rather than a Location model: this codebase has no sites
    #: table, and inventing one to hold a string would be a migration and a
    #: CRUD screen for data nothing else reads.
    location = models.CharField(max_length=120, blank=True)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_shift_assignment"
        ordering = ["-start_date"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="ck_hr_shift_assignment_dates",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "employee", "-start_date"],
                         name="ix_hr_shiftasg_emp"),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.employee} → {self.shift} from {self.start_date}"

    def covers(self, on_date) -> bool:
        """Is this assignment in force on ``on_date``?"""
        if on_date < self.start_date:
            return False
        return self.end_date is None or on_date <= self.end_date


# ---------------------------------------------------------------------------
# Overtime
# ---------------------------------------------------------------------------

class OvertimeType(TenantScopedModel):
    """A category of overtime and what it multiplies the hourly rate by.

    Separate from ``payroll.PayrollComponent`` because they answer different
    questions. A component says "this money is an allowance, it is taxable,
    and it posts to account 6110". A type says "Friday work is paid at 2×".
    One overtime *component* on the payslip is fed by several types, and the
    multiplier belongs to the type — otherwise a company with weekday, weekend
    and public-holiday rates needs three components and three GL accounts to
    express one concept.

    ``component`` links the type to the payroll component whose accounts the
    money lands in, so the GL treatment stays defined in one place.
    """

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    #: Applied to the employee's derived hourly rate. 1.5 = time and a half.
    #: A RateField, not a float: a 1.5 that is really 1.4999999 understates
    #: every overtime payment in the company by a fraction of a currency unit,
    #: and payroll is the one place those fractions are noticed.
    multiplier = RateField(default=1)
    #: The payroll component this overtime is paid through. Optional so a type
    #: can be coded before the component exists, but ``calculate_overtime``
    #: refuses to price a slip whose type has none — the alternative is money
    #: with no account to post to.
    component = models.ForeignKey(
        "payroll.PayrollComponent", null=True, blank=True,
        on_delete=models.PROTECT, related_name="overtime_types",
    )
    requires_approval = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_overtime_type"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_hr_overtime_type_code"
            ),
            models.CheckConstraint(
                condition=models.Q(multiplier__gt=0),
                name="ck_hr_overtime_multiplier_positive",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.code} ({self.multiplier}×)"


class OvertimeSlip(TenantScopedModel):
    """Hours worked beyond the shift, and what they are worth.

    Lifecycle mirrors every other claim in this codebase — ``DRAFT ->
    SUBMITTED -> APPROVED`` (or ``REJECTED``) — and for the same reason: the
    person who worked the hours must not be the person who certifies them.
    Only ``APPROVED`` slips are picked up by payroll.

    ``amount`` is stored, not derived. The employee's salary can change
    between the night they worked and the day they are paid, and a slip that
    silently re-prices itself against the new salary is a number nobody can
    reconcile back to the hours. It is computed once, by
    ``apps.hr.services.overtime.price_slip``, at approval.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        PAID = "paid", "Paid"

    #: DRAFT and SUBMITTED may still be edited; the rest are terminal for the
    #: claimant. Mirrors the transition maps on Expense and LeaveRequest so
    #: one rule ("ask the model") holds across every claim in the product.
    ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
        "draft": ("submitted",),
        "submitted": ("approved", "rejected", "draft"),
        "approved": ("paid",),
        "rejected": ("draft",),
        "paid": (),
    }

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="overtime_slips"
    )
    overtime_type = models.ForeignKey(
        OvertimeType, on_delete=models.PROTECT, related_name="slips"
    )
    work_date = models.DateField(db_index=True)
    hours = QuantityField()
    #: Snapshot of the hourly rate used, so the arithmetic on a paid slip can
    #: still be checked years later against a salary that has since changed.
    hourly_rate = MoneyField(default=ZERO)
    amount = MoneyField(default=ZERO)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    approved_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="approved_overtime_slips",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.CharField(max_length=255, blank=True)
    #: Set when a payroll run consumes the slip, so it cannot be paid twice.
    payroll_run = models.ForeignKey(
        "payroll.PayrollRun", null=True, blank=True, on_delete=models.PROTECT,
        related_name="overtime_slips",
    )
    notes = models.CharField(max_length=500, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "hr_overtime_slip"
        ordering = ["-work_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(hours__gt=0),
                name="ck_hr_overtime_hours_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0) & models.Q(hourly_rate__gte=0),
                name="ck_hr_overtime_amounts_non_negative",
            ),
            # An approved slip must say who approved it and when. Without this
            # the segregation-of-duties control is unverifiable after the fact.
            models.CheckConstraint(
                condition=~models.Q(status="approved")
                | models.Q(approved_at__isnull=False),
                name="ck_hr_overtime_approved_has_timestamp",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="rejected") | ~models.Q(rejected_reason=""),
                name="ck_hr_overtime_rejected_has_reason",
            ),
            # One slip per employee per type per day: two rows for the same
            # night is the shape a double payment takes.
            models.UniqueConstraint(
                fields=["tenant", "employee", "work_date", "overtime_type"],
                name="uq_hr_overtime_slip_day",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "employee", "-work_date"],
                         name="ix_hr_ot_emp_date"),
            models.Index(fields=["tenant", "status"], name="ix_hr_ot_status"),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.employee} {self.work_date} {self.hours}h"

    def assert_can_transition(self, new_status: str) -> None:
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, ())
        if new_status not in allowed:
            raise ValueError(
                f"An overtime slip cannot move from "
                f"{self.get_status_display().lower()} to {new_status}. "
                f"Allowed: {', '.join(allowed) or 'nothing — it is terminal'}."
            )
