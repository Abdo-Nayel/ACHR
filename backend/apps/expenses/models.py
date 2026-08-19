"""
Cash out: what the tenant spent, who they owe, and the evidence for both.

Two documents live here and they are not the same thing, however similar they
look on screen:

* :class:`Expense` — money that has **already left** (a card swipe, petty
  cash, an employee's own pocket awaiting reimbursement). It is recognised
  when approved and posts against a payment account or an employee payable.
* :class:`Bill` — a vendor's invoice creating an **obligation to pay later**.
  It posts to Accounts Payable and is settled by a separate
  :class:`BillPayment`.

Collapsing them loses the AP aging report entirely, because an expense has no
"open balance" and a bill's whole point is that it does.

As everywhere else, no model here writes ``JournalLine`` rows; they build a
``JournalEntryDraft`` and hand it to ``post_entry()``.
"""

from __future__ import annotations

from django.db import models

from apps.core.fields import MoneyField, QuantityField, RateField, ZERO
from apps.core.models import (
    Currency,
    ImmutableFinancialModel,
    TenantScopedModel,
)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

class ExpenseCategory(TenantScopedModel):
    """A spend classification that resolves to exactly one expense account.

    Categories exist because the chart of accounts is the accountant's
    vocabulary and this is the employee's. "Client lunch" is a category;
    ``6420 Entertainment — non-deductible`` is an account. Forcing staff to
    pick GL accounts produces miscoded expenses, and miscoded entertainment
    spend is a tax adjustment nobody notices until the audit.

    Hierarchical for reporting roll-ups only. ``parent`` is ``PROTECT``: a
    category with children whose descendants carry posted history must not
    vanish and silently reparent its subtree to the root.

    ``is_tax_deductible`` and ``deductible_rate`` are stored here rather than
    derived at report time because deductibility rules change by tax year, and
    a historical expense must keep the treatment it was filed under.
    """

    code = models.CharField(max_length=32)
    name = models.CharField(max_length=150)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    expense_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="expense_categories",
        help_text="Leaf expense account this category posts to.",
    )
    default_tax_rate = models.ForeignKey(
        "accounting.TaxRate",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expense_categories",
    )
    is_tax_deductible = models.BooleanField(default=True)
    #: Fraction allowed, e.g. 0.500000 where only half of entertainment is
    #: deductible. 1 when fully deductible.
    deductible_rate = RateField(default=1)
    #: Spend above this needs a second approver. 0 = always needs approval.
    approval_threshold_amount = MoneyField()
    requires_receipt = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "expenses_category"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_expense_category_code"
            ),
            models.CheckConstraint(
                condition=~models.Q(parent=models.F("id")),
                name="ck_expense_category_no_self_parent",
            ),
            models.CheckConstraint(
                condition=models.Q(deductible_rate__gte=0)
                & models.Q(deductible_rate__lte=1),
                name="ck_expense_category_deductible_fraction",
            ),
            models.CheckConstraint(
                condition=models.Q(approval_threshold_amount__gte=0),
                name="ck_expense_category_threshold_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_expense_cat_active"),
            models.Index(fields=["tenant", "parent"], name="ix_expense_cat_parent"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.name}"


