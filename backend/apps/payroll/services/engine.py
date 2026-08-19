"""
The payroll engine: calculate, approve, post to the ledger, pay.

This module is the reason the payroll models look the way they do. Four
properties are non-negotiable and every function below exists to preserve one
of them:

**Reproducibility.**
    Re-running March in June must produce the same numbers as March did. The
    engine therefore reads *effective-dated history*
    (:class:`hr.SalaryRevision`, :class:`payroll.EmployeeComponent`,
    :class:`payroll.TaxBracket`) and never the current mutable value on
    :class:`hr.Employee`.

**Exactness.**
    Every intermediate value is a :class:`decimal.Decimal`. ``net`` is
    asserted to equal ``gross - deductions`` exactly, with Decimals, before a
    payslip is written. A float anywhere in this file would make that
    assertion fail intermittently, at the worst possible moment, on the
    largest payroll.

**Exactly-once posting.**
    The general ledger is written once per run, at ``APPROVED -> POSTED``,
    inside one transaction, with ``idempotency_key = f"payroll:{run.id}"``.

**Segregation of duties.**
    The person who computes payroll may not be the person who approves it.

Nothing here writes ``JournalLine`` rows: everything goes through
``apps.accounting.services.posting.post_entry()``, the single choke point
where ``sum(debits) == sum(credits)`` is verified.
"""

from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Optional

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounting.models import Account, Journal, JournalEntry
from apps.accounting.services.posting import (
    JournalEntryDraft,
    UnbalancedEntry,
    post_entry,
)
from apps.core.fields import ZERO, quantize_currency, to_money
from apps.hr.models import (
    AttendanceRecord,
    Employee,
    LeaveRequest,
    SalaryRevision,
)
from apps.payroll.models import (
    EmployeeComponent,
    EmployeePayrollProfile,
    PayrollComponent,
    PayrollRun,
    Payslip,
    PayslipLine,
    TaxBracket,
)

ONE = Decimal("1")
TWELVE = Decimal("12")


# ---------------------------------------------------------------------------
# GL account wiring
# ---------------------------------------------------------------------------
# Accounts are looked up by ``system_key`` rather than by code: the code of
# "Salaries payable" differs between every national standard chart of
# accounts, but its role in an automated posting does not.

SALARY_EXPENSE = "payroll_salary_expense"
EMPLOYER_SI_EXPENSE = "payroll_employer_social_insurance_expense"
SALARIES_PAYABLE = "payroll_salaries_payable"
INCOME_TAX_PAYABLE = "payroll_income_tax_payable"
SOCIAL_INSURANCE_PAYABLE = "payroll_social_insurance_payable"
OTHER_DEDUCTIONS_PAYABLE = "payroll_other_deductions_payable"
DEFAULT_BANK = "bank_main"

#: Fallback statutory rates. Real deployments override them per tenant in
#: ``Tenant.settings["payroll"]``; they exist so a misconfigured tenant fails
#: an explicit assertion rather than silently computing zero contributions.
DEFAULT_SI_EMPLOYEE_RATE = Decimal("0.110000")
DEFAULT_SI_EMPLOYER_RATE = Decimal("0.187500")


class PayrollError(ValidationError):
    """Any refusal by the engine. Distinct class so monitoring can separate a
    payroll failure from ordinary form validation."""


class NetIdentityViolation(PayrollError):
    """net != gross - deductions. Always a bug in this module, never user
    input; it is raised rather than rounded away because a payslip that does
    not add up cannot be posted to a balanced ledger."""


# ---------------------------------------------------------------------------
# Intermediate result
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ComputedLine:
    """One evaluated component, ready to become a :class:`PayslipLine`."""

    component: PayrollComponent
    sequence: int
    quantity: Decimal
    rate: Decimal
    amount: Decimal
    is_taxable: bool
    note: str


@dataclass(slots=True)
class PayslipComputation:
    """Every intermediate Decimal for one employee's pay, before persistence.

    Deliberately a plain dataclass and not a model instance: nothing here can
    be accidentally saved half-computed. The engine builds it fully, asserts
    the net identity on it, and only then writes rows.
    """

    employee: Employee
    currency: str

    #: Contractual salary effective at period_end, from SalaryRevision.
    base_salary: Decimal = ZERO
    working_days: Decimal = ZERO
    paid_days: Decimal = ZERO
    unpaid_leave_days: Decimal = ZERO
    overtime_hours: Decimal = ZERO

    #: Approved, unpaid overtime slips consumed by this run. Held so that
    #: `post_run` can stamp them with the run id — a slip that is paid but
    #: still unstamped is a slip the next run pays again.
    overtime_slips: list = field(default_factory=list)

    #: Prorated base after unpaid absence.
    prorated_base: Decimal = ZERO
    earnings_total: Decimal = ZERO
    taxable_earnings: Decimal = ZERO
    insurable_earnings: Decimal = ZERO

    gross: Decimal = ZERO
    income_tax: Decimal = ZERO
    social_insurance_employee: Decimal = ZERO
    social_insurance_employer: Decimal = ZERO
    other_deductions: Decimal = ZERO
    net: Decimal = ZERO

    lines: list[ComputedLine] = field(default_factory=list)

    @property
    def total_deductions(self) -> Decimal:
        """Everything that reduces the employee's net pay.

        The employer's social insurance share is *not* here: it is a company
        cost that never passes through the employee's hands. Including it
        would understate net pay and unbalance the GL posting, because it is
        debited to its own expense account.
        """
        return self.income_tax + self.social_insurance_employee + self.other_deductions

    def assert_net_identity(self) -> None:
        """``net == gross - deductions``, exactly, in Decimal.

        Not ``abs(diff) < 0.01``: a tolerance here is how a systematic
        one-cent error per employee becomes a five-hundred-cent difference in
        the trial balance on a fifty-thousand-employee run. The ledger's
        balance check has no tolerance either, so neither does this.
        """
        expected = self.gross - self.total_deductions
        if self.net != expected:
            raise NetIdentityViolation(
                f"Payslip for employee {self.employee.employee_code} does not "
                f"add up: net {self.net} != gross {self.gross} - deductions "
                f"{self.total_deductions} (= {expected}, difference "
                f"{self.net - expected}). Refusing to issue the payslip."
            )
        if self.net < ZERO:
            raise PayrollError(
                f"Payslip for {self.employee.employee_code} has a negative net "
                f"({self.net}). Deductions exceed gross; this must be resolved "
                f"by capping or deferring a deduction, not by paying a negative."
            )


