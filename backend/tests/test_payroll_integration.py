"""Payroll -> general ledger: the integration that must never drift.

Payroll is the largest recurring outflow in most companies and the classic
vector for occupational fraud, so this file asserts four different kinds of
property, each guarding a different failure:

* **Arithmetic identity** — ``net == gross - deductions``, exactly, in
  Decimal, on every payslip and on the run's control totals. A tolerance here
  is how a systematic one-cent error becomes a five-hundred-cent trial balance
  difference on a fifty-thousand-employee run.
* **Ledger shape** — one balanced entry per run, debits equal to the full
  employer cost, credits equal to the payables. The entry is an *accrual*, not
  a cash movement; nothing has left the bank yet.
* **Exactly-once posting** — the run's ``idempotency_key`` collapses retries.
* **Segregation of duties** — the person who computed the run may not approve
  it. Fraud then requires collusion, which is detectable and deterrable.

Progressive tax is table-driven because a marginal calculation is only worth
testing at the boundaries: the classic payroll bug applies the top rate to the
whole income, which produces the correct answer for anyone in the bottom
bracket and a large overcharge for everyone else.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied

from apps.accounting.models import JournalEntry, JournalLine
from apps.accounting.services.posting import assert_ledger_balanced
from apps.core.fields import ZERO, quantize_currency
from apps.hr.models import Department, Employee, SalaryRevision, WorkSchedule
from apps.payroll.models import (
    EmployeeComponent,
    EmployeePayrollProfile,
    PayrollComponent,
    PayrollRun,
    Payslip,
    TaxBracket,
)
from apps.payroll.services.engine import (
    SALARY_EXPENSE,
    EMPLOYER_SI_EXPENSE,
    INCOME_TAX_PAYABLE,
    OTHER_DEDUCTIONS_PAYABLE,
    SALARIES_PAYABLE,
    SOCIAL_INSURANCE_PAYABLE,
    approve_run,
    build_journal_entry,
    calculate_run,
    compute_income_tax,
    post_run_to_ledger,
)
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: A deliberately plain annual scale so every expected figure below can be
#: verified with a pocket calculator: 0% to 50k, 10% to 150k, 20% above.
SIMPLE_SCALE: tuple[tuple[str, str | None, str], ...] = (
    ("0", "50000", "0"),
    ("50000", "150000", "0.100000"),
    ("150000", None, "0.200000"),
)


def _month_end(day: date) -> date:
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


@pytest.fixture
def tax_scale(tenant):
    """The tenant's income-tax scale. Without one the engine refuses to run —
    it will not guess a rate, because guessing understates withholding and the
    employer, not the employee, pays the penalty."""
    effective_from = date(date.today().year, 1, 1)
    for sequence, (lower, upper, rate) in enumerate(SIMPLE_SCALE):
        TaxBracket.objects.create(
            tenant=tenant,
            country=tenant.country,
            effective_from=effective_from,
            lower_bound=Decimal(lower),
            upper_bound=Decimal(upper) if upper is not None else None,
            rate=Decimal(rate),
            fixed_deduction=ZERO,
            currency=TEST_CURRENCY,
            is_annual_basis=True,
            sequence=sequence,
        )
    return SIMPLE_SCALE


@pytest.fixture
def workforce(tenant, chart_of_accounts, tax_scale):
    """Two departments, two employees, effective-dated salaries.

    ``SalaryRevision`` is mandatory, not decorative: the engine reads
    effective-dated history and never ``Employee.base_salary``, so an employee
    without a HIRE revision cannot be paid at all.
    """
    hire_date = date(date.today().year - 1, 1, 1)

    hq = Department.objects.create(tenant=tenant, code="hq", name="Head office", depth=0)
    hq.path = hq.build_path()
    hq.save(update_fields=["path", "updated_at"])

    engineering = Department.objects.create(
        tenant=tenant, code="eng", name="Engineering", parent=hq, depth=1
    )
    engineering.path = engineering.build_path()
    engineering.save(update_fields=["path", "updated_at"])

    schedule = WorkSchedule.objects.create(
        tenant=tenant, code="std", name="Standard week",
        working_days=[1, 2, 3, 4, 5], expected_hours_per_week=Decimal("40"),
        is_default=True,
    )

    employees = []
    for index, salary in enumerate(
        [Decimal("20000.000000"), Decimal("9000.000000")], start=1
    ):
        employee = Employee.objects.create(
            tenant=tenant,
            employee_code=f"E-{index:04d}",
            first_name=f"Person{index}",
            last_name="Test",
            department=engineering if index == 1 else hq,
            work_schedule=schedule,
            hire_date=hire_date,
            base_salary=salary,
            salary_currency=TEST_CURRENCY,
            pay_frequency=Employee.PayFrequency.MONTHLY,
            status=Employee.Status.ACTIVE,
        )
        SalaryRevision.objects.create(
            tenant=tenant,
            employee=employee,
            change_type=SalaryRevision.ChangeType.HIRE,
            effective_date=hire_date,
            previous_salary=ZERO,
            new_salary=salary,
            currency=TEST_CURRENCY,
        )
        EmployeePayrollProfile.objects.create(
            tenant=tenant, employee=employee, currency=TEST_CURRENCY,
            insurable_wage=Decimal("12600.000000"),
        )
        employees.append(employee)

    # One voluntary deduction, in the 800–999 band the model documents, so the
    # "other deductions" leg of the journal entry is exercised rather than
    # silently zero.
    loan = PayrollComponent.objects.create(
        tenant=tenant, code="LOAN", name="Staff loan repayment",
        component_type=PayrollComponent.ComponentType.DEDUCTION,
        calculation_type=PayrollComponent.CalculationType.FIXED,
        amount=Decimal("500.000000"), currency=TEST_CURRENCY, sequence=850,
        is_taxable=False, is_subject_to_social_insurance=False,
        liability_account=chart_of_accounts["payroll_other_deductions_payable"],
    )
    EmployeeComponent.objects.create(
        tenant=tenant, employee=employees[0], component=loan,
        effective_from=hire_date,
    )

    return SimpleNamespace(
        departments={"hq": hq, "engineering": engineering},
        employees=employees,
        schedule=schedule,
        loan=loan,
    )


@pytest.fixture
def draft_run(tenant, open_period, workforce):
    today = date.today()
    return PayrollRun.objects.create(
        tenant=tenant,
        name=f"Payroll {today:%B %Y}",
        period_start=today.replace(day=1),
        period_end=_month_end(today),
        # The pay date drives the GL entry date, so it must land in an open
        # period; the fixture calendar makes the current month open.
        pay_date=_month_end(today),
        frequency=PayrollRun.Frequency.MONTHLY,
        currency=TEST_CURRENCY,
    )


@pytest.fixture
def calculated_run(draft_run, accountant_user):
    """A run calculated *by the accountant*, so the approver must be someone
    else. That asymmetry is the point of the segregation-of-duties test."""
    return calculate_run(draft_run, user_id=accountant_user.id)


def _submit(run: PayrollRun) -> PayrollRun:
    """CALCULATED -> PENDING_APPROVAL.

    There is no submit service in this revision of the repository, so the
    transition is applied directly here — validated against the model's own
    map first, so the test cannot invent an edge the product does not allow.
    """
    run.assert_can_transition(PayrollRun.Status.PENDING_APPROVAL)
    PayrollRun.objects.filter(pk=run.pk).update(
        status=PayrollRun.Status.PENDING_APPROVAL
    )
    run.refresh_from_db()
    return run


def _approve(run: PayrollRun, approver) -> PayrollRun:
    return approve_run(_submit(run), approver)


# ---------------------------------------------------------------------------
# The net identity
# ---------------------------------------------------------------------------

def test_every_payslip_satisfies_net_equals_gross_minus_deductions(calculated_run):
    """Exactly, in Decimal — not ``abs(diff) < 0.01``.

    The ledger's balance check has no tolerance, so neither does this. The
    employer's social insurance share is deliberately excluded from
    ``total_deductions``: it is a company cost that never passes through the
    employee's hands, and including it would understate net pay.
    """
    payslips = list(Payslip.objects.filter(run=calculated_run))
    assert payslips, "The run produced no payslips; the fixture is not exercising anything."

    for slip in payslips:
        expected = slip.gross_amount - slip.total_deductions
        assert slip.net_amount == expected, (
            f"Payslip for {slip.employee_id} does not add up: "
            f"net {slip.net_amount} != {expected}"
        )
        assert slip.total_deductions == (
            slip.income_tax_amount
            + slip.social_insurance_employee
            + slip.other_deductions
        )
        assert slip.net_amount >= ZERO
        assert slip.net_amount <= slip.gross_amount


def test_run_control_totals_agree_with_the_payslips(calculated_run):
    """The materialised totals are what the posting service asserts against;
    if they can drift from the slips, the ledger can agree with itself and
    still disagree with the payroll register handed to the auditor."""
    slips = list(Payslip.objects.filter(run=calculated_run))

    assert calculated_run.employee_count == len(slips)
    assert calculated_run.total_gross == sum(
        (slip.gross_amount for slip in slips), ZERO
    )
    assert calculated_run.total_net == sum((slip.net_amount for slip in slips), ZERO)
    assert calculated_run.total_deductions == sum(
        (slip.total_deductions for slip in slips), ZERO
    )
    assert calculated_run.total_net == (
        calculated_run.total_gross - calculated_run.total_deductions
    )
    assert calculated_run.total_employer_cost == calculated_run.total_gross + sum(
        (slip.social_insurance_employer for slip in slips), ZERO
    )


def test_recalculating_replaces_payslips_rather_than_adding_them(
    calculated_run, accountant_user
):
    """``uq_pay_payslip_run_employee`` plus ``_discard_payslips``: a retried
    calculation must not leave a second slip behind, which would overstate the
    run's gross and therefore the GL posting."""
    first_count = Payslip.objects.filter(run=calculated_run).count()

    recalculated = calculate_run(calculated_run, user_id=accountant_user.id)

    assert Payslip.objects.filter(run=recalculated).count() == first_count
    assert recalculated.status == PayrollRun.Status.CALCULATED


