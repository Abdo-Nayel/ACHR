"""
The general ledger — the single source of financial truth.

Everything else in this system (invoices, payroll runs, stock movements) is a
*subsidiary* record whose financial effect is expressed as a journal entry.
No module writes to the ledger directly; they all call
``apps.accounting.services.posting.post_entry()``.

Core invariants, each enforced at the database level so that no code path —
ORM, raw SQL, or a future service written by someone who has not read this
file — can violate them:

1. ``SUM(debit) == SUM(credit)`` for every posted entry
   (deferred CHECK on a materialised total + trigger, see migration 0002).
2. A line is either a debit or a credit, never both, never neither.
3. Posted entries are append-only: no UPDATE of monetary columns, no DELETE.
   Corrections are made by reversing entries.
4. Nothing may be posted into a closed fiscal period.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.core.fields import MoneyField, RateField, ZERO
from apps.core.models import (
    Currency,
    ImmutableFinancialModel,
    TenantScopedModel,
    TimeStampedModel,
    UUIDModel,
)


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------

class AccountType(models.TextChoices):
    ASSET = "asset", "Asset"
    LIABILITY = "liability", "Liability"
    EQUITY = "equity", "Equity"
    INCOME = "income", "Income"
    EXPENSE = "expense", "Expense"


#: Which side increases the balance of each account type. This single mapping
#: is what turns a signed "amount" into a correct debit or credit, and is the
#: reason the posting service never asks a caller to specify a side.
NORMAL_BALANCE: dict[str, str] = {
    AccountType.ASSET: "debit",
    AccountType.EXPENSE: "debit",
    AccountType.LIABILITY: "credit",
    AccountType.EQUITY: "credit",
    AccountType.INCOME: "credit",
}


class Account(TenantScopedModel):
    """A node in the tenant's chart of accounts.

    Hierarchical (``parent``) for presentation, but **only leaf accounts may
    be posted to** (``is_postable``). Allowing postings to a parent makes the
    parent's balance ambiguous — is it its own postings, or the roll-up? —
    and every accountant who has inherited such a chart has a story about it.
    """

    code = models.CharField(max_length=20)
    name = models.CharField(max_length=150)
    type = models.CharField(max_length=12, choices=AccountType.choices, db_index=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    description = models.CharField(max_length=255, blank=True)

    #: Currency the account is denominated in. NULL = base currency of tenant.
    currency = models.CharField(
        max_length=3, choices=Currency.choices, null=True, blank=True
    )
    is_postable = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    #: System accounts (AR control, AP control, Retained Earnings, Payroll
    #: Payable...) are wired into automated postings and must not be deleted
    #: or retyped by a user. Identified by `system_key`, not by code — codes
    #: differ per country's standard chart.
    system_key = models.CharField(max_length=50, blank=True, db_index=True)
    is_reconcilable = models.BooleanField(
        default=False, help_text="Bank/cash accounts eligible for reconciliation."
    )

    #: Denormalised running balance, maintained only by the posting service
    #: inside the same transaction as the journal lines. Reports never trust
    #: it for period figures (they aggregate lines); it exists so the
    #: dashboard does not aggregate ten million rows on every page load.
    cached_balance = MoneyField()
    cached_balance_as_of = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "accounting_account"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_account_code"),
            models.UniqueConstraint(
                fields=["tenant", "system_key"],
                condition=~models.Q(system_key=""),
                name="uq_account_system_key",
            ),
            models.CheckConstraint(
                condition=~models.Q(parent=models.F("id")),
                name="ck_account_no_self_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "type", "is_active"], name="ix_account_type"),
            models.Index(
                fields=["tenant", "is_reconcilable"], name="ix_account_reconcilable"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.name}"

    @property
    def normal_balance(self) -> str:
        return NORMAL_BALANCE[self.type]

    @property
    def increases_on_debit(self) -> bool:
        return self.normal_balance == "debit"


class TaxRate(TenantScopedModel):
    """VAT / sales-tax definition, linked to the accounts the tax posts to."""

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20)
    rate = RateField(help_text="Fraction, e.g. 0.140000 for 14%.")
    is_compound = models.BooleanField(default=False)
    is_recoverable = models.BooleanField(
        default=True, help_text="Input VAT reclaimable; otherwise expensed."
    )
    collected_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="+",
        help_text="Output VAT — a liability.",
    )
    paid_account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        help_text="Input VAT — an asset when recoverable.",
    )
    is_active = models.BooleanField(default=True)
    effective_from = models.DateField(default=timezone.localdate)
    effective_to = models.DateField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "accounting_tax_rate"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code", "effective_from"], name="uq_tax_rate_code"
            ),
            models.CheckConstraint(
                condition=models.Q(rate__gte=0) & models.Q(rate__lte=1),
                name="ck_tax_rate_fraction",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gte=models.F("effective_from")),
                name="ck_tax_rate_period_order",
            ),
        ]


# ---------------------------------------------------------------------------
# Fiscal calendar
# ---------------------------------------------------------------------------

class FiscalYear(TenantScopedModel):
    """A tenant's financial year. Closing one rolls net income into equity."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    name = models.CharField(max_length=50)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "accounting_fiscal_year"
        ordering = ["-start_date"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="uq_fiscal_year_name"),
            models.CheckConstraint(
                condition=models.Q(end_date__gt=models.F("start_date")),
                name="ck_fiscal_year_date_order",
            ),
        ]