# ---------------------------------------------------------------------------
# Effective-dated lookups
# ---------------------------------------------------------------------------

def effective_salary(employee: Employee, as_of: date) -> Decimal:
    """The salary that was contractually in force on ``as_of``.

    Reads :class:`hr.SalaryRevision`, **not** ``Employee.base_salary``. The
    column holds today's figure; a payroll run for a past period needs the
    figure that applied then. Using the column is the single most common way
    a payroll re-run produces numbers that differ from the payslips already
    issued.
    """
    revision = (
        SalaryRevision.objects.filter(employee=employee, effective_date__lte=as_of)
        .order_by("-effective_date")
        .first()
    )
    if revision is None:
        raise PayrollError(
            f"No salary revision on or before {as_of:%Y-%m-%d} for employee "
            f"{employee.employee_code}. Every employee must have at least a "
            f"HIRE revision; payroll refuses to guess from the current column."
        )
    return to_money(revision.new_salary, field_name="base_salary")


def effective_components(
    employee: Employee, period_start: date, period_end: date
) -> list[EmployeeComponent]:
    """Component assignments live at any point during the pay period.

    Ordered by ``component.sequence`` — see :class:`PayrollComponent` for why
    the order is load-bearing (a percentage-of-gross deduction must be
    evaluated after every earning has been added).
    """
    return list(
        EmployeeComponent.objects.filter(
            employee=employee,
            component__is_active=True,
            effective_from__lte=period_end,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=period_start))
        .select_related("component")
        .order_by("component__sequence", "component__code")
    )


