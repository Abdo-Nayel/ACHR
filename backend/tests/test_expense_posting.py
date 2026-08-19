"""Expense claims must reach the general ledger, and reach it once.

Until ``apps.expenses.services.posting`` existed, approving an expense moved a
status and nothing else: the columns to hold the entries were there, the
posting service was not, and the P&L understated costs by the whole approved
backlog. These tests pin the behaviour that closed that gap.

The distinction that carries the most weight here is reimbursable vs not,
because the two produce different *liabilities*, not merely different
accounts. A company-card expense is settled the moment it is approved. An
out-of-pocket expense creates a debt to an employee that survives until
payroll or finance actually pays it, and that debt has to be on the balance
sheet in between.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounting.models import Account, JournalEntry
from apps.core.fields import ZERO
from apps.expenses.models import Expense, ExpenseCategory
from apps.hr.models import Department, Employee
from apps.expenses.services.posting import (
    ExpensePostingError,
    build_expense_entry,
    post_expense,
    post_reimbursement,
)
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db

NET = Decimal("1000.00")
TAX = Decimal("140.00")
GROSS = NET + TAX


@pytest.fixture
def category(tenant, chart_of_accounts) -> ExpenseCategory:
    return ExpenseCategory.objects.create(
        tenant=tenant,
        code="TRAVEL",
        name="Travel",
        expense_account=chart_of_accounts["office_expense"],
    )


@pytest.fixture
def department(tenant) -> Department:
    dept = Department.objects.create(
        tenant=tenant, code="hq", name="Head office", parent=None, depth=0
    )
    dept.path = dept.build_path()
    dept.save(update_fields=["path", "updated_at"])
    return dept


@pytest.fixture
def claimant(tenant, department) -> Employee:
    """A real employee, because ``ck_expense_reimbursable_has_employee``
    requires one: a reimbursable claim with nobody to reimburse is a debt to
    no-one, and the database refuses it rather than letting the payable float
    unattributed."""
    return Employee.objects.create(
        tenant=tenant,
        employee_code="E-9001",
        first_name="Karim",
        last_name="Fahmy",
        department=department,
        hire_date=date(2020, 1, 6),
        base_salary=Decimal("10000.00"),
        salary_currency=TEST_CURRENCY,
    )


def _expense(tenant, category, chart_of_accounts, *, reimbursable: bool,
             tax=TAX, employee=None) -> Expense:
    return Expense.objects.create(
        tenant=tenant,
        category=category,
        employee=employee,
        description="Client visit — Alexandria",
        expense_date=date.today(),
        currency=TEST_CURRENCY,
        exchange_rate=Decimal("1"),
        payment_method=Expense.PaymentMethod.CASH,
        paid_from_account=chart_of_accounts["bank_main"],
        amount=NET,
        tax_amount=tax,
        total_amount=NET + tax,
        markup_rate=Decimal("1"),
        status=Expense.Status.APPROVED,
        # ck_expense_approved_has_timestamp: the database refuses an APPROVED
        # row with no approved_at, so the fixture has to be a state the system
        # could actually have produced.
        approved_at=timezone.now(),
        is_reimbursable=reimbursable,
    )


def _sides(entry: JournalEntry) -> dict:
    """{account_id: (debit_total, credit_total)} — the shape assertions read."""
    out: dict = {}
    for line in entry.lines.all():
        debit, credit = out.get(line.account_id, (ZERO, ZERO))
        out[line.account_id] = (debit + line.debit, credit + line.credit)
    return out


# ---------------------------------------------------------------------------
# The accrual
# ---------------------------------------------------------------------------

def test_company_paid_expense_credits_the_account_it_was_paid_from(
    tenant, category, chart_of_accounts, owner_user, open_period
):
    expense = _expense(tenant, category, chart_of_accounts, reimbursable=False)

    entry = post_expense(expense, user_id=owner_user.id)
    sides = _sides(entry)

    assert sides[chart_of_accounts["office_expense"].id][0] == NET
    assert sides[chart_of_accounts["bank_main"].id][1] == GROSS


def test_input_vat_is_debited_separately_not_buried_in_the_expense(
    tenant, category, chart_of_accounts, owner_user, open_period
):
    """VAT is recoverable, so it is an asset, not a cost.

    Posting the gross to the expense account would overstate the cost centre
    by the tax and quietly forfeit the reclaim — and every downstream report
    would agree with itself, because the entry still balances.
    """
    expense = _expense(tenant, category, chart_of_accounts, reimbursable=False)

    entry = post_expense(expense, user_id=owner_user.id)
    sides = _sides(entry)

    input_vat = Account.objects.get(system_key="input_vat")
    assert sides[input_vat.id][0] == TAX
    assert sides[chart_of_accounts["office_expense"].id][0] == NET


def test_reimbursable_expense_credits_a_payable_not_the_bank(
    tenant, category, chart_of_accounts, owner_user, open_period, claimant
):
    """The company owes the employee; no money has moved yet.

    Crediting the bank here would assert a payment that has not happened and
    understate both cash and liabilities at once.
    """
    expense = _expense(tenant, category, chart_of_accounts, reimbursable=True,
                       employee=claimant)

    entry = post_expense(expense, user_id=owner_user.id)
    sides = _sides(entry)

    assert chart_of_accounts["bank_main"].id not in sides
    payable = Account.objects.get(system_key="employee_reimbursements_payable")
    assert sides[payable.id][1] == GROSS


def test_posted_entry_is_attached_to_the_expense(
    tenant, category, chart_of_accounts, owner_user, open_period
):
    expense = _expense(tenant, category, chart_of_accounts, reimbursable=False)

    entry = post_expense(expense, user_id=owner_user.id)

    expense.refresh_from_db()
    assert expense.journal_entry_id == entry.id
    assert entry.source == JournalEntry.Source.EXPENSE
    assert entry.source_document_id == expense.id


def test_posting_the_same_expense_twice_is_refused(
    tenant, category, chart_of_accounts, owner_user, open_period
):
    """Double-counting a cost is the failure this guard exists for."""
    expense = _expense(tenant, category, chart_of_accounts, reimbursable=False)
    post_expense(expense, user_id=owner_user.id)

    with pytest.raises(ExpensePostingError):
        post_expense(expense, user_id=owner_user.id)


def test_a_zero_value_expense_is_refused(
    tenant, category, chart_of_accounts, owner_user, open_period
):
    """Belt and braces with ``ck_expense_amounts_valid``.

    The database already refuses a zero-total row, so this exercises the
    service guard against an *unsaved* instance — the shape a future importer
    or API path could hand it before anything hits a constraint.
    """
    expense = Expense(
        tenant=tenant,
        category=category,
        expense_date=date.today(),
        currency=TEST_CURRENCY,
        exchange_rate=Decimal("1"),
        payment_method=Expense.PaymentMethod.CASH,
        paid_from_account=chart_of_accounts["bank_main"],
        amount=ZERO,
        tax_amount=ZERO,
        total_amount=ZERO,
        markup_rate=Decimal("1"),
    )

    with pytest.raises(ExpensePostingError):
        build_expense_entry(expense)


# ---------------------------------------------------------------------------
# The settlement
# ---------------------------------------------------------------------------

def test_reimbursement_clears_the_payable_and_credits_the_bank(
    tenant, category, chart_of_accounts, owner_user, open_period, claimant
):
    expense = _expense(tenant, category, chart_of_accounts, reimbursable=True,
                       employee=claimant)
    post_expense(expense, user_id=owner_user.id)

    entry = post_reimbursement(expense, user_id=owner_user.id)
    sides = _sides(entry)

    payable = Account.objects.get(system_key="employee_reimbursements_payable")
    assert sides[payable.id][0] == GROSS          # debit clears the liability
    assert sides[chart_of_accounts["bank_main"].id][1] == GROSS


def test_reimbursing_before_approval_is_refused(
    tenant, category, chart_of_accounts, owner_user, open_period, claimant
):
    """Nothing to settle: the payable was never raised.

    Allowing it would debit a liability that does not exist and leave the
    account negative — a balance that is extremely hard to explain later.
    """
    expense = _expense(tenant, category, chart_of_accounts, reimbursable=True,
                       employee=claimant)

    with pytest.raises(ExpensePostingError):
        post_reimbursement(expense, user_id=owner_user.id)


def test_reimbursing_a_non_reimbursable_expense_is_refused(
    tenant, category, chart_of_accounts, owner_user, open_period
):
    expense = _expense(tenant, category, chart_of_accounts, reimbursable=False)
    post_expense(expense, user_id=owner_user.id)

    with pytest.raises(ExpensePostingError):
        post_reimbursement(expense, user_id=owner_user.id)


def test_reimbursing_twice_is_refused(
    tenant, category, chart_of_accounts, owner_user, open_period, claimant
):
    expense = _expense(tenant, category, chart_of_accounts, reimbursable=True,
                       employee=claimant)
    post_expense(expense, user_id=owner_user.id)
    post_reimbursement(expense, user_id=owner_user.id)

    with pytest.raises(ExpensePostingError):
        post_reimbursement(expense, user_id=owner_user.id)


def test_accrual_and_settlement_together_leave_cash_down_and_cost_up(
    tenant, category, chart_of_accounts, owner_user, open_period, claimant
):
    """The end state of the two-entry sequence, asserted as one fact.

    After both entries the payable nets to zero, the expense account holds the
    net cost and the bank has fallen by the gross. Any single entry can look
    right while the pair does not.
    """
    expense = _expense(tenant, category, chart_of_accounts, reimbursable=True,
                       employee=claimant)
    accrual = post_expense(expense, user_id=owner_user.id)
    settlement = post_reimbursement(expense, user_id=owner_user.id)

    payable = Account.objects.get(system_key="employee_reimbursements_payable")
    net_payable = ZERO
    for entry in (accrual, settlement):
        for line in entry.lines.all():
            if line.account_id == payable.id:
                net_payable += line.credit - line.debit

    assert net_payable == ZERO
    assert _sides(accrual)[chart_of_accounts["office_expense"].id][0] == NET
    assert _sides(settlement)[chart_of_accounts["bank_main"].id][1] == GROSS
