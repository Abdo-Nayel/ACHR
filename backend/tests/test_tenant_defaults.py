"""A new tenant must arrive usable, and an empty payroll run must say why.

Both failures here were reported from the running app and share a shape: the
screen was not broken, it simply had nothing in it and said nothing about
that.

* **Empty Leave Type / Shift dropdowns.** A tenant is provisioned with a chart
  of accounts because ``seed_chart_of_accounts`` treats that as part of
  provisioning. Nothing did the same for HR, so the Leave Request form loaded
  with an empty required dropdown and could not be submitted at all.
* **"Run X has no payslips".** A pay run whose frequency matched no employee
  calculated to zero payslips, reached CALCULATED, and failed two steps later
  at approval with a message describing the symptom rather than the cause.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.core.management.commands.seed_tenant_defaults import seed_tenant_defaults
from apps.core.fields import ZERO
from apps.hr.models import Department, Employee, LeaveType, OvertimeType, SalaryRevision, Shift
from apps.payroll.models import PayrollRun
from apps.payroll.services.engine import PayrollError, calculate_run
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

def test_seeding_creates_the_leave_types_the_request_form_needs(tenant):
    seed_tenant_defaults(tenant.id)

    codes = set(LeaveType.objects.values_list("code", flat=True))
    assert {"ANNUAL", "SICK", "UNPAID"} <= codes


def test_unpaid_leave_is_the_one_that_reaches_payroll(tenant):
    """The distinction that matters: unpaid days prorate the salary, and a
    seeded set where none of them do makes the integration silently inert."""
    seed_tenant_defaults(tenant.id)

    unpaid = LeaveType.objects.get(code="UNPAID")
    annual = LeaveType.objects.get(code="ANNUAL")
    assert unpaid.affects_payroll is True
    assert unpaid.is_paid is False
    assert annual.affects_payroll is False


def test_seeding_creates_shifts_including_one_that_crosses_midnight(tenant):
    """A night shift read as ending before it starts computes negative hours,
    so the flag has to be set on the seeded row rather than left to the user
    to discover."""
    seed_tenant_defaults(tenant.id)

    night = Shift.objects.get(code="NIGHT")
    assert night.crosses_midnight is True
    assert Shift.objects.get(code="MORNING").crosses_midnight is False


def test_seeded_overtime_types_carry_no_component(tenant):
    """Deliberately unset: the component decides which expense account the
    money lands in, and guessing that puts overtime in the wrong cost centre
    for a year. Payroll refuses a type with no component and says so."""
    seed_tenant_defaults(tenant.id)

    assert OvertimeType.objects.count() == 3
    assert all(t.component_id is None for t in OvertimeType.objects.all())
    assert OvertimeType.objects.get(code="WEEKEND").multiplier == Decimal("2.0")


def test_seeding_is_idempotent(tenant):
    """Safe to re-run on a tenant that is only missing some of it."""
    first = seed_tenant_defaults(tenant.id)
    second = seed_tenant_defaults(tenant.id)

    assert first["leave_types"] == 3
    assert second["leave_types"] == 0
    assert LeaveType.objects.count() == 3


def test_defaults_do_not_leak_between_tenants(tenant, other_tenant):
    seed_tenant_defaults(tenant.id)

    from apps.core.tenancy_context import tenant_context  # noqa: PLC0415

    with tenant_context(other_tenant.id):
        assert LeaveType.objects.count() == 0
        assert Shift.objects.count() == 0


# ---------------------------------------------------------------------------
# The empty payroll run
# ---------------------------------------------------------------------------

@pytest.fixture
def monthly_staff(tenant):
    dept = Department.objects.create(tenant=tenant, code="hq", name="HQ", depth=0)
    dept.path = dept.build_path()
    dept.save(update_fields=["path", "updated_at"])
    e = Employee.objects.create(
        tenant=tenant, employee_code="E-6001", first_name="Omar", last_name="Fathy",
        department=dept, hire_date=date(date.today().year - 1, 1, 1),
        base_salary=Decimal("10000.00"), salary_currency=TEST_CURRENCY,
    )
    SalaryRevision.objects.create(
        tenant=tenant, employee=e, effective_date=date(date.today().year - 1, 1, 1),
        previous_salary=ZERO, new_salary=Decimal("10000.00"),
        currency=TEST_CURRENCY, reason="hire",
    )
    return e


def _run(tenant, frequency, open_period):
    start = open_period.start_date
    return PayrollRun.objects.create(
        tenant=tenant, name="probe", period_start=start,
        period_end=start + timedelta(days=6), pay_date=start + timedelta(days=6),
        frequency=frequency, currency=TEST_CURRENCY,
    )


def test_a_run_matching_nobody_is_refused_not_left_empty(
    tenant, monthly_staff, open_period, chart_of_accounts
):
    """The bug: this used to reach CALCULATED with zero payslips."""
    run = _run(tenant, PayrollRun.Frequency.WEEKLY, open_period)

    with pytest.raises(PayrollError):
        calculate_run(run)

    run.refresh_from_db()
    assert run.status != PayrollRun.Status.CALCULATED


def test_the_refusal_names_the_frequency_mismatch(
    tenant, monthly_staff, open_period, chart_of_accounts
):
    """"Run X has no payslips" describes the symptom. This names the cause,
    which is the difference between a dead end and a fixable error."""
    run = _run(tenant, PayrollRun.Frequency.WEEKLY, open_period)

    with pytest.raises(PayrollError) as exc:
        calculate_run(run)

    message = str(exc.value)
    assert "weekly" in message
    assert "monthly" in message


def test_the_refusal_names_an_empty_workforce(
    tenant, open_period, chart_of_accounts
):
    run = _run(tenant, PayrollRun.Frequency.MONTHLY, open_period)

    with pytest.raises(PayrollError) as exc:
        calculate_run(run)

    assert "no active staff" in str(exc.value)