def count_attendance(
    employee: Employee, period_start: date, period_end: date
) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(working_days, present_days, overtime_hours)`` for the period.

    Only *approved* overtime is counted: unapproved overtime on an attendance
    row is a claim, not an entitlement, and paying it automatically removes
    the control that stops a terminal misconfiguration becoming payroll cost.
    """
    records = AttendanceRecord.objects.filter(
        employee=employee, work_date__gte=period_start, work_date__lte=period_end
    )

    working_days = ZERO
    present_days = ZERO
    overtime = ZERO
    non_working = {
        AttendanceRecord.Status.WEEKEND,
        AttendanceRecord.Status.HOLIDAY,
    }
    for record in records:
        if record.status in non_working:
            continue
        working_days += ONE
        if record.status in {
            AttendanceRecord.Status.PRESENT,
            AttendanceRecord.Status.LATE,
            AttendanceRecord.Status.ON_LEAVE,
        }:
            present_days += ONE
        elif record.status == AttendanceRecord.Status.HALF_DAY:
            present_days += Decimal("0.5")
        if record.approved_at is not None:
            overtime += to_money(record.overtime_hours, field_name="overtime_hours")

    return working_days, present_days, overtime


def unpaid_leave_days(
    employee: Employee, period_start: date, period_end: date
) -> Decimal:
    """Approved, payroll-affecting leave days that fall inside the period.

    Only ``APPROVED`` requests count. A pending request must never reduce pay:
    the employee has not been told they may be absent, and if it is later
    rejected the deduction would have to be clawed back from a payslip that
    is by then immutable.
    """
    requests = LeaveRequest.objects.filter(
        employee=employee,
        status=LeaveRequest.Status.APPROVED,
        leave_type__affects_payroll=True,
        leave_type__is_paid=False,
        start_date__lte=period_end,
        end_date__gte=period_start,
    ).select_related("leave_type")

    total = ZERO
    for request in requests:
        # Clip to the period: a leave spanning a month boundary is deducted in
        # each month for its own days only, never twice in full.
        overlap_start = max(request.start_date, period_start)
        overlap_end = min(request.end_date, period_end)
        days = Decimal((overlap_end - overlap_start).days + 1)
        if request.half_day_start and overlap_start == request.start_date:
            days -= Decimal("0.5")
        if request.half_day_end and overlap_end == request.end_date:
            days -= Decimal("0.5")
        total += max(days, ZERO)
    return total


# ---------------------------------------------------------------------------
# Safe formula evaluation
# ---------------------------------------------------------------------------

#: The only AST node types a tenant-supplied formula may contain. Anything
#: else — a call, an attribute access, a subscript, a comprehension, an
#: import — is rejected before evaluation.
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BinOp, ast.UnaryOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
    ast.Constant,
    ast.Name, ast.Load,
    ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.BoolOp, ast.And, ast.Or,
    ast.IfExp,
)


def evaluate_formula(expression: str, context: dict[str, Decimal]) -> Decimal:
    """Evaluate a tenant-supplied arithmetic formula, safely.

    ``eval()`` IS REMOTE CODE EXECUTION
    -----------------------------------
    ``formula_expression`` is a string a customer types into a settings
    screen. Passing it to ``eval()`` — even with ``{"__builtins__": {}}`` —
    hands that customer arbitrary code execution on the payroll host, which
    holds every salary, national ID and bank account in the database. The
    ``__builtins__`` trick is not a mitigation; escaping it through
    ``().__class__.__bases__`` is a well-known one-liner. The same applies to
    ``exec``, ``compile(mode="exec")``, ``literal_eval`` on untrusted input
    that then gets interpolated, and to any "sandbox" built by blacklisting
    names.

    The only defensible approach is a whitelist: parse the string to an AST,
    walk every node, and refuse anything that is not plain arithmetic over a
    fixed set of names supplied by us. No calls, no attributes, no
    subscripts, no comprehensions, no lambdas, no dunder anything.

    Supported: ``+ - * / % **``, unary minus, comparisons, ``and``/``or``,
    and a conditional expression (``base * 0.1 if base > 5000 else 0``).
    Names must exist in ``context``; every value in ``context`` is a Decimal.
    """
    if not expression or not expression.strip():
        return ZERO

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise PayrollError(f"Invalid formula {expression!r}: {exc.msg}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise PayrollError(
                f"Formula {expression!r} contains a disallowed construct "
                f"({type(node).__name__}). Payroll formulas may only use "
                f"arithmetic over the provided variables."
            )
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, str)):
            # Floats are rejected at parse time: `0.1` in a formula would
            # inject binary floating point into a Decimal calculation. Write
            # it as a quoted decimal string, e.g. "0.1", or as a fraction.
            raise PayrollError(
                f"Formula {expression!r} contains a float literal "
                f"({node.value!r}). Use a quoted decimal string instead; "
                f"binary floats are forbidden in monetary arithmetic."
            )
        if isinstance(node, ast.Name) and node.id not in context:
            raise PayrollError(
                f"Formula {expression!r} references unknown variable "
                f"'{node.id}'. Available: {sorted(context)}."
            )

    return _eval_node(tree.body, context, expression)


def _eval_node(node: ast.AST, context: dict[str, Decimal], source: str) -> Any:
    """Recursive evaluator over the already-validated node whitelist."""
    if isinstance(node, ast.Constant):
        # int -> Decimal exactly; str is treated as a decimal literal.
        return Decimal(node.value)
    if isinstance(node, ast.Name):
        return context[node.id]
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, context, source)
        return -operand if isinstance(node.op, ast.USub) else +operand
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, context, source)
        right = _eval_node(node.right, context, source)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == ZERO:
                raise PayrollError(f"Formula {source!r} divides by zero.")
            return left / right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow):
            return left ** right
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context, source)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, context, source)
            if not _compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, context, source) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.IfExp):
        if _eval_node(node.test, context, source):
            return _eval_node(node.body, context, source)
        return _eval_node(node.orelse, context, source)
    raise PayrollError(f"Unsupported node in formula {source!r}.")


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    return left >= right


# ---------------------------------------------------------------------------
# Tax and social insurance
# ---------------------------------------------------------------------------

def compute_income_tax(
    *,
    tenant_id: uuid.UUID,
    country: str,
    taxable_monthly: Decimal,
    as_of: date,
    periods_per_year: Decimal = TWELVE,
    exemption: Decimal = ZERO,
) -> Decimal:
    """Progressive (marginal) withholding for one pay period.

    The scale is published annually, so the monthly taxable amount is
    annualised, taxed slab by slab, and divided back down. Taxing the monthly
    figure against annual bands directly would put almost everyone in the
    lowest bracket; taxing it against monthly-scaled bands is equivalent only
    when income is flat across the year, which a bonus month is not.

    Each slab taxes **only the portion of income inside it** — see
    :class:`payroll.TaxBracket` for the worked example. ``fixed_deduction``
    is subtracted from that slab's tax for jurisdictions that publish the
    scale in "rate minus fixed amount" form.
    """
    annual = (taxable_monthly * periods_per_year) - exemption
    if annual <= ZERO:
        return ZERO

    # Explicit tenant filter rather than the ambient manager: this function is
    # also called from management commands and from a Celery task whose tenant
    # binding is set by its caller, and a silently empty bracket list would
    # mean "no tax" rather than "misconfigured".
    brackets = list(
        TaxBracket.all_tenants.filter(
            tenant_id=tenant_id, country=country, effective_from__lte=as_of
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=as_of))
        .order_by("-effective_from", "sequence", "lower_bound")
    )
    if not brackets:
        raise PayrollError(
            f"No income tax scale configured for country '{country}' effective "
            f"{as_of:%Y-%m-%d}. Payroll will not guess a rate."
        )
    # Only the most recent scale in force applies; older ones are history.
    newest = brackets[0].effective_from
    brackets = [b for b in brackets if b.effective_from == newest]
    brackets.sort(key=lambda b: b.lower_bound)

    tax = ZERO
    for bracket in brackets:
        lower = to_money(bracket.lower_bound, field_name="lower_bound")
        if annual <= lower:
            break
        upper = (
            annual
            if bracket.upper_bound is None
            else min(annual, to_money(bracket.upper_bound, field_name="upper_bound"))
        )
        slab = upper - lower
        if slab <= ZERO:
            continue
        slab_tax = (slab * bracket.rate) - to_money(
            bracket.fixed_deduction, field_name="fixed_deduction"
        )
        tax += max(slab_tax, ZERO)

    return max(tax / periods_per_year, ZERO)


def _rate_from_settings(settings: dict, key: str, default: Decimal) -> Decimal:
    """Read a Decimal rate out of ``Tenant.settings`` JSONB.

    A JSON number decodes to a Python ``float``, so rates must be stored as
    **strings** ("0.11"), and a float found here is rejected rather than
    coerced: silently accepting 0.11 as a float reintroduces exactly the
    binary-representation error the whole money layer exists to prevent.
    """
    raw = settings.get(key)
    if raw is None:
        return default
    if isinstance(raw, float):
        raise PayrollError(
            f"Tenant setting '{key}' is a JSON float ({raw!r}). Store payroll "
            f"rates as strings, e.g. \"0.110000\"."
        )
    return Decimal(str(raw))


def compute_social_insurance(
    *,
    profile: Optional[EmployeePayrollProfile],
    insurable_base: Decimal,
    tenant_settings: dict,
) -> tuple[Decimal, Decimal]:
    """Return ``(employee_share, employer_share)``.

    The base is capped at ``profile.insurable_wage`` when one is set: most
    schemes insure a statutory maximum wage rather than actual pay, and
    applying the rate to uncapped salary over-contributes for senior staff
    and over-states the employer expense in the GL.
    """
    if profile is not None and profile.is_exempt_from_social_insurance:
        return ZERO, ZERO

    payroll_settings = tenant_settings.get("payroll", {}) if tenant_settings else {}
    employee_rate = _rate_from_settings(
        payroll_settings, "social_insurance_employee_rate", DEFAULT_SI_EMPLOYEE_RATE
    )
    employer_rate = _rate_from_settings(
        payroll_settings, "social_insurance_employer_rate", DEFAULT_SI_EMPLOYER_RATE
    )

    base = insurable_base
    if profile is not None and profile.insurable_wage > ZERO:
        base = min(base, to_money(profile.insurable_wage, field_name="insurable_wage"))

    return base * employee_rate, base * employer_rate


# ---------------------------------------------------------------------------
# Calculation
# ---------------------------------------------------------------------------

def eligible_employees(run: PayrollRun) -> Iterable[Employee]:
    """Everyone who should be paid in this run.

    Joiners and leavers are included when their employment *overlaps* the
    period — a leaver on the 10th is paid for ten days, not skipped — and
    excluded when it does not. SUSPENDED staff are excluded because
    ``Employee.is_payable`` says suspension always needs an explicit decision.
    """
    qs = Employee.objects.filter(
        status__in=[Employee.Status.ACTIVE, Employee.Status.ON_LEAVE],
        hire_date__lte=run.period_end,
    ).filter(
        Q(termination_date__isnull=True) | Q(termination_date__gte=run.period_start)
    )
    if run.frequency != PayrollRun.Frequency.OFF_CYCLE:
        qs = qs.filter(pay_frequency=run.frequency)
    if run.department_id:
        # Materialised path: the whole department subtree in one prefix scan.
        qs = qs.filter(department__path__startswith=run.department.subtree_prefix)
    return qs.select_related("department", "job_title", "default_cost_center")


def _collect_overtime(run_employee: Employee, run: PayrollRun) -> list:
    """Approved, unpaid overtime slips falling in this run's period.

    Imported lazily: `apps.hr` imports payroll models for its own links, and a
    module-level import closes the cycle.
    """
    from apps.hr.services.overtime import approved_overtime_for  # noqa: PLC0415

    return approved_overtime_for(run_employee, run.period_start, run.period_end)


def compute_payslip(run: PayrollRun, employee: Employee) -> PayslipComputation:
    """Compute one employee's pay. Pure: reads the database, writes nothing."""
    currency = run.currency
    comp = PayslipComputation(employee=employee, currency=currency)

    # 1. Salary as at the end of the period, from history — never the column.
    comp.base_salary = effective_salary(employee, run.period_end)

    # 2. Time. Fall back to the schedule when no attendance is captured at
    #    all (salaried office staff with no terminal): absent attendance data
    #    must not silently zero someone's pay.
    working, present, overtime = count_attendance(
        employee, run.period_start, run.period_end
    )
    comp.unpaid_leave_days = unpaid_leave_days(
        employee, run.period_start, run.period_end
    )
    if working == ZERO:
        working = _scheduled_working_days(employee, run)
        present = working
    comp.working_days = working
    comp.overtime_hours = overtime
    comp.paid_days = max(present - comp.unpaid_leave_days, ZERO)

    # 3. Proration. Full pay when every working day is paid.
    if comp.working_days > ZERO:
        comp.prorated_base = comp.base_salary * comp.paid_days / comp.working_days
    else:
        comp.prorated_base = comp.base_salary

    comp.earnings_total = comp.prorated_base
    comp.taxable_earnings = comp.prorated_base
    comp.insurable_earnings = comp.prorated_base

    # 3b. Approved overtime, priced when it was approved.
    #
    #     Added as ordinary earning lines *before* the component loop, so a
    #     percentage-of-gross allowance and the tax calculation both see the
    #     overtime — which is the point. Paying it after tax has been computed
    #     would under-withhold on every hour of overtime in the company.
    #
    #     The amount is read off the slip, never recomputed: it was priced
    #     against the salary in force on the night worked (see
    #     apps.hr.services.overtime), and a raise granted since must not
    #     retroactively reprice hours already certified.
    comp.overtime_slips = _collect_overtime(employee, run)
    for slip in comp.overtime_slips:
        component = slip.overtime_type.component
        if component is None:
            raise PayrollError(
                f"Overtime type {slip.overtime_type.code} has no payroll "
                f"component, so {slip.amount} of approved overtime for "
                f"{employee.employee_code} has no account to post to. "
                f"Attach a component to the type before running payroll."
            )
        comp.lines.append(ComputedLine(
            component=component,
            sequence=component.sequence,
            quantity=slip.hours,
            rate=slip.hourly_rate * slip.overtime_type.multiplier,
            amount=slip.amount,
            is_taxable=component.is_taxable,
            note=f"Overtime {slip.work_date:%d %b} · {slip.overtime_type.code}"
                 f" ×{slip.overtime_type.multiplier}",
        ))
        comp.earnings_total += slip.amount
        if component.is_taxable:
            comp.taxable_earnings += slip.amount
        if component.is_subject_to_social_insurance:
            comp.insurable_earnings += slip.amount

    # 4. Components, in `sequence` order. Earnings first (so that a
    #    percentage-of-gross deduction sees the final gross), then deductions.
    assignments = effective_components(employee, run.period_start, run.period_end)
    for assignment in assignments:
        line = _evaluate_component(assignment, comp)
        if line is None:
            continue
        comp.lines.append(line)
        component = assignment.component
        if not component.affects_net:
            continue
        if component.component_type == PayrollComponent.ComponentType.EARNING:
            comp.earnings_total += line.amount
            if component.is_taxable:
                comp.taxable_earnings += line.amount
            if component.is_subject_to_social_insurance:
                comp.insurable_earnings += line.amount
        elif component.component_type == PayrollComponent.ComponentType.DEDUCTION:
            comp.other_deductions += line.amount
        elif component.component_type == (
            PayrollComponent.ComponentType.EMPLOYER_CONTRIBUTION
        ):
            # Employer cost: raises company expense, never the employee's net.
            comp.social_insurance_employer += line.amount

    comp.gross = comp.earnings_total

    # 5. Statutory deductions.
    profile = EmployeePayrollProfile.objects.filter(employee=employee).first()
    si_employee, si_employer = compute_social_insurance(
        profile=profile,
        insurable_base=comp.insurable_earnings,
        tenant_settings=run.tenant.settings or {},
    )
    comp.social_insurance_employee = quantize_currency(si_employee, currency)
    comp.social_insurance_employer += quantize_currency(si_employer, currency)

    exempt = profile.is_exempt_from_tax if profile else False
    if exempt:
        comp.income_tax = ZERO
    else:
        # Social insurance is deductible from the tax base in most schemes;
        # the ordering here mirrors the statutory computation order.
        taxable = max(comp.taxable_earnings - comp.social_insurance_employee, ZERO)
        comp.income_tax = quantize_currency(
            compute_income_tax(
                tenant_id=run.tenant_id,
                country=run.tenant.country,
                taxable_monthly=taxable,
                as_of=run.period_end,
                periods_per_year=_periods_per_year(run.frequency),
                exemption=(
                    to_money(profile.tax_exemption_amount, field_name="exemption")
                    if profile
                    else ZERO
                ),
            ),
            currency,
        )

    # 6. Round once, at the boundary, then derive net from the rounded parts
    #    so that the stored identity holds exactly.
    comp.gross = quantize_currency(comp.gross, currency)
    comp.taxable_earnings = quantize_currency(comp.taxable_earnings, currency)
    comp.other_deductions = quantize_currency(comp.other_deductions, currency)
    comp.social_insurance_employer = quantize_currency(
        comp.social_insurance_employer, currency
    )
    comp.net = comp.gross - comp.total_deductions

    comp.assert_net_identity()
    return comp


