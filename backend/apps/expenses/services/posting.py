"""Post an expense claim to the general ledger, and pay the claimant back.

Why this module did not exist
-----------------------------
``Expense`` has carried ``journal_entry`` and ``reimbursement_entry`` columns,
a required ``paid_from_account``, and a required ``ExpenseCategory.
expense_account`` since its first migration. ``JournalEntry.Source.EXPENSE``
was already a choice. Everything was in place except the function that joins
them, so approved expenses accumulated as documents with no ledger effect:
the P&L understated costs by the whole claim backlog, and
``docs/09-verification-report.md``'s "expenses integrate with accounting"
described columns rather than behaviour.

The two entries, and why they are two
-------------------------------------
An expense can involve the company's money or the employee's, and the ledger
has to distinguish them because they create different obligations.

**Non-reimbursable** -- company card, direct debit, petty cash. One entry, at
approval, and the money is already gone::

    Dr  Expense account        net amount
    Dr  Input VAT              tax amount        (recoverable, so an asset)
        Cr  paid_from_account                    gross amount

**Reimbursable** -- the employee paid and is owed. Two entries, because
approval and payment are separate events that can be days apart and must both
be visible::

    on approve:   Dr Expense account     net
                  Dr Input VAT           tax
                      Cr Employee payable        gross
    on reimburse: Dr Employee payable    gross
                      Cr paid_from_account       gross

Collapsing those into one entry at reimbursement time would leave an approved
liability off the balance sheet for as long as finance takes to run the
payment batch -- which is exactly the period in which someone asks what the
company owes.

Input VAT is debited, not netted into the expense, because it is recoverable
from the tax authority: it is an asset, and burying it in the cost centre both
overstates the expense and loses the reclaim.

Everything goes through ``accounting.services.posting.post_entry`` -- the one
choke point that verifies ``sum(debits) == sum(credits)``, resolves and locks
the fiscal period, allocates the entry number and enforces idempotency
(CONVENTIONS §7). This module builds drafts; it never writes ``JournalLine``.
"""

from __future__ import annotations

import uuid
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
from apps.expenses.models import Expense

#: Expenses are purchases, so they belong in the purchases journal beside
#: vendor bills rather than in the general journal. A reviewer scanning PUR
#: sees everything the company bought, however it was paid for.
EXPENSE_JOURNAL_CODE = "PUR"

#: Where the credit goes when the employee is out of pocket. Tried in order:
#: a chart may model employee reimbursements as their own control account, or
#: fold them into the general "owed to staff" account. Resolving by
#: ``system_key`` rather than by code is required -- account codes differ
#: between national charts, which is why ``system_key`` exists at all.
#: The first is the purpose-built account ``seed_chart_of_accounts`` creates.
#: The others are the fallback for tenants whose chart was seeded before it
#: existed: "owed to staff" is the right *kind* of account, so folding
#: reimbursements into salaries payable is defensible and reconcilable, where
#: crediting the bank would not be. Note the ``payroll_`` prefix -- these are
#: the literal ``system_key`` values, not the historical aliases in
#: ``SYSTEM_KEY_ALIASES``, which are not what is stored on the row.
REIMBURSEMENT_PAYABLE_KEYS = (
    "employee_reimbursements_payable",
    "payroll_salaries_payable",
    "payroll_other_deductions_payable",
)

INPUT_VAT_KEY = "input_vat"


class ExpensePostingError(ValidationError):
    """Raised when an expense cannot be posted. Its own class so a caller can
    tell a chart-configuration problem from a transition problem."""


def _account_id(tenant_id: uuid.UUID, *system_keys: str) -> Optional[uuid.UUID]:
    """First active account matching any of ``system_keys``, or ``None``."""
    for key in system_keys:
        account_id = (
            Account.all_tenants.filter(
                tenant_id=tenant_id, system_key=key, is_active=True
            )
            .values_list("id", flat=True)
            .first()
        )
        if account_id is not None:
            return account_id
    return None


def _reimbursement_payable_id(tenant_id: uuid.UUID) -> uuid.UUID:
    account_id = _account_id(tenant_id, *REIMBURSEMENT_PAYABLE_KEYS)
    if account_id is None:
        raise ExpensePostingError(
            "This tenant's chart has no account for money owed to employees. "
            "Configure one with system_key "
            f"'{REIMBURSEMENT_PAYABLE_KEYS[0]}' before approving reimbursable "
            "expenses -- posting the credit to the bank instead would record "
            "a payment that has not happened."
        )
    return account_id


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