class Vendor(TenantScopedModel):
    """A party the tenant buys from.

    Deliberately a separate model from ``sales.Customer`` even though the same
    legal entity is often both. Merging them into a generic "Partner" seems
    tidy and then every query needs a role filter, the AR and AP control
    accounts hang off the same row, and a customer credit limit sits next to a
    vendor payment term confusing everyone. The duplication is cheap; the
    conflation is not.

    ``payable_account`` mirrors ``Customer.receivable_account``: most vendors
    roll up to the default AP control account, but retentions, intercompany
    and factored payables need their own.
    """

    code = models.CharField(max_length=32)
    name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    tax_number = models.CharField(max_length=50, blank=True)
    address = models.JSONField(default=dict, blank=True)

    currency = models.CharField(max_length=3, choices=Currency.choices)
    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    payable_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="ap_vendors",
        help_text="AP control account this vendor's balance rolls up to.",
    )
    default_expense_account = models.ForeignKey(
        "accounting.Account",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="default_expense_vendors",
    )
    #: Subject to withholding tax / 1099-style reporting. Stored because the
    #: year-end filing needs it and it is not derivable from the postings.
    is_withholding_applicable = models.BooleanField(default=False)
    withholding_rate = RateField()
    bank_details = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "expenses_vendor"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_vendor_code"),
            models.CheckConstraint(
                condition=models.Q(withholding_rate__gte=0)
                & models.Q(withholding_rate__lte=1),
                name="ck_vendor_withholding_fraction",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_vendor_active"),
            models.Index(fields=["tenant", "name"], name="ix_vendor_name"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.display_name or self.name}"


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------

class Expense(ImmutableFinancialModel):
    """A single item of spend, already paid, awaiting classification/approval.

    Lifecycle
    ---------
    ``DRAFT -> SUBMITTED -> APPROVED -> REIMBURSED``, with ``REJECTED`` as the
    exit from review.

    Only ``APPROVED`` posts to the ledger. A submitted expense is a claim, not
    a liability: recognising it on submission lets any employee move the P&L
    by typing, and unwinding a rejected claim then requires a reversing entry
    for something that was never real.

    ``REIMBURSED`` is a distinct state from ``APPROVED`` because it answers a
    different question. Approval says "the company accepts this cost"; it
    posts ``Dr Expense / Cr Employee payable``. Reimbursement says "we have
    paid the employee back"; it posts ``Dr Employee payable / Cr Bank``. For
    company-card spend the two collapse — the money already left the company
    account — which is why ``is_reimbursable`` gates the second step rather
    than every expense walking the full chain.

    ``is_billable`` marks spend to be recharged to a customer. It requires a
    ``customer``, enforced below, because an expense flagged billable with
    nobody to bill is invisible lost margin — it never appears on an invoice
    and never appears on an exception report either.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        REIMBURSED = "reimbursed", "Reimbursed"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.SUBMITTED},
        Status.SUBMITTED: {Status.APPROVED, Status.REJECTED, Status.DRAFT},
        # Approved spend has posted; correcting it means reversing, not
        # editing, so there is no path back to DRAFT.
        Status.APPROVED: {Status.REIMBURSED, Status.REJECTED},
        # A rejected claim can be corrected and resubmitted — the common case
        # is a missing receipt, not fraud.
        Status.REJECTED: {Status.DRAFT},
        Status.REIMBURSED: set(),
    }

    class PaymentMethod(models.TextChoices):
        COMPANY_CARD = "company_card", "Company card"
        PERSONAL_CARD = "personal_card", "Personal card"
        CASH = "cash", "Cash"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        PETTY_CASH = "petty_cash", "Petty cash"

    number = models.CharField(max_length=32, blank=True)
    vendor = models.ForeignKey(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses",
        help_text="NULL when the merchant is not a tracked vendor.",
    )
    category = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT, related_name="expenses"
    )
    description = models.CharField(max_length=500, blank=True)
    expense_date = models.DateField(db_index=True)

    currency = models.CharField(max_length=3, choices=Currency.choices)
    exchange_rate = RateField(default=1)
    amount = MoneyField(help_text="Net of tax.")
    tax_amount = MoneyField()
    total_amount = MoneyField(help_text="Gross — what actually left the account.")
    tax_rate = models.ForeignKey(
        "accounting.TaxRate",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses",
    )

    payment_method = models.CharField(
        max_length=16, choices=PaymentMethod.choices, db_index=True
    )
    #: Bank/cash/card account credited on approval. For a reimbursable
    #: employee expense this is the employee-payable liability instead, which
    #: is why it is a plain Account FK and not a "bank account" FK.
    paid_from_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, related_name="expenses_paid"
    )

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True
    )

    is_billable = models.BooleanField(default=False)
    is_reimbursable = models.BooleanField(default=False)
    #: Markup applied when recharging. 0 = at cost.
    markup_rate = RateField()
    #: Set once the cost has been pulled onto a customer invoice, so the next
    #: billing run does not recharge it twice.
    invoiced_line = models.OneToOneField(
        "sales.InvoiceLine",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="source_expense",
    )

    customer = models.ForeignKey(
        "sales.Customer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses",
    )
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses",
    )
    employee = models.ForeignKey(
        "hr.Employee",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses",
        help_text="Claimant, for reimbursable staff spend.",
    )

    journal_entry = models.OneToOneField(
        "accounting.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expense",
    )
    #: Second entry, for the reimbursement leg. Separate field rather than a
    #: reused one: both postings must remain independently traceable.
    reimbursement_entry = models.OneToOneField(
        "accounting.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reimbursed_expense",
    )

    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    rejected_reason = models.CharField(max_length=255, blank=True)
    reimbursed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "expenses_expense"
        ordering = ["-expense_date", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"],
                condition=~models.Q(number=""),
                name="uq_expense_number",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    total_amount=models.F("amount") + models.F("tax_amount")
                ),
                name="ck_expense_total_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0)
                & models.Q(tax_amount__gte=0)
                & models.Q(total_amount__gt=0),
                name="ck_expense_amounts_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(exchange_rate__gt=0), name="ck_expense_fx_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(markup_rate__gte=0), name="ck_expense_markup_non_negative"
            ),
            # Billable spend with nobody to bill is silently lost margin.
            models.CheckConstraint(
                condition=models.Q(is_billable=False)
                | models.Q(customer__isnull=False),
                name="ck_expense_billable_has_customer",
            ),
            # Somebody has to be reimbursed.
            models.CheckConstraint(
                condition=models.Q(is_reimbursable=False)
                | models.Q(employee__isnull=False),
                name="ck_expense_reimbursable_has_employee",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="approved")
                | models.Q(approved_at__isnull=False),
                name="ck_expense_approved_has_timestamp",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="rejected") | ~models.Q(rejected_reason=""),
                name="ck_expense_rejected_has_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status", "expense_date"], name="ix_expense_status"),
            models.Index(fields=["tenant", "category"], name="ix_expense_category"),
            models.Index(fields=["tenant", "employee", "status"], name="ix_expense_employee"),
            models.Index(fields=["tenant", "project"], name="ix_expense_project"),
            models.Index(fields=["tenant", "vendor"], name="ix_expense_vendor"),
            # The "what can we recharge this month?" query.
            models.Index(
                fields=["tenant", "customer"],
                condition=models.Q(is_billable=True, invoiced_line__isnull=True),
                name="ix_expense_rebillable",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.number or f"expense {self.id}"

    def assert_can_transition(self, new_status: str) -> None:
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal expense transition {self.status} -> {new_status} "
                f"on expense {self.number or self.id}."
            )


class ExpenseReceipt(TenantScopedModel):
    """The evidence file for an expense, plus what OCR read from it.

    The ``sha256`` unique-per-tenant constraint is a fraud control, not a
    storage optimisation. The oldest expense fraud in the world is submitting
    the same restaurant receipt on three different claims, in three different
    months, possibly by three colleagues who shared a meal. Humans reviewing
    claims one at a time cannot see the repeat; a unique index over the file's
    content hash sees it on the second submission, before approval, and makes
    the duplicate a rejected upload rather than an audit finding.

    It is scoped per tenant rather than globally because two tenants uploading
    an identical file (a standard invoice template, a blank form) is not
    fraud, and a global index would also leak the existence of other tenants'
    documents.

    ``ocr_confidence`` is stored so low-confidence extractions can be routed
    to a human instead of silently posting a mis-read total. A wrong amount
    read from a receipt is worse than no amount read at all.
    """

    expense = models.ForeignKey(
        Expense, on_delete=models.CASCADE, related_name="receipts"
    )
    #: Object-store key, not a filesystem path — the app runs on more than one
    #: node and local disk is not shared.
    file_key = models.CharField(max_length=500)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveIntegerField()
    #: Hex SHA-256 of the file contents. 64 chars.
    sha256 = models.CharField(max_length=64, db_index=True)

    ocr_text = models.TextField(blank=True)
    #: 0..1. NULL means OCR has not run yet, which is different from "ran and
    #: found nothing" — the queue needs to tell those apart.
    ocr_confidence = RateField(null=True, blank=True, default=None)
    ocr_extracted = models.JSONField(
        default=dict, blank=True, help_text="Parsed merchant/date/total/tax."
    )
    ocr_processed_at = models.DateTimeField(null=True, blank=True)

    uploaded_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "expenses_receipt"
        ordering = ["-created_at"]
        constraints = [
            # The duplicate-submission control. See the class docstring.
            models.UniqueConstraint(
                fields=["tenant", "sha256"], name="uq_expense_receipt_sha256"
            ),
            models.CheckConstraint(
                condition=models.Q(size_bytes__gt=0),
                name="ck_expense_receipt_size_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(ocr_confidence__isnull=True)
                | (
                    models.Q(ocr_confidence__gte=0)
                    & models.Q(ocr_confidence__lte=1)
                ),
                name="ck_expense_receipt_confidence_fraction",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["expense"], name="ix_expense_receipt_parent"),
            models.Index(
                fields=["tenant", "ocr_processed_at"], name="ix_expense_receipt_ocr"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.original_filename


# ---------------------------------------------------------------------------
# Vendor bills (accounts payable)
# ---------------------------------------------------------------------------

class Bill(ImmutableFinancialModel):
    """A vendor's invoice to us: an obligation to pay, recorded when incurred.

    The AP mirror of :class:`~apps.sales.models.Invoice`, and it carries the
    same balance invariants for the same reasons: ``amount_due`` is stored so
    the AP aging report is a single scan, and constrained to
    ``total_amount - amount_paid`` so it cannot drift.

    ``vendor_reference`` is the vendor's *own* document number. It is unique
    per vendor per tenant because entering the same supplier invoice twice —
    once from the emailed PDF, once from the posted copy — is the most common
    way a company pays a bill twice. The database refusing the second entry is
    worth far more than a warning nobody reads.

    Approval before posting matters here in a way it does not for a sales
    invoice: a bill creates a liability from a document produced by someone
    outside the company.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
        APPROVED = "approved", "Approved"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        VOIDED = "voided", "Voided"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.AWAITING_APPROVAL, Status.VOIDED},
        Status.AWAITING_APPROVAL: {Status.APPROVED, Status.DRAFT, Status.VOIDED},
        Status.APPROVED: {
            Status.PARTIALLY_PAID,
            Status.PAID,
            Status.OVERDUE,
            Status.VOIDED,
        },
        Status.PARTIALLY_PAID: {Status.PAID, Status.OVERDUE, Status.APPROVED},
        # OVERDUE is derived by the same kind of scheduled sweep as on the
        # sales side, and is likewise reversible when a due date is renegotiated.
        Status.OVERDUE: {Status.PARTIALLY_PAID, Status.PAID, Status.APPROVED},
        Status.PAID: {Status.PARTIALLY_PAID},
        Status.VOIDED: set(),
    }

    number = models.CharField(max_length=32, blank=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="bills")
    #: The vendor's own invoice number — the duplicate-payment control.
    vendor_reference = models.CharField(max_length=100, blank=True)

    bill_date = models.DateField(db_index=True)
    due_date = models.DateField(db_index=True)
    received_at = models.DateTimeField(null=True, blank=True)

    currency = models.CharField(max_length=3, choices=Currency.choices)
    exchange_rate = RateField(default=1)
    subtotal_amount = MoneyField()
    tax_amount = MoneyField()
    withholding_amount = MoneyField(
        help_text="Tax withheld at source; reduces cash paid, not the expense."
    )
    total_amount = MoneyField()
    amount_paid = MoneyField()
    amount_due = MoneyField()

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bill",
    )
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bills",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "expenses_bill"
        ordering = ["-bill_date", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"],
                condition=~models.Q(number=""),
                name="uq_bill_number",
            ),
            # Duplicate supplier invoice guard.
            models.UniqueConstraint(
                fields=["tenant", "vendor", "vendor_reference"],
                condition=~models.Q(vendor_reference=""),
                name="uq_bill_vendor_reference",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    amount_due=models.F("total_amount") - models.F("amount_paid")
                ),
                name="ck_bill_due_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(subtotal_amount__gte=0)
                & models.Q(tax_amount__gte=0)
                & models.Q(withholding_amount__gte=0)
                & models.Q(total_amount__gte=0)
                & models.Q(amount_paid__gte=0),
                name="ck_bill_amounts_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_paid__lte=models.F("total_amount")),
                name="ck_bill_no_overpayment",
            ),
            models.CheckConstraint(
                condition=models.Q(due_date__gte=models.F("bill_date")),
                name="ck_bill_due_after_bill_date",
            ),
            models.CheckConstraint(
                condition=models.Q(exchange_rate__gt=0), name="ck_bill_fx_positive"
            ),
            models.CheckConstraint(
                condition=~models.Q(status="voided") | ~models.Q(void_reason=""),
                name="ck_bill_void_has_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status", "due_date"], name="ix_bill_status"),
            models.Index(fields=["tenant", "vendor", "status"], name="ix_bill_vendor"),
            models.Index(fields=["tenant", "bill_date"], name="ix_bill_date"),
            models.Index(
                fields=["tenant", "due_date"],
                condition=models.Q(amount_due__gt=0),
                name="ix_bill_open_balance",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.number or f"DRAFT bill {self.id}"

    def assert_can_transition(self, new_status: str) -> None:
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal bill transition {self.status} -> {new_status} "
                f"on bill {self.number or self.id}."
            )