def _periods_per_year(frequency: str) -> Decimal:
    return {
        PayrollRun.Frequency.MONTHLY: TWELVE,
        PayrollRun.Frequency.BIWEEKLY: Decimal("26"),
        PayrollRun.Frequency.WEEKLY: Decimal("52"),
        PayrollRun.Frequency.OFF_CYCLE: TWELVE,
    }.get(frequency, TWELVE)


def _scheduled_working_days(employee: Employee, run: PayrollRun) -> Decimal:
    """Working days in the period derived from the employee's schedule.

    Used only when attendance is not captured for this employee at all. It is
    a separate function so that the "no attendance rows" case is an explicit,
    testable branch rather than a division by zero.
    """
    schedule = employee.work_schedule
    working_days = set(schedule.working_days or []) if schedule else set()
    if not working_days:
        working_days = {1, 2, 3, 4, 5}

    total = ZERO
    current = run.period_start
    while current <= run.period_end:
        if current.isoweekday() in working_days:
            total += ONE
        current = date.fromordinal(current.toordinal() + 1)
    return total


@transaction.atomic
def _no_eligible_employees_reason(run: PayrollRun) -> str:
    """Say *why* nobody matched, not merely that nobody did.

    The three filters in `eligible_employees` are re-applied one at a time so
    the message can name the one that emptied the set. Frequency is the usual
    culprit: a weekly run in a company whose staff are all monthly matches
    nobody, and nothing on the screen hints at that.
    """
    active = Employee.objects.filter(
        status__in=[Employee.Status.ACTIVE, Employee.Status.ON_LEAVE]
    )
    total = active.count()
    if total == 0:
        return (
            f"Run {run.name} matched no employees: this tenant has no active "
            f"staff. Add employees before running payroll."
        )

    employed = active.filter(hire_date__lte=run.period_end).filter(
        Q(termination_date__isnull=True) | Q(termination_date__gte=run.period_start)
    )
    if not employed.exists():
        return (
            f"Run {run.name} matched no employees: none of the {total} active "
            f"staff were employed between {run.period_start:%d %b %Y} and "
            f"{run.period_end:%d %b %Y}."
        )

    if run.frequency != PayrollRun.Frequency.OFF_CYCLE:
        by_frequency = employed.filter(pay_frequency=run.frequency)
        if not by_frequency.exists():
            present = sorted(set(employed.values_list("pay_frequency", flat=True)))
            return (
                f"Run {run.name} is a {run.get_frequency_display().lower()} run, "
                f"but none of the {employed.count()} employees in this period "
                f"are paid {run.get_frequency_display().lower()} "
                f"(they are: {', '.join(present) or 'unset'}). Change the run's "
                f"frequency, or the employees' pay frequency."
            )
        employed = by_frequency

    if run.department_id:
        return (
            f"Run {run.name} is restricted to {run.department}, and none of "
            f"the {employed.count()} otherwise-eligible employees are in that "
            f"department or below it."
        )

    return (
        f"Run {run.name} matched no employees. Check the period, the pay "
        f"frequency and the department restriction."
    )


