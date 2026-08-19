"""
Operational reports: aging, payroll register, project profitability and stock
valuation.

These differ from the statutory statements in one important way: they read
sub-ledgers (payslips, timesheets, stock levels) as well as the general
ledger, and they exist largely to be *reconciled against* it. A payroll
register whose total does not equal the payroll journal entry, or a stock
valuation that disagrees with the inventory control account, means one of the
two is wrong — and finding out which is the whole reason to produce them side
by side. Every generator here that has a GL counterpart compares itself to it
and surfaces the drift rather than presenting a number in isolation.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional

from django.db.models import Q, Sum

from apps.accounting.models import Account, AccountType
from apps.core.fields import ZERO
from apps.reporting.generators.base import (
    ReportContext,
    ReportError,
    ReportGenerator,
    ReportLine,
    ReportResult,
    ReportSection,
    ledger_query,
    register_report,
)
from apps.reporting.models import ReportType

__all__ = [
    "ARAgingGenerator",
    "APAgingGenerator",
    "PayrollRegisterGenerator",
    "ProjectProfitabilityGenerator",
    "InventoryValuationGenerator",
]


#: Aging bucket edges, in days past the transaction date. Expressed as a tuple
#: of (label, lower, upper) with an inclusive lower bound and inclusive upper
#: bound; ``None`` for the upper bound means unbounded. Data rather than five
#: hand-written filters, so that a tenant who wants 45-day buckets changes a
#: parameter instead of the arithmetic.
DEFAULT_BUCKETS: tuple[tuple[str, int, Optional[int]], ...] = (
    ("current", None, 0),
    ("1_30", 1, 30),
    ("31_60", 31, 60),
    ("61_90", 61, 90),
    ("90_plus", 91, None),
)

AR_CONTROL_KEYS: frozenset[str] = frozenset({"ar_control", "accounts_receivable"})
AP_CONTROL_KEYS: frozenset[str] = frozenset({"ap_control", "accounts_payable"})


# ---------------------------------------------------------------------------
# Aging
# ---------------------------------------------------------------------------

class _AgingGenerator(ReportGenerator):
    """Shared implementation of the AR and AP aging reports.

    Both are the same query with the sign, the partner type and the control
    accounts swapped, so they share one implementation. Two near-identical
    copies would drift — and an AR aging and an AP aging that bucket
    differently make the working-capital picture incoherent.

    Why aging is driven off ``JournalLine`` and not off the invoice table
    --------------------------------------------------------------------
    The aging report must tie to the balance sheet. If it is computed from
    ``sales.Invoice.balance_due`` and the balance sheet is computed from the AR
    control account, the two will disagree the first time anything reaches the
    control account without an invoice — a manual adjustment, an opening
    balance, a write-off, a customer refund. Both figures are then defensible
    and different, which is the worst outcome. Aging the *ledger* means the
    total of the aging report is, by construction, the control account balance.

    The known limitation, stated rather than hidden
    -----------------------------------------------
    Journal lines carry an accounting date, not a due date. So these buckets
    age by transaction date, which equals due-date aging only where terms are
    immediate. Where payment terms matter, the sub-ledger's due dates are the
    authority and this report will read as more overdue than the customer
    actually is. The result carries a warning saying exactly that, because a
    collections team acting on the wrong bucket calls customers who are not
    late.
    """

    is_as_of = True
    #: "customer" or "vendor" — matches ``JournalLine.partner_type``.
    partner_type: str = ""
    #: System keys of the control accounts this report ages.
    control_keys: frozenset[str] = frozenset()
    #: +1 when a debit balance is what is owed to us (AR), -1 when a credit
    #: balance is what we owe (AP). Presenting either as a negative number
    #: guarantees somebody sums the two and gets nonsense.
    sign: int = 1

    def control_account_ids(self, context: ReportContext) -> set[uuid.UUID]:
        """Every account that behaves as a control account for this report.

        Resolved from ``system_key`` *and* from the per-partner override
        accounts (``sales.Customer.receivable_account``,
        ``expenses.Vendor.payable_account``), because a tenant that gives its
        intercompany customers a separate receivable account would otherwise
        have those balances silently missing from the aging while still
        present on the balance sheet.

        Index used: ``uq_account_system_key`` (tenant, system_key) partial
        unique index answers the system-key lookup directly.
        """
        ids = set(
            Account.all_tenants.filter(
                tenant_id=context.tenant_id, system_key__in=self.control_keys
            ).values_list("id", flat=True)
        )
        ids |= set(self._override_account_ids(context))
        return ids

    def _override_account_ids(self, context: ReportContext) -> Iterable[uuid.UUID]:
        return ()

    def partner_names(self, context: ReportContext,
                      partner_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        return {}

    def generate(self, context: ReportContext) -> ReportResult:
        as_of = context.effective_as_of
        result = ReportResult(report_type=self.report_type, currency=context.currency)
        result.warn(
            "Buckets age by accounting date, not by invoice due date: the "
            "general ledger does not carry payment terms. Where terms are not "
            "immediate this report shows balances as older than they "
            "contractually are — check the sub-ledger before chasing."
        )

        account_ids = self.control_account_ids(context)
        if not account_ids:
            result.warn(
                f"No control account is configured for this report "
                f"(system keys: {sorted(self.control_keys)}). Nothing could be "
                f"aged. Set the system key on the control account in the chart "
                f"of accounts."
            )
            return result

        buckets = self._buckets(context)

        # One query, one pass over the journal, with a conditional SUM per
        # bucket. The alternative — one query per bucket — is five scans of the
        # largest table in the system for the same answer.
        #
        # Index used: ix_line_partner (tenant, partner_type, partner_id) is the
        # grouping key and is highly selective once partner_type is pinned;
        # ix_entry_status (tenant, status, entry_date) bounds the entry side at
        # `as_of`. ix_line_account then restricts to the control accounts.
        annotations: dict[str, Any] = {}
        for label, lower, upper in buckets:
            annotations[f"debit_{label}"] = Sum(
                "base_debit", filter=self._bucket_q(as_of, lower, upper), default=ZERO
            )
            annotations[f"credit_{label}"] = Sum(
                "base_credit", filter=self._bucket_q(as_of, lower, upper), default=ZERO
            )

        rows = (
            ledger_query(context, date_to=as_of, ignore_period=True)
            .filter(account_id__in=account_ids, partner_type=self.partner_type)
            .values("partner_id")
            .annotate(**annotations)
        )

        rows = list(rows)
        partner_ids = {r["partner_id"] for r in rows if r["partner_id"] is not None}
        names = self.partner_names(context, partner_ids)

        section = ReportSection(
            key="partners", title="Aging by partner", sequence=0
        )
        bucket_totals: dict[str, Decimal] = {label: ZERO for label, _, _ in buckets}
        grand_total = ZERO

        for row in rows:
            partner_id = row["partner_id"]
            per_bucket: dict[str, Decimal] = {}
            partner_total = ZERO
            for label, _, _ in buckets:
                amount = (
                    (row[f"debit_{label}"] or ZERO) - (row[f"credit_{label}"] or ZERO)
                ) * self.sign
                per_bucket[label] = amount
                partner_total += amount
            if partner_total == ZERO and all(v == ZERO for v in per_bucket.values()):
                # Fully settled: showing it is noise on a collections worklist.
                continue
            for label, amount in per_bucket.items():
                bucket_totals[label] += amount
            grand_total += partner_total
            section.add(
                ReportLine(
                    label=names.get(
                        partner_id,
                        f"(unnamed {self.partner_type} {partner_id})"
                        if partner_id else "(no partner recorded)",
                    ),
                    amount=partner_total,
                    meta={
                        "partner_id": str(partner_id) if partner_id else None,
                        "partner_type": self.partner_type,
                        "buckets": per_bucket,
                    },
                )
            )

        section.lines.sort(key=lambda line: line.amount, reverse=True)
        section.total = grand_total

        summary = ReportSection(key="buckets", title="Bucket totals", sequence=1)
        for label, lower, upper in buckets:
            summary.add(
                ReportLine(
                    label=self._bucket_label(label, lower, upper),
                    amount=bucket_totals[label],
                    meta={"bucket": label},
                )
            )
        summary.total = grand_total

        result.sections = [section, summary]
        result.totals = {f"bucket_{k}": v for k, v in bucket_totals.items()}
        result.totals["total_outstanding"] = grand_total
        result.metadata["as_of"] = as_of.isoformat()
        result.metadata["partner_count"] = len(section.lines)
        return result

    # -- bucket plumbing ----------------------------------------------------

    def _buckets(
        self, context: ReportContext
    ) -> tuple[tuple[str, Optional[int], Optional[int]], ...]:
        override = (context.options or {}).get("buckets")
        if not override:
            return DEFAULT_BUCKETS
        return tuple(
            (str(label), lower, upper) for label, lower, upper in override
        )

    @staticmethod
    def _bucket_q(as_of: date, lower: Optional[int], upper: Optional[int]) -> Q:
        """Age band expressed as a *date* range, not a computed age.

        ``as_of - entry_date BETWEEN x AND y`` is not sargable: PostgreSQL
        cannot use ``ix_entry_status`` for it, so every bucket becomes a
        sequential scan. Inverting it into a plain range on ``entry_date``
        keeps the index in play, which is the difference between an aging
        report that runs in the request and one that runs in a Celery task.
        """
        q = Q()
        if lower is not None:
            # age >= lower  <=>  entry_date <= as_of - lower
            q &= Q(entry__entry_date__lte=as_of - timedelta(days=lower))
        if upper is not None:
            # age <= upper  <=>  entry_date >= as_of - upper
            q &= Q(entry__entry_date__gte=as_of - timedelta(days=upper))
        return q

    @staticmethod
    def _bucket_label(label: str, lower: Optional[int], upper: Optional[int]) -> str:
        if lower is None:
            return "Current (not yet aged)"
        if upper is None:
            return f"{lower}+ days"
        return f"{lower}-{upper} days"


@register_report(ReportType.AR_AGING)
class ARAgingGenerator(_AgingGenerator):
    """What customers owe us, bucketed by age. The collections worklist.

    A debit balance on the AR control account is money owed to us, so the sign
    is +1 and a customer in credit (an unapplied payment or a credit note)
    appears as a negative balance — which is correct and is exactly the row a
    credit controller needs to see before chasing.
    """

    title = "Accounts receivable aging"
    partner_type = "customer"
    control_keys = AR_CONTROL_KEYS
    sign = 1

    def _override_account_ids(self, context: ReportContext) -> Iterable[uuid.UUID]:
        from apps.sales.models import Customer  # local import: avoids an app cycle

        return (
            Customer.all_tenants.filter(
                tenant_id=context.tenant_id, receivable_account__isnull=False
            )
            .values_list("receivable_account_id", flat=True)
            .distinct()
        )

    def partner_names(self, context: ReportContext,
                      partner_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        from apps.sales.models import Customer

        if not partner_ids:
            return {}
        return dict(
            Customer.all_tenants.filter(
                tenant_id=context.tenant_id, id__in=partner_ids
            ).values_list("id", "name")
        )


@register_report(ReportType.AP_AGING)
class APAgingGenerator(_AgingGenerator):
    """What we owe suppliers, bucketed by age. The payment run worklist.

    The AP control account is credit-normal, so ``sign = -1`` turns the
    balance into a positive "amount we owe". Presenting AP as a negative
    number is how a cash forecast ends up adding a payable to a receivable.
    """

    title = "Accounts payable aging"
    partner_type = "vendor"
    control_keys = AP_CONTROL_KEYS
    sign = -1

    def _override_account_ids(self, context: ReportContext) -> Iterable[uuid.UUID]:
        from apps.expenses.models import Vendor

        return (
            Vendor.all_tenants.filter(
                tenant_id=context.tenant_id, payable_account__isnull=False
            )
            .values_list("payable_account_id", flat=True)
            .distinct()
        )

    def partner_names(self, context: ReportContext,
                      partner_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        from apps.expenses.models import Vendor

        if not partner_ids:
            return {}
        return dict(
            Vendor.all_tenants.filter(
                tenant_id=context.tenant_id, id__in=partner_ids
            ).values_list("id", "name")
        )


# ---------------------------------------------------------------------------
# Payroll register
# ---------------------------------------------------------------------------

@register_report(ReportType.PAYROLL_REGISTER)
class PayrollRegisterGenerator(ReportGenerator):
    """Per-employee gross, deductions and net for a period, with department
    subtotals.

    Why this generator refuses to decide its own scope
    --------------------------------------------------
    Salary is the most access-controlled data in the system: a department
    manager may see their own team's pay and must not see the CFO's. It is
    tempting to put that rule here — "filter to the caller's department
    subtree". That would be a mistake, and the reason is a separation-of-duties
    argument rather than a stylistic one:

    * **Authorisation belongs to one layer.** ``apps.iam.permissions``
      already encodes every scope strategy (own record, department subtree,
      assigned projects, managed team) as a ``Q`` object via
      :func:`apps.iam.permissions.build_scope_q`. A second, independent
      implementation inside a report is a second thing that can be wrong, and
      it will be wrong differently — the report will grant access the API
      denies, or the reverse, and nobody will notice until it is the former.
    * **A report cannot see the request.** Scope depends on the actor's
      memberships, role assignments and scope rules, which live on the request
      and its caches. Reaching for them from a generator that also runs inside
      a Celery beat task means the task path has no actor at all, and the
      usual "fix" is to default to unrestricted — a silent, total bypass of
      payroll confidentiality that looks like a bug fix in the diff.
    * **Testability.** A ``Q`` handed in is a value: the scoping can be tested
      without constructing a request, and this generator can be tested without
      constructing a permission graph.

    So the caller does it::

        gen = PayrollRegisterGenerator(
            scope_filter=build_scope_q(request.user, "payroll.payslip",
                                       request=request)
        )
        result = gen.run(context)

    ``scope_filter`` defaults to ``None`` and ``None`` is an **error**, not
    "everything". Fail-closed: a caller that forgot to pass a scope gets a
    refusal naming what to pass, rather than the entire company's salaries.
    Unrestricted access is expressible, but only by saying so explicitly with
    ``Q()``.
    """

    title = "Payroll register"
    is_as_of = False

    def __init__(self, scope_filter: Optional[Q] = None) -> None:
        self.scope_filter = scope_filter

    def validate_context(self, context: ReportContext) -> None:
        super().validate_context(context)
        if self.scope_filter is None:
            raise ReportError(
                "PayrollRegisterGenerator requires an explicit `scope_filter`. "
                "Pass apps.iam.permissions.build_scope_q(user, "
                "'payroll.payslip', request=request), or pass Q() to state "
                "deliberately that this caller may see every employee's pay. "
                "Defaulting to unrestricted would silently expose every "
                "salary in the tenant."
            )

    def generate(self, context: ReportContext) -> ReportResult:
        from apps.payroll.models import Payslip  # local import: avoids an app cycle

        # Selected on ``run.pay_date`` rather than on the run's period, because
        # pay_date is the date the money moves and the date the payroll journal
        # entry carries. Selecting on period_start would put a January payment
        # for December work into December, and the register would then not tie
        # to December's payroll journal entry.
        #
        # Index used: ix_pay_run_pay_date (tenant, pay_date) on payroll_run
        # bounds the runs; ix_pay_slip_run (tenant, run) then fetches their
        # payslips. The department subtotal reads the snapshot rather than
        # joining hr_department, so an employee who moved department since the
        # pay date is still counted where the cost was actually charged.
        queryset = (
            Payslip.all_tenants.filter(
                tenant_id=context.tenant_id,
                run__pay_date__gte=context.date_from,
                run__pay_date__lte=context.date_to,
            )
            .filter(self.scope_filter)
            .select_related("employee", "employee__department", "run")
            .order_by("employee__department__path", "employee__employee_code")
        )
        if context.department_id is not None:
            queryset = queryset.filter(employee__department_id=context.department_id)

        by_department: dict[str, list[ReportLine]] = defaultdict(list)
        department_titles: dict[str, str] = {}
        total_gross = ZERO
        total_deductions = ZERO
        total_net = ZERO
        total_employer_cost = ZERO
        employee_count = 0

        for payslip in queryset.iterator(chunk_size=500):
            employee = payslip.employee
            department = getattr(employee, "department", None)
            department_key = str(department.id) if department else "unassigned"
            department_titles[department_key] = (
                department.name if department else "(no department)"
            )

            deductions = payslip.total_deductions
            total_gross += payslip.gross_amount
            total_deductions += deductions
            total_net += payslip.net_amount
            total_employer_cost += payslip.employer_cost
            employee_count += 1

            by_department[department_key].append(
                ReportLine(
                    label=f"{employee.employee_code} — "
                          f"{employee.first_name} {employee.last_name}".strip(),
                    amount=payslip.net_amount,
                    meta={
                        "payslip_id": str(payslip.id),
                        "employee_id": str(employee.id),
                        "gross_amount": payslip.gross_amount,
                        "taxable_amount": payslip.taxable_amount,
                        "income_tax_amount": payslip.income_tax_amount,
                        "social_insurance_employee": payslip.social_insurance_employee,
                        "social_insurance_employer": payslip.social_insurance_employer,
                        "other_deductions": payslip.other_deductions,
                        "total_deductions": deductions,
                        "net_amount": payslip.net_amount,
                        "employer_cost": payslip.employer_cost,
                        "payment_status": payslip.payment_status,
                    },
                )
            )

        sections: list[ReportSection] = []
        for sequence, (department_key, lines) in enumerate(
            sorted(by_department.items(), key=lambda kv: department_titles[kv[0]])
        ):
            section = ReportSection(
                key=f"department:{department_key}",
                title=department_titles[department_key],
                lines=lines,
                sequence=sequence,
            )
            section.recompute_total()
            section.add(
                ReportLine(
                    label=f"{department_titles[department_key]} — net total",
                    amount=section.total,
                    is_subtotal=True,
                    is_bold=True,
                )
            )
            sections.append(section)

        result = ReportResult(
            report_type=self.report_type,
            sections=sections,
            currency=context.currency,
            totals={
                "total_gross": total_gross,
                "total_deductions": total_deductions,
                "total_net": total_net,
                "total_employer_cost": total_employer_cost,
            },
            metadata={
                "employee_count": employee_count,
                "scope_restricted": bool(self.scope_filter),
            },
        )

        # The identity the payroll engine asserts when it builds a payslip,
        # re-checked here over the whole register. If it fails, the register
        # and the payroll journal entry cannot both be right.
        if total_net != total_gross - total_deductions:
            result.warn(
                f"PAYROLL REGISTER DOES NOT ADD UP: net {total_net} != gross "
                f"{total_gross} - deductions {total_deductions} (difference "
                f"{total_net - (total_gross - total_deductions)}). Note that "
                f"an ABAC scope filter narrows the rows but cannot break this "
                f"identity, so this is a data problem, not a scoping one."
            )
        return result


# ---------------------------------------------------------------------------
# Project profitability
# ---------------------------------------------------------------------------

@register_report(ReportType.PROJECT_PROFITABILITY)
class ProjectProfitabilityGenerator(ReportGenerator):
    """Revenue against cost against billable hours, per project.

    Revenue and cost are taken from the *ledger* (income and expense accounts
    carrying a ``project_id``), not from the project's denormalised
    ``invoiced_amount`` / ``actual_cost_amount`` roll-ups. Those roll-ups exist
    so a project list page is one query; they are refreshed by the projects
    service and can lag. A profitability figure that disagrees with the P&L is
    worse than no profitability figure, because someone will price the next
    quote from it.

    Hours come from ``projects.TimesheetEntry`` and are reported alongside,
    never converted into revenue. For a fixed-fee project, hours and revenue
    are deliberately unrelated — revenue is the agreed price no matter how long
    it took — and multiplying hours by a rate there would report income that
    will never be invoiced. The realisation rate (revenue per billable hour) is
    given instead, which is the number that actually tells you the quote was
    too low.
    """

    title = "Project profitability"
    is_as_of = False

    def generate(self, context: ReportContext) -> ReportResult:
        from apps.projects.models import Project, TimesheetEntry

        # Index used: ix_line_project (tenant, project) is the grouping key;
        # ix_entry_status (tenant, status, entry_date) bounds the period.
        rows = (
            ledger_query(context)
            .filter(project_id__isnull=False)
            .values("project_id", "account__type")
            .annotate(debit=Sum("base_debit"), credit=Sum("base_credit"))
        )

        revenue: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
        cost: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
        for row in rows:
            debit = row["debit"] or ZERO
            credit = row["credit"] or ZERO
            if row["account__type"] == AccountType.INCOME:
                revenue[row["project_id"]] += credit - debit
            elif row["account__type"] == AccountType.EXPENSE:
                cost[row["project_id"]] += debit - credit

        # Index used: ix_timesheet_project (tenant, project) plus the
        # (tenant, work_date) bound; is_billable is indexed on its own column.
        hours_rows = (
            TimesheetEntry.all_tenants.filter(
                tenant_id=context.tenant_id,
                work_date__gte=context.date_from,
                work_date__lte=context.date_to,
            )
            .values("project_id", "is_billable")
            .annotate(hours=Sum("hours"))
        )
        billable_hours: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
        non_billable_hours: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
        for row in hours_rows:
            target = billable_hours if row["is_billable"] else non_billable_hours
            target[row["project_id"]] += row["hours"] or ZERO

        project_ids = (
            set(revenue) | set(cost) | set(billable_hours) | set(non_billable_hours)
        )
        if context.project_id is not None:
            project_ids &= {context.project_id}

        projects = {
            p.id: p
            for p in Project.all_tenants.filter(
                tenant_id=context.tenant_id, id__in=project_ids
            )
        }

        section = ReportSection(key="projects", title="Projects", sequence=0)
        total_revenue = ZERO
        total_cost = ZERO
        total_billable = ZERO

        for project_id in sorted(
            project_ids,
            key=lambda pid: projects[pid].code if pid in projects else str(pid),
        ):
            project = projects.get(project_id)
            project_revenue = revenue.get(project_id, ZERO)
            project_cost = cost.get(project_id, ZERO)
            margin = project_revenue - project_cost
            hours = billable_hours.get(project_id, ZERO)
            total_revenue += project_revenue
            total_cost += project_cost
            total_billable += hours

            # None, not zero: "no billable hours" has no realisation rate, and
            # printing 0.00 reads as "we realised nothing per hour", which is a
            # different and alarming statement.
            realisation = (project_revenue / hours) if hours > ZERO else None
            margin_pct = (
                (margin / project_revenue) * Decimal("100")
                if project_revenue != ZERO else None
            )

            section.add(
                ReportLine(
                    label=f"{project.code} — {project.name}" if project
                    else f"(deleted project {project_id})",
                    amount=margin,
                    quantity=hours,
                    meta={
                        "project_id": str(project_id),
                        "billing_type": project.billing_type if project else "",
                        "status": project.status if project else "",
                        "revenue": project_revenue,
                        "cost": project_cost,
                        "margin": margin,
                        "margin_pct": margin_pct,
                        "billable_hours": hours,
                        "non_billable_hours": non_billable_hours.get(project_id, ZERO),
                        "revenue_per_billable_hour": realisation,
                        "budget_amount": project.budget_amount if project else ZERO,
                        "budget_hours": project.budget_hours if project else ZERO,
                    },
                    note=(
                        "Fixed-fee: hours do not drive revenue and the two are "
                        "deliberately unrelated."
                        if project and project.billing_type == "fixed_fee" else ""
                    ),
                )
            )

        section.total = total_revenue - total_cost
        result = ReportResult(
            report_type=self.report_type,
            sections=[section],
            currency=context.currency,
            totals={
                "total_revenue": total_revenue,
                "total_cost": total_cost,
                "total_margin": total_revenue - total_cost,
                "total_billable_hours": total_billable,
            },
            metadata={"project_count": len(section.lines)},
        )
        return result


# ---------------------------------------------------------------------------
# Inventory valuation
# ---------------------------------------------------------------------------

@register_report(ReportType.INVENTORY_VALUATION)
class InventoryValuationGenerator(ReportGenerator):
    """Quantity on hand x average cost, per item and warehouse, reconciled to
    the inventory control account.

    Two independent records of the same asset exist, and they must agree:

    * ``inventory.StockLevel.total_value`` — the operational view, maintained
      by the stock service inside the same locked transaction as every
      movement.
    * The balance of the inventory asset accounts in the general ledger —
      the financial view, written by ``post_entry`` when a movement is valued.

    They are written by the same service in the same transaction, so in a
    healthy system they are equal. When they are not, something specific has
    happened: a movement was posted without a GL effect (an item with
    ``track_inventory`` and no ``inventory_account``), a manual journal was
    posted straight to the control account, a stock level was repaired with
    raw SQL, or a rounding difference accumulated in a valuation method.

    This report surfaces the drift as a line and a warning rather than
    reporting either number alone. Reporting only the stock levels hides a GL
    problem; reporting only the GL hides a warehouse problem; reporting one and
    calling it "the" inventory value guarantees that the balance sheet and the
    stock report are quoted at each other in a meeting and nobody can say which
    is right.

    A note on the as-of date
    ------------------------
    ``StockLevel`` is a *current* projection: it has no history, so this report
    is only truthful for "now". Requesting it as at a past date compares
    today's warehouse against that date's ledger, which will differ by every
    movement since — so a past ``as_of`` is refused rather than answered
    misleadingly. Historical valuation must be rebuilt from ``StockMovement``.
    """

    title = "Inventory valuation"
    is_as_of = True

    def validate_context(self, context: ReportContext) -> None:
        super().validate_context(context)
        from django.utils import timezone

        if context.effective_as_of < timezone.localdate():
            raise ReportError(
                f"Inventory valuation was requested as at "
                f"{context.effective_as_of:%Y-%m-%d}, but StockLevel holds only "
                f"the current position — it has no history. Answering would "
                f"compare today's warehouse against that date's ledger and "
                f"report the difference as drift. Rebuild a historical "
                f"valuation from inventory.StockMovement instead."
            )

    def generate(self, context: ReportContext) -> ReportResult:
        from apps.inventory.models import Item, StockLevel

        # Index used: ix_stock_level_item (tenant, item) drives the ordering
        # and grouping; the (tenant, warehouse) index serves the per-warehouse
        # summary. total_value is a stored column precisely so that this report
        # is a SUM rather than a per-row multiplication that rounds differently
        # from the value actually posted to the ledger.
        levels = (
            StockLevel.all_tenants.filter(tenant_id=context.tenant_id)
            .select_related("item", "warehouse")
            .order_by("item__sku", "warehouse__code")
        )

        items_section = ReportSection(
            key="items", title="Stock by item and warehouse", sequence=0
        )
        by_warehouse: dict[str, Decimal] = defaultdict(lambda: ZERO)
        warehouse_titles: dict[str, str] = {}
        stock_value = ZERO

        for level in levels.iterator(chunk_size=500):
            if level.quantity_on_hand == ZERO and level.total_value == ZERO:
                continue
            stock_value += level.total_value
            warehouse_key = str(level.warehouse_id)
            warehouse_titles[warehouse_key] = level.warehouse.name
            by_warehouse[warehouse_key] += level.total_value
            items_section.add(
                ReportLine(
                    label=f"{level.item.sku} — {level.item.name} "
                          f"@ {level.warehouse.code}",
                    amount=level.total_value,
                    quantity=level.quantity_on_hand,
                    meta={
                        "item_id": str(level.item_id),
                        "warehouse_id": warehouse_key,
                        "quantity_on_hand": level.quantity_on_hand,
                        "quantity_reserved": level.quantity_reserved,
                        "quantity_available": level.quantity_available,
                        "average_cost": level.average_cost,
                        "valuation_method": level.item.valuation_method,
                    },
                )
            )
        items_section.total = stock_value

        warehouse_section = ReportSection(
            key="warehouses", title="Value by warehouse", sequence=1
        )
        for warehouse_key, value in sorted(
            by_warehouse.items(), key=lambda kv: warehouse_titles[kv[0]]
        ):
            warehouse_section.add(
                ReportLine(
                    label=warehouse_titles[warehouse_key],
                    amount=value,
                    meta={"warehouse_id": warehouse_key},
                )
            )
        warehouse_section.total = stock_value

        # -- reconciliation against the GL ----------------------------------
        control_ids = set(
            Item.all_tenants.filter(
                tenant_id=context.tenant_id, inventory_account__isnull=False
            )
            .values_list("inventory_account_id", flat=True)
            .distinct()
        )
        control_ids |= set(
            Account.all_tenants.filter(
                tenant_id=context.tenant_id,
                system_key__in=["inventory_control", "inventory_asset"],
            ).values_list("id", flat=True)
        )

        result = ReportResult(report_type=self.report_type, currency=context.currency)

        gl_value = ZERO
        if control_ids:
            # Cumulative, not period: an asset balance is a stock.
            # Index used: ix_line_account (tenant, account) plus the
            # entry_date <= as_of range from ix_entry_status.
            totals = (
                ledger_query(
                    context, date_to=context.effective_as_of, ignore_period=True
                )
                .filter(account_id__in=control_ids)
                .aggregate(debit=Sum("base_debit"), credit=Sum("base_credit"))
            )
            gl_value = (totals["debit"] or ZERO) - (totals["credit"] or ZERO)
        else:
            result.warn(
                "No inventory control account is configured (no item carries "
                "an inventory_account and no account has the "
                "'inventory_control' system key), so the valuation could not "
                "be reconciled against the general ledger. The stock figure "
                "below is unverified."
            )

        drift = stock_value - gl_value
        reconciliation = ReportSection(
            key="reconciliation", title="Reconciliation to the general ledger",
            sequence=2,
        )
        reconciliation.add(
            ReportLine(label="Value per stock levels", amount=stock_value)
        )
        reconciliation.add(
            ReportLine(label="Value per inventory control accounts", amount=gl_value)
        )
        reconciliation.add(
            ReportLine(
                label="Drift",
                amount=drift,
                is_bold=True,
                note="Must be zero. Both figures are written by the stock "
                     "service in the same transaction.",
            )
        )
        reconciliation.total = drift

        if drift != ZERO and control_ids:
            result.warn(
                f"INVENTORY DRIFT: stock levels total {stock_value} while the "
                f"inventory control accounts total {gl_value} — a difference "
                f"of {drift}. Both are written by "
                f"apps.inventory.services.stock in one transaction, so a "
                f"difference means one of them was written by something else. "
                f"Usual causes, in order: a tracked item with no "
                f"inventory_account (its movements never reached the GL), a "
                f"manual journal posted directly to the control account, or a "
                f"stock level repaired outside the service. Reported here "
                f"rather than reconciled away, because a silent fix destroys "
                f"the evidence of which one is wrong."
            )

        result.sections = [items_section, warehouse_section, reconciliation]
        result.totals = {
            "stock_value": stock_value,
            "gl_control_value": gl_value,
            "drift": drift,
        }
        result.metadata["as_of"] = context.effective_as_of.isoformat()
        result.metadata["line_count"] = len(items_section.lines)
        return result
