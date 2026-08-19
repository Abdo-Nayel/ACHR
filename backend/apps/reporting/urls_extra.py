"""
Non-viewset reporting routes — mounted at ``/api/v1/reporting/``.

    GET|POST  /reporting/profit-loss/       reporting.profit_loss.read
    GET|POST  /reporting/balance-sheet/     reporting.balance_sheet.read
    GET|POST  /reporting/trial-balance/     reporting.trial_balance.read
    GET|POST  /reporting/cash-flow/         reporting.cash_flow.read
    GET|POST  /reporting/ar-aging/          reporting.aging.read
    GET|POST  /reporting/ap-aging/          reporting.aging.read
    GET|POST  /reporting/tax-summary/       reporting.tax_summary.read
    GET|POST  /reporting/payroll-register/  reporting.payroll_register.read
    GET       /reporting/available/         the registry, for the report picker

One endpoint per statement, not one ``?report_type=`` endpoint
--------------------------------------------------------------
Permissions are granted per statement and that is not an accident: the HR
manager holds ``reporting.payroll_register.read`` and does not hold
``reporting.balance_sheet.read``; the department manager holds ``profit_loss``
and ``aging`` and nothing else. A single endpoint switching on a query
parameter can only be guarded by one codename, so it would have to be the
*loosest* one — and every separately-granted statement would leak through it.
Splitting the routes makes the matrix in ``config/permissions.json`` the thing
that is actually enforced.

``GET`` looks, ``POST`` files
-----------------------------
``GET`` runs the report and returns ``ReportResult.to_dict()`` — every amount
a string, because JSON's single numeric type is an IEEE-754 double and
``numeric(19, 6)`` does not survive it. ``POST`` runs it and writes a
:class:`~apps.reporting.models.ReportSnapshot`: the frozen figures, the
resolved parameters, the moment, the actor and a checksum. Those are different
acts — one is a screen, the other is the statement handed to a bank — so they
are different verbs rather than a query flag, because snapshots cannot be
deleted and one filed by accident is permanent.
"""

from __future__ import annotations

from typing import Any

from django.urls import path
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.tenancy_context import get_current_tenant_id
from apps.iam.permissions import HasPermission, build_scope_q
from apps.reporting.generators.base import registered_reports
from apps.reporting.models import ReportType
from apps.reporting.services.hr_metrics import compute_hr_metrics
from apps.reporting.services.kpis import compute_kpis
from apps.reporting.viewsets import ReportRunView


class ProfitLossView(ReportRunView):
    """Income and expenses over a period. Needs ``date_from`` and ``date_to``.

    A period report with no period silently reports the whole ledger, which is
    a plausible-looking number that answers a different question — so
    ``ReportGenerator.validate_context`` refuses it and this endpoint answers
    400 rather than returning it.
    """

    report_type = ReportType.PROFIT_LOSS
    required_permissions = {"*": ["reporting.profit_loss.read"]}


class BalanceSheetView(ReportRunView):
    """Assets, liabilities and equity at an instant. Needs ``as_of``.

    An *instant*, not a period: passing a ``date_from`` to a balance sheet
    produces a "balance" that is really one month's movement — a figure that
    still balances and is completely wrong. ``ReportContext`` keeps ``as_of``
    separate from the range for exactly that reason.
    """

    report_type = ReportType.BALANCE_SHEET
    required_permissions = {"*": ["reporting.balance_sheet.read"]}


class TrialBalanceView(ReportRunView):
    """Every account's debits and credits, and the proof they are equal."""

    report_type = ReportType.TRIAL_BALANCE
    required_permissions = {"*": ["reporting.trial_balance.read"]}


class CashFlowView(ReportRunView):
    """Operating, investing and financing movements over a period."""

    report_type = ReportType.CASH_FLOW
    required_permissions = {"*": ["reporting.cash_flow.read"]}


class ARAgingView(ReportRunView):
    """What customers owe, bucketed by age — the collections worklist.

    Aged off the *ledger* rather than off ``Invoice.amount_due``, so the total
    of this report is by construction the AR control account balance. Ageing
    the sub-ledger instead makes the aging and the balance sheet disagree the
    first time anything reaches the control account without an invoice.
    """

    report_type = ReportType.AR_AGING
    required_permissions = {"*": ["reporting.aging.read"]}


class APAgingView(ReportRunView):
    """What we owe suppliers, bucketed by age — the payment-run worklist."""

    report_type = ReportType.AP_AGING
    required_permissions = {"*": ["reporting.aging.read"]}


class TaxSummaryView(ReportRunView):
    """Output and input VAT for a period."""

    report_type = ReportType.TAX_SUMMARY
    required_permissions = {"*": ["reporting.tax_summary.read"]}


class PayrollRegisterView(ReportRunView):
    """Per-employee gross, deductions and net, scoped to what the caller may see.

    :class:`~apps.reporting.generators.operational.PayrollRegisterGenerator`
    refuses to decide its own scope and treats ``scope_filter=None`` as an
    error rather than as "everything". The ``Q`` comes from
    :func:`apps.iam.permissions.build_scope_q` — the same function that narrows
    ``/payslips/`` — so the report and the API cannot disagree about who may
    see whose salary, which is exactly what a second implementation private to
    the generator would eventually do.
    """

    report_type = ReportType.PAYROLL_REGISTER
    required_permissions = {"*": ["reporting.payroll_register.read"]}

    def generator_kwargs(self, request) -> dict[str, Any]:
        return {
            "scope_filter": build_scope_q(request.user, "payslip", request=request)
        }