def calculate_run(run: PayrollRun, *, user_id: Optional[uuid.UUID] = None) -> PayrollRun:
    """Compute every payslip in the run and materialise the control totals.

    Atomic on purpose: a run that is half-calculated is worse than one that
    is not calculated at all, because its totals look plausible. Any failure
    — a missing salary revision, a missing tax scale, a net identity
    violation — rolls the whole run back to its previous state.

    Re-calculation is legal until the run is approved; previous payslips are
    deleted first so the ``uq_pay_payslip_run_employee`` constraint holds and
    no stale slip survives a headcount change.
    """
    run.assert_can_transition(PayrollRun.Status.CALCULATING)
    if run.journal_entry_id is not None:
        raise PayrollError(
            f"Run {run.name} has already been posted to the ledger and cannot "
            f"be recalculated. Reverse the journal entry first."
        )

    PayrollRun.objects.filter(pk=run.pk).update(
        status=PayrollRun.Status.CALCULATING, updated_by_id=user_id
    )
    run.status = PayrollRun.Status.CALCULATING

    _discard_payslips(run)

    totals = {
        "count": 0,
        "gross": ZERO,
        "deductions": ZERO,
        "net": ZERO,
        "employer": ZERO,
    }

    for employee in eligible_employees(run):
        comp = compute_payslip(run, employee)
        payslip = _persist_payslip(run, comp, user_id=user_id)
        _claim_overtime(run, comp)
        totals["count"] += 1
        totals["gross"] += payslip.gross_amount
        totals["deductions"] += comp.total_deductions
        totals["net"] += payslip.net_amount
        totals["employer"] += comp.social_insurance_employer

    # A run that matched nobody is a configuration mistake, not a valid empty
    # result. Left to stand it becomes a CALCULATED run with zero payslips,
    # which passes every check here and fails two steps later at approval with
    # "Run X has no payslips" — a message that describes the symptom and says
    # nothing about the cause. Refusing here, with the actual mismatch named,
    # turns a dead end into a fixable error.
    if totals["count"] == 0:
        raise PayrollError(_no_eligible_employees_reason(run))

    # The run-level identity. If this fails, one of the payslips was written
    # by something other than this function.
    if totals["net"] != totals["gross"] - totals["deductions"]:
        raise NetIdentityViolation(
            f"Run totals do not add up: net {totals['net']} != gross "
            f"{totals['gross']} - deductions {totals['deductions']}."
        )

    PayrollRun.objects.filter(pk=run.pk).update(
        status=PayrollRun.Status.CALCULATED,
        employee_count=totals["count"],
        total_gross=totals["gross"],
        total_deductions=totals["deductions"],
        total_net=totals["net"],
        total_employer_cost=totals["gross"] + totals["employer"],
        calculated_at=timezone.now(),
        calculated_by_id=user_id,
        updated_by_id=user_id,
    )
    run.refresh_from_db()
    return run


def _claim_overtime(run: PayrollRun, comp) -> None:
    """Stamp the run id onto every overtime slip this payslip paid.

    `approved_overtime_for` filters on `payroll_run__isnull=True`, so this
    stamp is the entire mechanism preventing a slip being paid twice — by the
    next period's run, or by a re-run after the period boundary moved. Written
    inside `calculate_run`'s transaction, so a run that rolls back releases
    the slips with it.
    """
    from apps.hr.models import OvertimeSlip  # noqa: PLC0415

    ids = [slip.pk for slip in getattr(comp, "overtime_slips", [])]
    if not ids:
        return
    OvertimeSlip.objects.filter(pk__in=ids).update(
        payroll_run=run, status=OvertimeSlip.Status.PAID
    )


def _release_overtime(run: PayrollRun) -> None:
    """Un-stamp slips when a run is discarded, so they can be paid later.

    Without this, recalculating a run would strand every slip it had claimed:
    still marked PAID against a run whose payslips no longer exist, and
    invisible to the recalculation that was meant to replace them.
    """
    from apps.hr.models import OvertimeSlip  # noqa: PLC0415

    OvertimeSlip.objects.filter(payroll_run=run).update(
        payroll_run=None, status=OvertimeSlip.Status.APPROVED
    )