def build_expense_entry(expense: Expense) -> JournalEntryDraft:
    """The accrual entry: recognise the cost and whatever it created.

    Split net/tax rather than posting the gross to the expense account, so
    that the expense account reads as cost and the VAT account reads as a
    reclaim. ``expense.amount`` is documented as net and ``total_amount`` as
    gross, and the model's own CHECK constraint keeps
    ``total = amount + tax``, so this cannot silently drift.
    """
    tenant_id = expense.tenant_id
    net = expense.amount
    tax = expense.tax_amount or ZERO

    if expense.total_amount <= ZERO:
        raise ExpensePostingError(
            "Refusing to post a zero-value expense: it has no ledger effect "
            "and would leave an entry that explains nothing."
        )

    credit_account_id = (
        _reimbursement_payable_id(tenant_id)
        if expense.is_reimbursable
        else expense.paid_from_account_id
    )

    draft = JournalEntryDraft(
        journal_code=EXPENSE_JOURNAL_CODE,
        entry_date=expense.expense_date,
        currency=expense.currency,
        exchange_rate=expense.exchange_rate or 1,
        memo=(
            f"Expense {expense.number or expense.id} — "
            f"{expense.description or expense.category.name}"
        )[:255],
        source=JournalEntry.Source.EXPENSE,
        source_document_type="expenses.Expense",
        source_document_id=expense.id,
        # Keyed on the expense, not on the request: an approve retried after a
        # timeout must return the original entry rather than double-count the
        # cost.
        idempotency_key=f"expense:{expense.id}",
    )

    draft.add(LineDraft(
        account_id=expense.category.expense_account_id,
        debit=net,
        description=expense.description or expense.category.name,
        partner_type="vendor" if expense.vendor_id else "",
        partner_id=expense.vendor_id,
        project_id=expense.project_id,
        tax_rate_id=expense.tax_rate_id,
    ))

    if tax > ZERO:
        input_vat_id = _account_id(tenant_id, INPUT_VAT_KEY)
        if input_vat_id is None:
            # Refuse rather than silently absorbing the VAT into the expense:
            # that overstates the cost centre and quietly forfeits the reclaim,
            # and nothing downstream would ever flag it.
            raise ExpensePostingError(
                f"Expense {expense.number or expense.id} carries "
                f"{tax} of tax but this tenant's chart has no account with "
                f"system_key '{INPUT_VAT_KEY}'."
            )
        draft.add(LineDraft(
            account_id=input_vat_id,
            debit=tax,
            description="Recoverable input VAT",
            tax_rate_id=expense.tax_rate_id,
        ))

    draft.add(LineDraft(
        account_id=credit_account_id,
        credit=expense.total_amount,
        description=(
            f"Owed to {expense.employee}" if expense.is_reimbursable
            else f"Paid by {expense.get_payment_method_display()}"
        )[:255],
        partner_type="employee" if expense.is_reimbursable else "",
        partner_id=expense.employee_id if expense.is_reimbursable else None,
    ))

    return draft


def build_reimbursement_entry(expense: Expense) -> JournalEntryDraft:
    """The settlement entry: the liability raised at approval is paid off."""
    return JournalEntryDraft(
        journal_code=EXPENSE_JOURNAL_CODE,
        entry_date=timezone.now().date(),
        currency=expense.currency,
        exchange_rate=expense.exchange_rate or 1,
        memo=f"Reimbursement of expense {expense.number or expense.id}"[:255],
        source=JournalEntry.Source.EXPENSE,
        source_document_type="expenses.Expense",
        source_document_id=expense.id,
        idempotency_key=f"expense-reimbursement:{expense.id}",
        lines=[
            LineDraft(
                account_id=_reimbursement_payable_id(expense.tenant_id),
                debit=expense.total_amount,
                description="Settle employee reimbursement",
                partner_type="employee",
                partner_id=expense.employee_id,
            ),
            LineDraft(
                account_id=expense.paid_from_account_id,
                credit=expense.total_amount,
                description=f"Reimbursed {expense.get_payment_method_display()}",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

@transaction.atomic
def post_expense(
    expense: Expense,
    *,
    user_id: Optional[uuid.UUID] = None,
) -> JournalEntry:
    """Post the accrual entry and attach it to the expense.

    Called from the approve transition inside the caller's transaction: an
    expense that is APPROVED with no entry understates cost, and one with an
    entry but no approval records a cost nobody accepted. Neither half is a
    tolerable resting state.
    """
    if expense.journal_entry_id is not None:
        raise ExpensePostingError(
            f"Expense {expense.number or expense.id} already has journal "
            f"entry {expense.journal_entry_id}; posting again would double "
            f"count the cost."
        )

    entry = post_entry(
        build_expense_entry(expense),
        tenant_id=expense.tenant_id,
        user_id=user_id,
    )
    expense.journal_entry = entry
    expense.updated_by_id = user_id
    expense.save(update_fields=["journal_entry", "updated_by", "updated_at"])
    return entry


@transaction.atomic
def post_reimbursement(
    expense: Expense,
    *,
    user_id: Optional[uuid.UUID] = None,
) -> JournalEntry:
    """Post the settlement entry when the claimant is actually paid."""
    if not expense.is_reimbursable:
        raise ExpensePostingError(
            f"Expense {expense.number or expense.id} is not reimbursable; "
            f"there is no liability to settle."
        )
    if expense.journal_entry_id is None:
        # Without the accrual there is no credit balance to clear, so this
        # entry would debit a payable that was never raised and leave the
        # account negative -- a number that is very hard to explain later.
        raise ExpensePostingError(
            f"Expense {expense.number or expense.id} has not been posted to "
            f"the ledger, so there is nothing to reimburse. Approve it first."
        )
    if expense.reimbursement_entry_id is not None:
        raise ExpensePostingError(
            f"Expense {expense.number or expense.id} was already reimbursed "
            f"by entry {expense.reimbursement_entry_id}."
        )

    entry = post_entry(
        build_reimbursement_entry(expense),
        tenant_id=expense.tenant_id,
        user_id=user_id,
    )
    expense.reimbursement_entry = entry
    expense.updated_by_id = user_id
    expense.save(
        update_fields=["reimbursement_entry", "updated_by", "updated_at"]
    )
    return entry


__all__ = [
    "EXPENSE_JOURNAL_CODE",
    "ExpensePostingError",
    "build_expense_entry",
    "build_reimbursement_entry",
    "post_expense",
    "post_reimbursement",
]