class AvailableReportsView(APIView):
    """``GET /reporting/available/`` — what this deployment can actually produce.

    Read from the ``@register_report`` registry rather than from
    ``ReportType.choices``: the enum lists every report the *schema* knows
    about, the registry lists the ones whose generator module was actually
    imported. A picker built from the enum offers reports that answer "no
    generator registered for ...", and the user has no way to tell which.
    """

    permission_classes = [IsAuthenticated, HasPermission]
    #: ``trial_balance.read`` and not ``report.export``: the latter is
    #: ``is_sensitive``, and a re-authentication prompt to populate a dropdown
    #: teaches people to click through the prompt that guards payroll approval.
    #: This endpoint discloses nothing but the deployment's feature list.
    required_permissions = {"*": ["reporting.trial_balance.read"]}

    def get(self, request):
        labels = dict(ReportType.choices)
        return Response(
            {
                "reports": [
                    {"report_type": key, "label": labels.get(key, key)}
                    for key in registered_reports()
                ]
            }
        )


class KpiView(APIView):
    """``GET /reporting/kpis/`` — the dashboard's headline ratios.

    Query: ``date_from``, ``date_to`` (ISO dates). Both required, for the same
    reason ``ProfitLossGenerator`` requires them: a ratio over an unstated
    window is a number that answers a different question every day.

    Guarded by ``balance_sheet.read`` rather than ``profit_loss.read``. Working
    capital, the quick ratio and debt-to-equity are balance-sheet facts, and a
    department manager who holds only ``profit_loss.read`` is deliberately not
    shown the company's solvency.
    """

    permission_classes = [IsAuthenticated, HasPermission]
    required_permissions = {"*": ["reporting.balance_sheet.read"]}

    def get(self, request):
        from datetime import date as _date  # noqa: PLC0415

        def _parse(name: str):
            raw = request.query_params.get(name)
            if not raw:
                raise DRFValidationError(
                    {name: f"{name} is required (ISO date, e.g. 2026-01-01)."}
                )
            try:
                return _date.fromisoformat(raw)
            except ValueError:
                raise DRFValidationError({name: f"{raw!r} is not an ISO date."})

        date_from = _parse("date_from")
        date_to = _parse("date_to")
        if date_to < date_from:
            raise DRFValidationError(
                {"date_to": "date_to falls before date_from."}
            )

        tenant_id = getattr(request, "tenant_id", None) or get_current_tenant_id()
        return Response(compute_kpis(tenant_id, date_from=date_from, date_to=date_to))


class HrMetricsView(APIView):
    """``GET /reporting/hr-metrics/`` — the dashboard's operational block.

    Optional ``date_from``/``date_to``; defaults to the calendar month, since
    payroll is a monthly fact and an attendance rate over an arbitrary window
    invites comparison against one computed over a different window.

    Guarded by ``hr.employee.read`` rather than a reporting codename. The
    figures are headcount, attendance and payroll cost — HR's own data — and
    gating them behind ``reporting.*`` would hand them to a finance-only role
    that holds no HR permission at all.
    """

    permission_classes = [IsAuthenticated, HasPermission]
    required_permissions = {"*": ["hr.employee.read"]}

    def get(self, request):
        from datetime import date as _date  # noqa: PLC0415

        def _optional(name):
            raw = request.query_params.get(name)
            if not raw:
                return None
            try:
                return _date.fromisoformat(raw)
            except ValueError:
                raise DRFValidationError({name: f"{raw!r} is not an ISO date."})

        date_from = _optional("date_from")
        date_to = _optional("date_to")
        if date_from and date_to and date_to < date_from:
            raise DRFValidationError({"date_to": "date_to falls before date_from."})

        tenant_id = getattr(request, "tenant_id", None) or get_current_tenant_id()
        return Response(
            compute_hr_metrics(tenant_id, date_from=date_from, date_to=date_to)
        )


urlpatterns = [
    path("profit-loss/", ProfitLossView.as_view(), name="profit-loss"),
    path("balance-sheet/", BalanceSheetView.as_view(), name="balance-sheet"),
    path("trial-balance/", TrialBalanceView.as_view(), name="trial-balance"),
    path("cash-flow/", CashFlowView.as_view(), name="cash-flow"),
    path("ar-aging/", ARAgingView.as_view(), name="ar-aging"),
    path("ap-aging/", APAgingView.as_view(), name="ap-aging"),
    path("tax-summary/", TaxSummaryView.as_view(), name="tax-summary"),
    path("payroll-register/", PayrollRegisterView.as_view(), name="payroll-register"),
    path("available/", AvailableReportsView.as_view(), name="available"),
    path("kpis/", KpiView.as_view(), name="kpis"),
    path("hr-metrics/", HrMetricsView.as_view(), name="hr-metrics"),
]