def _discard_payslips(run: PayrollRun) -> None:
    """Drop the payslips of a never-posted run so it can be recalculated.

    Two guard rails deliberately stand in the way, and this is the one place
    in the product that steps around them:

    * ``Payslip.delete()`` raises — it inherits ``ImmutableFinancialModel``.
    * ``TenantQuerySet.delete()`` raises — bulk delete is disabled on
      tenant-scoped models.

    Both exist to protect *posted* documents. A payslip belonging to a run
    with no ``journal_entry`` is not one: nothing references it from the
    ledger, no bank file has been generated from it and no employee has been
    paid on it. It is intermediate output of a calculation that is being
    re-run. The precondition is asserted here rather than assumed, and this
    function is the only caller of the base queryset delete.
    """
    if run.journal_entry_id is not None:  # pragma: no cover - defended twice
        raise PayrollError(
            "Refusing to discard payslips of a run that has been posted to the "
            "ledger. Reverse the journal entry instead."
        )

    # The plain (non-tenant) queryset, filtered explicitly by tenant, so the
    # bypass cannot accidentally widen its scope.
    from django.db.models.query import QuerySet  # local import: see docstring

    QuerySet(model=PayslipLine).filter(
        tenant_id=run.tenant_id, payslip__run=run
    ).delete()
    QuerySet(model=Payslip).filter(tenant_id=run.tenant_id, run=run).delete()
    _release_overtime(run)


def _persist_payslip(
    run: PayrollRun, comp: PayslipComputation, *, user_id: Optional[uuid.UUID]
) -> Payslip:
    """Write one computation to the database, snapshot included."""
    employee = comp.employee
    payslip = Payslip(
        tenant_id=run.tenant_id,
        run=run,
        employee=employee,
        # The snapshot is the payslip's source of truth for rendering; see
        # Payslip's docstring for why joining to hr_employee is not an option.
        employee_snapshot={
            "employee_code": employee.employee_code,
            "full_name": employee.full_name,
            "arabic_name": employee.arabic_name,
            "job_title": employee.job_title.name if employee.job_title_id else "",
            "grade": employee.job_title.grade if employee.job_title_id else "",
            "department": employee.department.name,
            "department_code": employee.department.code,
            "department_path": employee.department.path,
            "employment_type": employee.employment_type,
            "hire_date": employee.hire_date.isoformat(),
            "base_salary": str(comp.base_salary),
            "salary_currency": employee.salary_currency,
            "pay_frequency": employee.pay_frequency,
            # Masked: a payslip must show which account was used without
            # reproducing the full IBAN in an object store.
            "bank_iban_masked": (
                f"****{employee.bank_account_iban[-4:]}"
                if employee.bank_account_iban
                else ""
            ),
            "bank_name": employee.bank_name,
        },
        working_days=comp.working_days,
        paid_days=comp.paid_days,
        leave_days_unpaid=comp.unpaid_leave_days,
        overtime_hours=comp.overtime_hours,
        gross_amount=comp.gross,
        taxable_amount=comp.taxable_earnings,
        income_tax_amount=comp.income_tax,
        social_insurance_employee=comp.social_insurance_employee,
        social_insurance_employer=comp.social_insurance_employer,
        other_deductions=comp.other_deductions,
        net_amount=comp.net,
        currency=comp.currency,
        created_by_id=user_id,
    )
    payslip.save()

    PayslipLine.objects.bulk_create(
        [
            PayslipLine(
                tenant_id=run.tenant_id,
                payslip=payslip,
                component=line.component,
                component_snapshot={
                    "code": line.component.code,
                    "name": line.component.name,
                    "component_type": line.component.component_type,
                    "calculation_type": line.component.calculation_type,
                    "rate": str(line.component.rate),
                },
                sequence=index,
                quantity=line.quantity,
                rate=line.rate,
                amount=quantize_currency(line.amount, comp.currency),
                is_taxable=line.is_taxable,
                calculation_note=line.note[:255],
                created_by_id=user_id,
            )
            for index, line in enumerate(comp.lines, start=1)
        ]
    )
    return payslip


def _evaluate_component(
    assignment: EmployeeComponent, comp: PayslipComputation
) -> Optional[ComputedLine]:
    """Turn one component assignment into an amount, with an audit note."""
    component = assignment.component
    amount_source = (
        assignment.amount_override
        if assignment.amount_override is not None
        else component.amount
    )
    rate = (
        assignment.rate_override
        if assignment.rate_override is not None
        else component.rate
    )
    quantity = to_money(assignment.quantity, field_name="quantity")

    if component.calculation_type == PayrollComponent.CalculationType.FIXED:
        amount = to_money(amount_source, field_name="amount")
        note = f"fixed {amount}"
    elif component.calculation_type == (
        PayrollComponent.CalculationType.PERCENTAGE_OF_BASE
    ):
        amount = comp.prorated_base * rate
        note = f"{comp.prorated_base} x {rate} (of base)"
    elif component.calculation_type == (
        PayrollComponent.CalculationType.PERCENTAGE_OF_GROSS
    ):
        # `earnings_total` is the running gross. Because components are
        # iterated in `sequence` order and deductions sort after earnings,
        # every earning has already been added by the time we get here.
        amount = comp.earnings_total * rate
        note = f"{comp.earnings_total} x {rate} (of gross)"
    elif component.calculation_type == PayrollComponent.CalculationType.PER_UNIT:
        units = quantity if quantity > ZERO else comp.overtime_hours
        amount = units * to_money(amount_source, field_name="amount")
        note = f"{units} x {amount_source} per unit"
    elif component.calculation_type == PayrollComponent.CalculationType.FORMULA:
        context = {
            "base": comp.base_salary,
            "prorated_base": comp.prorated_base,
            "gross": comp.earnings_total,
            "paid_days": comp.paid_days,
            "working_days": comp.working_days,
            "overtime_hours": comp.overtime_hours,
            "unpaid_leave_days": comp.unpaid_leave_days,
            "quantity": quantity,
            "rate": rate,
            "amount": to_money(amount_source, field_name="amount"),
        }
        amount = to_money(
            evaluate_formula(component.formula_expression, context), field_name="amount"
        )
        note = f"formula: {component.formula_expression}"
    else:  # pragma: no cover - defensive
        raise PayrollError(f"Unknown calculation type {component.calculation_type}.")

    if amount < ZERO:
        raise PayrollError(
            f"Component {component.code} evaluated to a negative amount "
            f"({amount}). Direction is expressed by component_type, not by sign."
        )
    if amount == ZERO and component.component_type != (
        PayrollComponent.ComponentType.INFORMATIONAL
    ):
        # A zero line adds noise to the payslip without changing anything.
        return None

    return ComputedLine(
        component=component,
        sequence=component.sequence,
        quantity=quantity,
        rate=rate,
        amount=amount,
        is_taxable=component.is_taxable,
        note=note,
    )


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

