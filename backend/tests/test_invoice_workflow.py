"""The invoice state machine and its ledger effects.

An invoice is the AR sub-ledger's unit, and every transition it makes either
moves a balance or must provably not. The tests below pair each transition
with the money assertion that makes it meaningful — asserting only that a
status changed would pass on an implementation that changed nothing else.

Two facts about this module's fixtures, both deliberate:

* **Invoice lines carry no item.** These tests are about the AR path — the
  revenue entry, the receivable, the state machine — and a free-text service
  line exercises all of it without dragging in a warehouse, a unit of measure
  and a costing method.

  This was previously justified on different grounds: that
  ``apps.inventory.services.stock.issue_stock`` "does not define" in this
  revision and an item line would raise ``ImportError``. That was not true —
  ``fulfilment`` defines it and ``stock`` re-exports it deliberately — and
  believing it left the sales/inventory/COGS seam with no coverage at all.
  ``tests/test_invoice_stock_integration.py`` covers it now.
* **Amounts are chosen so the identities are checkable by eye.**
  ``total = subtotal - discount + tax`` and ``due = total - paid`` are
  database CHECK constraints, so a fixture that violated them would fail with
  an ``IntegrityError`` rather than a readable assertion.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounting.models import JournalEntry, JournalLine
from apps.accounting.services.posting import assert_ledger_balanced
from apps.core.fields import ZERO, quantize_currency
from apps.payments.models import Payment, PaymentApplication
from apps.sales.models import Customer, Invoice, InvoiceLine
from apps.sales.services.invoice_workflow import (
    SALES_JOURNAL_CODE,
    apply_payment,
    build_invoice_entry,
    issue_invoice,
    refresh_overdue_status,
    void_invoice,
    write_off_invoice,
)
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db

#: 10 units at 100.00, no discount, 14% VAT: subtotal 1000.00, tax 140.00,
#: total 1140.00. Round numbers on purpose — a rounding bug should show up as
#: an obviously wrong figure, not as a plausible one.
QUANTITY = Decimal("10.000000")
UNIT_PRICE = Decimal("100.000000")
VAT_RATE = Decimal("0.140000")
SUBTOTAL = Decimal("1000.00")
TAX = Decimal("140.00")
TOTAL = Decimal("1140.00")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vat(tenant, chart_of_accounts):
    from apps.accounting.models import TaxRate

    return TaxRate.objects.create(
        tenant=tenant,
        name="Standard VAT 14%",
        code="VAT-STD",
        rate=VAT_RATE,
        collected_account=chart_of_accounts["output_vat"],
        paid_account=chart_of_accounts["input_vat"],
        effective_from=date(date.today().year, 1, 1),
    )


@pytest.fixture
def customer(tenant, chart_of_accounts):
    return Customer.objects.create(
        tenant=tenant,
        code="C-0001",
        name="Nile Retail Group",
        currency=TEST_CURRENCY,
        receivable_account=chart_of_accounts["ar_control"],
        payment_terms_days=30,
    )


def _make_invoice(
    tenant,
    customer,
    chart_of_accounts,
    vat,
    *,
    issue_date: date | None = None,
    due_in_days: int = 30,
) -> Invoice:
    issue_date = issue_date or date.today()
    invoice = Invoice.objects.create(
        tenant=tenant,
        customer=customer,
        issue_date=issue_date,
        due_date=issue_date + timedelta(days=due_in_days),
        currency=TEST_CURRENCY,
        exchange_rate=Decimal("1"),
        subtotal_amount=SUBTOTAL,
        discount_amount=ZERO,
        tax_amount=TAX,
        total_amount=TOTAL,
        amount_paid=ZERO,
        amount_due=TOTAL,
        status=Invoice.Status.DRAFT,
    )
    InvoiceLine.objects.create(
        tenant=tenant,
        invoice=invoice,
        line_number=1,
        item=None,  # see the module docstring: no stock leg in this revision
        description="Consulting — implementation sprint",
        quantity=QUANTITY,
        unit_price=UNIT_PRICE,
        discount_rate=ZERO,
        tax_rate=vat,
        line_subtotal=SUBTOTAL,
        line_tax=TAX,
        line_total=TOTAL,
        income_account=chart_of_accounts["service_revenue"],
    )
    return invoice


@pytest.fixture
def draft_invoice(tenant, customer, chart_of_accounts, vat, open_period):
    return _make_invoice(tenant, customer, chart_of_accounts, vat)


@pytest.fixture
def sent_invoice(tenant, draft_invoice, owner_user):
    return issue_invoice(draft_invoice.id, tenant_id=tenant.id, user_id=owner_user.id)


def _pay(tenant, customer, invoice, chart_of_accounts, owner_user, amount: Decimal):
    """Record a receipt and apply it, then let the workflow recompute.

    ``apply_payment`` deliberately *recomputes* ``amount_paid`` from the
    application rows rather than incrementing it, so the test creates the
    application and asks the service for the consequence — the same shape the
    production caller uses.
    """
    payment = Payment.objects.create(
        tenant=tenant,
        customer=customer,
        number=f"PMT-{timezone.now().timestamp():.6f}"[:32],
        payment_date=date.today(),
        currency=TEST_CURRENCY,
        amount=amount,
        unapplied_amount=ZERO,
        fee_amount=ZERO,
        method=Payment.Method.BANK_TRANSFER,
        status=Payment.Status.SETTLED,
        deposit_account=chart_of_accounts["bank_main"],
        settled_at=timezone.now(),
    )
    PaymentApplication.objects.create(
        tenant=tenant,
        payment=payment,
        invoice=invoice,
        amount=amount,
        applied_on=date.today(),
    )
    return apply_payment(invoice.id, tenant_id=tenant.id, user_id=owner_user.id)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_draft_to_sent_to_partially_paid_to_paid(
    tenant, customer, chart_of_accounts, draft_invoice, owner_user
):
    """The whole lifecycle, asserting ``amount_due`` after every step.

    ``amount_due`` is a stored column *and* a CHECK constraint
    (``due == total - paid``), because the aging report reads it on every row
    and a drifting cache is an AR ledger that no longer ties to the GL.
    """
    assert draft_invoice.status == Invoice.Status.DRAFT
    assert draft_invoice.number == "", (
        "A draft must not burn a number: an abandoned draft would leave a gap "
        "in the sequence, which a tax auditor reads as a deleted invoice."
    )
    assert draft_invoice.amount_due == TOTAL

    sent = issue_invoice(draft_invoice.id, tenant_id=tenant.id, user_id=owner_user.id)
    assert sent.status == Invoice.Status.SENT
    assert sent.number, "An issued invoice must carry a number."
    assert sent.journal_entry_id is not None
    assert sent.amount_due == TOTAL
    assert sent.amount_paid == ZERO

    part = _pay(tenant, customer, sent, chart_of_accounts, owner_user, Decimal("400.00"))
    assert part.status == Invoice.Status.PARTIALLY_PAID
    assert part.amount_paid == Decimal("400.00")
    assert part.amount_due == TOTAL - Decimal("400.00")
    assert part.paid_at is None

    settled = _pay(
        tenant, customer, part, chart_of_accounts, owner_user, Decimal("740.00")
    )
    assert settled.status == Invoice.Status.PAID
    assert settled.amount_paid == TOTAL
    assert settled.amount_due == ZERO
    assert settled.paid_at is not None


def test_issuing_posts_a_balanced_entry_whose_ar_debit_is_the_invoice_total(
    tenant, chart_of_accounts, draft_invoice, owner_user
):
    """``Dr AR total / Cr revenue subtotal / Cr output VAT``.

    It balances because ``total = subtotal - discount + tax`` — the identity
    the ``ck_invoice_total_identity`` constraint keeps true on every row, and
    the reason the posting can be built without a plug figure.
    """
    invoice = issue_invoice(draft_invoice.id, tenant_id=tenant.id, user_id=owner_user.id)
    entry = JournalEntry.all_tenants.get(pk=invoice.journal_entry_id)

    assert entry.status == JournalEntry.Status.POSTED
    assert entry.source == JournalEntry.Source.INVOICE
    assert entry.journal.code == SALES_JOURNAL_CODE
    assert entry.total_debit == entry.total_credit == TOTAL

    lines = list(JournalLine.all_tenants.filter(tenant_id=tenant.id, entry=entry))
    by_account = {}
    for line in lines:
        debit, credit = by_account.get(line.account_id, (ZERO, ZERO))
        by_account[line.account_id] = (debit + line.debit, credit + line.credit)

    ar_debit, ar_credit = by_account[chart_of_accounts["ar_control"].id]
    assert ar_debit == TOTAL, "The AR debit must equal the invoice total, gross of tax."
    assert ar_credit == ZERO

    revenue_debit, revenue_credit = by_account[chart_of_accounts["service_revenue"].id]
    assert revenue_credit == SUBTOTAL, "Revenue is recognised net of tax."
    assert revenue_debit == ZERO

    vat_debit, vat_credit = by_account[chart_of_accounts["output_vat"].id]
    assert vat_credit == TAX
    assert vat_debit == ZERO

    # Every AR line is tagged with the customer, or the sub-ledger cannot be
    # reconciled to the control account.
    ar_lines = [
        line
        for line in lines
        if line.account_id == chart_of_accounts["ar_control"].id
    ]
    assert all(line.partner_type == "customer" for line in ar_lines)
    assert all(line.partner_id == invoice.customer_id for line in ar_lines)

    assert_ledger_balanced(tenant.id)


def test_issuing_is_idempotent_at_the_ledger(
    tenant, chart_of_accounts, draft_invoice, owner_user
):
    """The draft's key is ``invoice:issue:{id}``, so a retried issue after a
    partial failure cannot create a second revenue entry."""
    invoice = issue_invoice(draft_invoice.id, tenant_id=tenant.id, user_id=owner_user.id)

    from apps.accounting.services.posting import post_entry

    replay = post_entry(
        build_invoice_entry(invoice), tenant_id=tenant.id, user_id=owner_user.id
    )
    assert replay.id == invoice.journal_entry_id
    assert (
        JournalEntry.all_tenants.filter(
            tenant_id=tenant.id, source_document_id=invoice.id
        ).count()
        == 1
    )


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (Invoice.Status.PAID, Invoice.Status.DRAFT),
        (Invoice.Status.PAID, Invoice.Status.VOIDED),
        (Invoice.Status.VOIDED, Invoice.Status.SENT),
        (Invoice.Status.VOIDED, Invoice.Status.PAID),
        (Invoice.Status.WRITTEN_OFF, Invoice.Status.SENT),
        (Invoice.Status.DRAFT, Invoice.Status.PAID),
        (Invoice.Status.DRAFT, Invoice.Status.WRITTEN_OFF),
        (Invoice.Status.PARTIALLY_PAID, Invoice.Status.VOIDED),
    ],
    ids=lambda value: str(value),
)
def test_illegal_transitions_are_refused(draft_invoice, from_status, to_status):
    """``ALLOWED_TRANSITIONS`` is the contract, and each refusal has a reason:

    * ``PAID -> DRAFT`` — "mark as unpaid" is the single most common way an AR
      ledger gets corrupted; it changes the invoice without changing the cash
      recorded against it.
    * ``PAID -> VOIDED`` / ``PARTIALLY_PAID -> VOIDED`` — cash has arrived; a
      void would leave that money sitting against nothing.
    * ``VOIDED -> *`` — terminal. The document was a mistake and stays one.
    * ``DRAFT -> WRITTEN_OFF`` — a write-off recognises a real, uncollectable
      sale; a draft was never a sale.
    """
    draft_invoice.status = from_status
    with pytest.raises(ValueError) as exc:
        draft_invoice.assert_can_transition(to_status)
    assert "Illegal invoice transition" in str(exc.value)


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (Invoice.Status.DRAFT, Invoice.Status.SENT),
        (Invoice.Status.DRAFT, Invoice.Status.VOIDED),
        (Invoice.Status.SENT, Invoice.Status.PARTIALLY_PAID),
        (Invoice.Status.SENT, Invoice.Status.OVERDUE),
        (Invoice.Status.OVERDUE, Invoice.Status.PAID),
        # The payment-reversal path: the only route back out of PAID.
        (Invoice.Status.PAID, Invoice.Status.PARTIALLY_PAID),
        (Invoice.Status.PAID, Invoice.Status.SENT),
    ],
    ids=lambda value: str(value),
)
def test_legal_transitions_are_allowed(draft_invoice, from_status, to_status):
    draft_invoice.status = from_status
    draft_invoice.assert_can_transition(to_status)  # must not raise


def test_issuing_an_already_issued_invoice_is_refused(tenant, sent_invoice, owner_user):
    with pytest.raises(ValueError):
        issue_invoice(sent_invoice.id, tenant_id=tenant.id, user_id=owner_user.id)


def test_issuing_a_zero_value_invoice_is_refused(
    tenant, customer, chart_of_accounts, vat, open_period, owner_user
):
    invoice = Invoice.objects.create(
        tenant=tenant, customer=customer, issue_date=date.today(),
        due_date=date.today() + timedelta(days=30), currency=TEST_CURRENCY,
        subtotal_amount=ZERO, discount_amount=ZERO, tax_amount=ZERO,
        total_amount=ZERO, amount_paid=ZERO, amount_due=ZERO,
        status=Invoice.Status.DRAFT,
    )
    with pytest.raises(ValidationError) as exc:
        issue_invoice(invoice.id, tenant_id=tenant.id, user_id=owner_user.id)
    assert "zero-value invoice" in str(exc.value)


def test_issuing_an_invoice_with_no_lines_is_refused(
    tenant, customer, open_period, owner_user
):
    invoice = Invoice.objects.create(
        tenant=tenant, customer=customer, issue_date=date.today(),
        due_date=date.today() + timedelta(days=30), currency=TEST_CURRENCY,
        subtotal_amount=SUBTOTAL, discount_amount=ZERO, tax_amount=ZERO,
        total_amount=SUBTOTAL, amount_paid=ZERO, amount_due=SUBTOTAL,
        status=Invoice.Status.DRAFT,
    )
    with pytest.raises(ValidationError):
        issue_invoice(invoice.id, tenant_id=tenant.id, user_id=owner_user.id)


# ---------------------------------------------------------------------------
# Overpayment and voiding
# ---------------------------------------------------------------------------

def test_overpayment_is_refused(
    tenant, customer, chart_of_accounts, sent_invoice, owner_user
):
    """Surplus cash belongs on the payment as unapplied credit, never on the
    invoice.

    Letting ``amount_paid`` exceed ``total_amount`` de-syncs the AR sub-ledger
    from the GL control account: the invoice claims to have absorbed money the
    control account never received against it. The database would refuse it
    (``ck_invoice_no_overpayment``); the service refuses it first so the error
    names the cause.
    """
    payment = Payment.objects.create(
        tenant=tenant, customer=customer, number="PMT-OVER",
        payment_date=date.today(), currency=TEST_CURRENCY,
        amount=TOTAL + Decimal("100.00"), unapplied_amount=ZERO, fee_amount=ZERO,
        method=Payment.Method.BANK_TRANSFER, status=Payment.Status.SETTLED,
        deposit_account=chart_of_accounts["bank_main"], settled_at=timezone.now(),
    )
    PaymentApplication.objects.create(
        tenant=tenant, payment=payment, invoice=sent_invoice,
        amount=TOTAL + Decimal("100.00"), applied_on=date.today(),
    )

    with pytest.raises(ValidationError) as exc:
        apply_payment(sent_invoice.id, tenant_id=tenant.id, user_id=owner_user.id)
    assert "exceed invoice total" in str(exc.value)

    sent_invoice.refresh_from_db()
    assert sent_invoice.amount_paid == ZERO, "A refused application still moved money."
    assert sent_invoice.amount_due == TOTAL


def test_voiding_a_paid_invoice_is_refused(
    tenant, customer, chart_of_accounts, sent_invoice, owner_user
):
    """A void erases the document; the received money would then sit against
    nothing. The correct instrument is a credit note plus a refund."""
    paid = _pay(tenant, customer, sent_invoice, chart_of_accounts, owner_user, TOTAL)
    assert paid.status == Invoice.Status.PAID

    with pytest.raises(ValueError):
        # PAID -> VOIDED is not in the transition map at all.
        void_invoice(paid.id, tenant_id=tenant.id, reason="changed my mind",
                     user_id=owner_user.id)

    paid.refresh_from_db()
    assert paid.status == Invoice.Status.PAID


def test_voiding_a_partially_paid_invoice_is_refused(
    tenant, customer, chart_of_accounts, sent_invoice, owner_user
):
    _pay(tenant, customer, sent_invoice, chart_of_accounts, owner_user, Decimal("100.00"))
    sent_invoice.refresh_from_db()

    with pytest.raises((ValidationError, ValueError)):
        void_invoice(sent_invoice.id, tenant_id=tenant.id, reason="oops",
                     user_id=owner_user.id)


def test_voiding_an_unpaid_invoice_unwinds_the_ledger_and_keeps_the_number(
    tenant, chart_of_accounts, sent_invoice, owner_user
):
    """The number stays consumed — a gap is what an auditor looks for first."""
    from apps.accounting.models import Account

    number = sent_invoice.number
    ar = chart_of_accounts["ar_control"]
    balance_before_void = Account.all_tenants.get(pk=ar.pk).cached_balance

    voided = void_invoice(
        sent_invoice.id, tenant_id=tenant.id, reason="duplicate of INV-2",
        user_id=owner_user.id,
    )

    assert voided.status == Invoice.Status.VOIDED
    assert voided.number == number
    assert voided.void_reason == "duplicate of INV-2"

    entry = JournalEntry.all_tenants.get(pk=voided.journal_entry_id)
    assert entry.status == JournalEntry.Status.VOIDED
    assert Account.all_tenants.get(pk=ar.pk).cached_balance == (
        balance_before_void - TOTAL
    )


def test_voiding_requires_a_reason(tenant, sent_invoice, owner_user):
    with pytest.raises(ValidationError):
        void_invoice(sent_invoice.id, tenant_id=tenant.id, reason="  ",
                     user_id=owner_user.id)


def test_write_off_posts_bad_debt_and_settles_the_balance(
    tenant, chart_of_accounts, sent_invoice, owner_user
):
    """``Dr Bad debt expense / Cr AR`` — distinct from a void in every way
    that matters: the sale was real and the revenue stays recognised; it is
    the *asset* that turned out to be worthless."""
    written_off = write_off_invoice(
        sent_invoice.id, tenant_id=tenant.id, reason="customer insolvent",
        user_id=owner_user.id,
    )

    assert written_off.status == Invoice.Status.WRITTEN_OFF
    assert written_off.amount_due == ZERO
    assert written_off.written_off_at is not None

    entry = JournalEntry.all_tenants.filter(
        tenant_id=tenant.id, idempotency_key=f"invoice:writeoff:{sent_invoice.id}"
    ).first()
    assert entry is not None
    lines = {
        line.account_id: (line.debit, line.credit)
        for line in JournalLine.all_tenants.filter(tenant_id=tenant.id, entry=entry)
    }
    assert lines[chart_of_accounts["bad_debt_expense"].id][0] == TOTAL
    assert lines[chart_of_accounts["ar_control"].id][1] == TOTAL
    assert_ledger_balanced(tenant.id)


# ---------------------------------------------------------------------------
# Derived lateness
# ---------------------------------------------------------------------------

def test_refresh_overdue_flips_only_unpaid_invoices_past_their_due_date(
    tenant, customer, chart_of_accounts, vat, open_period, owner_user
):
    """``OVERDUE`` is a *presentation* of lateness, not an accounting fact.

    Nothing happens on the due date: no actor acts, the world simply keeps
    turning. So the sweep is derived from ``due_date < as_of AND due > 0``,
    evaluated in the tenant's own time zone — hence ``as_of`` being a
    parameter rather than ``timezone.localdate()`` inline.
    """
    today = date.today()

    late = issue_invoice(
        _make_invoice(
            tenant, customer, chart_of_accounts, vat,
            issue_date=today - timedelta(days=40), due_in_days=10,
        ).id,
        tenant_id=tenant.id, user_id=owner_user.id,
    )
    current = issue_invoice(
        _make_invoice(
            tenant, customer, chart_of_accounts, vat,
            issue_date=today, due_in_days=30,
        ).id,
        tenant_id=tenant.id, user_id=owner_user.id,
    )
    settled = issue_invoice(
        _make_invoice(
            tenant, customer, chart_of_accounts, vat,
            issue_date=today - timedelta(days=40), due_in_days=10,
        ).id,
        tenant_id=tenant.id, user_id=owner_user.id,
    )
    settled = _pay(tenant, customer, settled, chart_of_accounts, owner_user, TOTAL)
    assert settled.status == Invoice.Status.PAID

    result = refresh_overdue_status(tenant_id=tenant.id, as_of=today)

    late.refresh_from_db()
    current.refresh_from_db()
    settled.refresh_from_db()

    assert late.status == Invoice.Status.OVERDUE
    assert current.status == Invoice.Status.SENT, "An invoice not yet due is not late."
    assert settled.status == Invoice.Status.PAID, "A paid invoice is never overdue."
    assert result["became_overdue"] >= 1


def test_refresh_overdue_is_idempotent_and_reversible(
    tenant, customer, chart_of_accounts, vat, open_period, owner_user
):
    """Lateness must be able to go away again — extending a due date, or money
    arriving, un-lates the invoice. A one-way sweep leaves invoices stuck in
    OVERDUE forever, which is the bug that makes people distrust the aging
    report."""
    today = date.today()
    invoice = issue_invoice(
        _make_invoice(
            tenant, customer, chart_of_accounts, vat,
            issue_date=today - timedelta(days=40), due_in_days=10,
        ).id,
        tenant_id=tenant.id, user_id=owner_user.id,
    )

    refresh_overdue_status(tenant_id=tenant.id, as_of=today)
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OVERDUE

    # Running twice must not change anything further.
    second = refresh_overdue_status(tenant_id=tenant.id, as_of=today)
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OVERDUE
    assert second["became_overdue"] == 0

    # Extend the due date: the invoice stops being late.
    Invoice.all_tenants.filter(pk=invoice.pk).update(
        due_date=today + timedelta(days=15)
    )
    third = refresh_overdue_status(tenant_id=tenant.id, as_of=today)
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.SENT
    assert third["no_longer_overdue"] >= 1


def test_a_draft_invoice_is_never_swept_into_overdue(
    tenant, customer, chart_of_accounts, vat, open_period
):
    """Drafts have no due date obligation and no number; they are not part of
    the collection process at all."""
    today = date.today()
    draft = _make_invoice(
        tenant, customer, chart_of_accounts, vat,
        issue_date=today - timedelta(days=90), due_in_days=1,
    )

    refresh_overdue_status(tenant_id=tenant.id, as_of=today)

    draft.refresh_from_db()
    assert draft.status == Invoice.Status.DRAFT


# ---------------------------------------------------------------------------
# Money typing
# ---------------------------------------------------------------------------

def test_invoice_amounts_round_trip_as_decimal(sent_invoice):
    """A float anywhere in the AR chain would break the ``due == total - paid``
    CHECK the first time it failed to be exactly representable."""
    for field in ("subtotal_amount", "tax_amount", "total_amount", "amount_due"):
        assert isinstance(getattr(sent_invoice, field), Decimal)
    assert sent_invoice.total_amount == quantize_currency(TOTAL, TEST_CURRENCY)
    assert sent_invoice.amount_due == sent_invoice.total_amount - sent_invoice.amount_paid
