"""The dashboard's operations block.

These are the numbers a manager acts on without opening the underlying screen,
so the failure that matters is not "slightly off" — it is a figure that looks
authoritative and answers a different question. Three of those are pinned
here:

* **Payroll cost that is really net pay.** Understates the budget line by the
  employer's social-insurance contribution — about a fifth at the default
  Egyptian rates — and nothing on the screen hints at the omission.
* **Payroll cost that counts unapproved runs.** A CALCULATED run can still be
  recalculated to a different number, so including it makes the dashboard
  disagree with the general ledger.
* **Attendance divided by every calendar row.** Weekends and holidays are not
  absences; a company on a five-day week would show ~71% attendance forever.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.core.fields import ZERO
from apps.hr.models import (
    AttendanceRecord,
    Department,
    Employee,
    LeaveRequest,
    LeaveType,
    Shift,
    ShiftAssignment,
)
from apps.payroll.models import PayrollRun
from apps.reporting.services.hr_metrics import compute_hr_metrics
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db

TODAY = date.today()
MONTH_START = TODAY.replace(day=1)


@pytest.fixture
def dept(tenant) -> Department:
    d = Department.objects.create(tenant=tenant, code="ops", name="Ops", depth=0)
    d.path = d.build_path()
    d.save(update_fields=["path", "updated_at"])
    return d


def _employee(tenant, dept, code, *, status=Employee.Status.ACTIVE, hired=None,
              terminated=None) -> Employee:
    return Employee.objects.create(
        tenant=tenant, employee_code=code, first_name=code, last_name="Test",
        department=dept, hire_date=hired or date(2024, 1, 1), status=status,
        termination_date=terminated,
        base_salary=Decimal("10000.00"), salary_currency=TEST_CURRENCY,
    )


# ---------------------------------------------------------------------------
# Headcount
# ---------------------------------------------------------------------------

def test_headcount_counts_active_and_on_leave_but_not_suspended(tenant, dept):
    """Someone on maternity leave is still on the payroll and still costs
    money. Suspended always needs an explicit decision — payroll excludes it
    for the same reason."""
    _employee(tenant, dept, "A")
    _employee(tenant, dept, "B", status=Employee.Status.ON_LEAVE)
    _employee(tenant, dept, "C", status=Employee.Status.SUSPENDED)
    _employee(tenant, dept, "D", status=Employee.Status.TERMINATED,
              terminated=date(2025, 6, 30))

    m = compute_hr_metrics(tenant.id)

    assert m["headcount"]["active"] == 2
    assert m["headcount"]["on_leave"] == 1


def test_joiners_and_leavers_in_the_window_are_reported(tenant, dept):
    _employee(tenant, dept, "OLD")
    _employee(tenant, dept, "NEW", hired=MONTH_START + timedelta(days=2))
    _employee(tenant, dept, "GONE", status=Employee.Status.TERMINATED,
              terminated=MONTH_START + timedelta(days=3))

    m = compute_hr_metrics(tenant.id)

    assert m["headcount"]["joined"] == 1
    assert m["headcount"]["left"] == 1


# ---------------------------------------------------------------------------
# Payroll cost
# ---------------------------------------------------------------------------

def _posted_entry(tenant, chart_of_accounts, owner_user):
    """A real posted entry, because ck_pay_run_posted_has_entry requires one.

    Built through `post_entry` rather than by writing a row: the constraint
    exists to stop a run claiming it reached the ledger when it did not, and a
    fixture that fabricated the entry would be testing around the guard.
    """
    from apps.accounting.services.posting import post_entry  # noqa: PLC0415
    from tests.conftest import make_draft  # noqa: PLC0415

    return post_entry(
        make_draft(
            debit_account=chart_of_accounts["payroll_salary_expense"],
            credit_account=chart_of_accounts["payroll_salaries_payable"],
            amount=Decimal("100.00"),
        ),
        tenant_id=tenant.id, user_id=owner_user.id,
    )


#: `employer` is `total_employer_cost`, which the engine writes as
#: gross + contributions — the whole cost of employment, not the
#: contributions alone. 100 000 gross + 18 750 contributions = 118 750.
def _run(tenant, status, *, gross="100000", employer="118750", net="74000",
         deductions="26000", approver=None, entry=None):
    return PayrollRun.objects.create(
        tenant=tenant, name=f"run-{status}", period_start=MONTH_START,
        period_end=MONTH_START + timedelta(days=27),
        pay_date=MONTH_START + timedelta(days=27),
        frequency=PayrollRun.Frequency.OFF_CYCLE, currency=TEST_CURRENCY,
        status=status, employee_count=3,
        approved_by=approver, journal_entry=entry,
        total_gross=Decimal(gross), total_employer_cost=Decimal(employer),
        total_net=Decimal(net), total_deductions=Decimal(deductions),
    )


def test_payroll_cost_is_gross_plus_employer_contributions_not_net(
    tenant, owner_user
):
    """Net is what lands in people's accounts. The company also pays employer
    social insurance on top, and that is part of the cost of employing them."""
    _run(tenant, PayrollRun.Status.APPROVED, approver=owner_user)

    m = compute_hr_metrics(tenant.id)

    assert Decimal(m["payroll"]["cost"]) == Decimal("118750")
    # ...and the contribution on its own is the difference, not the field.
    assert Decimal(m["payroll"]["employer_contributions"]) == Decimal("18750")
    assert Decimal(m["payroll"]["net_paid"]) == Decimal("74000")


def test_unapproved_runs_are_excluded_and_counted(tenant):
    """A CALCULATED run can still be recalculated to a different number.
    Counting it would make the dashboard disagree with the ledger."""
    _run(tenant, PayrollRun.Status.CALCULATED)

    m = compute_hr_metrics(tenant.id)

    assert Decimal(m["payroll"]["cost"]) == ZERO
    assert m["payroll"]["runs_excluded"] == 1
    assert any("not yet approved" in a for a in m["assumptions"])


def test_posted_and_paid_runs_both_count(
    tenant, chart_of_accounts, open_period, owner_user
):
    _run(tenant, PayrollRun.Status.POSTED, gross="50000", employer="59000",
         entry=_posted_entry(tenant, chart_of_accounts, owner_user))
    PayrollRun.objects.create(
        tenant=tenant, name="second", period_start=MONTH_START,
        period_end=MONTH_START + timedelta(days=27),
        pay_date=MONTH_START + timedelta(days=26),
        frequency=PayrollRun.Frequency.MONTHLY, currency=TEST_CURRENCY,
        status=PayrollRun.Status.PAID, employee_count=1,
        journal_entry=_posted_entry(tenant, chart_of_accounts, owner_user),
        total_gross=Decimal("10000"), total_employer_cost=Decimal("11000"),
        total_net=Decimal("8000"), total_deductions=Decimal("2000"),
    )

    m = compute_hr_metrics(tenant.id)

    assert Decimal(m["payroll"]["cost"]) == Decimal("70000")   # 59 000 + 11 000
    assert m["payroll"]["runs_counted"] == 2


def test_a_run_paid_outside_the_window_does_not_count(
    tenant, chart_of_accounts, open_period, owner_user
):
    PayrollRun.objects.create(
        tenant=tenant, name="last month", period_start=MONTH_START - timedelta(days=40),
        period_end=MONTH_START - timedelta(days=12),
        pay_date=MONTH_START - timedelta(days=12),
        frequency=PayrollRun.Frequency.MONTHLY, currency=TEST_CURRENCY,
        status=PayrollRun.Status.PAID, employee_count=3,
        journal_entry=_posted_entry(tenant, chart_of_accounts, owner_user),
        total_gross=Decimal("99999"), total_employer_cost=ZERO,
        total_net=Decimal("99999"), total_deductions=ZERO,
    )

    m = compute_hr_metrics(tenant.id)

    assert Decimal(m["payroll"]["cost"]) == ZERO


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

def _attend(tenant, employee, day, status):
    return AttendanceRecord.objects.create(
        tenant=tenant, employee=employee, work_date=day, status=status
    )


def test_weekends_holidays_and_leave_are_not_absences(tenant, dept):
    """Dividing by every calendar row would show a five-day-week company at
    roughly 71% attendance forever — a number nobody can act on."""
    e = _employee(tenant, dept, "A")
    _attend(tenant, e, MONTH_START, "present")
    _attend(tenant, e, MONTH_START + timedelta(days=1), "weekend")
    _attend(tenant, e, MONTH_START + timedelta(days=2), "holiday")
    _attend(tenant, e, MONTH_START + timedelta(days=3), "on_leave")

    m = compute_hr_metrics(tenant.id)

    assert m["attendance"]["expected_days"] == 1
    assert Decimal(m["attendance"]["rate"]) == Decimal("1.0000")


def test_late_counts_as_attended(tenant, dept):
    """Being late is a punctuality problem, not an attendance one. Conflating
    them hides both."""
    e = _employee(tenant, dept, "A")
    _attend(tenant, e, MONTH_START, "late")
    _attend(tenant, e, MONTH_START + timedelta(days=1), "present")

    m = compute_hr_metrics(tenant.id)

    assert Decimal(m["attendance"]["rate"]) == Decimal("1.0000")
    assert m["attendance"]["late_days"] == 1


def test_a_half_day_counts_as_half(tenant, dept):
    e = _employee(tenant, dept, "A")
    _attend(tenant, e, MONTH_START, "present")
    _attend(tenant, e, MONTH_START + timedelta(days=1), "half_day")

    m = compute_hr_metrics(tenant.id)

    # 1 full + 0.5 credited over 2 expected days.
    assert Decimal(m["attendance"]["rate"]) == Decimal("0.7500")


def test_absence_lowers_the_rate(tenant, dept):
    e = _employee(tenant, dept, "A")
    for offset, status in enumerate(["present", "present", "present", "absent"]):
        _attend(tenant, e, MONTH_START + timedelta(days=offset), status)

    m = compute_hr_metrics(tenant.id)

    assert Decimal(m["attendance"]["rate"]) == Decimal("0.7500")
    assert m["attendance"]["absent_days"] == 1


def test_no_attendance_data_reads_as_not_computable_not_as_perfect(tenant, dept):
    """The distinction the whole widget turns on: silence is not 100%."""
    _employee(tenant, dept, "A")

    m = compute_hr_metrics(tenant.id)

    assert m["attendance"]["rate"] is None
    assert any("not computable" in a for a in m["assumptions"])


# ---------------------------------------------------------------------------
# Pending leave
# ---------------------------------------------------------------------------

def _leave(tenant, employee, ltype, status, start):
    return LeaveRequest.objects.create(
        tenant=tenant, employee=employee, leave_type=ltype,
        start_date=start, end_date=start + timedelta(days=1),
        total_days=Decimal("2"), status=status,
    )


@pytest.fixture
def leave_type(tenant) -> LeaveType:
    return LeaveType.objects.create(tenant=tenant, code="ANNUAL", name="Annual leave")


def test_only_requests_awaiting_a_decision_are_pending(tenant, dept, leave_type):
    """DRAFT is still the employee's to edit and has been submitted to nobody;
    counting it would put work on a manager's list that nobody asked for."""
    e = _employee(tenant, dept, "A")
    _leave(tenant, e, leave_type, LeaveRequest.Status.DRAFT, TODAY + timedelta(days=5))
    _leave(tenant, e, leave_type, LeaveRequest.Status.SUBMITTED, TODAY + timedelta(days=7))
    _leave(tenant, e, leave_type, LeaveRequest.Status.PENDING_HR, TODAY + timedelta(days=9))
    _leave(tenant, e, leave_type, LeaveRequest.Status.APPROVED, TODAY + timedelta(days=11))

    m = compute_hr_metrics(tenant.id)

    assert m["leave"]["pending_count"] == 2


