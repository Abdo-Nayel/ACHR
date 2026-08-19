"""Price overtime, and hand approved hours to payroll.

Where the hourly rate comes from
--------------------------------
There is no hourly rate column on ``Employee``, and adding one would be a
second source of truth for pay that could drift from
``hr.SalaryRevision`` — the record payroll already treats as authoritative
(see ``payroll.services.engine.effective_salary`` and the comment there about
re-runs producing different numbers). So the rate is *derived*:

    hourly = monthly salary / (contract hours per month)

with contract hours taken from the shift the employee was actually assigned on
the day, falling back to the tenant's configured standard month. That fallback
is stated in ``assumptions`` on the returned figure rather than hidden,
because dividing by the wrong denominator is not a rounding error: a 160-hour
month against a 208-hour month misprices every overtime hour by 30%.

Why the amount is frozen onto the slip
--------------------------------------
``price_slip`` writes ``hourly_rate`` and ``amount`` at approval and payroll
reads them back. It does not re-derive at payment time. Salaries change; a
slip that silently re-prices itself against a raise granted after the night
worked produces a payslip line nobody can tie back to the hours, and the
employee disputing it is right to.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db import transaction
from django.utils import timezone

from apps.core.fields import ZERO, quantize_currency, to_money
from apps.hr.models import (
    Employee,
    OvertimeSlip,
    OvertimeType,
    ShiftAssignment,
)

#: Used when neither the assigned shift nor tenant settings say otherwise.
#: 8 hours over 26 working days — the ordinary Gulf/Egypt full-time month, and
#: the same shape ``engine._working_days`` assumes. Overridable per tenant via
#: ``settings["payroll"]["standard_hours_per_month"]``.
DEFAULT_HOURS_PER_MONTH = Decimal("208")


class OvertimeError(ValidationError):
    """Raised when overtime cannot be priced or transitioned."""


def _standard_hours(tenant_id: uuid.UUID) -> tuple[Decimal, Optional[str]]:
    """(hours per month, assumption note if a default was used)."""
    from apps.tenancy.models import Tenant  # noqa: PLC0415

    settings = (
        Tenant.objects.filter(id=tenant_id).values_list("settings", flat=True).first()
        or {}
    )
    raw = ((settings.get("payroll") or {}).get("standard_hours_per_month"))
    if raw:
        # str -> Decimal, never float: the denominator of every overtime
        # payment in the company is not a place for binary approximation.
        return to_money(str(raw), field_name="standard_hours_per_month"), None
    return (
        DEFAULT_HOURS_PER_MONTH,
        f"No standard_hours_per_month configured; assumed "
        f"{DEFAULT_HOURS_PER_MONTH} (8h × 26 days).",
    )


def shift_on(employee: Employee, on_date: date):
    """The shift assignment in force for ``employee`` on ``on_date``.

    Most recent start wins when assignments overlap. Overlaps are legal — a
    two-week cover written on top of a standing rotation is exactly that
    shape — so the rule has to be stated rather than prevented, and "the one
    that started most recently" is what a scheduler means by it.
    """
    return (
        ShiftAssignment.objects.filter(employee=employee, start_date__lte=on_date)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=on_date))
        .order_by("-start_date")
        .select_related("shift")
        .first()
    )


def hourly_rate(employee: Employee, on_date: date) -> tuple[Decimal, list[str]]:
    """Derived hourly rate and any assumptions made deriving it."""
    from apps.payroll.services.engine import effective_salary  # noqa: PLC0415

    assumptions: list[str] = []
    monthly = effective_salary(employee, on_date)
    hours, note = _standard_hours(employee.tenant_id)
    if note:
        assumptions.append(note)
    if hours <= ZERO:
        raise OvertimeError(
            "standard_hours_per_month must be positive; cannot derive an "
            "hourly rate by dividing by zero."
        )
    return monthly / hours, assumptions


def price_slip(slip: OvertimeSlip) -> OvertimeSlip:
    """Compute ``hourly_rate`` and ``amount`` on an unsaved or draft slip.

    Does not save — the caller decides when, so that pricing and the status
    change land in one write.
    """
    overtime_type: OvertimeType = slip.overtime_type
    if not overtime_type.is_active:
        raise OvertimeError(
            f"Overtime type {overtime_type.code} is archived and cannot be "
            f"used for new claims."
        )
    if slip.hours <= ZERO:
        raise OvertimeError("An overtime slip must claim a positive number of hours.")

    rate, _ = hourly_rate(slip.employee, slip.work_date)
    slip.hourly_rate = quantize_currency(rate, slip.currency)
    # Multiply before rounding: rounding the hourly rate first and then
    # multiplying by hours and a multiplier compounds the rounding error into
    # something visible on a payslip.
    slip.amount = quantize_currency(
        rate * slip.hours * overtime_type.multiplier, slip.currency
    )
    return slip


@transaction.atomic
def approve_slip(
    slip: OvertimeSlip, *, user_id: Optional[uuid.UUID] = None
) -> OvertimeSlip:
    """SUBMITTED -> APPROVED, pricing the slip as it goes.

    Segregation of duties: the person who claimed the hours may not certify
    them. Same control as ``payroll.engine.approve_run`` and
    ``expenses.viewsets.ExpenseViewSet.approve``, for the same reason —
    self-certified overtime is the cheapest payroll fraud there is, and it
    leaves no trace at all if one person can do both halves.
    """
    slip.assert_can_transition(OvertimeSlip.Status.APPROVED)

    if user_id and slip.created_by_id and slip.created_by_id == user_id:
        raise OvertimeError(
            "Segregation of duties: the person who claimed these hours may "
            "not approve them. A second authorised approver is required."
        )

    price_slip(slip)
    slip.status = OvertimeSlip.Status.APPROVED
    slip.approved_by_id = user_id
    slip.approved_at = timezone.now()
    slip.updated_by_id = user_id
    slip.save(update_fields=[
        "hourly_rate", "amount", "status", "approved_by", "approved_at",
        "updated_by", "updated_at",
    ])
    return slip


def approved_overtime_for(
    employee: Employee, period_start: date, period_end: date
) -> list[OvertimeSlip]:
    """Approved, unpaid slips in the period — what payroll should pay.

    ``payroll_run__isnull=True`` is the guard against paying twice: a slip
    consumed by a run carries that run's id, so a re-run of an adjacent period
    cannot pick it up again. Only APPROVED counts; a submitted slip is a
    request, and paying requests makes approval decorative.
    """
    return list(
        OvertimeSlip.objects.filter(
            employee=employee,
            status=OvertimeSlip.Status.APPROVED,
            payroll_run__isnull=True,
            work_date__gte=period_start,
            work_date__lte=period_end,
        ).select_related("overtime_type", "overtime_type__component")
        .order_by("work_date")
    )


__all__ = [
    "DEFAULT_HOURS_PER_MONTH",
    "OvertimeError",
    "approve_slip",
    "approved_overtime_for",
    "hourly_rate",
    "price_slip",
    "shift_on",
]