class BillLine(TenantScopedModel):
    """One coded line of a vendor bill.

    ``expense_account`` is per line, not per bill: a single utility invoice
    routinely splits across office rent, electricity and a service charge, and
    coding it as one lump makes the P&L useless at exactly the level of detail
    anyone cares about.

    Carries no currency; the bill header pins it.
    """

    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="lines")
    line_number = models.PositiveSmallIntegerField()
    item = models.ForeignKey(
        "inventory.Item",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bill_lines",
    )
    description = models.CharField(max_length=500)
    quantity = QuantityField(default=1)
    unit_price = MoneyField()
    tax_rate = models.ForeignKey(
        "accounting.TaxRate",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bill_lines",
    )
    line_subtotal = MoneyField()
    line_tax = MoneyField()
    line_total = MoneyField()

    expense_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, related_name="bill_lines"
    )
    category = models.ForeignKey(
        ExpenseCategory,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bill_lines",
    )
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bill_lines",
    )
    is_billable = models.BooleanField(default=False)
    customer = models.ForeignKey(
        "sales.Customer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bill_lines",
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "expenses_bill_line"
        ordering = ["bill", "line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["bill", "line_number"], name="uq_bill_line_number"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0) & models.Q(unit_price__gte=0),
                name="ck_bill_line_qty_price_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(line_subtotal__gte=0)
                & models.Q(line_tax__gte=0)
                & models.Q(line_total__gte=0),
                name="ck_bill_line_amounts_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(is_billable=False)
                | models.Q(customer__isnull=False),
                name="ck_bill_line_billable_has_customer",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["bill", "line_number"], name="ix_bill_line_parent"),
            models.Index(fields=["tenant", "project"], name="ix_bill_line_project"),
            models.Index(fields=["tenant", "expense_account"], name="ix_bill_line_account"),
        ]


