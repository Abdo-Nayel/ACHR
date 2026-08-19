"""Vendor bills must create a real payable, and settle it exactly.

``BillViewSet.post_to_ledger`` used to raise ``NotImplementedYet``. Everything
around it was built for a posting that did not exist: the ``journal_entry``
column, the seven-state lifecycle, ``ck_bill_due_identity``, and a read-only
``BillPaymentViewSet`` whose docstring said it would stay that way "until the
AP payment service exists". Approving a bill therefore recorded an obligation
nowhere — the Bills screen listed creditors the balance sheet did not have.

The assertions that carry weight here are the ones about *where the credit
goes*. An entry that balances can still put the money in the wrong place, and
for accounts payable the wrong place is usually the bank: that records a
payment nobody made.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.accounting.models import Account, JournalEntry
from apps.core.fields import ZERO
from apps.expenses.models import Bill, BillLine, ExpenseCategory, Vendor
from apps.expenses.services.bill_posting import (
    BillPostingError,
    pay_bill,
    post_bill,
)
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db

NET = Decimal("2000.00")
TAX = Decimal("280.00")
GROSS = NET + TAX


@pytest.fixture
def vendor(tenant, chart_of_accounts) -> Vendor:
    return Vendor.objects.create(
        tenant=tenant, code="V-2001", name="Domasco Logistics",
        currency=TEST_CURRENCY, payable_account=chart_of_accounts["ap_control"],
    )


@pytest.fixture
def category(tenant, chart_of_accounts) -> ExpenseCategory:
    return ExpenseCategory.objects.create(
        tenant=tenant, code="FUEL", name="Fuel",
        expense_account=chart_of_accounts["office_expense"],
    )


def _bill(tenant, vendor, chart_of_accounts, category, *,
          withholding=ZERO, tax=TAX, lines=1) -> Bill:
    today = date.today()
    bill = Bill.objects.create(
        tenant=tenant, vendor=vendor, bill_date=today,
        due_date=today + timedelta(days=30), currency=TEST_CURRENCY,
        exchange_rate=Decimal("1"),
        subtotal_amount=NET, tax_amount=tax, withholding_amount=withholding,
        total_amount=NET + tax, amount_paid=ZERO, amount_due=NET + tax,
        status=Bill.Status.AWAITING_APPROVAL,
    )
    per = (NET / lines).quantize(Decimal("0.01"))
    for n in range(1, lines + 1):
        BillLine.objects.create(
            tenant=tenant, bill=bill, line_number=n, description=f"Line {n}",
            unit_price=per, line_subtotal=per, line_tax=ZERO, line_total=per,
            expense_account=category.expense_account, category=category,
        )
    return bill


def _sides(entry: JournalEntry) -> dict:
    out: dict = {}
    for line in entry.lines.all():
        d, c = out.get(line.account_id, (ZERO, ZERO))
        out[line.account_id] = (d + line.debit, c + line.credit)
    return out


# ---------------------------------------------------------------------------
# The accrual
# ---------------------------------------------------------------------------

def test_posting_a_bill_credits_accounts_payable_not_the_bank(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    """The company owes the vendor; no money has moved.

    Crediting a bank account here would assert a payment that never happened
    and understate cash and creditors simultaneously.
    """
    bill = _bill(tenant, vendor, chart_of_accounts, category)

    entry = post_bill(bill, user_id=owner_user.id)
    sides = _sides(entry)

    assert chart_of_accounts["bank_main"].id not in sides
    assert sides[chart_of_accounts["ap_control"].id][1] == GROSS


def test_input_vat_is_debited_separately(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    bill = _bill(tenant, vendor, chart_of_accounts, category)

    sides = _sides(post_bill(bill, user_id=owner_user.id))

    assert sides[Account.objects.get(system_key="input_vat").id][0] == TAX
    assert sides[chart_of_accounts["office_expense"].id][0] == NET


def test_each_line_hits_its_own_expense_account(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    """A four-line bill produces four cost debits, not one lump.

    Collapsing them would leave every total correct and every cost-centre
    analysis wrong — a failure no reconciliation would surface.
    """
    bill = _bill(tenant, vendor, chart_of_accounts, category, lines=4)

    entry = post_bill(bill, user_id=owner_user.id)

    cost_lines = [
        l for l in entry.lines.all()
        if l.account_id == chart_of_accounts["office_expense"].id and l.debit > ZERO
    ]
    assert len(cost_lines) == 4
    assert sum((l.debit for l in cost_lines), ZERO) == NET


def test_withholding_is_credited_to_the_authority_not_netted_off_cost(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    """Tax withheld is a debt to the state, recognised when the bill is
    accepted. Netting it into the payable would hide a statutory liability and
    overstate what the vendor is owed."""
    withheld = Decimal("100.00")
    bill = _bill(tenant, vendor, chart_of_accounts, category, withholding=withheld)

    sides = _sides(post_bill(bill, user_id=owner_user.id))

    wht = Account.objects.get(system_key="payroll_income_tax_payable")
    assert sides[wht.id][1] == withheld
    # The vendor is owed the total less what was withheld from them.
    assert sides[chart_of_accounts["ap_control"].id][1] == GROSS - withheld
    # ...and the cost is unaffected by the withholding.
    assert sides[chart_of_accounts["office_expense"].id][0] == NET


def test_posting_the_same_bill_twice_is_refused(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    bill = _bill(tenant, vendor, chart_of_accounts, category)
    post_bill(bill, user_id=owner_user.id)

    with pytest.raises(BillPostingError):
        post_bill(bill, user_id=owner_user.id)


def test_a_line_with_no_expense_account_is_refused(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    """The cost must have somewhere to go, or the bill does not post.

    Exercised against an *unsaved* line: ``BillLine.expense_account`` is NOT
    NULL, so a persisted row can never reach this state. The guard covers the
    in-memory path an importer or a bulk-entry endpoint would use, where the
    alternative is a one-sided draft that fails later with an unrelated
    "does not balance".
    """
    from apps.expenses.services.bill_posting import build_bill_entry  # noqa: PLC0415

    bill = _bill(tenant, vendor, chart_of_accounts, category)
    orphan = BillLine(
        tenant=tenant, bill=bill, line_number=9, description="No account",
        unit_price=NET, line_subtotal=NET, line_tax=ZERO, line_total=NET,
        expense_account=None,
    )

    with pytest.raises(BillPostingError):
        build_bill_entry(bill, [orphan])


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def test_paying_a_bill_clears_the_payable_and_credits_the_bank(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    bill = _bill(tenant, vendor, chart_of_accounts, category)
    post_bill(bill, user_id=owner_user.id)

    entry = pay_bill(
        bill, amount=GROSS,
        paid_from_account_id=chart_of_accounts["bank_main"].id,
        user_id=owner_user.id,
    )
    sides = _sides(entry)

    assert sides[chart_of_accounts["ap_control"].id][0] == GROSS
    assert sides[chart_of_accounts["bank_main"].id][1] == GROSS


def test_full_payment_marks_the_bill_paid_and_keeps_the_due_identity(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    bill = _bill(tenant, vendor, chart_of_accounts, category)
    post_bill(bill, user_id=owner_user.id)

    pay_bill(bill, amount=GROSS,
             paid_from_account_id=chart_of_accounts["bank_main"].id,
             user_id=owner_user.id)

    bill.refresh_from_db()
    assert bill.status == Bill.Status.PAID
    assert bill.amount_paid == GROSS
    # ck_bill_due_identity: due == total - paid. The service recomputes both
    # rather than trusting a caller-supplied figure.
    assert bill.amount_due == ZERO


def test_part_payment_leaves_the_bill_partially_paid(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    bill = _bill(tenant, vendor, chart_of_accounts, category)
    post_bill(bill, user_id=owner_user.id)

    pay_bill(bill, amount=Decimal("1000.00"),
             paid_from_account_id=chart_of_accounts["bank_main"].id,
             user_id=owner_user.id)

    bill.refresh_from_db()
    assert bill.status == Bill.Status.PARTIALLY_PAID
    assert bill.amount_due == GROSS - Decimal("1000.00")


def test_two_part_payments_settle_exactly(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    """Distinct part payments must both land — the idempotency key includes
    the running paid figure, so they are different events, while a retry of
    either is not."""
    bill = _bill(tenant, vendor, chart_of_accounts, category)
    post_bill(bill, user_id=owner_user.id)
    acct = chart_of_accounts["bank_main"].id

    pay_bill(bill, amount=Decimal("1000.00"), paid_from_account_id=acct,
             user_id=owner_user.id)
    bill.refresh_from_db()
    pay_bill(bill, amount=Decimal("1280.00"), paid_from_account_id=acct,
             user_id=owner_user.id)

    bill.refresh_from_db()
    assert bill.amount_paid == GROSS
    assert bill.status == Bill.Status.PAID


def test_overpaying_is_refused(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    bill = _bill(tenant, vendor, chart_of_accounts, category)
    post_bill(bill, user_id=owner_user.id)

    with pytest.raises(BillPostingError):
        pay_bill(bill, amount=GROSS + Decimal("0.01"),
                 paid_from_account_id=chart_of_accounts["bank_main"].id,
                 user_id=owner_user.id)


def test_paying_an_unposted_bill_is_refused(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    """There is no payable to clear; the debit would drive it negative."""
    bill = _bill(tenant, vendor, chart_of_accounts, category)

    with pytest.raises(BillPostingError):
        pay_bill(bill, amount=GROSS,
                 paid_from_account_id=chart_of_accounts["bank_main"].id,
                 user_id=owner_user.id)


def test_the_payable_nets_to_zero_across_accrual_and_settlement(
    tenant, vendor, chart_of_accounts, category, owner_user, open_period
):
    """The end state of the pair, asserted as one fact: cost recognised, cash
    gone, nothing left owed."""
    bill = _bill(tenant, vendor, chart_of_accounts, category)
    accrual = post_bill(bill, user_id=owner_user.id)
    settle = pay_bill(bill, amount=GROSS,
                      paid_from_account_id=chart_of_accounts["bank_main"].id,
                      user_id=owner_user.id)

    ap = chart_of_accounts["ap_control"].id
    net = ZERO
    for entry in (accrual, settle):
        for line in entry.lines.all():
            if line.account_id == ap:
                net += line.credit - line.debit

    assert net == ZERO
    assert _sides(accrual)[chart_of_accounts["office_expense"].id][0] == NET
    assert _sides(settle)[chart_of_accounts["bank_main"].id][1] == GROSS
