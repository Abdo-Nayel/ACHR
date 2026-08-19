"""
Accounts receivable: who owes us money, for what, and since when.

This module owns the *subsidiary* AR ledger. The general ledger only ever
sees a control-account balance ("Accounts Receivable: 412,300.00"); the
breakdown by customer, invoice and age lives here. That split is deliberate:
the GL must stay small enough to aggregate quickly, while AR needs one row
per line item for dunning, revenue recognition and tax reporting.

Nothing in this module writes ``JournalLine`` rows. Every financial effect is
expressed as a :class:`~apps.accounting.services.posting.JournalEntryDraft`
and handed to ``post_entry()`` — see ``apps/sales/services/invoice_workflow.py``.

Money invariants that the database (not the application) enforces:

* ``amount_due == total_amount - amount_paid`` on every invoice row, always.
  Keeping ``amount_due`` as a stored column and *also* constraining it is the
  compromise between "aging report must not join payments" (needs the column)
  and "the column must never drift" (needs the constraint).
* ``amount_paid <= total_amount``. Over-application is a real bug class: a
  customer overpays, someone applies the whole receipt to one invoice, and
  the AR sub-ledger no longer ties to the GL control account. The surplus
  belongs in ``payments.Payment.unapplied_amount``, not on the invoice.
"""

from __future__ import annotations

import re

from django.db import models

from apps.core.fields import MoneyField, QuantityField, RateField, ZERO
from apps.core.models import (
    Currency,
    ImmutableFinancialModel,
    TenantScopedModel,
)


# ---------------------------------------------------------------------------
# Customer master data
# ---------------------------------------------------------------------------

class Customer(TenantScopedModel):
    """A party that buys from the tenant and may owe them money.

    ``code`` is the human-facing identifier printed on statements and typed
    into search boxes. It is unique **per tenant**: two tenants both using
    "C-0001" is normal and correct; a global unique index here would leak the
    existence of other tenants' customers through constraint violations.

    ``receivable_account`` overrides the tenant's default AR control account.
    Groups with intercompany trade, or with a separate "Retentions
    receivable" account, need per-customer control accounts, and discovering
    that after go-live means re-posting history. It is ``PROTECT`` because
    deleting an account that has posted AR balances against it orphans the
    sub-ledger from the GL.

    ``billing_address`` / ``shipping_address`` are JSONB rather than a
    normalised address table: addresses are printed as a block, never queried
    field-by-field, and every country disagrees about which fields exist.
    Historical invoices snapshot their own copy — changing a customer's
    address must not retroactively rewrite last year's tax documents.
    """

    code = models.CharField(max_length=32)
    name = models.CharField(max_length=255, help_text="Legal name for tax documents.")
    display_name = models.CharField(
        max_length=255, blank=True, help_text="Trading name shown in the UI."
    )
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    #: Tax registration number (VAT / TRN / CR). Blank for unregistered
    #: consumers — many jurisdictions require a *different* invoice layout in
    #: that case, so blankness is meaningful, not merely missing data.
    tax_number = models.CharField(max_length=50, blank=True)

    billing_address = models.JSONField(default=dict, blank=True)
    shipping_address = models.JSONField(default=dict, blank=True)

    #: Net terms. 0 means due on receipt. Copied onto each invoice at issue
    #: time so that renegotiating terms does not move existing due dates.
    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    #: 0 = no credit extended (prepay only); the "unlimited" case is modelled
    #: by ``has_credit_limit=False`` rather than by a magic large number.
    credit_limit = MoneyField()
    has_credit_limit = models.BooleanField(default=False)

    currency = models.CharField(max_length=3, choices=Currency.choices)
    receivable_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="ar_customers",
        help_text="AR control account this customer's balance rolls up to.",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "sales_customer"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_customer_code"),
            models.CheckConstraint(
                condition=models.Q(credit_limit__gte=0),
                name="ck_customer_credit_limit_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_customer_active"),
            models.Index(fields=["tenant", "name"], name="ix_customer_name"),
            models.Index(fields=["tenant", "email"], name="ix_customer_email"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.display_name or self.name}"


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

