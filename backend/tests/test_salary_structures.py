"""Salary structures: one package, many employees, dated.

``EmployeeComponent`` already attaches components one employee at a time. That
is the right primitive and the wrong *only* primitive: ninety drivers on the
same package means holding that package ninety times, and raising the
transport allowance means ninety edits of which one gets missed.

Two properties matter:

* **A structure is a template, not a grant.** Editing it changes what future
  assignments resolve to; it does not silently restate payslips already
  issued, because the assignment carries the base salary and the payslip
  carries the resolved lines.
* **Assignments are dated and additive.** A promotion is a second assignment,
  not an overwrite, so last March's pay can still be explained by the package
  in force in March.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.db.utils import IntegrityError

from apps.core.fields import ZERO
from apps.payroll.models import (
    PayrollComponent,
    SalaryStructure,
    SalaryStructureAssignment,
    SalaryStructureLine,
)
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db

BASE = Decimal("10000.00")


@pytest.fixture
def housing(tenant, chart_of_accounts) -> PayrollComponent:
    return PayrollComponent.objects.create(
        tenant=tenant, code="HOUSE", name="Housing allowance",
        component_type=PayrollComponent.ComponentType.EARNING,
        calculation_type=PayrollComponent.CalculationType.FIXED,
        amount=Decimal("2000.00"), currency=TEST_CURRENCY,
        expense_account=chart_of_accounts["payroll_salary_expense"],
        sequence=10,
    )


@pytest.fixture
def transport(tenant, chart_of_accounts) -> PayrollComponent:
    return PayrollComponent.objects.create(
        tenant=tenant, code="TRANS", name="Transport allowance",
        component_type=PayrollComponent.ComponentType.EARNING,
        calculation_type=PayrollComponent.CalculationType.FIXED,
        amount=ZERO, currency=TEST_CURRENCY,
        expense_account=chart_of_accounts["payroll_salary_expense"],
        sequence=20,
    )


@pytest.fixture
def structure(tenant, housing, transport) -> SalaryStructure:
    s = SalaryStructure.objects.create(
        tenant=tenant, code="DRIVER", name="Field Driver package",
        currency=TEST_CURRENCY,
    )
    SalaryStructureLine.objects.create(
        tenant=tenant, structure=s, component=housing,
        amount=Decimal("2000.00"), sequence=10,
    )
    SalaryStructureLine.objects.create(
        tenant=tenant, structure=s, component=transport,
        percentage_of_base=Decimal("0.100000"), sequence=20,
    )
    return s


def test_a_flat_line_resolves_to_its_amount(structure, housing):
    line = structure.lines.get(component=housing)

    assert line.resolve(BASE) == Decimal("2000.00")


def test_a_percentage_line_scales_with_the_base(structure, transport):
    """The reason percentages exist: a promotion re-sizes the package without
    anyone re-deriving the number."""
    line = structure.lines.get(component=transport)

    assert line.resolve(BASE) == Decimal("1000.00")
    assert line.resolve(BASE * 2) == Decimal("2000.00")


def test_a_line_cannot_carry_both_sizings(tenant, structure, housing):
    """Ambiguous by construction — there is no defensible rule for which wins,
    and picking one silently is how a payroll ends up 2 000 out per head."""
    other = PayrollComponent.objects.create(
        tenant=tenant, code="OTHER", name="Other",
        component_type=PayrollComponent.ComponentType.EARNING,
        calculation_type=PayrollComponent.CalculationType.FIXED,
        amount=ZERO, currency=TEST_CURRENCY, sequence=30,
    )

    with pytest.raises(IntegrityError):
        SalaryStructureLine.objects.create(
            tenant=tenant, structure=structure, component=other,
            amount=Decimal("500.00"), percentage_of_base=Decimal("0.050000"),
        )


def test_a_line_carrying_neither_sizing_is_refused(tenant, structure):
    other = PayrollComponent.objects.create(
        tenant=tenant, code="ZERO", name="Zero",
        component_type=PayrollComponent.ComponentType.EARNING,
        calculation_type=PayrollComponent.CalculationType.FIXED,
        amount=ZERO, currency=TEST_CURRENCY, sequence=40,
    )

    with pytest.raises(IntegrityError):
        SalaryStructureLine.objects.create(
            tenant=tenant, structure=structure, component=other,
        )


def test_one_component_appears_once_per_structure(tenant, structure, housing):
    """Two housing lines in one package is a data-entry slip that pays twice."""
    with pytest.raises(IntegrityError):
        SalaryStructureLine.objects.create(
            tenant=tenant, structure=structure, component=housing,
            amount=Decimal("999.00"),
        )


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------

@pytest.fixture
def department(tenant):
    from apps.hr.models import Department  # noqa: PLC0415

    d = Department.objects.create(tenant=tenant, code="ops", name="Ops", depth=0)
    d.path = d.build_path()
    d.save(update_fields=["path", "updated_at"])
    return d


@pytest.fixture
def driver(tenant, department):
    from apps.hr.models import Employee  # noqa: PLC0415

    return Employee.objects.create(
        tenant=tenant, employee_code="E-8001", first_name="Adel", last_name="Nour",
        department=department, hire_date=date(2024, 1, 1),
        base_salary=BASE, salary_currency=TEST_CURRENCY,
    )


def test_two_employees_share_one_structure(tenant, structure, driver, department):
    """The whole point: the package is held once, not once per head."""
    from apps.hr.models import Employee  # noqa: PLC0415

    second = Employee.objects.create(
        tenant=tenant, employee_code="E-8002", first_name="Hoda", last_name="Zaki",
        department=department, hire_date=date(2024, 1, 1),
        base_salary=Decimal("12000.00"), salary_currency=TEST_CURRENCY,
    )
    for emp, base in ((driver, BASE), (second, Decimal("12000.00"))):
        SalaryStructureAssignment.objects.create(
            tenant=tenant, employee=emp, structure=structure,
            from_date=date(2026, 1, 1), base_salary=base, currency=TEST_CURRENCY,
        )

    assert structure.assignments.count() == 2
    # Same package, different money — which is why base_salary is on the
    # assignment and not on the structure.
    transport_line = structure.lines.get(component__code="TRANS")
    assert transport_line.resolve(BASE) == Decimal("1000.00")
    assert transport_line.resolve(Decimal("12000.00")) == Decimal("1200.00")


def test_a_promotion_is_a_second_assignment_not_an_overwrite(
    tenant, structure, driver
):
    """March's payslip must still be explainable by March's package."""
    first = SalaryStructureAssignment.objects.create(
        tenant=tenant, employee=driver, structure=structure,
        from_date=date(2026, 1, 1), to_date=date(2026, 5, 31),
        base_salary=BASE, currency=TEST_CURRENCY,
    )
    second = SalaryStructureAssignment.objects.create(
        tenant=tenant, employee=driver, structure=structure,
        from_date=date(2026, 6, 1), base_salary=Decimal("15000.00"),
        currency=TEST_CURRENCY,
    )

    assert first.covers(date(2026, 3, 15)) is True
    assert second.covers(date(2026, 3, 15)) is False
    assert second.covers(date(2026, 7, 1)) is True
    assert first.covers(date(2026, 7, 1)) is False


def test_two_assignments_cannot_start_on_the_same_day(tenant, structure, driver):
    """Which package applies would be undecidable."""
    SalaryStructureAssignment.objects.create(
        tenant=tenant, employee=driver, structure=structure,
        from_date=date(2026, 1, 1), base_salary=BASE, currency=TEST_CURRENCY,
    )

    with pytest.raises(IntegrityError):
        SalaryStructureAssignment.objects.create(
            tenant=tenant, employee=driver, structure=structure,
            from_date=date(2026, 1, 1), base_salary=Decimal("11000.00"),
            currency=TEST_CURRENCY,
        )


def test_an_assignment_ending_before_it_starts_is_refused(
    tenant, structure, driver
):
    with pytest.raises(IntegrityError):
        SalaryStructureAssignment.objects.create(
            tenant=tenant, employee=driver, structure=structure,
            from_date=date(2026, 6, 1), to_date=date(2026, 5, 1),
            base_salary=BASE, currency=TEST_CURRENCY,
        )


def test_a_zero_base_salary_is_refused(tenant, structure, driver):
    """A percentage line against a zero base silently pays nothing."""
    with pytest.raises(IntegrityError):
        SalaryStructureAssignment.objects.create(
            tenant=tenant, employee=driver, structure=structure,
            from_date=date(2026, 1, 1), base_salary=ZERO, currency=TEST_CURRENCY,
        )


def test_structures_do_not_leak_between_tenants(tenant, other_tenant, structure):
    assert SalaryStructure.objects.filter(code="DRIVER").count() == 1
    from apps.core.tenancy_context import tenant_context  # noqa: PLC0415

    with tenant_context(other_tenant.id):
        assert SalaryStructure.objects.filter(code="DRIVER").count() == 0
