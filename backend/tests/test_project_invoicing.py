"""Billing a project's approved time — now a service, so tested as one.

``ProjectViewSet.create_invoice`` was 160 lines of projects→sales→accounting
orchestration inside an HTTP handler, with no test and no way to call it from a
task. It now lives in ``apps.projects.services.invoicing.create_invoice_from_time``.
These tests cover the guards (which were untested) and the happy path (which
proves the extraction preserves the once-only-billing behaviour).
"""

from __future__ import annotations

from datetime import date

import pytest
from django.utils import timezone
from decimal import Decimal

from apps.core.exceptions import DomainError
from apps.hr.models import Department, Employee
from apps.projects.models import Project, TimesheetEntry
from apps.projects.services.invoicing import create_invoice_from_time
from apps.sales.models import Customer, Invoice
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db


@pytest.fixture
def customer(tenant, chart_of_accounts) -> Customer:
    return Customer.objects.create(
        tenant=tenant, code="C-3001", name="Orbit Media",
        currency=TEST_CURRENCY, receivable_account=chart_of_accounts["ar_control"],
    )


@pytest.fixture
def employee(tenant) -> Employee:
    dept = Department.objects.create(tenant=tenant, code="eng", name="Eng", depth=0)
    dept.path = dept.build_path()
    dept.save(update_fields=["path", "updated_at"])
    return Employee.objects.create(
        tenant=tenant, employee_code="E-1", first_name="Sam", last_name="Dev",
        department=dept, hire_date=date(2024, 1, 1),
        base_salary=Decimal("10000.00"), salary_currency=TEST_CURRENCY,
    )


def _project(tenant, *, customer=None, is_billable=True) -> Project:
    return Project.objects.create(
        tenant=tenant, code="P-1", name="Redesign", customer=customer,
        currency=TEST_CURRENCY, status=Project.Status.ACTIVE, is_billable=is_billable,
    )


def _entry(tenant, project, employee, *, hours, rate):
    return TimesheetEntry.objects.create(
        tenant=tenant, project=project, employee=employee, work_date=date.today(),
        hours=Decimal(hours), currency=TEST_CURRENCY, is_billable=True,
        billable_rate=Decimal(rate), cost_rate=Decimal("50.00"),
        status=TimesheetEntry.Status.APPROVED, approved_at=timezone.now(),
    )


def test_project_without_a_customer_cannot_be_invoiced(tenant, owner_user):
    # A *billable* project must have a customer (DB constraint), so the guard is
    # only reachable on a non-billable one — where customer is checked first.
    project = _project(tenant, customer=None, is_billable=False)
    with pytest.raises(DomainError, match="no customer"):
        create_invoice_from_time(project.pk, tenant_id=tenant.id, user_id=owner_user.id)


def test_non_billable_project_is_refused(tenant, customer, owner_user):
    project = _project(tenant, customer=customer, is_billable=False)
    with pytest.raises(DomainError, match="non-billable"):
        create_invoice_from_time(project.pk, tenant_id=tenant.id, user_id=owner_user.id)


def test_no_approved_time_is_refused(tenant, customer, owner_user):
    project = _project(tenant, customer=customer)
    with pytest.raises(DomainError, match="no approved"):
        create_invoice_from_time(project.pk, tenant_id=tenant.id, user_id=owner_user.id)


def test_approved_time_becomes_a_draft_invoice_billed_once(
    tenant, customer, employee, owner_user
):
    project = _project(tenant, customer=customer)
    _entry(tenant, project, employee, hours="10", rate="100.00")
    _entry(tenant, project, employee, hours="5", rate="100.00")

    result = create_invoice_from_time(
        project.pk, tenant_id=tenant.id, user_id=owner_user.id
    )

    invoice = result.invoice
    assert invoice.status == Invoice.Status.DRAFT
    assert invoice.total_amount == Decimal("1500.000000")
    assert result.entry_count == 2

    # Every entry is now INVOICED and linked — the once-only-billing guarantee.
    for entry in TimesheetEntry.objects.filter(project=project):
        assert entry.status == TimesheetEntry.Status.INVOICED
        assert entry.invoice_line_id is not None

    # A second run finds nothing left to bill.
    with pytest.raises(DomainError, match="no approved"):
        create_invoice_from_time(project.pk, tenant_id=tenant.id, user_id=owner_user.id)
