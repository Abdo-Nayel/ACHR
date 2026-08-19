"""Approved overtime must reach the payslip, the tax base and the ledger.

This is the seam the overtime port exists for. Pricing a slip correctly is
worth nothing if payroll never reads it, and the two failure modes on either
side of that seam are both silent:

* **Overtime added after tax is computed** under-withholds on every hour of
  overtime in the company. The payslip still balances — net is still
  gross minus deductions — so nothing in the run's own checks disagrees.
* **A slip not stamped with the run that paid it** is picked up again by the
  next period's run, and the second payment looks exactly as legitimate as
  the first.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

import pytest

from apps.core.fields import ZERO
from apps.hr.models import Department, Employee, OvertimeSlip, OvertimeType, SalaryRevision
from apps.hr.services.overtime import approve_slip
from apps.payroll.models import PayrollComponent, PayrollRun
from apps.payroll.services.engine import calculate_run
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db

MONTHLY = Decimal("20800.00")     # / 208 standard hours = 100/hour


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


@pytest.fixture
def tax_scale(tenant):
    from apps.payroll.models import TaxBracket  # noqa: PLC0415

    TaxBracket.objects.create(
        tenant=tenant, country=tenant.country,
        effective_from=date(date.today().year, 1, 1),
        lower_bound=ZERO, upper_bound=None, rate=Decimal("0.100000"),
        fixed_deduction=ZERO, currency=TEST_CURRENCY,
        is_annual_basis=True, sequence=0,
    )


@pytest.fixture
def worker(tenant, chart_of_accounts, tax_scale) -> Employee:
    hq = Department.objects.create(tenant=tenant, code="hq", name="HQ", depth=0)
    hq.path = hq.build_path()
    hq.save(update_fields=["path", "updated_at"])
    e = Employee.objects.create(
        tenant=tenant, employee_code="E-9101", first_name="Sara", last_name="Adel",
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
def ot_component(tenant, chart_of_accounts) -> PayrollComponent:
    """The GL treatment of overtime lives on the component, so several
    overtime *types* can share one account."""
    return PayrollComponent.objects.create(
        tenant=tenant, code="OT", name="Overtime",
        component_type=PayrollComponent.ComponentType.EARNING,
        calculation_type=PayrollComponent.CalculationType.FIXED,
        amount=ZERO, currency=TEST_CURRENCY,
        is_taxable=True, is_subject_to_social_insurance=False,
        expense_account=chart_of_accounts["payroll_salary_expense"],
        sequence=15,
    )


@pytest.fixture
def ot_type(tenant, ot_component) -> OvertimeType:
    return OvertimeType.objects.create(
        tenant=tenant, code="WKND", name="Weekend",
        multiplier=Decimal("2.000000"), component=ot_component,
    )


@pytest.fixture
def run(tenant, open_period, worker) -> PayrollRun:
    today = date.today()
    return PayrollRun.objects.create(
        tenant=tenant, name=f"Payroll {today:%B %Y}",
        period_start=today.replace(day=1), period_end=_month_end(today),
        pay_date=_month_end(today), frequency=PayrollRun.Frequency.MONTHLY,
        currency=TEST_CURRENCY,
    )


def _approved_slip(tenant, worker, ot_type, owner_user, hours="5"):
    slip = OvertimeSlip.objects.create(
        tenant=tenant, employee=worker, overtime_type=ot_type,
        work_date=date.today().replace(day=2), hours=Decimal(hours),
        currency=TEST_CURRENCY, status=OvertimeSlip.Status.SUBMITTED,
    )
    return approve_slip(slip, user_id=owner_user.id)


def test_approved_overtime_appears_on_the_payslip(
    tenant, worker, ot_type, run, owner_user, accountant_user
):
    _approved_slip(tenant, worker, ot_type, owner_user)   # 5h × 100 × 2 = 1000

    calculate_run(run, user_id=accountant_user.id)

    payslip = run.payslips.get(employee=worker)
    ot_lines = [l for l in payslip.lines.all() if l.component.code == "OT"]
    assert len(ot_lines) == 1
    assert ot_lines[0].amount == Decimal("1000.00")


def test_overtime_raises_the_gross(
    tenant, worker, ot_type, run, owner_user, accountant_user
):
    _approved_slip(tenant, worker, ot_type, owner_user)

    calculate_run(run, user_id=accountant_user.id)

    payslip = run.payslips.get(employee=worker)
    assert payslip.gross_amount == MONTHLY + Decimal("1000.00")


def test_overtime_is_taxed(
    tenant, worker, ot_type, run, owner_user, accountant_user
):
    """The failure this ordering exists to prevent.

    Adding overtime after the tax computation leaves a payslip that still
    satisfies net == gross - deductions, so no invariant in the run
    disagrees — the company simply under-withholds on every overtime hour and
    finds out from the tax authority.
    """
    calculate_run(run, user_id=accountant_user.id)
    without_ot = run.payslips.get(employee=worker).income_tax_amount

    _approved_slip(tenant, worker, ot_type, owner_user)
    calculate_run(run, user_id=accountant_user.id)   # recalculate
    with_ot = run.payslips.get(employee=worker).income_tax_amount

    # 1 000 more taxable pay at 10% = 100 more withheld.
    assert with_ot - without_ot == Decimal("100.00")


def test_the_slip_is_stamped_with_the_run_that_paid_it(
    tenant, worker, ot_type, run, owner_user, accountant_user
):
    slip = _approved_slip(tenant, worker, ot_type, owner_user)

    calculate_run(run, user_id=accountant_user.id)

    slip.refresh_from_db()
    assert slip.payroll_run_id == run.id
    assert slip.status == OvertimeSlip.Status.PAID


def test_a_paid_slip_is_not_paid_again_by_the_next_run(
    tenant, worker, ot_type, run, owner_user, accountant_user, open_period
):
    """The double-payment guard, end to end."""
    _approved_slip(tenant, worker, ot_type, owner_user)
    calculate_run(run, user_id=accountant_user.id)
    first = run.payslips.get(employee=worker).gross_amount

    # uq_pay_run_period forbids a second MONTHLY run over the same dates.
    # OFF_CYCLE is the sanctioned way to run a correction across the same
    # period, and it is exactly the case where re-paying a consumed slip
    # would be easiest to miss.
    second_run = PayrollRun.objects.create(
        tenant=tenant, name="Off-cycle correction",
        period_start=run.period_start, period_end=run.period_end,
        pay_date=run.pay_date, frequency=PayrollRun.Frequency.OFF_CYCLE,
        currency=TEST_CURRENCY,
    )
    calculate_run(second_run, user_id=accountant_user.id)

    assert first == MONTHLY + Decimal("1000.00")
    # The slip was consumed; the second run pays salary only.
    assert second_run.payslips.get(employee=worker).gross_amount == MONTHLY


def test_recalculating_releases_and_reclaims_the_slip(
    tenant, worker, ot_type, run, owner_user, accountant_user
):
    """A discarded run must not strand its slips.

    Without the release, recalculating would leave the slip marked PAID
    against payslips that no longer exist — invisible to the recalculation
    meant to replace them, and never paid at all.
    """
    slip = _approved_slip(tenant, worker, ot_type, owner_user)

    calculate_run(run, user_id=accountant_user.id)
    calculate_run(run, user_id=accountant_user.id)   # again

    slip.refresh_from_db()
    assert slip.payroll_run_id == run.id
    assert run.payslips.get(employee=worker).gross_amount == MONTHLY + Decimal("1000.00")


def test_an_unapproved_slip_is_not_paid(
    tenant, worker, ot_type, run, accountant_user
):
    """Paying submitted claims would make approval decorative."""
    OvertimeSlip.objects.create(
        tenant=tenant, employee=worker, overtime_type=ot_type,
        work_date=date.today().replace(day=3), hours=Decimal("4"),
        currency=TEST_CURRENCY, status=OvertimeSlip.Status.SUBMITTED,
    )

    calculate_run(run, user_id=accountant_user.id)

    assert run.payslips.get(employee=worker).gross_amount == MONTHLY


def test_overtime_reaches_the_general_ledger(
    tenant, worker, ot_type, run, owner_user, accountant_user, chart_of_accounts,
    iam_permission_stub,
):
    """The end of the chain: hours worked become a debit in the salary
    expense account, through the same post_entry choke point as everything
    else."""
    from apps.payroll.services.engine import (  # noqa: PLC0415
        approve_run,
        post_run_to_ledger,
    )

    _approved_slip(tenant, worker, ot_type, owner_user)
    calculate_run(run, user_id=accountant_user.id)

    run.assert_can_transition(PayrollRun.Status.PENDING_APPROVAL)
    PayrollRun.objects.filter(pk=run.pk).update(
        status=PayrollRun.Status.PENDING_APPROVAL
    )
    run.refresh_from_db()
    approve_run(run, owner_user)
    entry = post_run_to_ledger(run, user_id=owner_user.id)

    salary_expense = chart_of_accounts["payroll_salary_expense"].id
    debit = sum(
        (l.debit for l in entry.lines.all() if l.account_id == salary_expense), ZERO
    )
    # Salary plus the overtime, both through the expense account.
    assert debit == MONTHLY + Decimal("1000.00")
    assert entry.total_debit == entry.total_credit
