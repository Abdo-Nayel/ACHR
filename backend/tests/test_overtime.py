"""Overtime: priced once, certified by a second person, paid exactly once.

Ported from the concepts in the standalone HRweb project, which models
``OvertimeType`` (a multiplier) separately from ``OvertimeSlip`` (hours on a
day). The separation is worth keeping — a company with weekday, weekend and
public-holiday rates expresses three rates against one payroll component and
one GL account, rather than three of each.

Three properties carry the weight:

* **The amount is frozen at approval.** Salaries move. A slip that re-prices
  itself against a later raise produces a payslip line that cannot be tied
  back to the hours worked.
* **The claimant cannot certify their own hours.** Self-approved overtime is
  the cheapest payroll fraud there is and leaves no trace.
* **A slip is paid once.** The run id stamped on the slip is the only thing
  standing between "approved overtime" and "approved overtime, paid every
  month until someone notices".
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.core.fields import ZERO
from apps.hr.models import (
    Employee,
    OvertimeSlip,
    OvertimeType,
    SalaryRevision,
    Shift,
    ShiftAssignment,
)
from apps.hr.services.overtime import (
    DEFAULT_HOURS_PER_MONTH,
    OvertimeError,
    approve_slip,
    approved_overtime_for,
    hourly_rate,
    price_slip,
    shift_on,
)
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db

#: 20 800 over 208 standard hours = exactly 100/hour, so every expected
#: figure below is checkable by eye rather than by re-running the formula.
MONTHLY = Decimal("20800.00")
WORK_DAY = date(2026, 3, 10)


@pytest.fixture
def department(tenant):
    from apps.hr.models import Department  # noqa: PLC0415

    d = Department.objects.create(tenant=tenant, code="ops", name="Operations", depth=0)
    d.path = d.build_path()
    d.save(update_fields=["path", "updated_at"])
    return d


@pytest.fixture
def worker(tenant, department) -> Employee:
    e = Employee.objects.create(
        tenant=tenant, employee_code="E-7001", first_name="Mona", last_name="Said",
        department=department, hire_date=date(2024, 1, 1),
        base_salary=MONTHLY, salary_currency=TEST_CURRENCY,
    )
    # effective_salary reads SalaryRevision, never the column.
    SalaryRevision.objects.create(
        tenant=tenant, employee=e, effective_date=date(2024, 1, 1),
        previous_salary=ZERO, new_salary=MONTHLY, currency=TEST_CURRENCY,
        reason="hire",
    )
    return e


@pytest.fixture
def ot_type(tenant) -> OvertimeType:
    return OvertimeType.objects.create(
        tenant=tenant, code="WKND", name="Weekend", multiplier=Decimal("2.000000"),
    )


def _slip(tenant, worker, ot_type, *, hours="3", status=OvertimeSlip.Status.SUBMITTED,
          when=WORK_DAY, created_by=None) -> OvertimeSlip:
    return OvertimeSlip.objects.create(
        tenant=tenant, employee=worker, overtime_type=ot_type, work_date=when,
        hours=Decimal(hours), currency=TEST_CURRENCY, status=status,
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def test_hourly_rate_is_derived_from_salary_history_not_the_column(
    tenant, worker, permission_catalogue
):
    """`Employee.base_salary` holds today's figure; a slip for a past date
    must use the figure that applied then."""
    SalaryRevision.objects.create(
        tenant=tenant, employee=worker, effective_date=date(2026, 6, 1),
        previous_salary=MONTHLY, new_salary=Decimal("41600.00"),
        currency=TEST_CURRENCY, reason="promotion",
    )

    before, _ = hourly_rate(worker, WORK_DAY)              # March, pre-raise
    after, _ = hourly_rate(worker, date(2026, 7, 1))       # July, post-raise

    assert before == MONTHLY / DEFAULT_HOURS_PER_MONTH     # 100/hour
    assert after == Decimal("41600.00") / DEFAULT_HOURS_PER_MONTH


def test_the_multiplier_is_applied(tenant, worker, ot_type):
    slip = price_slip(_slip(tenant, worker, ot_type, hours="3"))

    # 100/hour × 3 hours × 2.0 = 600
    assert slip.hourly_rate == Decimal("100.00")
    assert slip.amount == Decimal("600.00")


def test_a_missing_standard_hours_setting_is_reported_not_hidden(tenant, worker):
    """Dividing by the wrong month is a 30% error, not a rounding one, so the
    assumption is surfaced rather than silently applied."""
    _, assumptions = hourly_rate(worker, WORK_DAY)

    assert any("standard_hours_per_month" in a for a in assumptions)


def test_a_configured_standard_month_overrides_the_default(tenant, worker):
    tenant.settings = {**(tenant.settings or {}),
                       "payroll": {"standard_hours_per_month": "160"}}
    tenant.save(update_fields=["settings", "updated_at"])

    rate, assumptions = hourly_rate(worker, WORK_DAY)

    assert rate == MONTHLY / Decimal("160")
    assert assumptions == []


def test_an_archived_overtime_type_cannot_be_priced(tenant, worker, ot_type):
    ot_type.is_active = False
    ot_type.save(update_fields=["is_active", "updated_at"])

    with pytest.raises(OvertimeError):
        price_slip(_slip(tenant, worker, ot_type))


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

def test_approval_prices_and_stamps_the_slip(tenant, worker, ot_type, owner_user):
    slip = approve_slip(_slip(tenant, worker, ot_type), user_id=owner_user.id)

    assert slip.status == OvertimeSlip.Status.APPROVED
    assert slip.amount == Decimal("600.00")
    assert slip.approved_by_id == owner_user.id
    assert slip.approved_at is not None


def test_claiming_and_approving_your_own_hours_is_refused(
    tenant, worker, ot_type, owner_user
):
    slip = _slip(tenant, worker, ot_type, created_by=owner_user)

    with pytest.raises(OvertimeError):
        approve_slip(slip, user_id=owner_user.id)


def test_a_second_approver_may_certify(
    tenant, worker, ot_type, owner_user, accountant_user
):
    slip = _slip(tenant, worker, ot_type, created_by=accountant_user)

    approved = approve_slip(slip, user_id=owner_user.id)

    assert approved.status == OvertimeSlip.Status.APPROVED


def test_a_draft_slip_cannot_jump_straight_to_approved(tenant, worker, ot_type,
                                                       owner_user):
    """DRAFT -> APPROVED skips submission, which is where the claim is made."""
    slip = _slip(tenant, worker, ot_type, status=OvertimeSlip.Status.DRAFT)

    with pytest.raises(ValueError):
        approve_slip(slip, user_id=owner_user.id)


def test_the_frozen_amount_survives_a_later_raise(
    tenant, worker, ot_type, owner_user
):
    """The assertion the whole design exists for."""
    slip = approve_slip(_slip(tenant, worker, ot_type), user_id=owner_user.id)
    SalaryRevision.objects.create(
        tenant=tenant, employee=worker, effective_date=date(2026, 4, 1),
        previous_salary=MONTHLY, new_salary=Decimal("41600.00"),
        currency=TEST_CURRENCY, reason="promotion",
    )

    slip.refresh_from_db()
    assert slip.amount == Decimal("600.00")   # not 1200


# ---------------------------------------------------------------------------
# Handover to payroll
# ---------------------------------------------------------------------------

def test_only_approved_unpaid_slips_are_offered_to_payroll(
    tenant, worker, ot_type, owner_user
):
    approve_slip(_slip(tenant, worker, ot_type, when=WORK_DAY), user_id=owner_user.id)
    _slip(tenant, worker, ot_type, when=WORK_DAY + timedelta(days=1))  # submitted only

    due = approved_overtime_for(worker, date(2026, 3, 1), date(2026, 3, 31))

    assert [s.work_date for s in due] == [WORK_DAY]


def test_a_slip_already_claimed_by_a_run_is_not_offered_again(
    tenant, worker, ot_type, owner_user
):
    """The double-payment guard, asserted directly."""
    from apps.payroll.models import PayrollRun  # noqa: PLC0415

    slip = approve_slip(_slip(tenant, worker, ot_type), user_id=owner_user.id)
    run = PayrollRun.objects.create(
        tenant=tenant, name="March 2026", period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31), pay_date=date(2026, 3, 31),
        currency=TEST_CURRENCY,
    )
    OvertimeSlip.objects.filter(pk=slip.pk).update(
        payroll_run=run, status=OvertimeSlip.Status.PAID
    )

    assert approved_overtime_for(worker, date(2026, 3, 1), date(2026, 3, 31)) == []


def test_slips_outside_the_period_are_not_offered(tenant, worker, ot_type, owner_user):
    approve_slip(_slip(tenant, worker, ot_type, when=date(2026, 2, 20)),
                 user_id=owner_user.id)

    assert approved_overtime_for(worker, date(2026, 3, 1), date(2026, 3, 31)) == []


def test_one_slip_per_employee_per_type_per_day(tenant, worker, ot_type):
    """Two rows for the same night is the shape a double payment takes."""
    from django.db.utils import IntegrityError  # noqa: PLC0415

    _slip(tenant, worker, ot_type)

    with pytest.raises(IntegrityError):
        _slip(tenant, worker, ot_type)


# ---------------------------------------------------------------------------
# Shift assignment
# ---------------------------------------------------------------------------

def test_the_most_recent_assignment_wins_when_they_overlap(tenant, worker):
    """A short cover written over a standing rotation is a legal overlap, so
    the tie-break has to be stated rather than prevented."""
    night = Shift.objects.create(
        tenant=tenant, code="N", name="Night", start_time="22:00", end_time="06:00",
        crosses_midnight=True,
    )
    day = Shift.objects.create(
        tenant=tenant, code="D", name="Day", start_time="09:00", end_time="17:00",
    )
    ShiftAssignment.objects.create(
        tenant=tenant, employee=worker, shift=day, start_date=date(2026, 1, 1)
    )
    ShiftAssignment.objects.create(
        tenant=tenant, employee=worker, shift=night,
        start_date=date(2026, 3, 1), end_date=date(2026, 3, 14),
    )

    assert shift_on(worker, WORK_DAY).shift_id == night.id
    # ...and after the cover ends, the standing rotation is in force again.
    assert shift_on(worker, date(2026, 4, 1)).shift_id == day.id


def test_an_assignment_does_not_apply_before_it_starts(tenant, worker):
    day = Shift.objects.create(
        tenant=tenant, code="D", name="Day", start_time="09:00", end_time="17:00",
    )
    a = ShiftAssignment.objects.create(
        tenant=tenant, employee=worker, shift=day, start_date=date(2026, 5, 1)
    )

    assert a.covers(date(2026, 5, 1)) is True
    assert a.covers(date(2026, 4, 30)) is False
    assert shift_on(worker, date(2026, 4, 30)) is None