# ---------------------------------------------------------------------------
# The ledger posting
# ---------------------------------------------------------------------------

def test_posting_produces_exactly_one_balanced_entry(
    tenant, calculated_run, owner_user, accountant_user, iam_permission_stub
):
    approved = _approve(calculated_run, owner_user)
    before = JournalEntry.all_tenants.filter(tenant_id=tenant.id).count()

    entry = post_run_to_ledger(approved, user_id=owner_user.id)

    after = JournalEntry.all_tenants.filter(tenant_id=tenant.id).count()
    assert after == before + 1, "A payroll run posted more than one entry."

    assert entry.status == JournalEntry.Status.POSTED
    assert entry.source == JournalEntry.Source.PAYROLL
    assert entry.total_debit == entry.total_credit
    assert entry.source_document_type == "payroll.PayrollRun"
    assert entry.source_document_id == approved.id

    approved.refresh_from_db()
    assert approved.status == PayrollRun.Status.POSTED
    assert approved.journal_entry_id == entry.id
    assert_ledger_balanced(tenant.id)


def test_posting_the_same_run_twice_creates_no_second_entry(
    tenant, calculated_run, owner_user, iam_permission_stub
):
    """Exactly-once, proved at the level the guarantee actually lives at.

    ``run.idempotency_key`` is ``payroll:{run.id}`` and is enforced by
    ``uq_entry_idempotency``, so re-posting the *same draft* returns the
    original entry rather than doubling salary expense. The run's own state
    machine refuses the second attempt earlier still (``POSTED -> POSTED`` is
    not a legal move), and the ``journal_entry`` OneToOne is the third guard —
    all three are asserted here because each protects a different caller.
    """
    approved = _approve(calculated_run, owner_user)
    draft = build_journal_entry(approved)

    from apps.accounting.services.posting import post_entry

    first = post_entry(draft, tenant_id=tenant.id, user_id=owner_user.id)
    second = post_entry(
        build_journal_entry(approved), tenant_id=tenant.id, user_id=owner_user.id
    )

    assert second.id == first.id
    assert (
        JournalEntry.all_tenants.filter(
            tenant_id=tenant.id, idempotency_key=approved.idempotency_key
        ).count()
        == 1
    )

    # And the state machine refuses a re-post outright once the run is POSTED.
    post_run_to_ledger(approved, user_id=owner_user.id)
    approved.refresh_from_db()
    with pytest.raises(ValueError):
        post_run_to_ledger(approved, user_id=owner_user.id)

    assert (
        JournalEntry.all_tenants.filter(
            tenant_id=tenant.id, source=JournalEntry.Source.PAYROLL
        ).count()
        == 1
    )