class FiscalPeriod(TenantScopedModel):
    """Usually a calendar month. The unit at which the books are locked.

    ``SOFT_CLOSED`` exists because month-end is a process, not an instant:
    operations stop posting while the accountant finishes adjustments, and
    only users holding ``accounting.period.post_to_soft_closed`` may still
    post. That state prevents the common workaround of leaving periods open
    "just in case", which is how prior-period figures silently change.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        SOFT_CLOSED = "soft_closed", "Soft closed"
        CLOSED = "closed", "Closed"

    fiscal_year = models.ForeignKey(
        FiscalYear, on_delete=models.PROTECT, related_name="periods"
    )
    name = models.CharField(max_length=50)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "accounting_fiscal_period"
        ordering = ["start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "start_date"], name="uq_period_start"
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="ck_period_date_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "start_date", "end_date"], name="ix_period_range"
            ),
        ]

    @property
    def accepts_postings(self) -> bool:
        return self.status == self.Status.OPEN


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

class Journal(TenantScopedModel):
    """A book of original entry (Sales, Purchases, Cash, Payroll, General).

    Separating journals gives each document type its own numbering sequence
    and lets the audit trail answer "show me every payroll posting in March"
    without scanning the whole ledger.
    """

    class Kind(models.TextChoices):
        SALES = "sales", "Sales"
        PURCHASE = "purchase", "Purchase"
        CASH = "cash", "Cash & bank"
        PAYROLL = "payroll", "Payroll"
        INVENTORY = "inventory", "Inventory"
        GENERAL = "general", "General"

    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=12, choices=Kind.choices, db_index=True)
    default_account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    sequence_prefix = models.CharField(max_length=10, default="JE")
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "accounting_journal"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_journal_code")
        ]


class JournalEntry(ImmutableFinancialModel):
    """A balanced set of debits and credits recorded on one date.

    Lifecycle: ``DRAFT -> POSTED -> (VOIDED | REVERSED)``.

    * **DRAFT** entries are freely editable and invisible to reports.
    * **POSTED** entries are frozen. A database trigger rejects any UPDATE
      that touches a monetary column or the account of a posted line.
    * **VOIDED** is only reachable while the period is still OPEN and the
      entry was posted in error within the same period — the entry keeps its
      number (so the sequence has no gaps, which auditors flag) but
      contributes nothing.
    * **REVERSED** creates a *new* mirror entry dated in the current open
      period. This is the only legal correction after a period closes.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"
        VOIDED = "voided", "Voided"
        REVERSED = "reversed", "Reversed"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.POSTED, Status.VOIDED},
        Status.POSTED: {Status.VOIDED, Status.REVERSED},
        Status.VOIDED: set(),
        Status.REVERSED: set(),
    }

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        INVOICE = "invoice", "Customer invoice"
        BILL = "bill", "Vendor bill"
        PAYMENT = "payment", "Payment"
        EXPENSE = "expense", "Expense"
        PAYROLL = "payroll", "Payroll run"
        INVENTORY = "inventory", "Inventory movement"
        BANK = "bank", "Bank reconciliation"
        OPENING = "opening", "Opening balance"
        CLOSING = "closing", "Year-end closing"

    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="entries")
    period = models.ForeignKey(
        FiscalPeriod, on_delete=models.PROTECT, related_name="entries"
    )
    #: Human-readable sequential number, allocated at *posting* time from a
    #: per-(tenant, journal, year) sequence. Draft entries have none, so an
    #: abandoned draft cannot burn a number and create an audit gap.
    number = models.CharField(max_length=32, blank=True)
    entry_date = models.DateField(db_index=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    source = models.CharField(
        max_length=12, choices=Source.choices, default=Source.MANUAL, db_index=True
    )
    memo = models.CharField(max_length=500, blank=True)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    #: Rate to the tenant's base currency at entry_date. 1 when they match.
    exchange_rate = RateField(default=1)

    #: Materialised control totals. Populated by the posting service and
    #: guarded by ``ck_entry_balanced`` — this is the physical embodiment of
    #: "debits must equal credits".
    total_debit = MoneyField()
    total_credit = MoneyField()

    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Set on the *original* entry when it is reversed, pointing at the mirror.
    reversal_of = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    void_reason = models.CharField(max_length=255, blank=True)

    #: Generic pointer back to the subsidiary document that produced this
    #: entry, so the UI can show "posted from INV-2026-0042".
    source_document_type = models.CharField(max_length=50, blank=True)
    source_document_id = models.UUIDField(null=True, blank=True)

    #: Guards against double-posting the same business event when a webhook
    #: or a Celery task is retried. Unique per tenant when present.
    idempotency_key = models.CharField(max_length=128, blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "accounting_journal_entry"
        ordering = ["-entry_date", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "journal", "number"],
                condition=~models.Q(number=""),
                name="uq_entry_number",
            ),
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uq_entry_idempotency",
            ),
            # The invariant. A posted entry MUST balance and MUST be non-zero;
            # a draft may be unbalanced while it is being built.
            models.CheckConstraint(
                condition=~models.Q(status="posted")
                | (
                    models.Q(total_debit=models.F("total_credit"))
                    & models.Q(total_debit__gt=0)
                ),
                name="ck_entry_balanced",
            ),
            models.CheckConstraint(
                condition=models.Q(total_debit__gte=0) & models.Q(total_credit__gte=0),
                name="ck_entry_totals_non_negative",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="posted") | models.Q(posted_at__isnull=False),
                name="ck_entry_posted_has_timestamp",
            ),
            models.CheckConstraint(
                condition=models.Q(exchange_rate__gt=0), name="ck_entry_fx_positive"
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status", "entry_date"], name="ix_entry_status"),
            models.Index(fields=["tenant", "period", "status"], name="ix_entry_period"),
            models.Index(
                fields=["source_document_type", "source_document_id"],
                name="ix_entry_source_doc",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.number or f"DRAFT {self.id}"

    @property
    def is_posted(self) -> bool:
        return self.status == self.Status.POSTED

    def assert_can_transition(self, new_status: str) -> None:
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal journal entry transition {self.status} -> {new_status}."
            )


class JournalLine(ImmutableFinancialModel):
    """One debit or credit against one account.

    ``debit`` and ``credit`` are separate non-negative columns rather than a
    single signed ``amount``. It costs one extra column and buys three
    things: the balance check is expressible as a plain SQL constraint, the
    trial balance report is a straight ``SUM``, and a sign error becomes a
    constraint violation instead of a silently reversed entry.
    """

    entry = models.ForeignKey(
        JournalEntry, on_delete=models.CASCADE, related_name="lines"
    )
    line_number = models.PositiveSmallIntegerField()
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    description = models.CharField(max_length=500, blank=True)

    debit = MoneyField()
    credit = MoneyField()
    #: Amount converted into the tenant's base currency at the entry's rate.
    #: Stored, not computed, so historical reports do not shift when a rate
    #: table is corrected.
    base_debit = MoneyField()
    base_credit = MoneyField()

    #: Analytical dimensions. All optional; all indexed because every
    #: management report slices by at least one of them.
    partner_type = models.CharField(
        max_length=20, blank=True, help_text="customer | vendor | employee"
    )
    partner_id = models.UUIDField(null=True, blank=True)
    project = models.ForeignKey(
        "projects.Project", null=True, blank=True,
        on_delete=models.PROTECT, related_name="journal_lines",
    )
    department = models.ForeignKey(
        "hr.Department", null=True, blank=True,
        on_delete=models.PROTECT, related_name="journal_lines",
    )
    tax_rate = models.ForeignKey(
        TaxRate, null=True, blank=True, on_delete=models.PROTECT, related_name="lines"
    )

    #: Set when this line has been matched to a bank statement line.
    reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "accounting_journal_line"
        ordering = ["entry", "line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["entry", "line_number"], name="uq_line_number_per_entry"
            ),
            models.CheckConstraint(
                condition=models.Q(debit__gte=0) & models.Q(credit__gte=0),
                name="ck_line_non_negative",
            ),
            # Exactly one side carries the amount. `(debit > 0) XOR (credit > 0)`
            # written in a form PostgreSQL can index and check cheaply.
            models.CheckConstraint(
                condition=(models.Q(debit__gt=0) & models.Q(credit=0))
                | (models.Q(credit__gt=0) & models.Q(debit=0)),
                name="ck_line_single_sided",
            ),
            models.CheckConstraint(
                condition=models.Q(base_debit__gte=0) & models.Q(base_credit__gte=0),
                name="ck_line_base_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "account"], name="ix_line_account"),
            models.Index(
                fields=["tenant", "partner_type", "partner_id"], name="ix_line_partner"
            ),
            models.Index(fields=["tenant", "project"], name="ix_line_project"),
            models.Index(fields=["tenant", "department"], name="ix_line_department"),
            models.Index(
                fields=["account", "reconciled_at"], name="ix_line_unreconciled"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        side = "Dr" if self.debit > ZERO else "Cr"
        return f"{side} {self.account_id} {self.debit or self.credit}"


class ExchangeRate(TenantScopedModel):
    """Daily FX rates. Stored per tenant because a group may use a corporate
    rate table that differs from the central bank's."""

    from_currency = models.CharField(max_length=3, choices=Currency.choices)
    to_currency = models.CharField(max_length=3, choices=Currency.choices)
    rate = RateField()
    rate_date = models.DateField(db_index=True)
    source = models.CharField(max_length=50, default="manual")

    class Meta(TenantScopedModel.Meta):
        db_table = "accounting_exchange_rate"
        ordering = ["-rate_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "from_currency", "to_currency", "rate_date"],
                name="uq_fx_rate_day",
            ),
            models.CheckConstraint(condition=models.Q(rate__gt=0), name="ck_fx_positive"),
            models.CheckConstraint(
                condition=~models.Q(from_currency=models.F("to_currency")),
                name="ck_fx_distinct_currencies",
            ),
        ]


# ---------------------------------------------------------------------------
# Model registration
# ---------------------------------------------------------------------------
# `DocumentSequence` lives in its own module because gapless numbering is a
# self-contained concern with a long rationale of its own. Django only
# autodiscovers `models.py`, so without this re-export the model is never added
# to the app registry, no table is created, and the failure surfaces much later
# as "relation accounting_document_sequence does not exist" on the first
# posting. Importing it here keeps the file split without paying for it.
from apps.accounting.models_sequence import DocumentSequence  # noqa: E402,F401

__all__ = [
    "Account", "AccountType", "NORMAL_BALANCE", "TaxRate", "FiscalYear",
    "FiscalPeriod", "Journal", "JournalEntry", "JournalLine", "ExchangeRate",
    "DocumentSequence",
]