class BillPayment(ImmutableFinancialModel):
    """Cash leaving to settle one bill.

    Modelled one-payment-to-one-bill rather than with the sales side's
    application table, because outbound payments are initiated by us: a
    payment run generates one row per bill even when it produces a single
    bank transfer, and the batch is expressed by ``payment_batch_reference``.
    That keeps "how much of this transfer settled that bill" answerable
    without an allocation table, at the cost of a shared reference string —
    the right trade only because we control the outbound side.

    ``withholding_amount`` is the tax withheld and remitted to the authority
    rather than paid to the vendor. The vendor's balance is cleared by the
    full amount; only the cash is smaller. Netting it away instead leaves the
    bill permanently short-paid and the vendor permanently in the aging report.
    """

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.SCHEDULED: {Status.PAID, Status.FAILED, Status.CANCELLED},
        # A returned/bounced transfer un-pays the bill; it must be reversible.
        Status.PAID: {Status.FAILED},
        Status.FAILED: {Status.SCHEDULED, Status.CANCELLED},
        Status.CANCELLED: set(),
    }

    number = models.CharField(max_length=32, blank=True)
    bill = models.ForeignKey(Bill, on_delete=models.PROTECT, related_name="payments")
    vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT, related_name="bill_payments"
    )
    payment_date = models.DateField(db_index=True)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    exchange_rate = RateField(default=1)
    #: Amount credited against the bill (gross of withholding).
    amount = MoneyField()
    withholding_amount = MoneyField()
    #: amount - withholding_amount. Stored because the bank reconciliation
    #: matches on this figure, not on the gross.
    cash_amount = MoneyField()

    payment_method = models.CharField(
        max_length=16, choices=Expense.PaymentMethod.choices, db_index=True
    )
    paid_from_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, related_name="bill_payments"
    )
    reference = models.CharField(max_length=100, blank=True)
    #: Groups rows settled by one outbound transfer / cheque run.
    payment_batch_reference = models.CharField(max_length=100, blank=True, db_index=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.SCHEDULED, db_index=True
    )
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bill_payment",
    )
    failure_reason = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "expenses_bill_payment"
        ordering = ["-payment_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"],
                condition=~models.Q(number=""),
                name="uq_bill_payment_number",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    cash_amount=models.F("amount") - models.F("withholding_amount")
                ),
                name="ck_bill_payment_cash_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0)
                & models.Q(withholding_amount__gte=0)
                & models.Q(cash_amount__gte=0),
                name="ck_bill_payment_amounts_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(withholding_amount__lte=models.F("amount")),
                name="ck_bill_payment_withholding_within_amount",
            ),
            models.CheckConstraint(
                condition=models.Q(exchange_rate__gt=0),
                name="ck_bill_payment_fx_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status", "payment_date"], name="ix_bill_pmt_status"),
            models.Index(fields=["bill"], name="ix_bill_pmt_bill"),
            models.Index(fields=["tenant", "vendor"], name="ix_bill_pmt_vendor"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.number or f"bill payment {self.id}"

    def assert_can_transition(self, new_status: str) -> None:
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Illegal bill payment transition {self.status} -> {new_status}."
            )


