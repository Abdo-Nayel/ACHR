"""
The invoice state machine — the only sanctioned way to move an invoice.

Nothing outside this module assigns ``Invoice.status``. Every function here
follows the same shape, and the shape is the point:

    1. open a transaction,
    2. re-read the invoice ``FOR UPDATE`` (see "Why the row lock" below),
    3. validate the transition against ``Invoice.ALLOWED_TRANSITIONS``,
    4. build a :class:`~apps.accounting.services.posting.JournalEntryDraft`
       and post it through ``post_entry()``,
    5. write the invoice's new state,
    6. commit — all of it, or none of it.

Why the row lock
----------------
Every one of these operations is a read-modify-write on a *monetary* column:
read ``amount_paid``, add something, write it back. Under PostgreSQL's default
READ COMMITTED isolation two concurrent transactions both read the pre-image,
both compute their own new total, and the second write silently overwrites the
first. Two payments of 500 against a 1,000 invoice leave ``amount_paid = 500``
and the customer permanently 500 in credit that nobody can find.

``SELECT ... FOR UPDATE`` on the invoice row serialises those transactions:
the second one blocks until the first commits, then re-reads the *post*-image
and computes from the truth. It is deliberately the invoice row and not a
table-level lock — contention is per-invoice, which is almost none.

The same lock is what makes the status derivation safe. Deriving
``PAID`` from a stale ``amount_paid`` marks an invoice settled that is not.

Why post through ``post_entry`` rather than writing lines
---------------------------------------------------------
``post_entry`` is the single choke point that verifies ``debits == credits``,
refuses closed periods and enforces posting idempotency. A module that writes
``JournalLine`` rows directly bypasses all three, and the resulting imbalance
is discovered by the nightly trial-balance check with no way to tell which of
the day's postings caused it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, Q, Sum, Value, When
from django.utils import timezone

from apps.accounting.models import Account, JournalEntry
from apps.accounting.services.posting import (
    JournalEntryDraft,
    post_entry,
    void_entry,
)
from apps.core.fields import ZERO, quantize_currency, to_money
from apps.sales.models import Invoice, InvoiceLine

#: ``Journal.code`` the sales sub-ledger posts into. Kept as a constant rather
#: than looked up by ``kind`` because a tenant may run several sales journals
#: (e.g. per branch) and silently picking the first one is worse than failing.
SALES_JOURNAL_CODE = "SAL"

#: ``Account.system_key`` values this module resolves. Codes differ per
#: country's standard chart, so we never hard-code account codes.
SYSTEM_KEY_BAD_DEBT = "bad_debt_expense"
SYSTEM_KEY_SALES_DISCOUNT = "sales_discount"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _system_account(tenant_id: uuid.UUID, system_key: str) -> Account:
    """Resolve a wired-in account by ``system_key``, never by code.

    Failing loudly here is correct: posting a bad-debt write-off to a guessed
    account is far more expensive to unpick than a refused request telling the
    tenant to finish their chart-of-accounts setup.
    """
    account = Account.all_tenants.filter(
        tenant_id=tenant_id, system_key=system_key, is_active=True
    ).first()
    if account is None:
        raise ValidationError(
            f"No active account is mapped to system key '{system_key}' for this "
            f"tenant. Configure it in the chart of accounts before posting."
        )
    return account


def _allocate_invoice_number(tenant_id: uuid.UUID, issue_date: date) -> str:
    """Take the next gapless invoice number for (tenant, year).

    Uses the locked counter row rather than ``MAX(number) + 1``: under READ
    COMMITTED the latter hands the same number to two concurrent issuers, and
    the duplicate surfaces only at the unique index, after the GL entry has
    already been posted inside the same transaction.

    Because the counter is incremented inside the caller's transaction, a
    rollback returns the number — which is exactly what "gapless" requires,
    and exactly what a raw PostgreSQL SEQUENCE cannot give.
    """
    from apps.accounting.models_sequence import DocumentSequence

    year = issue_date.year
    seq, _ = DocumentSequence.all_tenants.select_for_update().get_or_create(
        tenant_id=tenant_id,
        scope="invoice",
        year=year,
        defaults={"next_value": 1, "prefix": "INV"},
    )
    value = seq.next_value
    seq.next_value = value + 1
    seq.save(update_fields=["next_value", "updated_at"])
    return seq.format(value)


def _locked_invoice(invoice_id: uuid.UUID, tenant_id: uuid.UUID) -> Invoice:
    """Re-read the invoice under ``FOR UPDATE``.

    The caller almost always already holds an ``Invoice`` instance, and using
    it would be wrong: it was loaded before the transaction started, so its
    ``status`` and ``amount_paid`` may already be stale. Re-reading under the
    lock is what makes "check then act" atomic instead of hopeful.
    """
    invoice = (
        Invoice.all_tenants.select_for_update()
        .filter(pk=invoice_id, tenant_id=tenant_id)
        .first()
    )
    if invoice is None:
        raise ValidationError(f"Invoice {invoice_id} not found in this tenant.")
    return invoice


def _set_status(invoice: Invoice, new_status: str, **extra_fields) -> None:
    """Validate and apply a transition, writing only the fields that changed."""
    invoice.assert_can_transition(new_status)
    invoice.status = new_status
    for name, value in extra_fields.items():
        setattr(invoice, name, value)
    invoice.save(update_fields=["status", *extra_fields.keys(), "updated_at"])


# ---------------------------------------------------------------------------
# GL draft construction
# ---------------------------------------------------------------------------

def build_invoice_entry(invoice: Invoice, lines: Optional[Iterable[InvoiceLine]] = None):
    """Build the issuing entry for ``invoice``.

    ::

        Dr  Accounts receivable          total_amount
        Dr  Sales discount (contra)      discount_amount
            Cr  Income (per line)                     line_subtotal
            Cr  Tax payable (per rate)                line_tax

    Which balances because ``total = subtotal - discount + tax``, the identity
    the ``ck_invoice_total_identity`` constraint holds true on every row.

    Revenue is credited **per line account**, not as one lump to a default
    income account: the whole reason a line carries ``income_account`` is that
    a single invoice can span product revenue, service revenue and
    reimbursable costs, and a P&L that merges them is a P&L nobody trusts.

    Tax is grouped by the tax rate's ``collected_account`` before posting.
    One credit line per invoice line would produce a twenty-line VAT posting
    for a twenty-line invoice, and the VAT return only ever needs the total
    per rate. Grouping also avoids twenty separate roundings where one is
    correct — each group is quantized once, at this boundary.
    """
    # ``all_tenants`` + an explicit tenant filter, not ``invoice.lines``: the
    # reverse manager inherits the tenant-filtered default manager, which
    # returns nothing when a Celery task has no ambient tenant bound. Services
    # take tenant_id explicitly precisely so they do not depend on that.
    lines = list(
        lines
        if lines is not None
        else InvoiceLine.all_tenants.filter(
            tenant_id=invoice.tenant_id, invoice_id=invoice.id
        ).select_related("tax_rate").order_by("line_number")
    )
    if not lines:
        raise ValidationError("Cannot issue an invoice with no lines.")

    currency = invoice.currency
    draft = JournalEntryDraft(
        journal_code=SALES_JOURNAL_CODE,
        entry_date=invoice.issue_date,
        currency=currency,
        exchange_rate=invoice.exchange_rate,
        memo=f"Invoice {invoice.number} — {invoice.customer.name}"[:500],
        source=JournalEntry.Source.INVOICE,
        source_document_type="sales.Invoice",
        source_document_id=invoice.id,
        # Re-running issue_invoice after a partial failure must not create a
        # second GL entry for the same invoice.
        idempotency_key=f"invoice:issue:{invoice.id}",
    )

    receivable_account_id = invoice.customer.receivable_account_id
    draft.debit(
        receivable_account_id,
        quantize_currency(invoice.total_amount, currency),
        description=f"Invoice {invoice.number}",
        partner_type="customer",
        partner_id=invoice.customer_id,
    )

    if invoice.discount_amount > ZERO:
        # Contra-revenue, debited. Netting the discount off the income credit
        # instead would hide gross sales and make discount analysis impossible.
        discount_account = _system_account(invoice.tenant_id, SYSTEM_KEY_SALES_DISCOUNT)
        draft.debit(
            discount_account.id,
            quantize_currency(invoice.discount_amount, currency),
            description=f"Discount on {invoice.number}",
            partner_type="customer",
            partner_id=invoice.customer_id,
        )

    for line in lines:
        if line.line_subtotal <= ZERO:
            continue  # A zero-value line (a note, a free item) posts nothing.
        draft.credit(
            line.income_account_id,
            quantize_currency(line.line_subtotal, currency),
            description=line.description[:500],
            partner_type="customer",
            partner_id=invoice.customer_id,
            project_id=line.project_id or invoice.project_id,
        )

    # Group tax by the account it is collected into, then round once per group.
    tax_by_account: dict[uuid.UUID, Decimal] = {}
    tax_rate_of_account: dict[uuid.UUID, uuid.UUID] = {}
    for line in lines:
        if line.line_tax <= ZERO or line.tax_rate_id is None:
            continue
        account_id = line.tax_rate.collected_account_id
        tax_by_account[account_id] = tax_by_account.get(account_id, ZERO) + line.line_tax
        tax_rate_of_account.setdefault(account_id, line.tax_rate_id)

    for account_id, amount in tax_by_account.items():
        draft.credit(
            account_id,
            quantize_currency(amount, currency),
            description=f"Output tax on {invoice.number}",
            partner_type="customer",
            partner_id=invoice.customer_id,
            tax_rate_id=tax_rate_of_account[account_id],
        )

    return draft


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

@transaction.atomic
def issue_invoice(
    invoice_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    mark_sent_at: Optional[datetime] = None,
) -> Invoice:
    """``DRAFT -> SENT``: allocate the number, post to the GL, release stock.

    All three effects share one transaction on purpose. An invoice that has a
    number but no journal entry misstates the ledger; one that has a journal
    entry but no stock movement misstates inventory; one that moved stock and
    then failed to commit has sold goods that are still on the shelf. Either
    the customer owes us money and the goods have left, or nothing happened.
    """
    invoice = _locked_invoice(invoice_id, tenant_id)
    invoice.assert_can_transition(Invoice.Status.SENT)

    if invoice.total_amount <= ZERO:
        raise ValidationError("Refusing to issue a zero-value invoice.")
    if invoice.journal_entry_id is not None:
        raise ValidationError(
            f"Invoice {invoice.number} already has journal entry "
            f"{invoice.journal_entry_id}; it cannot be issued twice."
        )

    lines = list(
        InvoiceLine.all_tenants.filter(tenant_id=tenant_id, invoice_id=invoice.id)
        .select_related("tax_rate")
        .order_by("line_number")
    )
    if not lines:
        raise ValidationError("Cannot issue an invoice with no lines.")

    # The number is taken *inside* the transaction so a later failure rolls it
    # back and the sequence stays gapless.
    invoice.number = _allocate_invoice_number(tenant_id, invoice.issue_date)

    entry = post_entry(
        build_invoice_entry(invoice, lines),
        tenant_id=tenant_id,
        user_id=user_id,
    )

    _release_stock_for_invoice(invoice, lines, user_id=user_id)

    invoice.journal_entry = entry
    invoice.status = Invoice.Status.SENT
    invoice.sent_at = mark_sent_at or timezone.now()
    invoice.updated_by_id = user_id
    invoice.save(
        update_fields=[
            "number",
            "journal_entry",
            "status",
            "sent_at",
            "updated_by",
            "updated_at",
        ]
    )
    return invoice


def _release_stock_for_invoice(
    invoice: Invoice,
    lines: list[InvoiceLine],
    *,
    user_id: Optional[uuid.UUID],
) -> None:
    """Hand stock-bearing lines to the inventory service.

    Contract expected of ``apps.inventory.services.stock.issue_stock``:
    it decrements on-hand quantity, values the movement under the tenant's
    costing method (FIFO / weighted average) and posts its **own** COGS entry
    (``Dr Cost of goods sold / Cr Inventory``). That posting is separate from
    the revenue entry above because they answer different questions and,
    under some revenue-recognition policies, happen on different dates.

    Called inside the caller's ``atomic`` block so a stock failure rolls back
    the invoice issue too. Imported lazily: ``inventory`` imports ``sales`` for
    its own document links, and a module-level import closes the cycle.
    """
    stock_lines = [line for line in lines if line.item_id is not None]
    if not stock_lines:
        return

    from apps.inventory.services.stock import issue_stock  # local: avoids a cycle

    issue_stock(
        tenant_id=invoice.tenant_id,
        occurred_on=invoice.issue_date,
        document_type="sales.Invoice",
        document_id=invoice.id,
        reference=invoice.number,
        user_id=user_id,
        lines=[
            {
                "item_id": line.item_id,
                "quantity": line.quantity,
                "project_id": line.project_id or invoice.project_id,
                "source_line_id": line.id,
            }
            for line in stock_lines
        ],
    )


@transaction.atomic
def apply_payment(
    invoice_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
) -> Invoice:
    """Recompute an invoice's paid/due figures from its applications.

    Deliberately a *recomputation*, not an increment. ``amount_paid += x`` is
    the read-modify-write that loses concurrent payments, and it also drifts:
    a reversed application, a re-applied receipt or a manual correction each
    leave the running total slightly wrong, and nothing ever notices because
    there is no second source to compare against. Summing the
    ``PaymentApplication`` rows makes those rows the single source of truth
    and this column a cache of them — the same relationship
    ``Account.cached_balance`` has to ``JournalLine``.

    The row lock is what makes it correct: without it, two concurrent
    applications both sum a snapshot that excludes the other's row.

    Status derivation follows the money and is never passed in by the caller:

    * ``paid >= total``     -> ``PAID``
    * ``0 < paid < total``  -> ``PARTIALLY_PAID`` (or ``OVERDUE`` if late)
    * ``paid == 0``         -> ``SENT`` (or ``OVERDUE`` if late)

    The last case is the payment-reversal path: a bounced cheque removes the
    application, the sum drops back to zero, and a previously ``PAID`` invoice
    correctly returns to the collection process. It is the only route out of
    ``PAID``, which is why no "mark as unpaid" verb exists.
    """
    from apps.payments.models import PaymentApplication  # local: avoids a cycle

    invoice = _locked_invoice(invoice_id, tenant_id)
    if invoice.status in {
        Invoice.Status.DRAFT,
        Invoice.Status.VOIDED,
        Invoice.Status.WRITTEN_OFF,
    }:
        raise ValidationError(
            f"Invoice {invoice.number or invoice.id} is {invoice.status}; "
            f"payments cannot be applied to it."
        )

    total_applied = (
        PaymentApplication.all_tenants.filter(
            tenant_id=tenant_id, invoice_id=invoice.id, is_reversed=False
        ).aggregate(total=Sum("amount"))["total"]
        or ZERO
    )
    amount_paid = quantize_currency(to_money(total_applied), invoice.currency)

    if amount_paid > invoice.total_amount:
        # The DB constraint would reject this anyway; failing here names the
        # cause instead of surfacing an opaque IntegrityError.
        raise ValidationError(
            f"Applications total {amount_paid} exceed invoice total "
            f"{invoice.total_amount}. The surplus belongs on the payment as "
            f"unapplied credit, not on the invoice."
        )

    invoice.amount_paid = amount_paid
    invoice.amount_due = quantize_currency(
        invoice.total_amount - amount_paid, invoice.currency
    )

    today = timezone.localdate()
    if invoice.amount_due <= ZERO:
        new_status = Invoice.Status.PAID
        invoice.paid_at = invoice.paid_at or timezone.now()
    elif amount_paid > ZERO:
        new_status = (
            Invoice.Status.OVERDUE
            if invoice.due_date < today
            else Invoice.Status.PARTIALLY_PAID
        )
        invoice.paid_at = None
    else:
        new_status = (
            Invoice.Status.OVERDUE if invoice.due_date < today else Invoice.Status.SENT
        )
        invoice.paid_at = None

    if new_status != invoice.status:
        invoice.assert_can_transition(new_status)
        invoice.status = new_status

    invoice.updated_by_id = user_id
    invoice.save(
        update_fields=[
            "amount_paid",
            "amount_due",
            "status",
            "paid_at",
            "updated_by",
            "updated_at",
        ]
    )
    return invoice


@transaction.atomic
def void_invoice(
    invoice_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    reason: str,
    user_id: Optional[uuid.UUID] = None,
) -> Invoice:
    """Cancel an invoice that should never have existed.

    Void, not delete: the number stays consumed. A gap in an invoice sequence
    is what a tax auditor looks for first, and "we deleted it" is not an
    answer any jurisdiction accepts.

    Refused once any cash has been received against it. A void erases the
    document; the received money would then be sitting against nothing. The
    correct instrument there is a credit note (which is itself a document, and
    reconciles) plus a refund.
    """
    invoice = _locked_invoice(invoice_id, tenant_id)
    invoice.assert_can_transition(Invoice.Status.VOIDED)

    if not reason.strip():
        raise ValidationError("A void reason is required for the audit trail.")
    if invoice.amount_paid > ZERO:
        raise ValidationError(
            f"Invoice {invoice.number} has {invoice.amount_paid} "
            f"{invoice.currency} applied against it and cannot be voided. "
            f"Issue a credit note and refund the payment instead."
        )

    if invoice.journal_entry_id is not None:
        # Delegated so the period lock and the cached-balance unwind stay in
        # one place. It raises if the period has closed — at which point the
        # correct action is a credit note, not a void.
        void_entry(invoice.journal_entry, reason=reason, user_id=user_id)

    _set_status(
        invoice,
        Invoice.Status.VOIDED,
        void_reason=reason[:255],
        updated_by_id=user_id,
    )
    return invoice


@transaction.atomic
def write_off_invoice(
    invoice_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    reason: str,
    write_off_date: Optional[date] = None,
    user_id: Optional[uuid.UUID] = None,
) -> Invoice:
    """Recognise that the remaining balance will never be collected.

    ::

        Dr  Bad debt expense       amount_due
            Cr  Accounts receivable            amount_due

    Distinct from a void in every way that matters. A void says the invoice
    was a mistake and removes it from the books. A write-off says the sale was
    real, the revenue stays recognised, and the *asset* is worthless — which
    is a cost, and belongs in the P&L where the tenant can see how much of
    their revenue they never collected.

    Only the open balance is written off, so a partially paid invoice keeps
    the cash it did collect.
    """
    invoice = _locked_invoice(invoice_id, tenant_id)
    invoice.assert_can_transition(Invoice.Status.WRITTEN_OFF)

    if not reason.strip():
        raise ValidationError("A write-off reason is required for the audit trail.")
    if invoice.amount_due <= ZERO:
        raise ValidationError(
            f"Invoice {invoice.number} has nothing outstanding to write off."
        )

    entry_date = write_off_date or timezone.localdate()
    amount = quantize_currency(invoice.amount_due, invoice.currency)
    bad_debt = _system_account(tenant_id, SYSTEM_KEY_BAD_DEBT)

    draft = JournalEntryDraft(
        journal_code=SALES_JOURNAL_CODE,
        entry_date=entry_date,
        currency=invoice.currency,
        exchange_rate=invoice.exchange_rate,
        memo=f"Write-off {invoice.number}: {reason}"[:500],
        source=JournalEntry.Source.INVOICE,
        source_document_type="sales.Invoice",
        source_document_id=invoice.id,
        idempotency_key=f"invoice:writeoff:{invoice.id}",
    )
    draft.debit(
        bad_debt.id,
        amount,
        description=f"Bad debt — {invoice.number}",
        partner_type="customer",
        partner_id=invoice.customer_id,
    )
    draft.credit(
        invoice.customer.receivable_account_id,
        amount,
        description=f"Write-off of {invoice.number}",
        partner_type="customer",
        partner_id=invoice.customer_id,
    )
    post_entry(draft, tenant_id=tenant_id, user_id=user_id)

    # The receivable is gone from the balance sheet, so nothing is due any
    # more. ``ck_invoice_due_identity`` forces amount_paid up to total_amount
    # to keep due == total - paid true, which is a deliberate trade: the
    # column now means "settled by cash or by write-off", and the cash figure
    # stays recoverable from the PaymentApplication rows, which remain the
    # source of truth. That is also why apply_payment refuses to run against a
    # written-off invoice — it would recompute this column back down.
    invoice.amount_paid = invoice.total_amount
    invoice.amount_due = ZERO
    invoice.written_off_at = timezone.now()
    invoice.assert_can_transition(Invoice.Status.WRITTEN_OFF)
    invoice.status = Invoice.Status.WRITTEN_OFF
    invoice.void_reason = reason[:255]
    invoice.updated_by_id = user_id
    invoice.save(
        update_fields=[
            "amount_paid",
            "amount_due",
            "status",
            "written_off_at",
            "void_reason",
            "updated_by",
            "updated_at",
        ]
    )
    return invoice


def _status_after_overdue():
    """The status an invoice returns to when it stops being overdue.

    ``PAID`` if settled, ``PARTIALLY_PAID`` if some cash arrived, else
    ``SENT`` — evaluated in SQL as a single ``CASE`` so the un-lating sweep
    stays one statement instead of one query per invoice.
    """
    return Case(
        When(amount_due__lte=ZERO, then=Value(Invoice.Status.PAID)),
        When(amount_paid__gt=ZERO, then=Value(Invoice.Status.PARTIALLY_PAID)),
        default=Value(Invoice.Status.SENT),
        output_field=Invoice._meta.get_field("status"),
    )


@transaction.atomic
def refresh_overdue_status(
    *, tenant_id: uuid.UUID, as_of: Optional[date] = None
) -> dict[str, int]:
    """Recompute the derived ``OVERDUE`` flag for a whole tenant. Idempotent.

    Run by a scheduled job once per day, per tenant, in the tenant's own time
    zone — which is why ``as_of`` is a parameter rather than
    ``timezone.localdate()`` inline. An invoice due on the 31st is not late in
    Cairo while it is still the 31st in Cairo, regardless of where the server
    is.

    Two bulk ``UPDATE``s, not a loop over model instances. A tenant with
    200,000 open invoices would otherwise mean 200,000 round trips and a
    transaction held open for minutes; this is two statements against the
    partial index ``ix_invoice_open_balance``. The cost is that
    ``assert_can_transition`` is not consulted per row — acceptable here, and
    only here, because the ``status__in`` filters *are* the transition map for
    this pair of moves, expressed in SQL.

    Both directions are applied. Lateness is derived, so it must be able to go
    away again: extending a due date, or a payment arriving, un-lates the
    invoice. A one-way sweep leaves invoices stuck in ``OVERDUE`` forever,
    which is the bug that makes people distrust the aging report.
    """
    as_of = as_of or timezone.localdate()
    base = Invoice.all_tenants.filter(tenant_id=tenant_id)

    became_overdue = base.filter(
        status__in=[Invoice.Status.SENT, Invoice.Status.PARTIALLY_PAID],
        due_date__lt=as_of,
        amount_due__gt=ZERO,
    ).update(status=Invoice.Status.OVERDUE, updated_at=timezone.now())

    # Back out of OVERDUE: the due date moved, or money arrived.
    no_longer_overdue = base.filter(
        Q(due_date__gte=as_of) | Q(amount_due__lte=ZERO),
        status=Invoice.Status.OVERDUE,
    ).update(
        status=_status_after_overdue(),
        updated_at=timezone.now(),
    )

    return {"became_overdue": became_overdue, "no_longer_overdue": no_longer_overdue}