class Invoice(ImmutableFinancialModel):
    """A demand for payment issued to a customer; the AR sub-ledger's unit.

    Lifecycle
    ---------
    ``DRAFT -> SENT -> PARTIALLY_PAID -> PAID``, with ``OVERDUE`` layered on
    top and ``VOIDED`` / ``WRITTEN_OFF`` as exits.

    Why OVERDUE is *derived*, not a user transition
    -----------------------------------------------
    Nothing happens on the due date. No actor acts; the world simply keeps
    turning and the invoice becomes late. Modelling that as a user-triggered
    transition would mean an invoice's status depends on whether somebody
    happened to open the screen, and two tenants in different time zones would
    disagree about whether the same invoice is late. So ``OVERDUE`` is
    computed by a scheduled job (``refresh_overdue_status``) from
    ``due_date < today AND amount_due > 0``, evaluated in the *tenant's* time
    zone, and it is written back to the column only so that list filters,
    aging buckets and dunning queries stay a single indexed predicate instead
    of a date expression the planner cannot use.

    The consequence to remember: ``OVERDUE`` is a *presentation* of lateness,
    not an accounting fact. It never changes a balance, never posts to the GL,
    and it must be reversible — extending a due date moves the invoice back to
    ``SENT`` or ``PARTIALLY_PAID``. That is why ``OVERDUE`` transitions back
    out to every unsettled state.

    Why PAID is terminal-ish, and how it un-terminates
    --------------------------------------------------
    Reaching ``PAID`` (``amount_due == 0``) ends the collection process:
    dunning stops, the invoice leaves the aging report, and revenue is
    recognised. No user action may reopen it — "mark as unpaid" is the single
    most common way people corrupt an AR ledger, because it changes the
    invoice without changing the cash that was recorded against it.

    The one legal route back is a *payment reversal*: a bounced cheque, a card
    chargeback, or a refund. Those are events on the ``payments`` side — they
    unwind a ``PaymentApplication`` and post their own reversing GL entry —
    and only as a consequence does this invoice recompute ``amount_paid`` and
    fall back to ``PARTIALLY_PAID`` (or ``SENT`` if the reversal was total,
    which ``apply_payment`` handles by recomputation rather than by asking the
    caller for a target state). Hence ``PAID -> PARTIALLY_PAID`` exists in the
    transition map but is unreachable from any UI verb.

    Invariants
    ----------
    * ``number`` is blank while ``DRAFT``. Numbers come from the gapless
      per-tenant sequence and are allocated at issue time only — an abandoned
      draft must not burn a number, because a gap in an invoice sequence is
      treated as evidence of a deleted invoice by most tax authorities.
    * ``customer`` is ``PROTECT``: a customer with posted invoices is part of
      the audit trail and cannot be deleted, only deactivated.
    * The line-level currency is pinned by this header; lines carry no
      currency of their own precisely so they cannot disagree with it.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        VOIDED = "voided", "Voided"
        WRITTEN_OFF = "written_off", "Written off"

    #: Explicit map, mirroring accounting.JournalEntry. Every edge here is a
    #: business decision, not an accident of code layout:
    #:  * DRAFT may be voided (nothing was posted) but never written off.
    #:  * SENT/OVERDUE/PARTIALLY_PAID may be written off — bad debt is a real
    #:    posting (Dr Bad Debt Expense / Cr AR), unlike a void.
    #:  * PARTIALLY_PAID may NOT be voided: cash has already been received
    #:    against it. The correction is a credit note plus a refund.
    #:  * PAID -> PARTIALLY_PAID exists only for payment reversal (see above).
    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.SENT, Status.VOIDED},
        Status.SENT: {
            Status.PARTIALLY_PAID,
            Status.PAID,
            Status.OVERDUE,
            Status.VOIDED,
            Status.WRITTEN_OFF,
        },
        Status.PARTIALLY_PAID: {
            Status.PAID,
            Status.OVERDUE,
            Status.SENT,
            Status.WRITTEN_OFF,
        },
        Status.OVERDUE: {
            Status.SENT,
            Status.PARTIALLY_PAID,
            Status.PAID,
            Status.WRITTEN_OFF,
            Status.VOIDED,
        },
        # Terminal-ish: only a payment reversal reopens a paid invoice.
        Status.PAID: {Status.PARTIALLY_PAID, Status.SENT},
        Status.VOIDED: set(),
        Status.WRITTEN_OFF: set(),
    }

    #: The customer's own reference for this sale — their purchase order
    #: number, usually. Free text and not unique: it is *their* identifier,
    #: two customers may legitimately use the same one, and validating it
    #: against a format we do not control would reject correct data.
    order_number = models.CharField(max_length=64, blank=True, db_index=True)

    #: One line describing what the invoice is for, printed above the items.
    #: Distinct from ``notes`` (which prints at the foot) and from a line
    #: description (which prices something).
    subject = models.CharField(max_length=255, blank=True)

    #: Who sold it. An Employee rather than a User: commission and sales
    #: reporting are about people on the payroll, and a salesperson who has
    #: left keeps their name on the invoices they raised after their login is
    #: gone. PROTECT for the same reason — archiving the employee must not
    #: rewrite what a filed invoice says.
    salesperson = models.ForeignKey(
        "hr.Employee", null=True, blank=True, on_delete=models.PROTECT,
        related_name="invoices_sold",
    )

    #: Blank until issued — see the docstring on gapless numbering.
    number = models.CharField(max_length=32, blank=True)
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="invoices"
    )

    issue_date = models.DateField(db_index=True)
    due_date = models.DateField(db_index=True)

    currency = models.CharField(max_length=3, choices=Currency.choices)
    #: Rate to the tenant's base currency, frozen at issue date. Stored so a
    #: later correction to the FX table cannot silently restate filed revenue.
    exchange_rate = RateField(default=1)

    subtotal_amount = MoneyField(help_text="Sum of line subtotals, before tax.")
    discount_amount = MoneyField(
        help_text="Header-level discount, deducted from subtotal_amount."
    )
    tax_amount = MoneyField()
    total_amount = MoneyField()
    amount_paid = MoneyField()
    #: Denormalised open balance. Constrained to equal total - paid so it can
    #: never drift; exists because the aging report reads it on every row.
    amount_due = MoneyField()

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True
    )

    #: NULL while DRAFT. One invoice produces exactly one issuing entry;
    #: payments and write-offs post their own separate entries.
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoice",
    )

    notes = models.TextField(blank=True, help_text="Shown to the customer.")
    terms = models.TextField(blank=True, help_text="Payment terms boilerplate.")

    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoices",
    )
    recurring_profile = models.ForeignKey(
        "sales.RecurringInvoiceProfile",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generated_invoices",
        help_text="Set when this invoice was generated from a schedule.",
    )

    sent_at = models.DateTimeField(null=True, blank=True)
    #: First time the customer opened the hosted invoice link. Weak evidence,
    #: but the only evidence available in a "we never received it" dispute.
    viewed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=255, blank=True)
    written_off_at = models.DateTimeField(null=True, blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "sales_invoice"
        ordering = ["-issue_date", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"],
                condition=~models.Q(number=""),
                name="uq_invoice_number",
            ),
            # The AR identity. Written as a stored column *and* a constraint so
            # that a bad UPDATE fails loudly instead of quietly de-syncing the
            # aging report from the ledger.
            models.CheckConstraint(
                condition=models.Q(
                    amount_due=models.F("total_amount") - models.F("amount_paid")
                ),
                name="ck_invoice_due_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(subtotal_amount__gte=0)
                & models.Q(discount_amount__gte=0)
                & models.Q(tax_amount__gte=0)
                & models.Q(total_amount__gte=0)
                & models.Q(amount_paid__gte=0),
                name="ck_invoice_amounts_non_negative",
            ),
            # total = subtotal - discount + tax. The same arithmetic the GL
            # posting relies on to balance, asserted where it cannot drift.
            models.CheckConstraint(
                condition=models.Q(
                    total_amount=models.F("subtotal_amount")
                    - models.F("discount_amount")
                    + models.F("tax_amount")
                ),
                name="ck_invoice_total_identity",
            ),
            # Surplus cash belongs on the payment, never on the invoice.
            models.CheckConstraint(
                condition=models.Q(amount_paid__lte=models.F("total_amount")),
                name="ck_invoice_no_overpayment",
            ),
            models.CheckConstraint(
                condition=models.Q(due_date__gte=models.F("issue_date")),
                name="ck_invoice_due_after_issue",
            ),
            models.CheckConstraint(
                condition=models.Q(exchange_rate__gt=0), name="ck_invoice_fx_positive"
            ),
            # A draft has no number; anything issued must have one.
            models.CheckConstraint(
                condition=models.Q(status="draft") | ~models.Q(number=""),
                name="ck_invoice_issued_has_number",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="voided") | ~models.Q(void_reason=""),
                name="ck_invoice_void_has_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status", "due_date"], name="ix_invoice_status"),
            models.Index(fields=["tenant", "customer", "status"], name="ix_invoice_customer"),
            models.Index(fields=["tenant", "issue_date"], name="ix_invoice_issue_date"),
            models.Index(fields=["tenant", "project"], name="ix_invoice_project"),
            # Drives the dunning and aging queries: open balances only.
            models.Index(
                fields=["tenant", "due_date"],
                condition=models.Q(amount_due__gt=0),
                name="ix_invoice_open_balance",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.number or f"DRAFT invoice {self.id}"

    @property
    def is_settled(self) -> bool:
        return self.amount_due <= ZERO

    @property
    def is_open(self) -> bool:
        """Still contributes to the AR control account balance."""
        return self.status in {
            self.Status.SENT,
            self.Status.PARTIALLY_PAID,
            self.Status.OVERDUE,
        }

    def assert_can_transition(self, new_status: str) -> None:
        """Reject illegal lifecycle moves before any row is touched.

        Mirrors ``accounting.JournalEntry.assert_can_transition`` on purpose:
        one shape of state machine across the codebase means a reviewer can
        audit every document type the same way.
        """
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal invoice transition {self.status} -> {new_status} "
                f"on invoice {self.number or self.id}."
            )

    def build_journal_entry(self):
        """Delegate to the workflow service (convention §7).

        Imported lazily: the service imports the posting engine, which imports
        accounting models, and a module-level import here would close the
        cycle at Django app-loading time.
        """
        from apps.sales.services.invoice_workflow import build_invoice_entry

        return build_invoice_entry(self)


class InvoiceLine(TenantScopedModel):
    """One billable item on an invoice.

    ``CASCADE`` from the invoice is the one place the convention permits it:
    a line has no meaning without its header, and a *draft* invoice may be
    freely rebuilt. Once the invoice is posted, the parent's ``delete()``
    raises, so the cascade is unreachable in practice — it exists to keep
    draft editing simple, not to permit destroying posted history.

    No ``currency`` column: the header pins it. A line that could carry its
    own currency is a line that can disagree with its total.

    The time-to-cash link is **not** a column here. It lives on
    ``projects.TimesheetEntry.invoice_line`` (a ``OneToOneField`` back to this
    model with ``related_name="timesheet_entry"``), so ``line.timesheet_entry``
    still reads naturally while there is only ever *one* column expressing the
    relationship. Two columns for one relationship is two sources of truth,
    and the day they disagree is the day an hour gets billed twice — the most
    damaging billing error a services business can make. The uniqueness and
    the "invoiced implies linked" guarantee are enforced on the timesheet side,
    where the status that must stay consistent with them also lives.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    line_number = models.PositiveSmallIntegerField()

    item = models.ForeignKey(
        "inventory.Item",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoice_lines",
        help_text="NULL for free-text service lines.",
    )
    description = models.CharField(max_length=500)

    quantity = QuantityField(default=1)
    unit_price = MoneyField()
    #: Fraction, e.g. 0.100000 for 10%. Stored as a rate rather than an amount
    #: so the printed invoice can show "10% off" the way the customer agreed.
    discount_rate = RateField()

    tax_rate = models.ForeignKey(
        "accounting.TaxRate",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoice_lines",
        help_text="NULL for out-of-scope / exempt lines.",
    )

    #: Materialised per line: quantity * unit_price * (1 - discount_rate),
    #: rounded to the currency's minor unit exactly once. Recomputing this at
    #: read time reproduces rounding differently in Python, SQL and the PDF
    #: renderer, and the three disagree by a cent on ~1 invoice in 300.
    line_subtotal = MoneyField()
    line_tax = MoneyField()
    line_total = MoneyField()

    income_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="invoice_lines",
        help_text="Revenue account this line credits.",
    )
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoice_lines",
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "sales_invoice_line"
        ordering = ["invoice", "line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "line_number"], name="uq_invoice_line_number"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0) & models.Q(unit_price__gte=0),
                name="ck_invoice_line_qty_price_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_rate__gte=0)
                & models.Q(discount_rate__lte=1),
                name="ck_invoice_line_discount_fraction",
            ),
            models.CheckConstraint(
                condition=models.Q(line_subtotal__gte=0)
                & models.Q(line_tax__gte=0)
                & models.Q(line_total__gte=0),
                name="ck_invoice_line_amounts_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["invoice", "line_number"], name="ix_invoice_line_parent"),
            models.Index(fields=["tenant", "item"], name="ix_invoice_line_item"),
            models.Index(fields=["tenant", "project"], name="ix_invoice_line_project"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.invoice_id} #{self.line_number} {self.description[:40]}"


# ---------------------------------------------------------------------------
# Credit notes
# ---------------------------------------------------------------------------

class CreditNote(ImmutableFinancialModel):
    """A negative invoice: goods returned, over-billing corrected, discount
    granted after the fact.

    A credit note exists instead of editing the original invoice because the
    original has already been filed with a tax authority and sent to a
    customer. Both documents must survive, and the *pair* must reconcile.

    ``invoice`` is nullable: a general credit ("goodwill, 500 off your next
    order") is issued against the customer, not against a specific document,
    and is later applied like a payment. When it *is* tied to an invoice, it
    reduces that invoice's ``amount_due`` through the same application path
    payments use, which keeps one code path for "something reduced this
    balance".
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        APPLIED = "applied", "Fully applied"
        VOIDED = "voided", "Voided"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.ISSUED, Status.VOIDED},
        # Un-applying (a return that itself is reversed) walks back to ISSUED.
        Status.ISSUED: {Status.APPLIED, Status.VOIDED},
        Status.APPLIED: {Status.ISSUED},
        Status.VOIDED: set(),
    }

    number = models.CharField(max_length=32, blank=True)
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="credit_notes"
    )
    invoice = models.ForeignKey(
        Invoice,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_notes",
    )

    issue_date = models.DateField(db_index=True)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    exchange_rate = RateField(default=1)

    subtotal_amount = MoneyField()
    tax_amount = MoneyField()
    total_amount = MoneyField()
    #: How much of this credit has been consumed against invoices.
    amount_applied = MoneyField()
    amount_remaining = MoneyField()

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    reason = models.CharField(max_length=255, blank=True)
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_note",
    )
    notes = models.TextField(blank=True)
    void_reason = models.CharField(max_length=255, blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "sales_credit_note"
        ordering = ["-issue_date", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"],
                condition=~models.Q(number=""),
                name="uq_credit_note_number",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    amount_remaining=models.F("total_amount") - models.F("amount_applied")
                ),
                name="ck_credit_note_remaining_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(subtotal_amount__gte=0)
                & models.Q(tax_amount__gte=0)
                & models.Q(total_amount__gte=0)
                & models.Q(amount_applied__gte=0),
                name="ck_credit_note_amounts_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_applied__lte=models.F("total_amount")),
                name="ck_credit_note_no_over_application",
            ),
            models.CheckConstraint(
                condition=models.Q(status="draft") | ~models.Q(number=""),
                name="ck_credit_note_issued_has_number",
            ),
            models.CheckConstraint(
                condition=models.Q(exchange_rate__gt=0),
                name="ck_credit_note_fx_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status", "issue_date"], name="ix_cn_status"),
            models.Index(fields=["tenant", "customer"], name="ix_cn_customer"),
            models.Index(fields=["tenant", "invoice"], name="ix_cn_invoice"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.number or f"DRAFT credit note {self.id}"

    def assert_can_transition(self, new_status: str) -> None:
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal credit note transition {self.status} -> {new_status}."
            )


class CreditNoteLine(TenantScopedModel):
    """One credited item. Mirrors :class:`InvoiceLine` field for field.

    The symmetry is intentional and worth the duplication: a credit note is
    printed, taxed and posted by the same routines as an invoice, only with
    the debits and credits swapped. Diverging the shapes would force every
    one of those routines to branch.
    """

    credit_note = models.ForeignKey(
        CreditNote, on_delete=models.CASCADE, related_name="lines"
    )
    line_number = models.PositiveSmallIntegerField()
    #: The invoice line being credited, when the credit is item-specific.
    #: PROTECT: the original line is evidence for the credit.
    invoice_line = models.ForeignKey(
        InvoiceLine,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_note_lines",
    )

    item = models.ForeignKey(
        "inventory.Item",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_note_lines",
    )
    description = models.CharField(max_length=500)

    quantity = QuantityField(default=1)
    unit_price = MoneyField()
    discount_rate = RateField()
    tax_rate = models.ForeignKey(
        "accounting.TaxRate",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_note_lines",
    )

    line_subtotal = MoneyField()
    line_tax = MoneyField()
    line_total = MoneyField()

    #: Debited on credit: the revenue account originally credited. Carried
    #: explicitly rather than looked up from the invoice line so that a
    #: general (invoice-less) credit still knows where to post.
    income_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, related_name="credit_note_lines"
    )
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_note_lines",
    )
    #: True when the credited goods physically came back and stock must be
    #: re-increased; a pure price adjustment must NOT move inventory.
    restocks_inventory = models.BooleanField(default=False)

    class Meta(TenantScopedModel.Meta):
        db_table = "sales_credit_note_line"
        ordering = ["credit_note", "line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["credit_note", "line_number"], name="uq_credit_note_line_number"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0) & models.Q(unit_price__gte=0),
                name="ck_cn_line_qty_price_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_rate__gte=0)
                & models.Q(discount_rate__lte=1),
                name="ck_cn_line_discount_fraction",
            ),
            models.CheckConstraint(
                condition=models.Q(line_subtotal__gte=0)
                & models.Q(line_tax__gte=0)
                & models.Q(line_total__gte=0),
                name="ck_cn_line_amounts_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["credit_note", "line_number"], name="ix_cn_line_parent"),
            models.Index(fields=["tenant", "item"], name="ix_cn_line_item"),
        ]


# ---------------------------------------------------------------------------
# Recurring billing
# ---------------------------------------------------------------------------

class RecurringInvoiceProfile(TenantScopedModel):
    """A subscription template: "bill this customer these lines, monthly".

    This is *not* an ``ImmutableFinancialModel`` — it has no financial effect
    of its own. It is a schedule that mints real invoices, and each generated
    invoice points back here via ``Invoice.recurring_profile``.

    ``next_run_date`` is a stored column rather than a computed "start_date +
    n * frequency". Two reasons, both learned the hard way: a tenant may skip
    or defer one cycle without breaking the rest of the series, and the
    generation job must be able to claim work with
    ``SELECT ... WHERE next_run_date <= today FOR UPDATE SKIP LOCKED`` — which
    needs an indexed column, not an expression.

    Idempotency of generation is the job's responsibility: it advances
    ``next_run_date`` inside the same transaction that creates the invoice, so
    a crashed run either produced both or neither.
    """

    class Frequency(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Every two weeks"
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        SEMIANNUAL = "semiannual", "Every six months"
        ANNUAL = "annual", "Annually"

    name = models.CharField(max_length=150)
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="recurring_profiles"
    )
    currency = models.CharField(max_length=3, choices=Currency.choices)
    frequency = models.CharField(
        max_length=12, choices=Frequency.choices, db_index=True
    )
    #: Multiplier on ``frequency`` — "every 2 months" is (MONTHLY, 2).
    interval = models.PositiveSmallIntegerField(default=1)

    start_date = models.DateField()
    #: The date the *next* invoice is due to be generated. Claimed by the job.
    next_run_date = models.DateField(null=True, blank=True, db_index=True)
    #: NULL = runs until stopped. Cheaper to reason about than a sentinel date.
    end_date = models.DateField(null=True, blank=True)
    #: NULL = unlimited. Counted so "12 invoices then stop" needs no calendar
    #: arithmetic that DST and month-length rules would get wrong.
    max_occurrences = models.PositiveSmallIntegerField(null=True, blank=True)
    occurrences_generated = models.PositiveIntegerField(default=0)

    #: Email the generated invoice immediately (DRAFT -> SENT without a human
    #: in the loop). Off by default: silently mailing a wrong invoice to a
    #: customer is far more expensive than a queue of drafts to review.
    auto_send = models.BooleanField(default=False)
    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    #: Days *before* the period start to cut the invoice, so a customer
    #: receives their 1 March invoice in late February.
    lead_days = models.PositiveSmallIntegerField(default=0)

    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recurring_profiles",
    )
    notes = models.TextField(blank=True)
    terms = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "sales_recurring_invoice_profile"
        ordering = ["next_run_date", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="uq_recurring_profile_name"
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="ck_recurring_profile_date_order",
            ),
            models.CheckConstraint(
                condition=models.Q(interval__gte=1), name="ck_recurring_profile_interval"
            ),
            # An active profile must know when it next fires, or the job will
            # never pick it up and the tenant silently stops billing.
            models.CheckConstraint(
                condition=models.Q(is_active=False)
                | models.Q(next_run_date__isnull=False),
                name="ck_recurring_profile_active_has_next_run",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "customer"], name="ix_recurring_customer"),
            # The generation job's claim query.
            models.Index(
                fields=["next_run_date"],
                condition=models.Q(is_active=True),
                name="ix_recurring_due",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} ({self.get_frequency_display()})"


class RecurringInvoiceLineTemplate(TenantScopedModel):
    """A line that will be copied onto every invoice this profile generates.

    Kept separate from :class:`InvoiceLine` rather than reusing it with a
    nullable invoice FK: a template has no computed totals and no tax
    snapshot, and a nullable-parent invoice line would weaken every constraint
    on the real table.
    """

    profile = models.ForeignKey(
        RecurringInvoiceProfile, on_delete=models.CASCADE, related_name="line_templates"
    )
    line_number = models.PositiveSmallIntegerField()
    item = models.ForeignKey(
        "inventory.Item",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recurring_line_templates",
    )
    description = models.CharField(max_length=500)
    quantity = QuantityField(default=1)
    unit_price = MoneyField()
    discount_rate = RateField()
    tax_rate = models.ForeignKey(
        "accounting.TaxRate",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recurring_line_templates",
    )
    income_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="recurring_line_templates",
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "sales_recurring_invoice_line_template"
        ordering = ["profile", "line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "line_number"], name="uq_recurring_line_number"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0) & models.Q(unit_price__gte=0),
                name="ck_recurring_line_qty_price_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_rate__gte=0)
                & models.Q(discount_rate__lte=1),
                name="ck_recurring_line_discount_fraction",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["profile", "line_number"], name="ix_recurring_line_parent"),
        ]


# ---------------------------------------------------------------------------
# Dunning
# ---------------------------------------------------------------------------

class PaymentReminderRule(TenantScopedModel):
    """When and how to nag a customer about an unpaid invoice.

    ``offset_days`` is signed and relative to ``Invoice.due_date``:
    ``-3`` is a courtesy reminder three days *before* the money is due,
    ``+7`` is the first chase after it is late. Encoding direction in the sign
    rather than in a separate ``before_or_after`` enum means the scheduler's
    query is a single ``due_date + offset_days == today``, and the rules sort
    naturally into the order the customer will experience them.
    """

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"
        WHATSAPP = "whatsapp", "WhatsApp"

    name = models.CharField(max_length=150)
    offset_days = models.SmallIntegerField(
        help_text="Negative = days before due date, positive = days after."
    )
    channel = models.CharField(max_length=10, choices=Channel.choices, db_index=True)
    template_subject = models.CharField(max_length=255, blank=True)
    template_body = models.TextField(
        help_text="Rendered with the invoice, customer and tenant in context."
    )
    #: Below this open balance the reminder is skipped — chasing 0.30 costs
    #: more in goodwill than it collects.
    minimum_amount_due = MoneyField()
    is_active = models.BooleanField(default=True)
    #: Escalation copy to the account manager rather than the customer.
    notify_internal_only = models.BooleanField(default=False)

    class Meta(TenantScopedModel.Meta):
        db_table = "sales_payment_reminder_rule"
        ordering = ["offset_days"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="uq_reminder_rule_name"
            ),
            # Two rules at the same offset on the same channel would send the
            # customer two identical messages on the same morning.
            models.UniqueConstraint(
                fields=["tenant", "offset_days", "channel"],
                name="uq_reminder_rule_offset_channel",
            ),
            models.CheckConstraint(
                condition=models.Q(minimum_amount_due__gte=0),
                name="ck_reminder_rule_min_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_reminder_rule_active"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        direction = "before" if self.offset_days < 0 else "after"
        return f"{self.name} ({abs(self.offset_days)}d {direction} due, {self.channel})"


class PaymentReminderLog(TenantScopedModel):
    """Proof that a specific reminder was (or was not) sent for an invoice.

    The unique constraint on ``(invoice, rule, scheduled_for)`` is the whole
    point of this table. The dunning job is a Celery beat task; beat schedules
    can fire twice after a worker restart, a deploy can overlap two runs, and
    a retried task looks exactly like a fresh one. Without a database-level
    claim, the customer receives the same chaser three times and the tenant
    looks incompetent.

    So the job **inserts this row first** and only then sends: the insert
    either succeeds (this worker owns the send) or violates the constraint
    (another worker already owns it, skip). Sending first and logging after
    leaves a window in which a crash loses the record and guarantees a
    duplicate on the next run.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="reminder_logs"
    )
    rule = models.ForeignKey(
        PaymentReminderRule, on_delete=models.PROTECT, related_name="logs"
    )
    #: The date the rule computed for this invoice (due_date + offset_days).
    #: Part of the dedup key so that extending a due date legitimately allows
    #: the same rule to fire again for the new date.
    scheduled_for = models.DateField(db_index=True)

    channel = models.CharField(max_length=10, choices=PaymentReminderRule.Channel.choices)
    recipient = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    #: Snapshot of what was owed when the reminder went out, for the dispute
    #: that starts "but I already paid that".
    amount_due_snapshot = MoneyField()
    sent_at = models.DateTimeField(null=True, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "sales_payment_reminder_log"
        ordering = ["-scheduled_for"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "rule", "scheduled_for"],
                name="uq_reminder_log_dedup",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_due_snapshot__gte=0),
                name="ck_reminder_log_amount_non_negative",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="sent") | models.Q(sent_at__isnull=False),
                name="ck_reminder_log_sent_has_timestamp",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status", "scheduled_for"], name="ix_reminder_log_due"),
            models.Index(fields=["invoice"], name="ix_reminder_log_invoice"),
        ]