# ---------------------------------------------------------------------------
# Vendor credits
# ---------------------------------------------------------------------------

class VendorCredit(TenantScopedModel):
    """A supplier's credit note: money the vendor owes back.

    The mirror of ``sales.CreditNote``, and modelled as its own document for
    the same reason: a bill is never edited after it posts, so an overcharge,
    a return or a rebate is corrected by a second document that reverses part
    of the first. Editing the bill would rewrite a figure the vendor has
    already invoiced and the ledger has already recorded.

    ``amount_applied`` / ``amount_remaining`` rather than a single balance:
    one credit is commonly consumed across several future bills, and the
    remaining figure is what accounts payable actually works from.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        PARTIALLY_APPLIED = "partially_applied", "Partially applied"
        APPLIED = "applied", "Applied"
        VOIDED = "voided", "Voided"

    ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
        "draft": ("open", "voided"),
        "open": ("partially_applied", "applied", "voided"),
        "partially_applied": ("applied", "open"),
        "applied": ("partially_applied",),
        "voided": (),
    }

    number = models.CharField(max_length=32, blank=True)
    vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT, related_name="credits"
    )
    #: The bill this credit relates to, when it relates to one. Optional: a
    #: volume rebate at year end credits the relationship, not a document.
    bill = models.ForeignKey(
        Bill, null=True, blank=True, on_delete=models.PROTECT,
        related_name="vendor_credits",
    )
    credit_date = models.DateField(db_index=True)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    exchange_rate = RateField(default=1)
    subtotal_amount = MoneyField(default=ZERO)
    tax_amount = MoneyField(default=ZERO)
    total_amount = MoneyField(default=ZERO)
    amount_applied = MoneyField(default=ZERO)
    amount_remaining = MoneyField(default=ZERO)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True,
        on_delete=models.PROTECT, related_name="vendor_credit",
    )
    reason = models.CharField(max_length=255, blank=True)
    void_reason = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "expenses_vendor_credit"
        ordering = ["-credit_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    amount_remaining=models.F("total_amount")
                    - models.F("amount_applied")
                ),
                name="ck_vendor_credit_remaining_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_applied__lte=models.F("total_amount")),
                name="ck_vendor_credit_no_overapplication",
            ),
            models.CheckConstraint(
                condition=models.Q(total_amount__gte=0)
                & models.Q(amount_applied__gte=0),
                name="ck_vendor_credit_amounts_non_negative",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="voided") | ~models.Q(void_reason=""),
                name="ck_vendor_credit_void_has_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "vendor", "-credit_date"],
                         name="ix_vcredit_vendor"),
            models.Index(fields=["tenant", "status"], name="ix_vcredit_status"),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.number or 'Draft'} — {self.vendor}"

    def assert_can_transition(self, new_status: str) -> None:
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, ())
        if new_status not in allowed:
            raise ValueError(
                f"A vendor credit cannot move from "
                f"{self.get_status_display().lower()} to {new_status}. "
                f"Allowed: {', '.join(allowed) or 'nothing — it is terminal'}."
            )


class VendorCreditLine(TenantScopedModel):
    """One line of a vendor credit."""

    credit_note = models.ForeignKey(
        VendorCredit, on_delete=models.CASCADE, related_name="lines"
    )
    line_number = models.PositiveSmallIntegerField()
    description = models.CharField(max_length=500, blank=True)
    quantity = QuantityField(default=1)
    unit_price = MoneyField(default=ZERO)
    tax_rate = models.ForeignKey(
        "accounting.TaxRate", null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    line_subtotal = MoneyField(default=ZERO)
    line_tax = MoneyField(default=ZERO)
    line_total = MoneyField(default=ZERO)
    #: Where the credit reverses to — normally the account the original bill
    #: line debited, so the reversal lands where the cost did.
    expense_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, related_name="+"
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "expenses_vendor_credit_line"
        ordering = ["credit_note", "line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["credit_note", "line_number"],
                name="uq_vendor_credit_line_number",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0) & models.Q(unit_price__gte=0),
                name="ck_vendor_credit_line_non_negative",
            ),
        ]


# ---------------------------------------------------------------------------
# Recurring purchases
# ---------------------------------------------------------------------------

class _RecurringBase(TenantScopedModel):
    """Shared schedule fields for the two recurring purchase profiles.

    Abstract rather than one concrete table with a ``kind`` column: a
    recurring bill produces a ``Bill`` (a vendor obligation with lines, tax
    and an approval step) and a recurring expense produces an ``Expense`` (a
    claim with a category and a payment method). They share a *schedule* and
    nothing else, and a single table would carry two mutually exclusive sets
    of nullable columns plus a constraint to keep them apart.

    ``next_run_date`` is stored rather than derived so the generator can find
    what is due with an index scan instead of recomputing every schedule.
    """

    class Frequency(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Bi-weekly"
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        SEMIANNUAL = "semiannual", "Semi-annual"
        ANNUAL = "annual", "Annual"

    name = models.CharField(max_length=150)
    vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT, related_name="%(class)s_profiles"
    )
    currency = models.CharField(max_length=3, choices=Currency.choices)
    frequency = models.CharField(
        max_length=12, choices=Frequency.choices, default=Frequency.MONTHLY
    )
    #: Every N periods. 2 + MONTHLY = every second month.
    interval = models.PositiveSmallIntegerField(default=1)
    start_date = models.DateField()
    next_run_date = models.DateField(db_index=True)
    end_date = models.DateField(null=True, blank=True)
    max_occurrences = models.PositiveSmallIntegerField(null=True, blank=True)
    occurrences_generated = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    #: Why the last generation failed, so a broken schedule is visible on the
    #: list rather than silently producing nothing.
    last_error = models.TextField(blank=True)

    class Meta(TenantScopedModel.Meta):
        abstract = True

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.name} ({self.get_frequency_display().lower()})"

    @property
    def is_exhausted(self) -> bool:
        """Has the schedule run its course?"""
        if self.max_occurrences is None:
            return False
        return self.occurrences_generated >= self.max_occurrences


class RecurringBillProfile(_RecurringBase):
    """A standing vendor bill: rent, a support contract, a lease."""

    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    #: Generated bills land in DRAFT unless this is set, in which case they go
    #: straight to AWAITING_APPROVAL. Never auto-approved: approval is the
    #: control that stops a compromised schedule paying a fake vendor forever.
    auto_submit = models.BooleanField(default=False)

    class Meta(_RecurringBase.Meta):
        db_table = "expenses_recurring_bill_profile"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="uq_recurring_bill_name"
            ),
            models.CheckConstraint(
                condition=models.Q(interval__gte=1), name="ck_recurring_bill_interval"
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="ck_recurring_bill_dates",
            ),
        ]


class RecurringBillLineTemplate(TenantScopedModel):
    """A line the schedule stamps onto each generated bill."""

    profile = models.ForeignKey(
        RecurringBillProfile, on_delete=models.CASCADE, related_name="lines"
    )
    line_number = models.PositiveSmallIntegerField()
    description = models.CharField(max_length=500, blank=True)
    quantity = QuantityField(default=1)
    unit_price = MoneyField(default=ZERO)
    tax_rate = models.ForeignKey(
        "accounting.TaxRate", null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    expense_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, related_name="+"
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "expenses_recurring_bill_line"
        ordering = ["profile", "line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "line_number"],
                name="uq_recurring_bill_line_number",
            ),
        ]


class RecurringExpenseProfile(_RecurringBase):
    """A standing expense: a subscription on the company card.

    Distinct from a recurring *bill* because nothing is owed to anybody — the
    money leaves the account directly, so there is no approval-to-pay step and
    no accounts-payable balance in between.
    """

    category = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT, related_name="recurring_profiles"
    )
    paid_from_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, related_name="+"
    )
    payment_method = models.CharField(
        max_length=16, choices=Expense.PaymentMethod.choices,
        default=Expense.PaymentMethod.COMPANY_CARD,
    )
    amount = MoneyField(default=ZERO)
    tax_amount = MoneyField(default=ZERO)
    description = models.CharField(max_length=500, blank=True)

    class Meta(_RecurringBase.Meta):
        db_table = "expenses_recurring_expense_profile"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="uq_recurring_expense_name"
            ),
            models.CheckConstraint(
                condition=models.Q(interval__gte=1),
                name="ck_recurring_expense_interval",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="ck_recurring_expense_amount_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="ck_recurring_expense_dates",
            ),
        ]