@transaction.atomic
def approve_run(run: PayrollRun, user) -> PayrollRun:
    """Sign off a calculated run. Enforces segregation of duties.

    Why the approver may not be the calculator
    ------------------------------------------
    Payroll is the highest-value recurring outflow in most companies and the
    classic vector for occupational fraud: add a ghost employee, or raise
    one's own salary component, then approve one's own work. Requiring a
    second pair of eyes does not make fraud impossible, but it makes it
    require collusion — which is detectable, deterrable and, for auditors and
    insurers, the difference between a control that exists and one that does
    not. The same reasoning is why ``calculated_by`` is stored on the run and
    why both actions are written to ``tenancy.TenantAuditLog``.
    """
    run.assert_can_transition(PayrollRun.Status.APPROVED)

    # Imported here rather than at module scope: the permission layer imports
    # payroll models for its resource registry, and a top-level import would
    # be circular.
    from apps.iam.services.permissions import assert_permission  # local import

    assert_permission(user, "payroll.payroll_run.approve", tenant_id=run.tenant_id)

    if run.calculated_by_id and run.calculated_by_id == user.id:
        raise PermissionDenied(
            "Segregation of duties: the user who calculated this payroll run "
            "may not approve it. A second authorised approver is required."
        )
    if run.employee_count == 0:
        raise PayrollError("Refusing to approve a payroll run with no payslips.")
    if run.total_net != run.total_gross - run.total_deductions:
        raise NetIdentityViolation(
            f"Run {run.name} totals are inconsistent; recalculate before approving."
        )

    PayrollRun.objects.filter(pk=run.pk).update(
        status=PayrollRun.Status.APPROVED,
        approved_by=user,
        approved_at=timezone.now(),
        locked=True,
        updated_by=user,
    )
    run.refresh_from_db()
    return run


# ---------------------------------------------------------------------------
# GL posting — the critical integration
# ---------------------------------------------------------------------------

def _account_id(tenant_id: uuid.UUID, system_key: str) -> uuid.UUID:
    account = Account.all_tenants.filter(
        tenant_id=tenant_id, system_key=system_key, is_active=True
    ).first()
    if account is None:
        raise PayrollError(
            f"No account configured with system_key '{system_key}'. Payroll "
            f"cannot post until the chart of accounts is mapped."
        )
    if not account.is_postable:
        raise PayrollError(
            f"Account {account.code} ('{system_key}') is a summary account and "
            f"cannot be posted to."
        )
    return account.id


def build_journal_entry(run: PayrollRun) -> JournalEntryDraft:
    """Build the run's balanced journal entry. Pure — persists nothing.

    Shape of the entry (one entry for the whole run)::

        Dr  Salary expense                    gross      (split per cost centre)
        Dr  Employer social insurance expense employer share
            Cr  Salaries payable              net
            Cr  Income tax payable            withheld tax
            Cr  Social insurance payable      employee share + employer share
            Cr  Other deductions payable      loans, advances, penalties

    It balances by construction::

        debits  = gross + employer_si
        credits = net + tax + si_employee + si_employer + other
                = (gross - tax - si_employee - other)
                  + tax + si_employee + si_employer + other
                = gross + employer_si

    Note what this entry is *not*: it is not a cash movement. Nothing has left
    the bank yet — the credit side is a set of payables. Cash moves later, in
    :func:`mark_run_paid`. Collapsing the two into a single "Dr expense / Cr
    bank" entry is the mistake that makes the balance sheet misstate the
    payroll liability between the pay run and the transfer, and makes it
    impossible to reconcile the bank file against the ledger.

    The salary expense is split by department cost centre so that management
    reporting gets departmental labour cost for free, from the ledger, rather
    than from a spreadsheet that disagrees with it.
    """
    tenant_id = run.tenant_id
    currency = run.currency

    journal = Journal.all_tenants.filter(
        tenant_id=tenant_id, kind=Journal.Kind.PAYROLL, is_active=True
    ).first()
    if journal is None:
        raise PayrollError("No active payroll journal configured for this tenant.")

    payslips = list(
        Payslip.all_tenants.filter(tenant_id=tenant_id, run=run).select_related(
            "employee", "employee__department", "employee__default_cost_center",
            "employee__department__cost_center_account",
        )
    )
    if not payslips:
        raise PayrollError("Refusing to post a payroll run with no payslips.")

    default_expense = _account_id(tenant_id, SALARY_EXPENSE)

    #: gross per (expense account, department) so each cost centre carries its
    #: own labour cost.
    expense_buckets: dict[tuple[uuid.UUID, Optional[uuid.UUID]], Decimal] = {}
    net_total = ZERO
    tax_total = ZERO
    si_employee_total = ZERO
    si_employer_total = ZERO
    other_total = ZERO

    for slip in payslips:
        employee = slip.employee
        cost_center = (
            employee.default_cost_center_id
            or employee.department.cost_center_account_id
            or default_expense
        )
        key = (cost_center, employee.department_id)
        expense_buckets[key] = expense_buckets.get(key, ZERO) + slip.gross_amount

        net_total += slip.net_amount
        tax_total += slip.income_tax_amount
        si_employee_total += slip.social_insurance_employee
        si_employer_total += slip.social_insurance_employer
        other_total += slip.other_deductions

    draft = JournalEntryDraft(
        journal_code=journal.code,
        entry_date=run.pay_date,
        currency=currency,
        memo=f"Payroll {run.name} ({run.period_start} – {run.period_end})"[:500],
        source=JournalEntry.Source.PAYROLL,
        source_document_type="payroll.PayrollRun",
        source_document_id=run.id,
        # Exactly-once: `accounting.uq_entry_idempotency` turns a retry into a
        # no-op that returns the original entry.
        idempotency_key=run.idempotency_key,
    )

    # --- Debits ---------------------------------------------------------
    for (account_id, department_id), amount in expense_buckets.items():
        amount = quantize_currency(amount, currency)
        if amount <= ZERO:
            continue
        draft.debit(
            account_id,
            amount,
            description="Salary expense",
            department_id=department_id,
        )

    employer_si = quantize_currency(si_employer_total, currency)
    if employer_si > ZERO:
        draft.debit(
            _account_id(tenant_id, EMPLOYER_SI_EXPENSE),
            employer_si,
            description="Employer social insurance contribution",
        )

    # --- Credits --------------------------------------------------------
    # Payables, not cash: the money is owed now and paid later.
    draft.credit(
        _account_id(tenant_id, SALARIES_PAYABLE),
        quantize_currency(net_total, currency),
        description="Net salaries payable",
    )
    tax = quantize_currency(tax_total, currency)
    if tax > ZERO:
        draft.credit(
            _account_id(tenant_id, INCOME_TAX_PAYABLE),
            tax,
            description="Income tax withheld",
        )
    social = quantize_currency(si_employee_total + si_employer_total, currency)
    if social > ZERO:
        draft.credit(
            _account_id(tenant_id, SOCIAL_INSURANCE_PAYABLE),
            social,
            description="Social insurance payable (employee + employer)",
        )
    other = quantize_currency(other_total, currency)
    if other > ZERO:
        draft.credit(
            _account_id(tenant_id, OTHER_DEDUCTIONS_PAYABLE),
            other,
            description="Other payroll deductions payable",
        )

    return draft