def invoice_attachment_path(instance: "InvoiceAttachment", filename: str) -> str:
    """``invoices/<tenant>/<invoice>/<uuid>.<ext>``.

    Three things this shape buys:

    * **Tenant-first** so a whole customer's files can be exported or erased
      with one prefix operation — which is what a GDPR deletion request or an
      offboarding actually looks like.
    * **A UUID, not the original name.** Two people uploading ``scan.pdf``
      must not collide, and the original name is preserved in a column where
      it is data rather than a path. It also means the stored name cannot
      contain ``../``, a null byte, or a Windows reserved device name — path
      traversal via upload is not a hypothetical.
    * **The extension kept** so a browser fetching the file gets a usable
      content type from the storage backend, and so an operator listing the
      bucket can see what is in it.
    """
    import uuid as _uuid
    from pathlib import PurePosixPath

    suffix = PurePosixPath(filename or "").suffix.lower()[:12]
    # Only characters that are safe in a key. Anything else and the extension
    # is simply dropped — a file with no extension is fine, a key with a
    # newline in it is not.
    if not re.fullmatch(r"\.[a-z0-9]{1,11}", suffix or ""):
        suffix = ""
    return f"invoices/{instance.tenant_id}/{instance.invoice_id}/{_uuid.uuid4().hex}{suffix}"