def test_debit_side_equals_gross_plus_employer_contributions(
    tenant, chart_of_accounts, calculated_run, owner_user, iam_permission_stub
):
    """``Dr Salary expense (gross) + Dr Employer SI expense (employer share)``.

    Split by cost centre so departmental labour cost comes out of the ledger
    rather than out of a spreadsheet that disagrees with it — hence the sum
    over the salary-expense lines rather than a single-line assertion.
    """
    approved = _approve(calculated_run, owner_user)
    entry = post_run_to_ledger(approved, user_id=owner_user.id)
    approved.refresh_from_db()

    lines = list(JournalLine.all_tenants.filter(tenant_id=tenant.id, entry=entry))
    debits = {line.account_id: ZERO for line in lines}
    for line in lines:
        debits[line.account_id] += line.debit

    salary_account = chart_of_accounts[SALARY_EXPENSE]
    employer_account = chart_of_accounts[EMPLOYER_SI_EXPENSE]

    employer_share = sum(
        (slip.social_insurance_employer for slip in Payslip.objects.filter(run=approved)),
        ZERO,
    )

    assert debits[salary_account.id] == quantize_currency(
        approved.total_gross, TEST_CURRENCY
    )
    if employer_share > ZERO:
        assert debits[employer_account.id] == quantize_currency(
            employer_share, TEST_CURRENCY
        )

    assert entry.total_debit == quantize_currency(
        approved.total_gross + employer_share, TEST_CURRENCY
    )
    assert entry.total_debit == quantize_currency(
        approved.total_employer_cost, TEST_CURRENCY
    )