@transaction.atomic
def post_run_to_ledger(
    run: PayrollRun, *, user_id: Optional[uuid.UUID] = None
) -> JournalEntry:
    """Post an APPROVED run to the general ledger. Exactly once, atomically.

    Everything in this function — the state transition, the entry, its lines
    and the account balance updates — commits together or not at all. A run
    marked POSTED with no journal entry (or a journal entry with no run) is a
    reconciliation incident that takes days to unpick; ``transaction.atomic``
    plus the ``ck_pay_run_posted_has_entry`` constraint make it unreachable.
    """
    run.assert_can_transition(PayrollRun.Status.POSTED)
    if run.journal_entry_id is not None:
        # Belt and braces: the OneToOne and the idempotency key both prevent
        # this, but returning the existing entry keeps a retried task quiet.
        return run.journal_entry

    draft = build_journal_entry(run)

    # Assert *before* posting, so the failure names payroll rather than
    # surfacing as a generic ledger error three frames down. post_entry()
    # re-checks it; that duplication is intentional (see posting.py).
    if draft.total_debit != draft.total_credit:
        raise UnbalancedEntry(
            f"Payroll run {run.name} produced an unbalanced entry: debits "
            f"{draft.total_debit} != credits {draft.total_credit} (difference "
            f"{draft.difference}). This means a payslip's net does not equal "
            f"its gross minus deductions. Refusing to post."
        )
    # Total debits must equal the run's full employer cost (gross + employer
    # contributions). Checking the draft against the *stored* control totals
    # catches the case where payslips were altered after the totals were
    # materialised — the ledger would still balance internally, but it would
    # no longer agree with the payroll register handed to the auditor.
    expected_debit = quantize_currency(run.total_employer_cost, run.currency)
    if quantize_currency(draft.total_debit, run.currency) != expected_debit:
        raise PayrollError(
            f"Journal draft total {draft.total_debit} does not agree with the "
            f"run's control total (employer cost {run.total_employer_cost}). "
            f"Recalculate the run before posting."
        )

    entry = post_entry(draft, tenant_id=run.tenant_id, user_id=user_id)

    PayrollRun.objects.filter(pk=run.pk).update(
        status=PayrollRun.Status.POSTED,
        journal_entry=entry,
        posted_at=timezone.now(),
        locked=True,
        updated_by_id=user_id,
    )
    run.refresh_from_db()
    return entry


@transaction.atomic
def mark_run_paid(
    run: PayrollRun,
    *,
    bank_account_system_key: str = DEFAULT_BANK,
    user_id: Optional[uuid.UUID] = None,
    payment_date: Optional[date] = None,
) -> JournalEntry:
    """Record the disbursement: the cash actually leaving the company.

    ::

        Dr  Salaries payable   net
            Cr  Bank           net

    This is a *separate* entry from the accrual for a reason: the liability
    was created when the payroll was posted, and it is discharged when the
    bank confirms the transfer, which is a different date, a different
    approver and a different reconciliation. Between them the balance sheet
    correctly shows money owed to employees.

    Idempotent through its own key, ``payroll:{id}:payment``, so a retried
    confirmation webhook cannot double-credit the bank.
    """
    run.assert_can_transition(PayrollRun.Status.PAID)
    if run.journal_entry_id is None:
        raise PayrollError(
            "A payroll run must be posted to the ledger before it can be paid; "
            "otherwise the payment discharges a liability that does not exist."
        )

    payment_date = payment_date or timezone.localdate()
    net = quantize_currency(run.total_net, run.currency)
    if net <= ZERO:
        raise PayrollError("Refusing to disburse a zero or negative net total.")

    journal = Journal.all_tenants.filter(
        tenant_id=run.tenant_id, kind=Journal.Kind.CASH, is_active=True
    ).first() or Journal.all_tenants.filter(
        tenant_id=run.tenant_id, kind=Journal.Kind.PAYROLL, is_active=True
    ).first()
    if journal is None:
        raise PayrollError("No active cash or payroll journal configured.")

    draft = JournalEntryDraft(
        journal_code=journal.code,
        entry_date=payment_date,
        currency=run.currency,
        memo=f"Payroll disbursement {run.name}"[:500],
        source=JournalEntry.Source.PAYROLL,
        source_document_type="payroll.PayrollRun",
        source_document_id=run.id,
        idempotency_key=f"payroll:{run.id}:payment",
    )
    draft.debit(
        _account_id(run.tenant_id, SALARIES_PAYABLE),
        net,
        description="Settlement of net salaries",
    )
    draft.credit(
        _account_id(run.tenant_id, bank_account_system_key),
        net,
        description="Salary transfer",
    )

    if draft.total_debit != draft.total_credit:  # pragma: no cover - defensive
        raise UnbalancedEntry("Disbursement entry does not balance.")

    entry = post_entry(draft, tenant_id=run.tenant_id, user_id=user_id)

    PayrollRun.objects.filter(pk=run.pk).update(
        status=PayrollRun.Status.PAID, paid_at=timezone.now(), updated_by_id=user_id
    )
    Payslip.all_tenants.filter(tenant_id=run.tenant_id, run=run).update(
        payment_status=Payslip.PaymentStatus.PAID, paid_at=timezone.now()
    )
    run.refresh_from_db()
    return entry