def test_a_pending_request_that_has_already_started_is_flagged(
    tenant, dept, leave_type
):
    """Someone is probably already away while the approval sits unmade —
    surfacing that is the point of the list."""
    e = _employee(tenant, dept, "A")
    _leave(tenant, e, leave_type, LeaveRequest.Status.SUBMITTED, TODAY - timedelta(days=2))

    m = compute_hr_metrics(tenant.id)

    assert m["leave"]["pending"][0]["starts_in_days"] == -2


# ---------------------------------------------------------------------------
# Shift coverage
# ---------------------------------------------------------------------------

def test_coverage_counts_employees_with_an_assignment_in_the_horizon(tenant, dept):
    e1 = _employee(tenant, dept, "A")
    _employee(tenant, dept, "B")          # no assignment
    shift = Shift.objects.create(
        tenant=tenant, code="M", name="Morning",
        start_time="09:00", end_time="17:00",
    )
    ShiftAssignment.objects.create(
        tenant=tenant, employee=e1, shift=shift, start_date=TODAY
    )

    m = compute_hr_metrics(tenant.id)

    assert m["coverage"]["employees_covered"] == 1
    assert m["coverage"]["employees_uncovered"] == 1
    assert m["coverage"]["shifts"][0]["employees"] == 1


def test_an_assignment_lapsing_inside_the_horizon_is_flagged_expiring(tenant, dept):
    """The one a scheduler needs to act on. An open-ended assignment needs no
    action and must not be counted as if it did."""
    e1 = _employee(tenant, dept, "A")
    e2 = _employee(tenant, dept, "B")
    shift = Shift.objects.create(
        tenant=tenant, code="M", name="Morning",
        start_time="09:00", end_time="17:00",
    )
    ShiftAssignment.objects.create(
        tenant=tenant, employee=e1, shift=shift, start_date=TODAY,
        end_date=TODAY + timedelta(days=3),
    )
    ShiftAssignment.objects.create(
        tenant=tenant, employee=e2, shift=shift, start_date=TODAY
    )

    m = compute_hr_metrics(tenant.id)

    assert m["coverage"]["shifts"][0]["expiring"] == 1
    assert m["coverage"]["shifts"][0]["employees"] == 2


def test_an_expired_assignment_does_not_cover(tenant, dept):
    e = _employee(tenant, dept, "A")
    shift = Shift.objects.create(
        tenant=tenant, code="M", name="Morning",
        start_time="09:00", end_time="17:00",
    )
    ShiftAssignment.objects.create(
        tenant=tenant, employee=e, shift=shift,
        start_date=TODAY - timedelta(days=30), end_date=TODAY - timedelta(days=1),
    )

    m = compute_hr_metrics(tenant.id)

    assert m["coverage"]["employees_covered"] == 0
    assert m["coverage"]["employees_uncovered"] == 1


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

def test_hr_metrics_do_not_leak_between_tenants(
    tenant, other_tenant, dept, owner_user
):
    _employee(tenant, dept, "A")
    _run(tenant, PayrollRun.Status.APPROVED, approver=owner_user)

    theirs = compute_hr_metrics(other_tenant.id)

    assert theirs["headcount"]["active"] == 0
    assert Decimal(theirs["payroll"]["cost"]) == ZERO