def test_credit_side_equals_net_plus_tax_plus_insurance_plus_other(
    tenant, chart_of_accounts, calculated_run, owner_user, iam_permission_stub
):
    """The credit side is a set of *payables*, not cash.

    The liability is created here and discharged later by ``mark_run_paid``.
    Collapsing the two into one "Dr expense / Cr bank" entry is what makes the
    balance sheet misstate the payroll liability between the run and the
    transfer, and makes the bank file impossible to reconcile.
    """
    approved = _approve(calculated_run, owner_user)
    entry = post_run_to_ledger(approved, user_id=owner_user.id)
    approved.refresh_from_db()

    slips = list(Payslip.objects.filter(run=approved))
    net = sum((slip.net_amount for slip in slips), ZERO)
    tax = sum((slip.income_tax_amount for slip in slips), ZERO)
    si_employee = sum((slip.social_insurance_employee for slip in slips), ZERO)
    si_employer = sum((slip.social_insurance_employer for slip in slips), ZERO)
    other = sum((slip.other_deductions for slip in slips), ZERO)

    credits = {}
    for line in JournalLine.all_tenants.filter(tenant_id=tenant.id, entry=entry):
        credits[line.account_id] = credits.get(line.account_id, ZERO) + line.credit

    assert credits.get(chart_of_accounts[SALARIES_PAYABLE].id, ZERO) == (
        quantize_currency(net, TEST_CURRENCY)
    )
    if tax > ZERO:
        assert credits.get(chart_of_accounts[INCOME_TAX_PAYABLE].id, ZERO) == (
            quantize_currency(tax, TEST_CURRENCY)
        )
    if si_employee + si_employer > ZERO:
        assert credits.get(chart_of_accounts[SOCIAL_INSURANCE_PAYABLE].id, ZERO) == (
            quantize_currency(si_employee + si_employer, TEST_CURRENCY)
        )
    if other > ZERO:
        assert credits.get(chart_of_accounts[OTHER_DEDUCTIONS_PAYABLE].id, ZERO) == (
            quantize_currency(other, TEST_CURRENCY)
        )

    assert entry.total_credit == quantize_currency(
        net + tax + si_employee + si_employer + other, TEST_CURRENCY
    )
    # No cash account is touched by the accrual.
    assert credits.get(chart_of_accounts["bank_main"].id, ZERO) == ZERO


