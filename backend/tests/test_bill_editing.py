"""Editing a draft bill's lines must not trip the bulk-delete guard.

The Bill update serializer replaced lines wholesale with ``bill.lines.all()
.delete()`` — the *tenant-scoped* manager, whose ``delete()`` is deliberately
disabled. So every attempt to edit a draft bill's lines raised
``PermissionDenied`` ("Bulk delete is disabled on tenant-scoped models"), a 403
about an internal guard on an entirely legitimate edit. No test covered bill
line editing, which is why it shipped. This is that test, plus proof the fix
(the shared ``replace_draft_lines`` helper) actually replaces the lines.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.expenses.models import Bill, BillLine, ExpenseCategory, Vendor
from apps.expenses.serializers import BillSerializer
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db


@pytest.fixture
def vendor(tenant, chart_of_accounts) -> Vendor:
    return Vendor.objects.create(
        tenant=tenant, code="V-9001", name="Nile Supplies",
        currency=TEST_CURRENCY, payable_account=chart_of_accounts["ap_control"],
    )


@pytest.fixture
def category(tenant, chart_of_accounts) -> ExpenseCategory:
    return ExpenseCategory.objects.create(
        tenant=tenant, code="OFFICE", name="Office",
        expense_account=chart_of_accounts["office_expense"],
    )


def _draft_bill(tenant, vendor) -> Bill:
    today = date.today()
    bill = Bill.objects.create(
        tenant=tenant, vendor=vendor, bill_date=today,
        due_date=today + timedelta(days=30), currency=TEST_CURRENCY,
        exchange_rate=Decimal("1"), status=Bill.Status.DRAFT,
    )
    BillLine.objects.create(
        tenant=tenant, bill=bill, line_number=1, description="Original line",
        unit_price=Decimal("100.00"), line_subtotal=Decimal("100.00"),
        line_tax=Decimal("0.00"), line_total=Decimal("100.00"),
        expense_account=vendor.payable_account,
    )
    return bill


def test_editing_a_draft_bills_lines_replaces_them_without_a_403(
    tenant, vendor, category, owner_user
):
    bill = _draft_bill(tenant, vendor)
    serializer = BillSerializer(
        instance=bill,
        data={"lines": [
            {"description": "New line A", "quantity": "1", "unit_price": "40.00",
             "expense_account": str(category.expense_account_id)},
            {"description": "New line B", "quantity": "1", "unit_price": "60.00",
             "expense_account": str(category.expense_account_id)},
        ]},
        partial=True,
        context={"tenant_id": tenant.id},
    )
    serializer.is_valid(raise_exception=True)
    serializer.save()  # previously raised PermissionDenied on the bulk delete

    lines = list(BillLine.objects.filter(bill=bill).order_by("line_number"))
    assert [line_.description for line_ in lines] == ["New line A", "New line B"]
    assert not BillLine.objects.filter(bill=bill, description="Original line").exists()
