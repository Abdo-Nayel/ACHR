"""Amending a draft invoice.

``InvoiceSerializer.update`` has always replaced lines wholesale — the right
call, since line identity carries no business meaning on a draft and a partial
diff is how a line the user deleted survives and quietly inflates the total.

But it did the replacement with ``invoice.lines.all().delete()``, and
``TenantQuerySet.delete()`` refuses: bulk delete is disabled on tenant-scoped
models so a stray ``.delete()`` cannot wipe a customer's ledger. So every
``PATCH /invoices/{id}/`` carrying lines answered *403 "Bulk delete is disabled
on tenant-scoped models"* — a message about an internal guard, on a request
that was entirely legitimate.

Nothing caught it because nothing had ever edited a draft: the UI could only
create, and no test exercised update. It surfaced the moment the invoice form
grew an Edit button.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.core.fields import ZERO
from apps.sales.models import Customer, Invoice, InvoiceLine
from apps.sales.serializers import InvoiceSerializer
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db


@pytest.fixture
def customer(tenant, chart_of_accounts) -> Customer:
    return Customer.objects.create(
        tenant=tenant, code="C-5001", name="Nile Retail Group",
        currency=TEST_CURRENCY,
        receivable_account=chart_of_accounts["ar_control"],
        payment_terms_days=30,
    )


def _payload(customer, chart_of_accounts, lines):
    today = date.today()
    return {
        "customer": customer.id,
        "issue_date": today,
        "due_date": today + timedelta(days=30),
        "currency": TEST_CURRENCY,
        "lines": [
            {
                "description": d,
                "quantity": Decimal(q),
                "unit_price": Decimal(p),
                "income_account": chart_of_accounts["service_revenue"].id,
            }
            for d, q, p in lines
        ],
    }


def _create(customer, chart_of_accounts, lines, tenant):
    serializer = InvoiceSerializer(
        data=_payload(customer, chart_of_accounts, lines),
        context={"tenant_id": tenant.id},
    )
    serializer.is_valid(raise_exception=True)
    return serializer.save(tenant_id=tenant.id)


def _update(invoice, customer, chart_of_accounts, lines, tenant):
    serializer = InvoiceSerializer(
        instance=invoice,
        data=_payload(customer, chart_of_accounts, lines),
        partial=True,
        context={"tenant_id": tenant.id},
    )
    serializer.is_valid(raise_exception=True)
    return serializer.save()


def test_updating_a_draft_replaces_its_lines(tenant, customer, chart_of_accounts):
    """The regression: this raised 403 on the bulk-delete guard."""
    invoice = _create(customer, chart_of_accounts,
                      [("Consulting", "10", "100")], tenant)
    assert invoice.total_amount == Decimal("1000.00")

    updated = _update(invoice, customer, chart_of_accounts,
                      [("Consulting", "4", "100")], tenant)

    assert updated.lines.count() == 1
    assert updated.total_amount == Decimal("400.00")


def test_a_removed_line_does_not_survive_the_update(
    tenant, customer, chart_of_accounts
):
    """The failure wholesale replacement exists to prevent: a diff leaves the
    deleted row behind and the total silently stays high."""
    invoice = _create(customer, chart_of_accounts,
                      [("Consulting", "10", "100"), ("Support", "5", "50")], tenant)
    assert invoice.total_amount == Decimal("1250.00")

    updated = _update(invoice, customer, chart_of_accounts,
                      [("Consulting", "10", "100")], tenant)

    assert updated.lines.count() == 1
    assert updated.total_amount == Decimal("1000.00")


def test_added_lines_are_numbered_from_one_again(
    tenant, customer, chart_of_accounts
):
    """``line_number`` is assigned from list position, so a replacement must
    not collide with ``uq_invoice_line_number`` left over from the old set."""
    invoice = _create(customer, chart_of_accounts,
                      [("A", "1", "10")], tenant)

    updated = _update(invoice, customer, chart_of_accounts,
                      [("B", "1", "20"), ("C", "1", "30")], tenant)

    numbers = sorted(updated.lines.values_list("line_number", flat=True))
    assert numbers == [1, 2]
    assert updated.total_amount == Decimal("50.00")


def test_header_fields_round_trip(tenant, customer, chart_of_accounts):
    """The three fields the rebuilt form added."""
    invoice = _create(customer, chart_of_accounts, [("A", "1", "10")], tenant)

    serializer = InvoiceSerializer(
        instance=invoice,
        data={"order_number": "PO-99881", "subject": "August retainer"},
        partial=True,
        context={"tenant_id": tenant.id},
    )
    serializer.is_valid(raise_exception=True)
    updated = serializer.save()

    assert updated.order_number == "PO-99881"
    assert updated.subject == "August retainer"


def test_an_issued_invoice_cannot_be_edited(
    tenant, customer, chart_of_accounts, open_period, owner_user
):
    """The guard the whole design rests on: an issued invoice has been sent to
    a customer and posted to the ledger. Amend it with a credit note."""
    from apps.sales.services.invoice_workflow import issue_invoice  # noqa: PLC0415

    invoice = _create(customer, chart_of_accounts, [("A", "1", "10")], tenant)
    issue_invoice(invoice.id, tenant_id=tenant.id, user_id=owner_user.id)
    invoice.refresh_from_db()

    serializer = InvoiceSerializer(
        instance=invoice,
        data={"subject": "sneaky amendment"},
        partial=True,
        context={"tenant_id": tenant.id},
    )
    assert not serializer.is_valid()


def test_replacement_does_not_touch_another_invoices_lines(
    tenant, customer, chart_of_accounts
):
    """The bypass filters explicitly by tenant *and* parent, so it cannot
    widen its own scope — the reason the plain queryset is used rather than
    disabling the guard."""
    keep = _create(customer, chart_of_accounts, [("Keep", "1", "99")], tenant)
    edit = _create(customer, chart_of_accounts, [("Edit", "1", "10")], tenant)

    _update(edit, customer, chart_of_accounts, [("Edited", "2", "10")], tenant)

    keep.refresh_from_db()
    assert keep.lines.count() == 1
    assert keep.lines.first().description == "Keep"
    assert InvoiceLine.objects.filter(invoice=keep).exists()