class InvoiceAttachment(TenantScopedModel):
    """A file attached to an invoice: the signed PO, a delivery note, a spec.

    Why the bytes go through Django's storage backend rather than a
    ``file_key`` string
    -------------------------------------------------------------------
    ``expenses.ExpenseReceipt`` models the key only, on the assumption that
    something else puts the bytes there. Nothing ever did — there was no
    upload path anywhere in the product. A ``FileField`` writes through
    ``settings.STORAGES``, which is ``FileSystemStorage`` in dev and whatever
    object store production configures, so the same code works in both without
    a second upload mechanism to keep in step.

    Deduplication is per **invoice**, not per tenant
    ------------------------------------------------
    ``ExpenseReceipt`` is unique on ``(tenant, sha256)`` because re-submitting
    one restaurant receipt against three claims is the oldest expense fraud
    there is. Attachments are not that: the same signed framework agreement
    legitimately belongs on every invoice raised under it, and a tenant-wide
    index would refuse the second one. Uniqueness per invoice still catches
    the thing worth catching — the same file attached twice to one document by
    a double-click.

    What is deliberately not stored
    -------------------------------
    No OCR fields. An attachment is evidence a human reads; a receipt is input
    to an extraction pipeline. Adding empty OCR columns here would invite
    somebody to wire the pipeline to the wrong model.
    """

    #: Content types a finance team actually attaches. An allowlist, not a
    #: blocklist: the set of dangerous types is open-ended and grows, while
    #: the set of useful ones is short and stable. HTML and SVG are excluded
    #: on purpose — both execute script when opened from the same origin the
    #: app is served from, which turns an attachment into stored XSS.
    ALLOWED_CONTENT_TYPES = (
        "application/pdf",
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/tiff",
        "text/plain", "text/csv",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
    )

    #: 10 MB. Large enough for a scanned multi-page contract, small enough
    #: that a mis-selected video does not fill the volume. Enforced in the
    #: serializer (a friendly 400) *and* asserted by a CHECK constraint, so a
    #: future importer that skips the serializer cannot write a 2 GB row.
    MAX_BYTES = 10 * 1024 * 1024

    invoice = models.ForeignKey(
        "sales.Invoice", on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(upload_to=invoice_attachment_path, max_length=500)
    #: What the user called it. Shown in the UI and used for the download
    #: filename; never used to build a path.
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveIntegerField()
    #: Hex SHA-256 of the contents, for the per-invoice duplicate check.
    sha256 = models.CharField(max_length=64, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "sales_invoice_attachment"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "sha256"], name="uq_invoice_attachment_sha256"
            ),
            models.CheckConstraint(
                condition=models.Q(size_bytes__gt=0)
                & models.Q(size_bytes__lte=10 * 1024 * 1024),
                name="ck_invoice_attachment_size",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "invoice"], name="ix_inv_attach_invoice"),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.original_filename} ({self.size_bytes} bytes)"
