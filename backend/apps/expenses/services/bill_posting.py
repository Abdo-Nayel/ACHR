"""Post a vendor bill to the general ledger, and settle it.

The hole this fills
-------------------
``BillViewSet.post_to_ledger`` raised ``NotImplementedYet`` with an accurate
note about what was needed. Everything else in accounts payable was built
around a posting that did not exist: ``Bill`` carries ``journal_entry``, a full
``DRAFT -> AWAITING_APPROVAL -> APPROVED -> PARTIALLY_PAID -> PAID`` lifecycle
and a ``ck_bill_due_identity`` constraint, and ``BillPaymentViewSet`` is
read-only "until the AP payment service exists".

The consequence was that approving a bill recorded an obligation nowhere. The
P&L understated cost by the whole AP backlog and the balance sheet showed no
creditors — while the Bills screen listed them, so the two disagreed and only
the screen was right.

The entry
---------
Accrual, at approval::

    Dr  Expense / inventory account   per line, net of tax
    Dr  Input VAT                     recoverable tax
        Cr  Accounts payable                     total
        Cr  Withholding tax payable              withheld at source

Withholding is credited *here*, not at payment, because the liability to the
tax authority arises when the invoice is accepted, not when the vendor is
paid. Netting it into the AP credit would overstate what is owed to the vendor
and hide a statutory debt entirely.

Per-line expense accounts, not one lump: ``BillLine.expense_account`` exists so
a single bill can hit several cost accounts, which is the normal shape of a
utility or a logistics invoice. Collapsing them would make the cost centre
analysis in every downstream report wrong in a way no total would reveal.

Settlement is a separate entry when the vendor is actually paid::

    Dr  Accounts payable      amount paid
        Cr  Bank / cash                  amount paid

Everything goes through ``accounting.services.posting.post_entry`` — the one
choke point that verifies ``sum(debits) == sum(credits)``, locks the fiscal
period, allocates the number and enforces idempotency (CONVENTIONS §7).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounting.models import Account, JournalEntry
from apps.accounting.services.posting import (
    JournalEntryDraft,
    LineDraft,
    post_entry,
)
from apps.core.fields import ZERO
from apps.expenses.models import Bill, BillLine, BillPayment, Expense

#: Vendor bills belong in the purchases journal, beside expense claims.
BILL_JOURNAL_CODE = "PUR"

AP_CONTROL_KEY = "ap_control"
INPUT_VAT_KEY = "input_vat"
#: Tax withheld from a supplier and owed to the authority. Falls back to the
#: payroll income-tax liability only because both are "tax collected on
#: someone else's behalf"; a chart that distinguishes them should carry the
#: first key.
WITHHOLDING_KEYS = ("withholding_tax_payable", "payroll_income_tax_payable")


class BillPostingError(ValidationError):
    """Raised when a bill cannot be posted. Its own class so a caller can tell
    a chart-configuration problem from a lifecycle problem."""


def _account_id(tenant_id: uuid.UUID, *system_keys: str) -> Optional[uuid.UUID]:
    for key in system_keys:
        found = (
            Account.all_tenants.filter(
                tenant_id=tenant_id, system_key=key, is_active=True
            )
            .values_list("id", flat=True)
            .first()
        )
        if found is not None:
            return found
    return None


def _payable_account_id(bill: Bill) -> uuid.UUID:
    """The vendor's own payable account, or the chart's AP control."""
    vendor_account = getattr(bill.vendor, "payable_account_id", None)
    if vendor_account:
        return vendor_account
    found = _account_id(bill.tenant_id, AP_CONTROL_KEY)
    if found is None:
        raise BillPostingError(
            f"No accounts-payable account is configured (system_key "
            f"'{AP_CONTROL_KEY}'), and vendor {bill.vendor} has none of its "
            f"own. A bill cannot be recorded without somewhere to owe it."
        )
    return found


def build_bill_entry(bill: Bill, lines: list[BillLine]) -> JournalEntryDraft:
    """Dr cost / Dr input VAT / Cr payable (+ Cr withholding)."""
    if bill.total_amount <= ZERO:
        raise BillPostingError(
            "Refusing to post a zero-value bill: it creates no obligation and "
            "would leave an entry that explains nothing."
        )
    if not lines:
        raise BillPostingError("Cannot post a bill with no lines.")

    draft = JournalEntryDraft(
        journal_code=BILL_JOURNAL_CODE,
        entry_date=bill.bill_date,
        currency=bill.currency,
        exchange_rate=bill.exchange_rate or Decimal("1"),
        memo=f"Bill {bill.number or bill.id} — {bill.vendor}"[:255],
        source=JournalEntry.Source.BILL,
        source_document_type="expenses.Bill",
        source_document_id=bill.id,
        # Keyed on the bill: an approval retried after a timeout returns the
        # original entry rather than double-counting the cost and the debt.
        idempotency_key=f"bill:{bill.id}",
    )

    for line in lines:
        # ``BillLine.expense_account`` is NOT NULL, so a persisted line always
        # has one and this guard cannot fire for a row read from the database.
        # It is here for the in-memory path — an importer or a future bulk
        # entry endpoint building lines before saving them — where the cost
        # having nowhere to go must stop the posting rather than produce a
        # one-sided entry that fails later with an unrelated message.
        account_id = line.expense_account_id
        if account_id is None:
            raise BillPostingError(
                f"Line {line.line_number} of bill {bill.number or bill.id} has "
                f"no expense account. The cost has nowhere to go."
            )
        if line.line_subtotal <= ZERO:
            continue
        draft.add(LineDraft(
            account_id=account_id,
            debit=line.line_subtotal,
            description=(line.description or "")[:500],
            partner_type="vendor",
            partner_id=bill.vendor_id,
            project_id=line.project_id or bill.project_id,
            tax_rate_id=line.tax_rate_id,
        ))

    tax = bill.tax_amount or ZERO
    if tax > ZERO:
        vat_id = _account_id(bill.tenant_id, INPUT_VAT_KEY)
        if vat_id is None:
            raise BillPostingError(
                f"Bill {bill.number or bill.id} carries {tax} of tax but this "
                f"chart has no '{INPUT_VAT_KEY}' account. Absorbing it into "
                f"cost would overstate the expense and forfeit the reclaim."
            )
        draft.add(LineDraft(
            account_id=vat_id, debit=tax, description="Recoverable input VAT"
        ))

    withheld = bill.withholding_amount or ZERO
    if withheld > ZERO:
        wht_id = _account_id(bill.tenant_id, *WITHHOLDING_KEYS)
        if wht_id is None:
            raise BillPostingError(
                f"Bill {bill.number or bill.id} withholds {withheld} but this "
                f"chart has no '{WITHHOLDING_KEYS[0]}' account. The amount is "
                f"owed to the tax authority and must land somewhere."
            )
        draft.add(LineDraft(
            account_id=wht_id, credit=withheld,
            description="Tax withheld at source",
            partner_type="vendor", partner_id=bill.vendor_id,
        ))

    # The vendor is owed the total less anything withheld from them.
    draft.add(LineDraft(
        account_id=_payable_account_id(bill),
        credit=bill.total_amount - withheld,
        description=f"Payable to {bill.vendor}"[:500],
        partner_type="vendor",
        partner_id=bill.vendor_id,
    ))

    return draft


@transaction.atomic
def post_bill(bill: Bill, *, user_id: Optional[uuid.UUID] = None) -> JournalEntry:
    """Record the obligation. Called from the approve transition."""
    if bill.journal_entry_id is not None:
        raise BillPostingError(
            f"Bill {bill.number or bill.id} already has journal entry "
            f"{bill.journal_entry_id}; posting again would double count both "
            f"the cost and the debt."
        )
    if bill.status == Bill.Status.VOIDED:
        raise BillPostingError(
            f"Bill {bill.number or bill.id} is voided and cannot be posted."
        )

    lines = list(
        BillLine.all_tenants.filter(tenant_id=bill.tenant_id, bill_id=bill.id)
        .select_related("category")
        .order_by("line_number")
    )
    entry = post_entry(
        build_bill_entry(bill, lines),
        tenant_id=bill.tenant_id,
        user_id=user_id,
    )
    bill.journal_entry = entry
    bill.updated_by_id = user_id
    bill.save(update_fields=["journal_entry", "updated_by", "updated_at"])
    return entry


@transaction.atomic
def pay_bill(
    bill: Bill,
    *,
    amount: Decimal,
    paid_from_account_id: uuid.UUID,
    payment_date=None,
    reference: str = "",
    payment_method: str = "",
    user_id: Optional[uuid.UUID] = None,
) -> JournalEntry:
    """Settle all or part of a posted bill: Dr payable / Cr bank.

    Recomputes ``amount_paid`` and the status from the payment rather than
    trusting the caller, and re-reads the bill under a row lock first: two
    concurrent payments that each read ``amount_paid = 0`` would otherwise
    both succeed and overpay the vendor, which ``ck_bill_no_overpayment``
    would catch only for the second one to commit.
    """
    locked = (
        Bill.all_tenants.select_for_update()
        .filter(pk=bill.pk, tenant_id=bill.tenant_id)
        .first()
    )
    if locked is None:  # pragma: no cover - defensive
        raise BillPostingError("Bill disappeared while being paid.")

    if locked.journal_entry_id is None:
        raise BillPostingError(
            f"Bill {locked.number or locked.id} has not been posted to the "
            f"ledger, so there is no payable to settle. Approve it first."
        )
    if locked.status == Bill.Status.VOIDED:
        raise BillPostingError("A voided bill cannot be paid.")
    if amount <= ZERO:
        raise BillPostingError("A payment must move a positive amount.")
    outstanding = locked.total_amount - locked.amount_paid
    if amount > outstanding:
        raise BillPostingError(
            f"Payment of {amount} exceeds the {outstanding} still outstanding "
            f"on bill {locked.number or locked.id}."
        )

    when = payment_date or timezone.now().date()
    draft = JournalEntryDraft(
        journal_code=BILL_JOURNAL_CODE,
        entry_date=when,
        currency=locked.currency,
        exchange_rate=locked.exchange_rate or Decimal("1"),
        memo=f"Payment for bill {locked.number or locked.id}"[:255],
        source=JournalEntry.Source.PAYMENT,
        source_document_type="expenses.Bill",
        source_document_id=locked.id,
        # Includes the running paid figure, so two *different* part payments
        # of the same amount are distinct events while a retry of one is not.
        idempotency_key=f"bill-payment:{locked.id}:{locked.amount_paid}:{amount}",
        lines=[
            LineDraft(
                account_id=_payable_account_id(locked),
                debit=amount,
                description=f"Settle bill {locked.number or locked.id}"[:500],
                partner_type="vendor",
                partner_id=locked.vendor_id,
            ),
            LineDraft(
                account_id=paid_from_account_id,
                credit=amount,
                description=(reference or f"Payment to {locked.vendor}")[:500],
            ),
        ],
    )
    entry = post_entry(draft, tenant_id=locked.tenant_id, user_id=user_id)

    # The payment *record*, not just the ledger entry.
    #
    # Without this the journal was correct and "Payments Made" was permanently
    # empty: the screen lists BillPayment rows, and nothing created any. The
    # ledger knew money had moved; accounts payable could not tell you which
    # payment discharged which bill, or produce a remittance advice.
    BillPayment.objects.create(
        tenant_id=locked.tenant_id,
        bill=locked,
        vendor=locked.vendor,
        payment_date=when,
        currency=locked.currency,
        exchange_rate=locked.exchange_rate or Decimal("1"),
        amount=amount,
        # Withholding is deducted from the bill at accrual, not at payment, so
        # the cash leaving equals the amount settled.
        withholding_amount=ZERO,
        cash_amount=amount,
        # BillPayment reuses Expense.PaymentMethod rather than declaring its
        # own — one vocabulary for "how did the money move" across the module.
        payment_method=payment_method or Expense.PaymentMethod.BANK_TRANSFER,
        paid_from_account_id=paid_from_account_id,
        reference=reference[:255],
        status=BillPayment.Status.PAID,
        journal_entry=entry,
        created_by_id=user_id,
        updated_by_id=user_id,
    )

    locked.amount_paid = locked.amount_paid + amount
    locked.amount_due = locked.total_amount - locked.amount_paid
    if locked.amount_due == ZERO:
        locked.status = Bill.Status.PAID
        locked.paid_at = timezone.now()
    else:
        locked.status = Bill.Status.PARTIALLY_PAID
    locked.updated_by_id = user_id
    locked.save(update_fields=[
        "amount_paid", "amount_due", "status", "paid_at",
        "updated_by", "updated_at",
    ])
    return entry


__all__ = [
    "BILL_JOURNAL_CODE",
    "BillPostingError",
    "build_bill_entry",
    "post_bill",
    "pay_bill",
]