def test_posting_a_run_with_no_payslips_is_refused(tenant, open_period, tax_scale):
    """An empty run that posts is a zero-value entry the ledger would reject
    anyway; failing in payroll names the real cause."""
    from apps.payroll.services.engine import PayrollError

    today = date.today()
    empty = PayrollRun.objects.create(
        tenant=tenant, name="Empty run",
        period_start=today.replace(day=1), period_end=_month_end(today),
        pay_date=_month_end(today), frequency=PayrollRun.Frequency.OFF_CYCLE,
        currency=TEST_CURRENCY,
    )
    with pytest.raises(PayrollError):
        build_journal_entry(empty)


# ---------------------------------------------------------------------------
# Segregation of duties
# ---------------------------------------------------------------------------

def test_approving_a_run_you_calculated_yourself_is_refused(
    calculated_run, accountant_user, iam_permission_stub
):
    """The control that turns payroll fraud into a conspiracy.

    ``calculated_by`` is stored on the run for exactly this comparison. One
    person adding a ghost employee and approving their own work is a single
    point of failure; requiring a second pair of eyes does not make fraud
    impossible, but it makes it require collusion — which is detectable,
    deterrable, and the difference between a control that exists and one that
    does not.
    """
    assert calculated_run.calculated_by_id == accountant_user.id

    submitted = _submit(calculated_run)
    with pytest.raises(PermissionDenied) as exc:
        approve_run(submitted, accountant_user)
    assert "Segregation of duties" in str(exc.value)

    submitted.refresh_from_db()
    assert submitted.status == PayrollRun.Status.PENDING_APPROVAL
    assert submitted.approved_by_id is None


def test_a_different_user_may_approve(calculated_run, owner_user, iam_permission_stub):
    approved = _approve(calculated_run, owner_user)
    assert approved.status == PayrollRun.Status.APPROVED
    assert approved.approved_by_id == owner_user.id
    assert approved.approved_at is not None
    assert approved.locked is True, "An approved run must be frozen against edits."


def test_an_unapproved_run_cannot_be_posted(calculated_run, owner_user):
    """POSTED is reachable only from APPROVED — the ledger is never written on
    somebody's unreviewed arithmetic."""
    with pytest.raises(ValueError):
        post_run_to_ledger(calculated_run, user_id=owner_user.id)


def test_a_posted_run_cannot_be_recalculated(
    calculated_run, owner_user, iam_permission_stub, accountant_user
):
    """After POSTED the only correction is a reversing journal entry; moving
    the run backwards would silently change a filed period."""
    approved = _approve(calculated_run, owner_user)
    post_run_to_ledger(approved, user_id=owner_user.id)
    approved.refresh_from_db()

    with pytest.raises(ValueError):
        calculate_run(approved, user_id=accountant_user.id)


# ---------------------------------------------------------------------------
# Progressive tax
# ---------------------------------------------------------------------------

#: ``(annual_taxable, expected_annual_tax, label)`` against SIMPLE_SCALE:
#: 0% to 50 000, 10% to 150 000, 20% above. Every figure is the marginal
#: result, computed slab by slab.
TAX_CASES: tuple[tuple[str, str, str], ...] = (
    ("0", "0", "no income"),
    ("24000", "0", "inside the zero band"),
    ("50000", "0", "exactly on the first boundary"),
    ("50040", "4.00", "one step over the first boundary"),
    ("60000", "1000.00", "inside the 10% band"),
    ("150000", "10000.00", "exactly on the second boundary"),
    ("240000", "28000.00", "inside the 20% band: 10000 + 90000*0.20"),
    ("600000", "100000.00", "well into the top band: 10000 + 450000*0.20"),
)


