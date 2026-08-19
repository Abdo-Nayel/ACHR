"""Operational HR figures for the executive dashboard.

Companion to ``kpis.py``, which answers the financial half. Kept separate
because the two are guarded differently: the ratio block needs
``reporting.balance_sheet.read`` (solvency is not everyone's business) while
headcount and attendance sit with HR's own permissions. One endpoint would
have to be gated by the loosest of the two.

Decisions worth stating
-----------------------
**Payroll cost is employer cost, not net pay.** ``total_net`` is what lands in
people's accounts; the company also pays employer social insurance on top.
Reporting net as "payroll cost" understates the real figure by that
contribution — for the Egyptian rates in the default settings, by roughly a
fifth. ``total_employer_cost`` is the number a budget holder means.

Note what that field actually holds: ``engine.py`` writes it as
``gross + employer contributions``, i.e. the *whole* cost of employment, not
the contributions alone. The name invites the opposite reading, and adding
gross to it — the obvious thing to do — double-counts the entire salary bill.

**Only runs that reached the ledger count.** A CALCULATED run is a proposal
that can still be recalculated to a different number; including it would make
the dashboard disagree with the general ledger, and the ledger is the one that
is right. So APPROVED, POSTED and PAID count; anything earlier does not, and
the count of what was excluded is returned so the figure can be explained.

**Attendance percentage excludes days nobody was expected in.** Weekends,
holidays and approved leave are not absences. Dividing by all calendar rows
would show a company on a five-day week at roughly 71% attendance forever,
which is a number nobody can act on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from django.db.models import Count, Q, Sum

from apps.core.fields import ZERO

#: Attendance rows that represent a day the employee was expected to work.
#: WEEKEND and HOLIDAY are not; ON_LEAVE is approved absence, not a failure to
#: attend, and counting it as one penalises taking the leave you are owed.
_EXPECTED = ("present", "absent", "late", "half_day")
#: Of those, the ones where the person actually turned up. LATE counts as
#: attended — being late is a punctuality problem, not an attendance one, and
#: conflating them hides both.
_ATTENDED = ("present", "late")
#: A half day is half a day. Counting it as a full attendance overstates the
#: figure; counting it as an absence understates it by the same amount.
_HALF = "half_day"

#: Leave states awaiting a human decision. DRAFT is not one of them — it is
#: still the employee's to edit and has been submitted to nobody.
PENDING_LEAVE_STATES = ("submitted", "pending_manager", "pending_hr")

#: Payroll runs whose figures are committed enough to report as cost.
COMMITTED_RUN_STATES = ("approved", "posted", "paid")

#: How far ahead "upcoming shift coverage" looks. A fortnight is the horizon a
#: scheduler can still act on; a quarter is a report, not a dashboard widget.
COVERAGE_DAYS = 14


@dataclass(slots=True)
class _Window:
    start: date
    end: date


def _month_window(on: date) -> _Window:
    first = on.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return _Window(first, nxt - timedelta(days=1))


def compute_hr_metrics(
    tenant_id: uuid.UUID,
    *,
    as_of: Optional[date] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict:
    """Headcount, payroll cost, attendance, pending leave and coverage.

    ``date_from``/``date_to`` bound the attendance and payroll windows. They
    default to the calendar month containing ``as_of`` — payroll is a monthly
    fact for most companies, and an attendance rate over an arbitrary window
    invites comparison against a figure computed over a different one.
    """
    from apps.hr.models import (  # noqa: PLC0415
        AttendanceRecord,
        Employee,
        LeaveRequest,
        ShiftAssignment,
    )
    from apps.payroll.models import PayrollRun  # noqa: PLC0415

    # Explicitly scoped with `all_tenants.filter(tenant_id=...)`, exactly as
    # `kpis.py` does, rather than relying on the ambient TenantManager. The
    # manager reads a ContextVar, so a caller that passes one tenant_id while
    # a different tenant is bound — a Celery task, a management command, an
    # admin tool looping over tenants — would silently receive the *bound*
    # tenant's figures under the requested tenant's name. Taking a tenant_id
    # argument and then ignoring it is the worst of both.
    scoped = lambda model: model.all_tenants.filter(tenant_id=tenant_id)  # noqa: E731

    as_of = as_of or date.today()
    window = _Window(date_from, date_to) if (date_from and date_to) else _month_window(as_of)
    assumptions: list[str] = []

    # -- headcount ----------------------------------------------------------
    # ACTIVE and ON_LEAVE are both employed; someone on maternity leave is
    # still on the payroll and still costs money. SUSPENDED is excluded for
    # the same reason payroll excludes it: it always needs an explicit
    # decision.
    headcount = scoped(Employee).filter(
        status__in=[Employee.Status.ACTIVE, Employee.Status.ON_LEAVE]
    ).count()
    on_leave_today = scoped(Employee).filter(status=Employee.Status.ON_LEAVE).count()

    joined_this_window = scoped(Employee).filter(
        hire_date__gte=window.start, hire_date__lte=window.end
    ).count()
    left_this_window = scoped(Employee).filter(
        termination_date__gte=window.start, termination_date__lte=window.end
    ).count()

    # -- payroll cost -------------------------------------------------------
    committed = scoped(PayrollRun).filter(
        status__in=COMMITTED_RUN_STATES,
        pay_date__gte=window.start,
        pay_date__lte=window.end,
    )
    totals = committed.aggregate(
        gross=Sum("total_gross"),
        net=Sum("total_net"),
        employer=Sum("total_employer_cost"),
        deductions=Sum("total_deductions"),
        runs=Count("id"),
    )
    gross = totals["gross"] or ZERO
    # `PayrollRun.total_employer_cost` is *already* gross plus the employer's
    # contributions — see engine.py, which writes
    # `total_employer_cost = totals["gross"] + totals["employer"]`, and
    # `post_run_to_ledger`, which checks the entry's debit against it. Adding
    # gross to it again double-counts the entire salary bill. The name reads
    # like "the contributions" and means "the whole cost of employment"; the
    # contribution on its own is the difference.
    employer_cost = totals["employer"] or ZERO
    payroll_cost = employer_cost if employer_cost > ZERO else gross
    contributions = max(employer_cost - gross, ZERO)

    uncommitted = scoped(PayrollRun).filter(
        pay_date__gte=window.start, pay_date__lte=window.end
    ).exclude(status__in=COMMITTED_RUN_STATES).exclude(status="cancelled").count()
    if uncommitted:
        assumptions.append(
            f"{uncommitted} payroll run(s) in this window are not yet approved "
            f"and are excluded — their figures can still change."
        )
    if totals["runs"] == 0:
        assumptions.append(
            "No approved payroll run falls in this window, so payroll cost "
            "reads zero rather than estimating from salaries."
        )

    # -- attendance ---------------------------------------------------------
    rows = scoped(AttendanceRecord).filter(
        work_date__gte=window.start, work_date__lte=window.end
    ).aggregate(
        expected=Count("id", filter=Q(status__in=_EXPECTED)),
        attended=Count("id", filter=Q(status__in=_ATTENDED)),
        half=Count("id", filter=Q(status=_HALF)),
        late=Count("id", filter=Q(status="late")),
        absent=Count("id", filter=Q(status="absent")),
    )
    expected = rows["expected"] or 0
    if expected:
        # Half days count as 0.5 of an attendance.
        credited = Decimal(rows["attended"] or 0) + (Decimal(rows["half"] or 0) / 2)
        attendance_rate = (credited / Decimal(expected)).quantize(Decimal("0.0001"))
    else:
        attendance_rate = None
        assumptions.append(
            "No attendance was captured in this window, so the rate is not "
            "computable — it is not 100%."
        )

    # -- pending leave ------------------------------------------------------
    pending_qs = (
        scoped(LeaveRequest).filter(status__in=PENDING_LEAVE_STATES)
        .select_related("employee", "leave_type")
        .order_by("start_date")
    )
    pending = [
        {
            "id": str(r.id),
            "employee": str(r.employee),
            "leave_type": getattr(r.leave_type, "name", ""),
            "start_date": r.start_date.isoformat() if r.start_date else None,
            "end_date": r.end_date.isoformat() if r.end_date else None,
            "total_days": str(r.total_days),
            "status": r.status,
            # A request whose start date has passed is blocking someone who is
            # probably already away; surfacing that is the point of the list.
            "starts_in_days": (r.start_date - as_of).days if r.start_date else None,
        }
        for r in pending_qs[:25]
    ]
    pending_total = pending_qs.count()

    # -- upcoming shift coverage -------------------------------------------
    horizon = as_of + timedelta(days=COVERAGE_DAYS)
    covering = scoped(ShiftAssignment).filter(start_date__lte=horizon).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=as_of)
    ).select_related("shift", "employee")

    by_shift: dict[str, dict] = {}
    covered_employees: set = set()
    for assignment in covering:
        shift = assignment.shift
        bucket = by_shift.setdefault(
            str(shift.id),
            {
                "shift": f"{shift.code} — {shift.name}",
                "start_time": shift.start_time.strftime("%H:%M") if shift.start_time else "",
                "end_time": shift.end_time.strftime("%H:%M") if shift.end_time else "",
                "crosses_midnight": shift.crosses_midnight,
                "employees": 0,
                # An assignment that lapses inside the horizon is the one a
                # scheduler needs to see; an open-ended one needs no action.
                "expiring": 0,
            },
        )
        bucket["employees"] += 1
        covered_employees.add(assignment.employee_id)
        if assignment.end_date and assignment.end_date <= horizon:
            bucket["expiring"] += 1

    uncovered = headcount - len(covered_employees)

    return {
        "as_of": as_of.isoformat(),
        "date_from": window.start.isoformat(),
        "date_to": window.end.isoformat(),
        "headcount": {
            "active": headcount,
            "on_leave": on_leave_today,
            "joined": joined_this_window,
            "left": left_this_window,
        },
        "payroll": {
            # What the company actually spends: gross plus employer
            # contributions, taken straight from the run's control total.
            "cost": str(payroll_cost),
            "gross": str(gross),
            "employer_contributions": str(contributions),
            "net_paid": str(totals["net"] or ZERO),
            "runs_counted": totals["runs"] or 0,
            "runs_excluded": uncommitted,
        },
        "attendance": {
            "rate": None if attendance_rate is None else str(attendance_rate),
            "expected_days": expected,
            "attended_days": rows["attended"] or 0,
            "absent_days": rows["absent"] or 0,
            "late_days": rows["late"] or 0,
            "half_days": rows["half"] or 0,
        },
        "leave": {"pending_count": pending_total, "pending": pending},
        "coverage": {
            "horizon_days": COVERAGE_DAYS,
            "shifts": sorted(by_shift.values(), key=lambda s: s["shift"]),
            "employees_covered": len(covered_employees),
            "employees_uncovered": max(uncovered, 0),
        },
        "assumptions": assumptions,
    }


__all__ = ["COVERAGE_DAYS", "PENDING_LEAVE_STATES", "compute_hr_metrics"]
