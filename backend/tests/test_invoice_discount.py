"""The header discount, and the 409 it used to produce.

``ck_invoice_total_identity`` asserts ``total = subtotal - discount + tax`` and
is an IMMEDIATE check, so it runs on the INSERT itself.

``subtotal_amount``, ``tax_amount`` and ``total_amount`` are read-only on
``InvoiceSerializer`` — they are derived from lines that do not exist until
after the header row is written — so the insert wrote ``0, 0, 0`` alongside
whatever discount the caller sent. With a discount of 5.00 the database
evaluated ``0 = 0 - 5 + 0``, refused the row, and the generic
``IntegrityError`` handler answered *"The request conflicts with existing
data."*

A zero discount slipped through because ``0 = 0 - 0 + 0`` holds, which is why
the field looked like it worked. It never had: nothing exercised a non-zero
header discount until the rebuilt invoice form put the input on screen.

The fix withholds the discount from the INSERT and writes it in the same
statement as the figures it is checked against.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.sales.models import Customer, Invoice
from apps.sales.serializers import InvoiceSerializer
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db

#: 3 × 33.33 = 99.99. Deliberately not a round number: a discount that divides
#: cleanly into the subtotal would hide a rounding error in the identity.
SUBTOTAL = Decimal("99.99")


@pytest.fixture
def customer(tenant, chart_of_accounts) -> Customer:
    return Customer.objects.create(
        tenant=tenant, code="C-7001", name="Nile Retail",
        currency=TEST_CURRENCY,
        receivable_account=chart_of_accounts["ar_control"],
    )


def _save(customer, chart_of_accounts, tenant, discount, instance=None):
    payload = {
        "customer": customer.id,
        "issue_date": date.today(),
        "due_date": date.today() + timedelta(days=30),
        "currency": TEST_CURRENCY,
        "discount_amount": Decimal(discount),
        "lines": [{
            "description": "Consulting",
            "quantity": Decimal("3"),
            "unit_price": Decimal("33.33"),
            "income_account": chart_of_accounts["service_revenue"].id,
        }],
    }
    serializer = InvoiceSerializer(
        instance=instance, data=payload, partial=bool(instance),
        context={"tenant_id": tenant.id},
    )
    serializer.is_valid(raise_exception=True)
    return serializer.save(**({} if instance else {"tenant_id": tenant.id}))


def test_a_header_discount_can_be_applied_at_all(
    tenant, customer, chart_of_accounts
):
    """The regression: this raised IntegrityError -> 409 for every non-zero
    value."""
    invoice = _save(customer, chart_of_accounts, tenant, "5.00")

    assert invoice.discount_amount == Decimal("5.00")
    assert invoice.subtotal_amount == SUBTOTAL
    assert invoice.total_amount == SUBTOTAL - Decimal("5.00")


def test_a_zero_discount_still_works(tenant, customer, chart_of_accounts):
    """The case that always passed, kept so the fix cannot regress it."""
    invoice = _save(customer, chart_of_accounts, tenant, "0.00")

    assert invoice.total_amount == SUBTOTAL


def test_the_identity_holds_for_an_awkward_discount(
    tenant, customer, chart_of_accounts
):
    """The database re-checks ``total = subtotal - discount + tax`` on every
    write, so a value that does not divide cleanly is the one worth asserting."""
    invoice = _save(customer, chart_of_accounts, tenant, "33.33")

    assert invoice.total_amount == Decimal("66.66")
    assert (invoice.subtotal_amount - invoice.discount_amount
            + invoice.tax_amount) == invoice.total_amount


def test_a_discount_equal_to_the_subtotal_is_allowed(
    tenant, customer, chart_of_accounts
):
    """A fully discounted invoice is a real document — a goodwill credit
    issued as a zero-value invoice rather than a credit note."""
    invoice = _save(customer, chart_of_accounts, tenant, str(SUBTOTAL))

    assert invoice.total_amount == Decimal("0.00")
    assert invoice.amount_due == Decimal("0.00")


def test_a_discount_larger_than_the_subtotal_is_a_400_not_a_409(
    tenant, customer, chart_of_accounts
):
    """A negative invoice is a credit note, and credit notes are their own
    document. The refusal must name the field, not surface as a database
    constraint the caller cannot see."""
    from rest_framework.exceptions import ValidationError  # noqa: PLC0415

    with pytest.raises(ValidationError) as exc:
        _save(customer, chart_of_accounts, tenant, "150.00")

    assert "discount_amount" in str(exc.value)


def test_adding_a_discount_on_update_works(tenant, customer, chart_of_accounts):
    """The UPDATE path had the identical defect: writing a new discount while
    subtotal/tax/total still held their old values broke the identity."""
    invoice = _save(customer, chart_of_accounts, tenant, "0.00")

    updated = _save(customer, chart_of_accounts, tenant, "10.00", instance=invoice)

    assert updated.discount_amount == Decimal("10.00")
    assert updated.total_amount == SUBTOTAL - Decimal("10.00")


def test_removing_a_discount_on_update_works(tenant, customer, chart_of_accounts):
    invoice = _save(customer, chart_of_accounts, tenant, "10.00")

    updated = _save(customer, chart_of_accounts, tenant, "0.00", instance=invoice)

    assert updated.discount_amount == Decimal("0.00")
    assert updated.total_amount == SUBTOTAL


def test_amount_due_follows_the_discounted_total(
    tenant, customer, chart_of_accounts
):
    """``ck_invoice_due_identity`` is the other constraint in the same row, and
    it is checked against the discounted total rather than the subtotal."""
    invoice = _save(customer, chart_of_accounts, tenant, "20.00")

    assert invoice.amount_due == invoice.total_amount - invoice.amount_paid