@pytest.mark.parametrize(
    ("annual_taxable", "expected_annual_tax", "label"),
    TAX_CASES,
    ids=[case[2] for case in TAX_CASES],
)
def test_progressive_tax_is_marginal_across_bracket_boundaries(
    tenant, tax_scale, annual_taxable, expected_annual_tax, label
):
    """Each slab taxes only the income inside it.

    The bug this catches is applying the top rate to the whole income, which
    is correct for anyone in the bottom bracket (so it survives a naive test)
    and overcharges everybody else. It produces the "a raise made my net pay
    drop" complaint, which is impossible under a correct marginal calculation
    — see the property test below.
    """
    annual = Decimal(annual_taxable)
    monthly = annual / Decimal("12")

    monthly_tax = compute_income_tax(
        tenant_id=tenant.id,
        country=tenant.country,
        taxable_monthly=monthly,
        as_of=date.today(),
        periods_per_year=Decimal("12"),
        exemption=ZERO,
    )

    assert monthly_tax * Decimal("12") == Decimal(expected_annual_tax), (
        f"{label}: annualised tax on {annual} should be {expected_annual_tax}"
    )


def test_top_rate_is_not_applied_to_the_whole_income(tenant, tax_scale):
    """States the classic bug as an explicit non-assertion."""
    annual = Decimal("600000")
    monthly_tax = compute_income_tax(
        tenant_id=tenant.id,
        country=tenant.country,
        taxable_monthly=annual / Decimal("12"),
        as_of=date.today(),
    )
    flat_top_rate = annual * Decimal("0.200000")
    assert monthly_tax * Decimal("12") != flat_top_rate
    assert monthly_tax * Decimal("12") == Decimal("100000.00")


@pytest.mark.parametrize(
    ("lower", "higher"),
    [
        ("49000", "51000"),
        ("149000", "151000"),
        ("199000", "260000"),
    ],
    ids=["first boundary", "second boundary", "deep in the top band"],
)
def test_a_raise_never_reduces_take_home_pay(tenant, tax_scale, lower, higher):
    """The monotonicity property of a marginal scale.

    Post-tax income must be strictly increasing in pre-tax income. If it ever
    is not, some slab is being applied to income outside it.
    """
    def net_annual(annual: str) -> Decimal:
        gross = Decimal(annual)
        monthly_tax = compute_income_tax(
            tenant_id=tenant.id,
            country=tenant.country,
            taxable_monthly=gross / Decimal("12"),
            as_of=date.today(),
        )
        return gross - monthly_tax * Decimal("12")

    assert net_annual(higher) > net_annual(lower)


def test_a_missing_tax_scale_raises_rather_than_taxing_nothing(tenant, tax_scale):
    """"No brackets configured" must never be read as "no tax due".

    The scale is expired rather than deleted: ``TenantQuerySet.delete`` is
    disabled on tenant-scoped models, and expiry is what actually happens when
    a jurisdiction publishes a new table.
    """
    from apps.payroll.services.engine import PayrollError

    for bracket in TaxBracket.objects.all():
        TaxBracket.all_tenants.filter(pk=bracket.pk).update(
            effective_to=date(2000, 1, 1)
        )

    with pytest.raises(PayrollError) as exc:
        compute_income_tax(
            tenant_id=tenant.id,
            country=tenant.country,
            taxable_monthly=Decimal("10000"),
            as_of=date.today(),
        )
    assert "No income tax scale" in str(exc.value)


def test_tax_exemption_reduces_the_annual_base_before_the_scale(tenant, tax_scale):
    """Personal relief is applied to annualised income, not to each slab."""
    common = dict(
        tenant_id=tenant.id,
        country=tenant.country,
        taxable_monthly=Decimal("60000") / Decimal("12"),
        as_of=date.today(),
    )
    without = compute_income_tax(**common, exemption=ZERO)
    with_relief = compute_income_tax(**common, exemption=Decimal("10000"))

    # 60 000 - 10 000 = 50 000, i.e. exactly the top of the zero band.
    assert without * Decimal("12") == Decimal("1000.00")
    assert with_relief == ZERO
