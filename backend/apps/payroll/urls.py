"""
Payroll URL registration.

``payroll-runs`` and ``payslips`` keep their prefixes. The configuration
surface — components, tax brackets and per-employee payroll profiles — is new.

``payslips`` is read-only by design: a payslip is an
``ImmutableFinancialModel`` and the statement an employee was given must stay
recoverable. It is also the most tightly scoped collection in the product —
``resource = "payslip"`` is in ``SCOPE_FIELDS``, so an actor with no scope rule
sees nothing rather than everything.
"""

from __future__ import annotations

from apps.payroll.viewsets import (
    EmployeePayrollProfileViewSet,
    PayrollComponentViewSet,
    PayrollRunViewSet,
    PayslipViewSet,
    SalaryStructureAssignmentViewSet,
    SalaryStructureLineViewSet,
    SalaryStructureViewSet,
    TaxBracketViewSet,
)


def register(router) -> None:
    """Mount this app's routes on the v1 router."""
    router.register(r"payroll-runs", PayrollRunViewSet, basename="payroll-runs")
    router.register(r"payslips", PayslipViewSet, basename="payslips")
    router.register(
        r"payroll-components", PayrollComponentViewSet, basename="payroll-components"
    )
    router.register(r"tax-brackets", TaxBracketViewSet, basename="tax-brackets")
    router.register(
        r"payroll-profiles", EmployeePayrollProfileViewSet, basename="payroll-profiles"
    )
    router.register(
        r"salary-structures", SalaryStructureViewSet, basename="salary-structures"
    )
    router.register(
        r"salary-structure-lines", SalaryStructureLineViewSet,
        basename="salary-structure-lines",
    )
    router.register(
        r"salary-structure-assignments", SalaryStructureAssignmentViewSet,
        basename="salary-structure-assignments",
    )
