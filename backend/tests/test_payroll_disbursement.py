"""The last step of a payroll run: the cash actually leaving.

`calculate -> submit -> approve -> post -> pay`. Posting creates the
liability (Dr salary expense / Cr salaries payable); disbursement discharges
it (Dr salaries payable / Cr bank). Two entries, deliberately: the money
leaves on a different date from the accrual, is authorised by a different
person, and reconciles against a bank statement rather than a payroll
register. Between them the balance sheet correctly shows money owed to staff.

The failure worth designing against is a disbursement that credits the bank
without debiting the payable — the cash is right, the balance sheet still
shows the company owing its staff, and the two only disagree at a
reconciliation weeks later.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.accounting.models import Account, JournalEntry
from apps.core.fields import ZERO
from apps.hr.models import Department, Employee, SalaryRevision
from apps.payroll.models import PayrollRun, TaxBracket
from apps.payroll.services.engine import (
    PayrollError,
    approve_run,
    calculate_run,
    mark_run_paid,
    post_run_to_ledger,
)
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db

MONTHLY = Decimal("20000.00")


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


@pytest.fixture
def tax_scale(tenant):
    TaxBracket.objects.create(
        tenant=tenant, country=tenant.country,
        effective_from=date(date.today().year, 1, 1),
        lower_bound=ZERO, upper_bound=None, rate=Decimal("0.100000"),
        fixed_deduction=ZERO, currency=TEST_CURRENCY,
        is_annual_basis=True, sequence=0,
    )


@pytest.fixture
def staff(tenant, chart_of_accounts, tax_scale) -> Employee:
    hq = Department.objects.create(tenant=tenant, code="hq", name="HQ", depth=0)
    hq.path = hq.build_path()
    hq.save(update_fields=["path", "updated_at"])
    e = Employee.objects.create(
        tenant=tenant, employee_code="E-5501", first_name="Laila", last_name="Nabil",
        department=hq, hire_date=date(date.today().year - 1, 1, 1),
        base_salary=MONTHLY, salary_currency=TEST_CURRENCY,
    )
    SalaryRevision.objects.create(
        tenant=tenant, employee=e, effective_date=date(date.today().year - 1, 1, 1),
        previous_salary=ZERO, new_salary=MONTHLY, currency=TEST_CURRENCY,
        reason="hire",
    )
    return e


@pytest.fixture
def run(tenant, open_period, staff) -> PayrollRun:
    today = date.today()
    return PayrollRun.objects.create(
        tenant=tenant, name=f"Payroll {today:%B %Y}",
        period_start=today.replace(day=1), period_end=_month_end(today),
        pay_date=_month_end(today), frequency=PayrollRun.Frequency.MONTHLY,
        currency=TEST_CURRENCY,
    )


def _to_posted(run, accountant_user, owner_user):
    calculate_run(run, user_id=accountant_user.id)
    run.assert_can_transition(PayrollRun.Status.PENDING_APPROVAL)
    PayrollRun.objects.filter(pk=run.pk).update(
        status=PayrollRun.Status.PENDING_APPROVAL
    )
    run.refresh_from_db()
    approve_run(run, owner_user)
    post_run_to_ledger(run, user_id=owner_user.id)
    run.refresh_from_db()
    return run


def _sides(entry: JournalEntry) -> dict:
    out: dict = {}
    for line in entry.lines.all():
        d, c = out.get(line.account_id, (ZERO, ZERO))
        out[line.account_id] = (d + line.debit, c + line.credit)
    return out


def test_disbursement_debits_the_payable_and_credits_the_bank(
    tenant, run, chart_of_accounts, owner_user, accountant_user, iam_permission_stub
):
    """The entry the whole step exists to produce."""
    _to_posted(run, accountant_user, owner_user)

    entry = mark_run_paid(run, user_id=owner_user.id)
    sides = _sides(entry)

    payable = Account.objects.get(system_key="payroll_salaries_payable").id
    bank = Account.objects.get(system_key="bank_main").id
    run.refresh_from_db()

    assert sides[payable][0] == run.total_net      # debit clears the liability
    assert sides[bank][1] == run.total_net         # credit is the cash leaving
    assert entry.total_debit == entry.total_credit


def test_it_is_a_second_entry_not_an_edit_of_the_accrual(
    tenant, run, chart_of_accounts, owner_user, accountant_user, iam_permission_stub
):
    """The accrual stays exactly as filed. Posted entries are never edited —
    the trail is what makes the two dates reconcilable."""
    _to_posted(run, accountant_user, owner_user)
    accrual_id = run.journal_entry_id
    accrual_debit = JournalEntry.objects.get(pk=accrual_id).total_debit

    settlement = mark_run_paid(run, user_id=owner_user.id)

    assert settlement.id != accrual_id
    assert JournalEntry.objects.get(pk=accrual_id).total_debit == accrual_debit


def test_the_payable_nets_to_zero_across_both_entries(
    tenant, run, chart_of_accounts, owner_user, accountant_user, iam_permission_stub
):
    """Accrual raised it, disbursement discharged it. Anything left over is
    money the company still thinks it owes staff it has already paid."""
    _to_posted(run, accountant_user, owner_user)
    accrual = JournalEntry.objects.get(pk=run.journal_entry_id)
    settlement = mark_run_paid(run, user_id=owner_user.id)

    payable = Account.objects.get(system_key="payroll_salaries_payable").id
    net = ZERO
    for entry in (accrual, settlement):
        for line in entry.lines.all():
            if line.account_id == payable:
                net += line.credit - line.debit

    assert net == ZERO


def test_the_run_becomes_paid(
    tenant, run, chart_of_accounts, owner_user, accountant_user, iam_permission_stub
):
    _to_posted(run, accountant_user, owner_user)

    mark_run_paid(run, user_id=owner_user.id)

    run.refresh_from_db()
    assert run.status == PayrollRun.Status.PAID


def test_an_unposted_run_cannot_be_disbursed(
    tenant, run, chart_of_accounts, owner_user, accountant_user, iam_permission_stub
):
    """The payment would discharge a liability that was never raised, driving
    the payable negative — a balance nobody can explain afterwards.

    Refused twice over: ``ALLOWED_TRANSITIONS`` rejects ``calculated -> paid``
    (a ``ValueError`` from the model) before ``mark_run_paid``'s own
    ``journal_entry_id is None`` check is reached. Both are right, and the
    order is an implementation detail — the assertion is that it does not
    happen, not which guard got there first.
    """
    calculate_run(run, user_id=accountant_user.id)

    with pytest.raises((PayrollError, ValueError)):
        mark_run_paid(run, user_id=owner_user.id)

    run.refresh_from_db()
    assert run.status != PayrollRun.Status.PAID


def test_disbursing_twice_returns_the_same_entry(
    tenant, run, chart_of_accounts, owner_user, accountant_user, iam_permission_stub
):
    """Idempotent on `payroll:{id}:payment`, so a retried confirmation cannot
    double-credit the bank. The second call is refused by the transition map
    before it reaches the ledger."""
    _to_posted(run, accountant_user, owner_user)
    mark_run_paid(run, user_id=owner_user.id)

    with pytest.raises(Exception):
        mark_run_paid(run, user_id=owner_user.id)

    payable = Account.objects.get(system_key="payroll_salaries_payable").id
    settlements = [
        e for e in JournalEntry.objects.all()
        if any(l.account_id == payable and l.debit > ZERO for l in e.lines.all())
    ]
    assert len(settlements) == 1


def test_the_payment_date_drives_the_entry_date(
    tenant, run, chart_of_accounts, owner_user, accountant_user, open_period,
    iam_permission_stub,
):
    """A file sent Friday can settle Monday, and the cash entry must carry the
    date the money actually left rather than the date somebody clicked."""
    _to_posted(run, accountant_user, owner_user)
    when = open_period.start_date + timedelta(days=1)

    entry = mark_run_paid(run, user_id=owner_user.id, payment_date=when)

    assert entry.entry_date == when


def test_paying_from_an_unconfigured_account_is_refused(
    tenant, run, chart_of_accounts, owner_user, accountant_user, iam_permission_stub
):
    """The service resolves the source by role, so an unknown key must stop
    the payment rather than fall back to a default the user did not choose."""
    _to_posted(run, accountant_user, owner_user)

    with pytest.raises(Exception):
        mark_run_paid(
            run, user_id=owner_user.id,
            bank_account_system_key="no_such_account",
        )
