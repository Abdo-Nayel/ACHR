"""Issuing an invoice for a stocked item must move stock *and* post COGS.

Why this file exists
--------------------
This is the one seam where three modules have to agree: sales decides that a
document is issued, inventory decides what leaving the warehouse costs, and
accounting records both consequences. Everything on either side of that seam
was already tested. The seam itself was not tested at all, and both
``tests/test_invoice_workflow.py`` and ``seed_demo_tenant`` said why, in
almost identical words:

    ``invoice_workflow._release_stock_for_invoice`` imports
    ``apps.inventory.services.stock.issue_stock``, which this revision of the
    inventory service does not define.

That is not true, and appears not to have been re-checked since it was
written. ``issue_stock`` is defined in ``apps.inventory.services.fulfilment``
and deliberately re-exported from ``stock`` (see the block at the foot of
``stock.py``, which explains that the re-export exists so callers outside the
app have one import path). The import resolves; the call signature matches the
call site keyword for keyword.

The cost of believing it was that the demo tenant and the AR suite both used
``item=None`` free-text lines to route around a function that worked. So the
revenue leg of an invoice was covered from six directions and the stock and
COGS legs were covered from none -- the two that touch the valuation method
and can silently misstate gross margin.

What is asserted here
---------------------
The four consequences that must hold together, because any one of them alone
can be satisfied by an implementation that is wrong:

1. on-hand quantity falls by the invoiced quantity;
2. a ``StockMovement`` exists linking the invoice to that decrement;
3. a COGS entry is posted (Dr cost of goods sold / Cr inventory) *separately*
   from the revenue entry, at the item's valuation cost, not its sale price;
4. the ledger still balances afterwards.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.accounting.models import JournalEntry
from apps.accounting.services.posting import assert_ledger_balanced
from apps.core.fields import ZERO
from apps.inventory.models import (
    Item,
    StockLevel,
    StockMovement,
    UnitOfMeasure,
    Warehouse,
)
from apps.inventory.services.stock import apply_movement, issue_stock
from apps.sales.models import Customer, Invoice, InvoiceLine
from apps.sales.services.invoice_workflow import issue_invoice
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db

#: Bought at 40, sold at 100. Distinct on purpose: an implementation that
#: values the stock movement at the *sale* price still balances and still
#: decrements, and would pass every assertion that used one number for both.
UNIT_COST = Decimal("40.00")
UNIT_PRICE = Decimal("100.00")
OPENING_QTY = Decimal("10")
SOLD_QTY = Decimal("3")

SUBTOTAL = UNIT_PRICE * SOLD_QTY          # 300.00
TOTAL = SUBTOTAL                          # no tax on this line, keeps the
                                          # identity checkable by eye
EXPECTED_COGS = UNIT_COST * SOLD_QTY      # 120.00


# ---------------------------------------------------------------------------
# Fixtures — the inventory side has none in conftest, because until now no
# test needed stock to exist.
# ---------------------------------------------------------------------------

@pytest.fixture
def uom(tenant) -> UnitOfMeasure:
    return UnitOfMeasure.objects.create(
        tenant=tenant, code="EA", name="Each", symbol="ea", decimal_places=2
    )


@pytest.fixture
def warehouse(tenant) -> Warehouse:
    return Warehouse.objects.create(
        tenant=tenant, code="WH-MAIN", name="Main warehouse", is_default=True
    )


@pytest.fixture
def stocked_item(tenant, uom, chart_of_accounts) -> Item:
    """An inventory item wired to the three accounts a sale touches.

    ``expense_account`` is the COGS account: ``stock._cogs_account`` reads it
    and refuses to post without one, which is the correct refusal -- a cost
    with nowhere to go must stop the sale, not vanish.
    """
    return Item.objects.create(
        tenant=tenant,
        sku="WIDGET-01",
        name="Widget",
        type=Item.Type.INVENTORY,
        uom=uom,
        currency=TEST_CURRENCY,
        income_account=chart_of_accounts["sales_revenue"],
        expense_account=chart_of_accounts["cogs"],
        inventory_account=chart_of_accounts["inventory_asset"],
        track_inventory=True,
    )


@pytest.fixture
def opening_stock(tenant, stocked_item, warehouse, owner_user, open_period) -> StockLevel:
    """Ten units on hand at 40 each, through the real movement service.

    Not created by writing a ``StockLevel`` row directly: the on-hand figure
    and the valuation are derived from movements, so a hand-written level
    would be a number the costing code never agreed to and the COGS assertion
    below would be testing the fixture rather than the system.
    """
    apply_movement(
        tenant_id=tenant.id,
        item=stocked_item,
        warehouse=warehouse,
        movement_type=StockMovement.MovementType.PURCHASE,
        # Signed: positive is goods arriving.
        quantity_delta=OPENING_QTY,
        unit_cost=UNIT_COST,
        reference_type="OPENING",
        user_id=owner_user.id,
    )
    return StockLevel.objects.get(item=stocked_item, warehouse=warehouse)


@pytest.fixture
def customer(tenant, chart_of_accounts) -> Customer:
    return Customer.objects.create(
        tenant=tenant,
        code="C-9001",
        name="Nile Retail Group",
        currency=TEST_CURRENCY,
        receivable_account=chart_of_accounts["ar_control"],
        payment_terms_days=30,
    )


@pytest.fixture
def invoice_with_stock_line(
    tenant, customer, stocked_item, chart_of_accounts, opening_stock
) -> Invoice:
    issue_date = date.today()
    invoice = Invoice.objects.create(
        tenant=tenant,
        customer=customer,
        issue_date=issue_date,
        due_date=issue_date + timedelta(days=30),
        currency=TEST_CURRENCY,
        exchange_rate=Decimal("1"),
        subtotal_amount=SUBTOTAL,
        discount_amount=ZERO,
        tax_amount=ZERO,
        total_amount=TOTAL,
        amount_paid=ZERO,
        amount_due=TOTAL,
        status=Invoice.Status.DRAFT,
    )
    InvoiceLine.objects.create(
        tenant=tenant,
        invoice=invoice,
        line_number=1,
        item=stocked_item,          # the whole point of this module
        description=stocked_item.name,
        quantity=SOLD_QTY,
        unit_price=UNIT_PRICE,
        discount_rate=ZERO,
        tax_rate=None,
        line_subtotal=SUBTOTAL,
        line_tax=ZERO,
        line_total=TOTAL,
        income_account=chart_of_accounts["sales_revenue"],
    )
    return invoice


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

def test_issue_stock_is_importable_from_the_documented_path():
    """The claim that made three other modules route around this path.

    Kept as its own test so that if the re-export at the foot of ``stock.py``
    is ever removed, the failure names the cause instead of surfacing as an
    ImportError inside an unrelated invoice test.
    """
    assert issue_stock.__module__ == "apps.inventory.services.fulfilment"


def test_issuing_an_invoice_decrements_stock(
    tenant, invoice_with_stock_line, owner_user, opening_stock
):
    issue_invoice(
        invoice_with_stock_line.id, tenant_id=tenant.id, user_id=owner_user.id
    )

    opening_stock.refresh_from_db()
    assert opening_stock.quantity_on_hand == OPENING_QTY - SOLD_QTY


def test_issuing_an_invoice_records_a_movement_against_the_invoice(
    tenant, invoice_with_stock_line, owner_user, stocked_item, opening_stock
):
    """The movement must be traceable back to the document that caused it.

    Without the document link a stock-take discrepancy cannot be explained:
    "three units left the building" is not an answer, "three units left on
    invoice INV-000123" is.
    """
    issue_invoice(
        invoice_with_stock_line.id, tenant_id=tenant.id, user_id=owner_user.id
    )

    movement = StockMovement.objects.get(
        item=stocked_item,
        movement_type=StockMovement.MovementType.SALE,
    )
    assert movement.reference_id == invoice_with_stock_line.id
    assert movement.reference_type == "sales.Invoice"
    assert abs(movement.quantity_delta) == SOLD_QTY


def test_issuing_posts_cogs_at_cost_not_at_sale_price(
    tenant, invoice_with_stock_line, owner_user, chart_of_accounts, opening_stock
):
    """Dr COGS / Cr Inventory at 120, while revenue is recognised at 300.

    This is the assertion that a single-number fixture cannot make. Valuing
    the movement at the sale price would overstate cost of sales by 180 and
    report a gross margin of zero on a 60%-margin product -- while leaving
    every entry balanced and every quantity correct.
    """
    before = set(JournalEntry.objects.values_list("id", flat=True))

    issue_invoice(
        invoice_with_stock_line.id, tenant_id=tenant.id, user_id=owner_user.id
    )

    new_entries = JournalEntry.objects.exclude(id__in=before)
    cogs_account = chart_of_accounts["cogs"]
    inventory_account = chart_of_accounts["inventory_asset"]

    cogs_debit = ZERO
    inventory_credit = ZERO
    for entry in new_entries:
        for line in entry.lines.all():
            if line.account_id == cogs_account.id:
                cogs_debit += line.debit
            if line.account_id == inventory_account.id:
                inventory_credit += line.credit

    assert cogs_debit == EXPECTED_COGS, (
        f"COGS was debited {cogs_debit}, expected {EXPECTED_COGS} "
        f"({SOLD_QTY} units at cost {UNIT_COST}). If it equals "
        f"{UNIT_PRICE * SOLD_QTY}, the movement was valued at the sale price."
    )
    assert inventory_credit == EXPECTED_COGS


def test_the_ledger_still_balances_after_a_stocked_sale(
    tenant, invoice_with_stock_line, owner_user, opening_stock
):
    """Two entries post from one transition; both must balance, together."""
    issue_invoice(
        invoice_with_stock_line.id, tenant_id=tenant.id, user_id=owner_user.id
    )
    assert_ledger_balanced(tenant.id)
