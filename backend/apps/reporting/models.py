"""
Reporting: saved report configurations, delivery schedules, frozen evidence
and the data-driven layout that turns a chart of accounts into a statement.

Nothing in this module computes a number. Computation lives in
``apps/reporting/generators/``; this file holds only the *configuration* and
the *evidence*:

``ReportDefinition``
    What a tenant has decided a report means for them — which type, which
    default parameters, who owns it, whether colleagues may run it.

``ReportSchedule``
    When it is delivered and to whom. Separate from the definition because
    one definition is legitimately delivered on three different cadences to
    three different audiences, and because a broken mail recipient must not
    force an edit to the report itself.

``ReportSnapshot``
    A frozen rendering. See its docstring: this is the only row in the system
    that records *what was reported*, as opposed to *what is true now*.

``AccountGrouping`` / ``ReportLineMapping``
    The presentation layer of the P&L and balance sheet, expressed as data
    rather than as a Python dict per country. An Egyptian tenant, a Saudi
    tenant and a UK tenant all want "Revenue", "Cost of Sales" and
    "Operating Expenses" — but their account codes for those lines share
    nothing, and hard-coding one country's ranges into the generator makes
    the second country a fork rather than a configuration.

All amounts referenced here are Decimal (``numeric``) end to end; the JSONB
payload of a snapshot stores every amount as a *string*, never a JSON number,
for the reason spelled out in
``apps.reporting.generators.base.ReportResult.to_dict``.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.core.models import ImmutableFinancialModel, TenantScopedModel


# ---------------------------------------------------------------------------
# Shared choice sets
# ---------------------------------------------------------------------------

class ReportType(models.TextChoices):
    """Every report this engine knows how to produce.

    Defined once, here, and imported by the generator registry. A generator
    registers itself under one of these values, so a typo becomes an
    ``ImproperlyConfigured``-style failure at import time rather than an empty
    report at month end.
    """

    PROFIT_LOSS = "profit_loss", "Profit & loss"
    BALANCE_SHEET = "balance_sheet", "Balance sheet"
    TRIAL_BALANCE = "trial_balance", "Trial balance"
    CASH_FLOW = "cash_flow", "Cash flow statement"
    TAX_SUMMARY = "tax_summary", "Tax summary"
    AR_AGING = "ar_aging", "Accounts receivable aging"
    AP_AGING = "ap_aging", "Accounts payable aging"
    GENERAL_LEDGER = "general_ledger", "General ledger"
    PAYROLL_REGISTER = "payroll_register", "Payroll register"
    EXPENSE_BY_CATEGORY = "expense_by_category", "Expenses by category"
    INVENTORY_VALUATION = "inventory_valuation", "Inventory valuation"
    PROJECT_PROFITABILITY = "project_profitability", "Project profitability"


class ReportFormat(models.TextChoices):
    """Delivery encodings.

    ``CSV`` is offered because it is the only format an accountant can open
    in their own tooling without a conversion step; ``XLSX`` because it keeps
    numeric types; ``PDF`` because it is what gets attached to a filing.
    """

    PDF = "pdf", "PDF"
    XLSX = "xlsx", "Excel"
    CSV = "csv", "CSV"


class StatementSection(models.TextChoices):
    """Which financial statement a grouping belongs to.

    Stored rather than inferred from the account types it contains: a
    grouping may deliberately mix types (a "Working capital" line pulls both
    assets and liabilities), and inferring the statement from its members
    would place such a line in whichever statement happened to sort first.
    """

    PROFIT_LOSS = "profit_loss", "Profit & loss"
    BALANCE_SHEET = "balance_sheet", "Balance sheet"
    CASH_FLOW = "cash_flow", "Cash flow"


# ---------------------------------------------------------------------------
# Saved configurations
# ---------------------------------------------------------------------------

class ReportDefinition(TenantScopedModel):
    """A tenant's saved report configuration — a *named* set of parameters.

    Why this exists at all, rather than passing parameters on every request:
    the figures a business files are produced from the same parameters every
    period, and a parameter that drifts (a department filter silently dropped,
    a date range off by one day) produces a report that is wrong in a way
    nobody notices, because it still looks like a report. Naming the
    configuration makes "the management P&L" a thing that can be diffed,
    reviewed and audited instead of a thing each user reconstructs from memory.

    ``default_parameters`` is JSONB and not a set of columns because the
    parameter surface differs per ``report_type`` (an aging report has
    buckets, a P&L has a comparison period) and because adding a parameter
    must not require a migration on a table every tenant writes to.

    ``owner`` is PROTECT, like every other FK in this codebase: deleting the
    user who created the statutory P&L definition must not take the
    definition — and therefore the tenant's reporting setup — with it.
    """

    code = models.CharField(
        max_length=50,
        help_text="Stable identifier used by schedules and API callers.",
    )
    name = models.CharField(max_length=150)
    description = models.CharField(max_length=500, blank=True)
    report_type = models.CharField(
        max_length=24, choices=ReportType.choices, db_index=True
    )

    #: Parameters merged *under* whatever the caller passes at run time, so a
    #: definition supplies defaults and never overrides an explicit request.
    default_parameters = models.JSONField(default=dict, blank=True)

    #: Which presentation layout to apply. NULL = the generator's built-in
    #: fallback grouping by account type, which is correct but unlabelled.
    grouping = models.ForeignKey(
        "reporting.AccountGrouping",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="definitions",
    )

    #: False = visible only to ``owner``. Sharing is opt-in because a saved
    #: report can encode a filter that leaks a scope its viewer does not have
    #: (one department's payroll cost, say); the ABAC layer still re-checks at
    #: run time, but the default must not invite the attempt.
    is_shared = models.BooleanField(default=False)
    owner = models.ForeignKey(
        "iam.User",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="report_definitions",
    )
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "reporting_definition"
        ordering = ["report_type", "name"]
        constraints = [
            # Per tenant, never global: two customers naming their management
            # P&L "MGMT_PL" is the normal case, and a global unique index both
            # blocks onboarding and leaks the existence of other tenants.
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_report_definition_code"
            ),
            # A blank code cannot be referenced by a schedule or an API call,
            # so the row would be unreachable configuration.
            models.CheckConstraint(
                condition=~models.Q(code=""), name="ck_report_definition_code_present"
            ),
            # A private report with no owner is unreachable by anyone: the
            # visibility rule is "owner or shared", and NULL matches neither.
            models.CheckConstraint(
                condition=models.Q(is_shared=True) | models.Q(owner__isnull=False),
                name="ck_report_definition_private_has_owner",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "report_type", "is_active"], name="ix_report_def_type"
            ),
            models.Index(fields=["tenant", "owner"], name="ix_report_def_owner"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.name}"


class ReportSchedule(TenantScopedModel):
    """Unattended delivery of one :class:`ReportDefinition`.

    Kept apart from the definition for two reasons that both show up in
    production. First, the same definition is delivered on several cadences to
    several audiences (the board gets the monthly P&L, the CFO gets it weekly
    in draft); folding recipients into the definition forces a duplicate
    definition per audience, and the duplicates drift. Second, delivery fails
    for reasons that have nothing to do with the report — a bounced address, a
    stale SMTP credential — and those failures must be fixable without
    touching a configuration that has been signed off.

    ``next_run_at`` is stored rather than derived from ``frequency`` on every
    beat tick. The scheduler's hot query is "everything due now", which is an
    indexed range scan on this column; recomputing a cron expression for every
    schedule of every tenant on every tick is the same answer at many times
    the cost, and it silently re-fires anything that was late.

    ``last_run_at`` exists so a schedule that has never run is distinguishable
    from one that ran and produced nothing — the two need very different
    operator responses.
    """

    class Frequency(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        ANNUAL = "annual", "Annual"
        #: Escape hatch for cadences the fixed list cannot express
        #: ("second Tuesday"). Uses ``cron_expression``.
        CRON = "cron", "Custom cron expression"

    definition = models.ForeignKey(
        ReportDefinition, on_delete=models.PROTECT, related_name="schedules"
    )
    name = models.CharField(max_length=150)
    frequency = models.CharField(
        max_length=12, choices=Frequency.choices, default=Frequency.MONTHLY
    )
    #: Only meaningful when ``frequency == CRON``. Five-field crontab syntax,
    #: evaluated in ``timezone`` below — not in UTC. A tenant asking for a
    #: report "on the 1st" means the 1st where they are; delivering it in UTC
    #: sends the December report on 30 November for anyone east of Greenwich.
    cron_expression = models.CharField(max_length=100, blank=True)
    timezone = models.CharField(max_length=64, default="UTC")

    #: One row per address rather than a comma-joined string: a string cannot
    #: be validated per address, cannot be indexed, and turns "remove one
    #: recipient" into text surgery.
    recipients = ArrayField(
        base_field=models.EmailField(max_length=254),
        default=list,
        blank=True,
        help_text="Email addresses the rendered report is delivered to.",
    )
    format = models.CharField(
        max_length=8, choices=ReportFormat.choices, default=ReportFormat.PDF
    )
    #: Parameter overrides layered on top of the definition's defaults —
    #: typically just the relative period ("previous month").
    parameters = models.JSONField(default=dict, blank=True)

    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    #: Set when a run raises. Kept as text on the schedule so an operator sees
    #: *why* deliveries stopped without correlating log lines by hand.
    last_error = models.CharField(max_length=500, blank=True)
    consecutive_failure_count = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "reporting_schedule"
        ordering = ["next_run_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "definition", "name"], name="uq_report_schedule_name"
            ),
            # An active schedule with no recipients runs a 40-second aggregate
            # every month and throws the result away. That is invisible waste,
            # and worse, it looks like the report *was* delivered.
            models.CheckConstraint(
                condition=models.Q(is_active=False)
                | models.Q(recipients__len__gt=0),
                name="ck_report_schedule_active_has_recipient",
            ),
            # Without a next_run_at an active schedule is never selected by
            # the due query, so it is silently dormant rather than active.
            models.CheckConstraint(
                condition=models.Q(is_active=False)
                | models.Q(next_run_at__isnull=False),
                name="ck_report_schedule_active_has_next_run",
            ),
            # frequency=cron with no expression has no cadence at all.
            models.CheckConstraint(
                condition=~models.Q(frequency="cron")
                | ~models.Q(cron_expression=""),
                name="ck_report_schedule_cron_present",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            # The scheduler's only hot query: due, active, oldest first.
            models.Index(
                fields=["is_active", "next_run_at"], name="ix_report_sched_due"
            ),
            models.Index(
                fields=["tenant", "definition"], name="ix_report_sched_definition"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} ({self.get_frequency_display()})"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

class ReportSnapshot(ImmutableFinancialModel):
    """A frozen rendering of a report: the evidence of what was reported, and when.

    Why snapshots exist
    -------------------
    A report generated from the live ledger is a *view*. Run it twice and it
    may legitimately give two different answers, because the ledger underneath
    it legitimately changed: a prior-period error was found in April and
    corrected by a reversing entry dated April (the only legal correction once
    a period is closed — see ``apps.accounting.services.posting.reverse_entry``).
    The March P&L rendered in April and the March P&L rendered in May are both
    correct, and they differ.

    That is fine internally and catastrophic externally. The P&L handed to a
    bank in support of a loan covenant, or filed with a tax authority, must be
    reproducible **byte for byte** years later — not "recomputable to something
    similar". When the bank asks "is this the statement you gave us?", the only
    acceptable answer is a row that contains the exact figures, the exact
    parameters they were produced from, the moment they were produced and by
    whom, plus a checksum proving none of it has been edited since.

    So the snapshot stores the *computed result*, not a recipe for
    recomputing it. It is deliberately redundant with the ledger. That
    redundancy is the entire point: it is what makes the difference between
    the filed figure and today's figure an answerable question
    (``compare_snapshots``) instead of an argument.

    Immutability is structural, not conventional: this model extends
    ``ImmutableFinancialModel``, so ``delete()`` raises, and the constraints
    below refuse a snapshot with no checksum. Correcting a report means taking
    a *new* snapshot and explaining the difference — exactly as correcting a
    posted journal entry means a reversing entry, never an edit.

    ``definition`` is nullable because ad-hoc reports are filed too: a
    one-off P&L run for a due-diligence request is evidence even though nobody
    saved its configuration first. ``report_type`` and ``parameters`` are
    therefore duplicated here rather than joined from the definition — a
    definition that is later edited must not rewrite the description of what
    an old snapshot actually contained.
    """

    definition = models.ForeignKey(
        ReportDefinition,
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="snapshots",
    )
    #: Copied, not joined. See the class docstring.
    report_type = models.CharField(
        max_length=24, choices=ReportType.choices, db_index=True
    )
    #: The exact, fully-resolved parameters used — no relative dates, no
    #: "previous month". A snapshot that says "last month" is not reproducible.
    parameters = models.JSONField(default=dict, blank=True)

    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    #: For as-of reports (balance sheet, aging, stock valuation) where a range
    #: is meaningless. Kept alongside the range rather than overloading it, so
    #: "as at 31 December" never has to be encoded as a zero-length period.
    as_of_date = models.DateField(null=True, blank=True)

    generated_at = models.DateTimeField(db_index=True)
    generated_by = models.ForeignKey(
        "iam.User",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="report_snapshots",
    )

    #: The computed result: ``ReportResult.to_dict()``. Every amount inside is
    #: a *string*, never a JSON number — see that method's docstring for why a
    #: float round-trip corrupts large amounts.
    payload = models.JSONField(default=dict, blank=True)
    row_count = models.PositiveIntegerField(default=0)

    #: sha256 over the canonical (sorted-key, separator-normalised) JSON of
    #: ``payload``. Proves the stored figures are the figures that were
    #: computed: an UPDATE that edits the payload without recomputing this
    #: is detectable, and recomputing it is a deliberate act, not a typo.
    checksum = models.CharField(max_length=64, db_index=True)
    #: Object-storage key for the rendered artefact (the actual PDF that was
    #: sent). Regenerable from ``payload``, but the bytes that left the
    #: building are worth keeping when a dispute is about the rendering.
    file_key = models.CharField(max_length=255, blank=True)
    file_format = models.CharField(
        max_length=8, choices=ReportFormat.choices, blank=True
    )

    #: Non-fatal findings surfaced by the generator (a cash-flow
    #: reconciliation difference, an inventory drift). Stored with the figures
    #: because "the report was produced despite a known discrepancy" is itself
    #: a fact a reader is entitled to.
    warnings = models.JSONField(default=list, blank=True)
    currency = models.CharField(max_length=3, blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "reporting_snapshot"
        ordering = ["-generated_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(period_end__isnull=True)
                | models.Q(period_start__isnull=True)
                | models.Q(period_end__gte=models.F("period_start")),
                name="ck_report_snapshot_period_order",
            ),
            # A snapshot without a checksum cannot prove anything, which makes
            # it worse than no snapshot: it looks like evidence and is not.
            models.CheckConstraint(
                condition=~models.Q(checksum=""), name="ck_report_snapshot_has_checksum"
            ),
            # Every report is bounded by *something*. A row with neither a
            # period nor an as-of date does not say what it is a report of.
            models.CheckConstraint(
                condition=models.Q(as_of_date__isnull=False)
                | models.Q(period_end__isnull=False),
                name="ck_report_snapshot_has_boundary",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            # "Show me every P&L we filed for Q1" — the auditor's query.
            models.Index(
                fields=["tenant", "report_type", "period_start", "period_end"],
                name="ix_report_snap_type_period",
            ),
            models.Index(
                fields=["tenant", "definition", "-generated_at"],
                name="ix_report_snap_definition",
            ),
            models.Index(
                fields=["tenant", "-generated_at"], name="ix_report_snap_generated"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.report_type} @ {self.generated_at:%Y-%m-%d %H:%M} ({self.checksum[:12]})"


# ---------------------------------------------------------------------------
# Data-driven presentation
# ---------------------------------------------------------------------------

class AccountGrouping(TenantScopedModel):
    """A named statement layout: the ordered set of presentation lines.

    One grouping is one way of laying out one statement — "Statutory P&L (EG)",
    "Management P&L", "IFRS balance sheet". A tenant may hold several and pick
    per report, which is the normal case rather than an edge case: the P&L a
    bank wants and the P&L the sales director wants contain the same money
    arranged differently, and forcing them to share a layout means one of them
    is always wrong.

    Why this is data and not code
    -----------------------------
    The naive implementation hard-codes ``if account.code.startswith("4")``
    into the P&L generator. That works precisely until the second country: the
    Egyptian unified chart, the Saudi SOCPA chart and a UK SME chart agree on
    nothing about which digit means revenue. The alternatives are a fork of
    the generator per jurisdiction (which then drift apart in their subtotal
    arithmetic, not just their labels) or this table.
    """

    code = models.CharField(max_length=50)
    name = models.CharField(max_length=150)
    statement = models.CharField(
        max_length=16, choices=StatementSection.choices, db_index=True
    )
    description = models.CharField(max_length=500, blank=True)
    #: Exactly one grouping per (tenant, statement) may be the default —
    #: enforced by a partial unique index below rather than by "unset the
    #: others first" application code, which loses the race between two
    #: concurrent admins and leaves two defaults that reports then pick
    #: between non-deterministically.
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "reporting_account_grouping"
        ordering = ["statement", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_report_grouping_code"
            ),
            models.UniqueConstraint(
                fields=["tenant", "statement"],
                condition=models.Q(is_default=True),
                name="uq_report_grouping_one_default",
            ),
            models.CheckConstraint(
                condition=~models.Q(code=""), name="ck_report_grouping_code_present"
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "statement", "is_active"], name="ix_report_grouping_stmt"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.name}"


class ReportLineMapping(TenantScopedModel):
    """One presentation line, and the accounts that roll into it.

    A line selects accounts by any combination of:

    * ``account_type`` — every income account, every asset account;
    * ``code_from`` / ``code_to`` — an inclusive, *string* range over
      ``Account.code``;
    * ``system_key`` — a single wired-in account by its stable role;
    * ``account`` — one explicit account.

    The selectors are ANDed. A line with none of them matches nothing, which
    is why ``ck_report_line_mapping_selector`` refuses it: an empty line
    silently contributes zero to its subtotal, and a P&L that is short by one
    category still adds up, so nothing catches it.

    Why the code range is a string comparison
    -----------------------------------------
    Account codes are *not* numbers. "4000" < "410" is false numerically and
    true lexically, and charts routinely contain "4100.01" and "4100-A".
    Comparing them as text is the only interpretation that matches how the
    codes were assigned, and it is what PostgreSQL will do against the
    ``uq_account_code`` index. Both bounds are inclusive because accountants
    describe ranges that way ("4000 to 4999"), and an exclusive upper bound is
    the classic way to lose account 4999 from revenue.

    ``sign``
    --------
    ``+1`` presents the account group's natural balance as-is; ``-1`` flips it.
    This is what makes contra accounts (sales discounts, accumulated
    depreciation, owner's drawings) print as deductions inside the section
    they belong to, instead of appearing as a positive number that inflates
    the very total they reduce. It is a presentation sign only — it never
    touches the ledger, where direction is always debit vs credit.

    ``is_subtotal``
    ---------------
    A line that shows the running total of the lines before it (Gross profit,
    Operating profit) rather than selecting accounts of its own. Modelled as a
    row so the subtotal's *position* is part of the tenant's layout: whether
    depreciation sits above or below "Operating profit" is a real accounting
    policy choice and not something the generator should decide.
    """

    grouping = models.ForeignKey(
        AccountGrouping, on_delete=models.CASCADE, related_name="lines"
    )
    label = models.CharField(max_length=150)
    #: Presentation order within the grouping. Sparse by convention (10, 20,
    #: 30) so a line can be inserted without renumbering — and therefore
    #: without an UPDATE that races another admin doing the same.
    sequence = models.PositiveSmallIntegerField(default=0)
    #: Indent level for nested presentation. Purely cosmetic; the arithmetic
    #: is driven by ``sequence`` and ``is_subtotal``.
    level = models.PositiveSmallIntegerField(default=0)

    #: Selectors — see the class docstring. Blank/NULL means "not filtered on".
    account_type = models.CharField(max_length=12, blank=True, db_index=True)
    code_from = models.CharField(max_length=20, blank=True)
    code_to = models.CharField(max_length=20, blank=True)
    system_key = models.CharField(max_length=50, blank=True)
    account = models.ForeignKey(
        "accounting.Account",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="report_line_mappings",
    )

    #: +1 or -1. See the class docstring.
    sign = models.SmallIntegerField(default=1)
    is_subtotal = models.BooleanField(default=False)
    #: For subtotal rows: the sequence at which accumulation starts. NULL
    #: means "from the top of the statement", which is what a Net profit line
    #: wants; Gross profit sets it so that it does not swallow the operating
    #: expenses printed above it in a rearranged layout.
    subtotal_from_sequence = models.PositiveSmallIntegerField(null=True, blank=True)
    is_bold = models.BooleanField(default=False)
    #: Suppress a line whose accounts all net to zero. Off by default: a
    #: missing line reads as "we do not have that category", which is a
    #: different statement from "that category was zero this period".
    hide_if_zero = models.BooleanField(default=False)

    class Meta(TenantScopedModel.Meta):
        db_table = "reporting_line_mapping"
        ordering = ["grouping", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "grouping", "sequence"],
                name="uq_report_line_mapping_seq",
            ),
            # A presentation sign is +1 or -1. Zero would silently delete the
            # line's contribution from its own subtotal.
            models.CheckConstraint(
                condition=models.Q(sign__in=[1, -1]), name="ck_report_line_mapping_sign"
            ),
            # Every non-subtotal line must select something. See docstring.
            models.CheckConstraint(
                condition=models.Q(is_subtotal=True)
                | ~models.Q(account_type="")
                | ~models.Q(code_from="")
                | ~models.Q(system_key="")
                | models.Q(account__isnull=False),
                name="ck_report_line_mapping_selector",
            ),
            # An inverted range matches nothing and looks like a typo of a
            # range that would have matched a lot.
            models.CheckConstraint(
                condition=models.Q(code_to="")
                | models.Q(code_from="")
                | models.Q(code_to__gte=models.F("code_from")),
                name="ck_report_line_mapping_code_range",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "grouping", "sequence"], name="ix_report_mapping_group"
            ),
            models.Index(
                fields=["tenant", "account_type"], name="ix_report_mapping_type"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.sequence:03d} {self.label}"

    def matches_account(self, code: str, account_type: str, system_key: str,
                        account_id) -> bool:
        """Whether one account rolls into this line.

        Implemented in Python as well as in SQL because the generator resolves
        the whole chart once and then buckets in memory: issuing one query per
        presentation line turns a 20-line P&L into 20 aggregate scans of the
        journal, which is the difference between a report that takes 200 ms
        and one that times out the request.
        """
        if self.is_subtotal:
            return False
        if self.account_id is not None:
            return account_id == self.account_id
        if self.system_key and system_key != self.system_key:
            return False
        if self.account_type and account_type != self.account_type:
            return False
        if self.code_from and code < self.code_from:
            return False
        if self.code_to and code > self.code_to:
            return False
        return bool(
            self.account_type or self.code_from or self.code_to or self.system_key
        )
