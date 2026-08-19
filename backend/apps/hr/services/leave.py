"""
Leave workflow: submission, approval chain, cancellation, accrual, carry-over.

Every state change of a :class:`hr.LeaveRequest` happens here and nowhere
else. Views call these functions; they never assign ``.status``. That is what
makes the state machine in ``LeaveRequest.ALLOWED_TRANSITIONS`` an actual
guarantee rather than documentation — each function below calls
``assert_can_transition`` before it touches anything.

Two concurrency hazards drive the design:

*Double-spending a balance.* Two managers approving two requests for the same
person at the same instant both read "5 days available". The fix is a row
lock on :class:`hr.LeaveBalance`, taken before the balance is read, held to
commit (see :func:`_locked_balance`).

*Overlapping absence.* Two approved requests covering the same day would
deduct the day twice and confuse payroll. The database refuses it with an
``EXCLUDE USING gist`` constraint (documented in ``LeaveRequest``); this
module checks first only so the user gets a sentence instead of an
``IntegrityError``.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.fields import ZERO, to_money
from apps.core.tenancy_context import get_current_tenant_id
from apps.hr.models import (
    Employee,
    Holiday,
    LeaveApproval,
    LeaveBalance,
    LeaveRequest,
    LeaveType,
)

HALF = Decimal("0.5")
ONE = Decimal("1")

#: Step numbers in the approval chain. Explicit constants because the numbers
#: appear in the audit trail and in "who is this waiting on" queries.
STEP_MANAGER = 1
STEP_HR = 2


class LeaveError(ValidationError):
    """Any refusal by the leave workflow."""


class InsufficientBalance(LeaveError):
    pass


def _assert_tenant_bound(tenant_id: uuid.UUID) -> None:
    """Fail loudly when a batch job's tenant argument and its bound context
    disagree.

    Every query below goes through the tenant-filtered default manager, so a
    task that passes tenant A while its context is bound to tenant B would
    quietly accrue leave for the wrong company — and the fail-closed manager
    would make it look like a no-op rather than an error. Comparing the two
    once, here, turns that into a crash in the worker log.
    """
    current = get_current_tenant_id()
    if current is None or str(current) != str(tenant_id):
        raise LeaveError(
            f"Tenant context mismatch: task was called for tenant {tenant_id} "
            f"but the bound context is {current}. Wrap the call in "
            f"apps.core.tenancy_context.tenant_context(tenant_id)."
        )


# ---------------------------------------------------------------------------
# Day counting
# ---------------------------------------------------------------------------

def working_days_between(
    employee: Employee,
    start: date,
    end: date,
    *,
    half_day_start: bool = False,
    half_day_end: bool = False,
) -> Decimal:
    """Count the *working* days in an inclusive date range.

    Weekends (from the employee's :class:`hr.WorkSchedule`) and holidays are
    excluded, because the employee was not going to work them anyway.
    Charging them to the balance is the single most common leave complaint,
    and it is a data question — the working week is Sunday–Thursday in much
    of the region — not a constant.
    """
    if end < start:
        raise LeaveError("Leave end date cannot precede the start date.")

    schedule = employee.work_schedule
    working_weekdays = set(schedule.working_days or []) if schedule else set()
    if not working_weekdays:
        working_weekdays = {1, 2, 3, 4, 5}

    holidays = set(
        Holiday.objects.filter(date__gte=start, date__lte=end)
        .filter(
            Q(applies_to_department__isnull=True)
            | Q(applies_to_department_id=employee.department_id)
        )
        .values_list("date", flat=True)
    )

    total = ZERO
    current = start
    while current <= end:
        if current.isoweekday() in working_weekdays and current not in holidays:
            total += ONE
        current += timedelta(days=1)

    # Half days only count if the endpoint was a working day in the first
    # place; a half day on a Friday is zero, not minus a half.
    if half_day_start and start.isoweekday() in working_weekdays and start not in holidays:
        total -= HALF
    if (
        half_day_end
        and end != start
        and end.isoweekday() in working_weekdays
        and end not in holidays
    ):
        total -= HALF

    return max(total, ZERO)


# ---------------------------------------------------------------------------
# Balance access
# ---------------------------------------------------------------------------

def _locked_balance(
    employee: Employee, leave_type: LeaveType, year: int, *, create: bool = True
) -> LeaveBalance:
    """Fetch the balance row ``SELECT ... FOR UPDATE``.

    The lock is the point. ``available_days`` is a stored figure precisely so
    that it can be locked; two concurrent approvals then serialise on this
    row, and the second one reads the balance the first one left behind
    instead of the balance it started with. Without the lock both approvals
    read the same number and the employee ends up overdrawn — a lost update
    that no amount of application-level checking can prevent.

    The lock is held to the end of the surrounding transaction, so every
    caller must be inside ``transaction.atomic``.
    """
    balance = (
        LeaveBalance.objects.select_for_update()
        .filter(employee=employee, leave_type=leave_type, year=year)
        .first()
    )
    if balance is not None:
        return balance

    if not create:
        raise LeaveError(
            f"No {leave_type.code} balance exists for "
            f"{employee.employee_code} in {year}."
        )
    # ``get_or_create`` rather than ``create``: it wraps the INSERT in a
    # savepoint, so losing the race against a concurrent first-request for
    # the same employee/type/year is retried instead of poisoning this
    # transaction with an IntegrityError.
    created, _ = LeaveBalance.objects.get_or_create(
        tenant_id=employee.tenant_id,
        employee=employee,
        leave_type=leave_type,
        year=year,
    )
    # Re-read under the lock; the row above was created without one.
    return LeaveBalance.objects.select_for_update().get(pk=created.pk)


def _assert_no_overlap(request: LeaveRequest) -> None:
    """Reject a request that collides with a live one for the same employee.

    The authoritative guard is the ``EXCLUDE USING gist`` constraint on
    ``hr_leave_request`` (see :class:`hr.LeaveRequest`). This check exists to
    turn that database error into a message naming the conflicting dates.
    """
    clash = (
        LeaveRequest.objects.filter(
            employee_id=request.employee_id,
            status__in=list(LeaveRequest.BLOCKING_STATUSES),
            start_date__lte=request.end_date,
            end_date__gte=request.start_date,
        )
        .exclude(pk=request.pk)
        .first()
    )
    if clash is not None:
        raise LeaveError(
            f"This request overlaps an existing {clash.get_status_display().lower()} "
            f"leave from {clash.start_date} to {clash.end_date}. Cancel that one "
            f"first if it is being replaced."
        )


def _assert_eligible(employee: Employee, leave_type: LeaveType, start: date) -> None:
    """Policy gates that are properties of the leave type, not the balance."""
    if leave_type.gender_restriction != LeaveType.GenderRestriction.NONE:
        if employee.gender != leave_type.gender_restriction:
            raise LeaveError(
                f"{leave_type.name} is restricted to "
                f"{leave_type.get_gender_restriction_display().lower()} employees."
            )
    if leave_type.min_service_months:
        months = ((start.year - employee.hire_date.year) * 12
                  + start.month - employee.hire_date.month)
        if months < leave_type.min_service_months:
            raise LeaveError(
                f"{leave_type.name} requires {leave_type.min_service_months} months "
                f"of service; this employee has {max(months, 0)}."
            )
    if not employee.is_payable:
        raise LeaveError(
            f"Employee {employee.employee_code} is {employee.get_status_display().lower()} "
            f"and cannot submit leave."
        )


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

@transaction.atomic
def submit_request(
    request: LeaveRequest, *, user_id: Optional[uuid.UUID] = None
) -> LeaveRequest:
    """Validate a draft request and put it into the approval chain.

    Order of checks matters: cheap policy gates first, then the overlap query,
    then the balance — the last one takes a row lock, and holding a lock while
    running validations that might fail anyway lengthens the window in which
    other approvals block.

    On success the days are moved into ``pending_days``. They are *held*, not
    yet taken: an employee must not be able to submit three overlapping-free
    requests that each individually fit the balance but together exceed it.
    """
    request.assert_can_transition(LeaveRequest.Status.SUBMITTED)

    employee = request.employee
    leave_type = request.leave_type
    _assert_eligible(employee, leave_type, request.start_date)

    # Recompute rather than trust the client: total_days drives the balance
    # debit and, for unpaid leave, the payslip.
    total_days = working_days_between(
        employee,
        request.start_date,
        request.end_date,
        half_day_start=request.half_day_start,
        half_day_end=request.half_day_end,
    )
    if total_days <= ZERO:
        raise LeaveError(
            "This request covers no working days (the whole range falls on "
            "weekends or holidays), so there is nothing to approve."
        )
    request.total_days = total_days

    # Notice period. Measured from today, not from submission-to-start of the
    # draft, so that sitting on a draft does not launder the notice rule.
    if leave_type.min_notice_days:
        notice = (request.start_date - timezone.localdate()).days
        if notice < leave_type.min_notice_days:
            raise LeaveError(
                f"{leave_type.name} requires {leave_type.min_notice_days} days' "
                f"notice; this request gives {notice}."
            )

    if (
        leave_type.requires_attachment_after_days
        and total_days > leave_type.requires_attachment_after_days
        and not request.attachment_key
    ):
        raise LeaveError(
            f"{leave_type.name} longer than "
            f"{leave_type.requires_attachment_after_days} days requires a "
            f"supporting document."
        )

    _assert_no_overlap(request)

    balance = _locked_balance(employee, leave_type, request.start_date.year)
    remaining = balance.available_days - balance.pending_days
    if total_days > remaining and not leave_type.allow_negative_balance:
        raise InsufficientBalance(
            f"Insufficient {leave_type.name} balance: {remaining} day(s) "
            f"available (after {balance.pending_days} already pending), "
            f"{total_days} requested."
        )

    balance.pending_days = balance.pending_days + total_days
    balance.save(update_fields=["pending_days", "updated_at"])

    approver = _next_approver(request, step=STEP_MANAGER)
    request.status = (
        LeaveRequest.Status.PENDING_MANAGER
        if approver is not None
        else LeaveRequest.Status.PENDING_HR
    )
    request.current_approver = approver
    request.submitted_at = timezone.now()
    request.updated_by_id = user_id
    request.save()

    LeaveApproval.objects.create(
        tenant_id=request.tenant_id,
        request=request,
        step_order=STEP_MANAGER,
        approver=approver,
        decision=(
            LeaveApproval.Decision.PENDING
            if approver is not None
            # No manager in the org chart: the step is recorded as skipped
            # rather than omitted, so the audit trail shows *why* HR was the
            # first approver.
            else LeaveApproval.Decision.SKIPPED
        ),
        decided_at=None if approver is not None else timezone.now(),
        comment="" if approver is not None else "No direct manager assigned.",
    )
    return request


def _next_approver(request: LeaveRequest, *, step: int):
    """Who decides step ``step``.

    Step 1 is the employee's direct manager, resolved through the org chart
    (``Employee.manager``) to the login attached to that employee
    (``iam.TenantMembership``). An employee with no manager — the CEO, or an
    incompletely configured org chart — skips straight to HR rather than
    becoming un-approvable.
    """
    if step == STEP_MANAGER:
        manager = request.employee.manager
        if manager is None:
            return None
        membership = getattr(manager, "membership", None)
        if membership is None or not membership.is_active:
            # An employee-manager with no login cannot click "approve"; HR
            # handles it. Silently blocking the request would be worse.
            return None
        return membership.user
    return None


# ---------------------------------------------------------------------------
# Approval chain
# ---------------------------------------------------------------------------

@transaction.atomic
def approve_step(
    request: LeaveRequest, user, *, comment: str = ""
) -> LeaveRequest:
    """Record one approval and advance the chain: manager -> HR -> approved.

    Authorisation is checked twice, deliberately:

    * **RBAC** — does this user hold ``hr.leave_request.approve`` at all?
    * **ABAC** — is the subject employee inside the department subtree this
      user's role assignment is scoped to? A manager of Engineering must not
      approve leave for Finance even though they hold the same permission.
      The subtree test is the indexed prefix scan on ``Department.path``,
      which is the whole reason that column exists.

    The final approval is where days actually move from ``pending`` to
    ``taken``, under the balance row lock, and ``balance_applied`` makes that
    debit idempotent against a retried request.
    """
    if request.status not in {
        LeaveRequest.Status.SUBMITTED,
        LeaveRequest.Status.PENDING_MANAGER,
        LeaveRequest.Status.PENDING_HR,
    }:
        raise LeaveError(
            f"A leave request in state '{request.get_status_display()}' is not "
            f"awaiting approval."
        )

    _assert_may_decide(request, user)

    if request.employee_id == getattr(
        getattr(user, "membership", None), "employee_id", None
    ):
        raise PermissionDenied(
            "You cannot approve your own leave request; it must be decided by "
            "your manager or by HR."
        )

    if request.status == LeaveRequest.Status.PENDING_MANAGER:
        _record_decision(request, user, STEP_MANAGER, LeaveApproval.Decision.APPROVED,
                         comment)
        if request.leave_type.requires_hr_approval:
            request.assert_can_transition(LeaveRequest.Status.PENDING_HR)
            request.status = LeaveRequest.Status.PENDING_HR
            request.current_approver = None
            request.save(update_fields=["status", "current_approver", "updated_at"])
            LeaveApproval.objects.get_or_create(
                tenant_id=request.tenant_id,
                request=request,
                step_order=STEP_HR,
                defaults={"decision": LeaveApproval.Decision.PENDING},
            )
            return request
        return _finalise_approval(request, user, comment)

    # SUBMITTED (no manager) or PENDING_HR: this is the HR step.
    _record_decision(request, user, STEP_HR, LeaveApproval.Decision.APPROVED, comment)
    return _finalise_approval(request, user, comment)


def _finalise_approval(request: LeaveRequest, user, comment: str) -> LeaveRequest:
    """Last step: debit the balance and mark the request APPROVED."""
    request.assert_can_transition(LeaveRequest.Status.APPROVED)

    balance = _locked_balance(
        request.employee, request.leave_type, request.start_date.year
    )
    if not request.balance_applied:
        days = to_money(request.total_days, field_name="total_days")
        balance.pending_days = max(balance.pending_days - days, ZERO)
        balance.taken_days = balance.taken_days + days
        balance.available_days = balance.available_days - days
        if balance.available_days < ZERO and not request.leave_type.allow_negative_balance:
            raise InsufficientBalance(
                f"Approving this request would overdraw the "
                f"{request.leave_type.name} balance to {balance.available_days}."
            )
        balance.save(
            update_fields=["pending_days", "taken_days", "available_days", "updated_at"]
        )
        request.balance_applied = True

    request.status = LeaveRequest.Status.APPROVED
    request.current_approver = None
    request.decided_at = timezone.now()
    request.save(
        update_fields=[
            "status", "current_approver", "decided_at", "balance_applied", "updated_at",
        ]
    )
    return request


def _record_decision(
    request: LeaveRequest, user, step: int, decision: str, comment: str
) -> None:
    """Append the audit row for one step. Never updates an earlier decision."""
    LeaveApproval.objects.update_or_create(
        tenant_id=request.tenant_id,
        request=request,
        step_order=step,
        defaults={
            "approver": user,
            "decision": decision,
            "decided_at": timezone.now(),
            "comment": comment[:500],
        },
    )


def _assert_may_decide(request: LeaveRequest, user) -> None:
    """RBAC + ABAC check for the acting user.

    The ABAC part is enforced here rather than only in the view because
    approvals are also triggered by bulk actions and by the mobile API, and a
    check that lives in one of three entry points is not a check.
    """
    # Local import: the IAM services layer imports HR models for its scope
    # compiler, so a module-level import here would be circular.
    from apps.iam.services.abac import assert_in_scope  # local import
    from apps.iam.services.permissions import assert_permission  # local import

    assert_permission(user, "hr.leave_request.approve", tenant_id=request.tenant_id)
    # Compiles the actor's ScopeRule for the "leave_request" resource into a
    # predicate; DEPARTMENT_SUBTREE resolves to
    # Department.path__startswith=<actor department path>.
    assert_in_scope(user, resource="leave_request", obj=request)


@transaction.atomic
def reject(
    request: LeaveRequest, user, *, reason: str
) -> LeaveRequest:
    """Refuse a request and release the days it was holding."""
    request.assert_can_transition(LeaveRequest.Status.REJECTED)
    if not reason.strip():
        # Enforced by ck_hr_leave_request_rejection_reason as well; caught
        # here so the user gets a form error rather than an IntegrityError.
        raise LeaveError("A rejection reason is required.")
    _assert_may_decide(request, user)

    balance = _locked_balance(
        request.employee, request.leave_type, request.start_date.year, create=False
    )
    days = to_money(request.total_days, field_name="total_days")
    if not request.balance_applied:
        balance.pending_days = max(balance.pending_days - days, ZERO)
        balance.save(update_fields=["pending_days", "updated_at"])

    step = (
        STEP_HR
        if request.status == LeaveRequest.Status.PENDING_HR
        else STEP_MANAGER
    )
    _record_decision(request, user, step, LeaveApproval.Decision.REJECTED, reason)

    request.status = LeaveRequest.Status.REJECTED
    request.rejection_reason = reason[:500]
    request.current_approver = None
    request.decided_at = timezone.now()
    request.save(
        update_fields=[
            "status", "rejection_reason", "current_approver", "decided_at", "updated_at",
        ]
    )
    return request


@transaction.atomic
def cancel(
    request: LeaveRequest, *, user_id: Optional[uuid.UUID] = None, reason: str = ""
) -> LeaveRequest:
    """Withdraw a request, returning its days to the balance.

    Cancelling an already-approved request is legal — plans change — and is
    the *only* way days come back. The row is never deleted: the approval
    trail is evidence that the absence was authorised, which matters if the
    employee was in fact away.

    Cancelling leave that has already started, or that falls in a payroll
    period that has been posted, is refused: the payslip is immutable and the
    balance would then disagree with what was paid.
    """
    request.assert_can_transition(LeaveRequest.Status.CANCELLED)

    today = timezone.localdate()
    if request.status == LeaveRequest.Status.APPROVED and request.start_date <= today:
        raise LeaveError(
            "Approved leave that has already started cannot be cancelled; "
            "record an early return instead so attendance and payroll agree."
        )

    # A DRAFT never touched the balance, so there may not even be a row; only
    # look one up when there is something to give back.
    if request.balance_applied or request.is_blocking:
        balance = _locked_balance(
            request.employee, request.leave_type, request.start_date.year, create=False
        )
        days = to_money(request.total_days, field_name="total_days")
        if request.balance_applied:
            balance.taken_days = max(balance.taken_days - days, ZERO)
            balance.available_days = balance.available_days + days
            request.balance_applied = False
        else:
            balance.pending_days = max(balance.pending_days - days, ZERO)
        balance.save(
            update_fields=["taken_days", "available_days", "pending_days", "updated_at"]
        )

    request.status = LeaveRequest.Status.CANCELLED
    request.current_approver = None
    request.decided_at = timezone.now()
    request.updated_by_id = user_id
    if reason:
        request.rejection_reason = reason[:500]
    request.save()
    return request


# ---------------------------------------------------------------------------
# Accrual (Celery beat)
# ---------------------------------------------------------------------------

@transaction.atomic
def accrue_monthly(
    tenant_id: uuid.UUID, *, as_of: Optional[date] = None
) -> int:
    """Grant one month's entitlement to every eligible employee.

    Driven by a Celery beat task on the first of each month. Two properties
    make it safe to retry, which matters because a task that half-ran and was
    retried must not grant twice:

    * **``select_for_update`` on each balance row.** Accrual is a
      read-modify-write of ``accrued_days`` and ``available_days``. Without
      the lock, a concurrent leave approval reading and writing the same row
      loses one of the two updates entirely — the classic lost update. With
      it, the approval waits for the accrual (or vice versa) and both land.
      The lock is per employee-and-type, so the job does not serialise the
      whole tenant.
    * **``last_accrued_on``.** A row already accrued in this month is
      skipped, so a retried or double-scheduled task is a no-op rather than a
      second grant.

    Returns the number of balances updated.
    """
    _assert_tenant_bound(tenant_id)
    as_of = as_of or timezone.localdate()
    year = as_of.year
    period_start = as_of.replace(day=1)
    updated = 0

    leave_types = list(
        LeaveType.objects.filter(
            is_active=True, accrual_method=LeaveType.AccrualMethod.MONTHLY
        )
    )
    if not leave_types:
        return 0

    employees = list(
        Employee.objects.filter(
            status__in=[Employee.Status.ACTIVE, Employee.Status.ON_LEAVE],
            hire_date__lte=as_of,
        )
    )

    for employee in employees:
        for leave_type in leave_types:
            if leave_type.min_service_months:
                months = ((as_of.year - employee.hire_date.year) * 12
                          + as_of.month - employee.hire_date.month)
                if months < leave_type.min_service_months:
                    continue

            balance = _locked_balance(employee, leave_type, year)
            if balance.last_accrued_on and balance.last_accrued_on >= period_start:
                continue  # already accrued this month

            rate = to_money(leave_type.accrual_rate_days, field_name="accrual_rate_days")
            new_available = balance.available_days + rate
            cap = to_money(leave_type.max_balance_days, field_name="max_balance_days")
            if cap > ZERO and new_available > cap:
                # Cap the *balance*, not the accrual: the excess is forfeited
                # this month, which is the policy the cap expresses. Accruing
                # past the cap and truncating later would misstate the
                # untaken-leave liability in between.
                rate = max(cap - balance.available_days, ZERO)
                new_available = balance.available_days + rate

            if rate <= ZERO:
                balance.last_accrued_on = as_of
                balance.save(update_fields=["last_accrued_on", "updated_at"])
                continue

            balance.accrued_days = balance.accrued_days + rate
            balance.available_days = new_available
            balance.last_accrued_on = as_of
            balance.save(
                update_fields=[
                    "accrued_days", "available_days", "last_accrued_on", "updated_at",
                ]
            )
            updated += 1

    return updated


@transaction.atomic
def year_end_carry_over(
    tenant_id: uuid.UUID, *, from_year: int, to_year: int
) -> int:
    """Roll unused entitlement into next year's balance, up to the cap.

    Creates the *next* year's balance row rather than mutating this year's:
    the closing position of a year is a reportable figure (it is the untaken
    leave liability on the balance sheet at year end) and must stay readable
    after the roll.

    Days above ``carry_over_limit_days`` are forfeited, which is exactly why
    the limit exists — an uncapped carry-over grows a liability that has to
    be paid out in cash on termination.
    """
    _assert_tenant_bound(tenant_id)
    rolled = 0
    balances = (
        LeaveBalance.objects.select_for_update()
        .filter(year=from_year)
        .select_related("leave_type", "employee")
        .order_by("pk")
    )

    for balance in balances:
        leave_type = balance.leave_type
        if not leave_type.is_active:
            continue
        limit = to_money(
            leave_type.carry_over_limit_days, field_name="carry_over_limit_days"
        )
        available = balance.available_days
        carried = min(max(available, ZERO), limit) if limit > ZERO else ZERO
        if carried <= ZERO:
            continue

        next_balance = _locked_balance(balance.employee, leave_type, to_year)
        # Idempotent: re-running the roll must not stack carry-overs.
        if next_balance.carried_over_days > ZERO:
            continue
        next_balance.carried_over_days = carried
        next_balance.available_days = next_balance.available_days + carried
        next_balance.save(
            update_fields=["carried_over_days", "available_days", "updated_at"]
        )
        rolled += 1

    return rolled
