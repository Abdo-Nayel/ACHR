"""The general-ledger detail reports and the financial-ratios report.

These four reports (general ledger, journal register, party statement,
financial ratios) are the ones a reader *acts on line by line*, so the failure
that matters is not a total that is off — the trial balance catches that — but
a running balance that drifts, a statement that includes lines it should not,
or a ratio that substitutes a denominator instead of admitting it is
undefined. Each test below pins one of those.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.accounting.services.posting import (
    JournalEntryDraft,
    LineDraft,
    post_entry,
)
from apps.reporting.generators.base import ReportContext, get_generator
from tests.conftest import TEST_CURRENCY, make_draft

pytestmark = pytest.mark.django_db

TODAY = date.today()
YEAR_START = date(TODAY.year, 1, 1)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ctx(tenant, **overrides) -> ReportContext:
    overrides.setdefault("date_from", YEAR_START)
    overrides.setdefault("date_to", TODAY)
    return ReportContext(tenant_id=tenant.id, **overrides)


def _run(tenant, report_type, **overrides):
    return get_generator(report_type).run(_ctx(tenant, **overrides))


def _post(tenant, owner_user, debit, credit, amount, when=None):
    draft = make_draft(debit_account=debit, credit_account=credit, amount=amount)
    if when is not None:
        draft.entry_date = when
    return post_entry(draft, tenant_id=tenant.id, user_id=owner_user.id)


def _post_invoice(tenant, owner_user, chart, amount, partner_id, when=None):
    """A receivable entry: debit AR control (with a party), credit revenue.

    The AR control account is ``requires_party``, so a party statement has a
    real control-account movement to follow — the shape a customer statement
    is actually made of.
    """
    draft = JournalEntryDraft(
        journal_code="SAL",
        entry_date=when or TODAY,
        currency=TEST_CURRENCY,
        memo="invoice",
    )
    draft.lines.append(
        LineDraft(
            account_id=chart["ar_control"].id, debit=amount,
            partner_type="customer", partner_id=partner_id, description="Invoice",
        )
    )
    draft.lines.append(
        LineDraft(
            account_id=chart["sales_revenue"].id, credit=amount, description="Revenue",
        )
    )
    return post_entry(draft, tenant_id=tenant.id, user_id=owner_user.id)


def _line(result, label_prefix):
    for section in result.sections:
        for line in section.lines:
            if line.label.startswith(label_prefix):
                return line
    raise AssertionError(f"no line starting {label_prefix!r} in {result.report_type}")


# ---------------------------------------------------------------------------
# general ledger
# ---------------------------------------------------------------------------

def test_general_ledger_carries_a_running_balance(
    tenant, chart_of_accounts, open_period, owner_user
):
    """Two postings to one account accumulate; the closing line is their sum.

    A running balance is a property of the *sequence*, not of each row. The
    bug this guards is restarting the accumulation per row (every line then
    shows its own movement, not the balance) or per page (page two disagrees
    with page one).
    """
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    _post(tenant, owner_user, bank, revenue, Decimal("1000.00"))
    _post(tenant, owner_user, bank, revenue, Decimal("250.00"))

    result = _run(tenant, "general_ledger")

    bank_section = next(s for s in result.sections if s.title.endswith("Bank — current account"))
    opening = bank_section.lines[0]
    closing = bank_section.lines[-1]
    assert opening.meta["kind"] == "opening" and opening.amount == Decimal("0")
    assert closing.meta["kind"] == "closing"
    assert closing.amount == Decimal("1250.00")     # 1000 then +250
    assert bank_section.total == Decimal("1250.00")
    # Whole report still ties out: it is the ledger, after all.
    assert result.totals["difference"] == Decimal("0")


def test_general_ledger_opening_balance_excludes_the_period(
    tenant, chart_of_accounts, open_period, owner_user
):
    """A balance brought forward is the opening line, never a movement row.

    Fold the pre-period activity into the first period line and the account's
    history is overstated and the movement rows lie about what happened inside
    the window.
    """
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    early = open_period.start_date
    _post(tenant, owner_user, bank, revenue, Decimal("400.00"), when=early)
    _post(tenant, owner_user, bank, revenue, Decimal("100.00"), when=early + timedelta(days=2))

    result = get_generator("general_ledger").run(
        ReportContext(tenant_id=tenant.id, date_from=early + timedelta(days=1), date_to=TODAY)
    )

    bank_section = next(s for s in result.sections if s.title.endswith("Bank — current account"))
    assert bank_section.lines[0].amount == Decimal("400.00")     # brought forward
    movements = [ln for ln in bank_section.lines if ln.meta.get("kind") == "movement"]
    assert len(movements) == 1 and movements[0].amount == Decimal("500.00")


def test_general_ledger_account_scope_excludes_other_accounts(
    tenant, chart_of_accounts, open_period, owner_user
):
    """``options.account`` restricts the report to that account's subtree."""
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    _post(tenant, owner_user, bank, revenue, Decimal("500.00"))

    result = _run(tenant, "general_ledger", options={"account": str(bank.id)})

    titles = [s.title for s in result.sections]
    assert any("Bank — current account" in t for t in titles)
    assert not any("revenue" in t.lower() for t in titles)


# ---------------------------------------------------------------------------
# journal register
# ---------------------------------------------------------------------------

def test_journal_register_lists_every_line_and_balances(
    tenant, chart_of_accounts, open_period, owner_user
):
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    _post(tenant, owner_user, bank, revenue, Decimal("300.00"))
    _post(tenant, owner_user, bank, revenue, Decimal("700.00"))

    result = _run(tenant, "journal_register")

    assert result.metadata["line_count"] == 4          # two balanced 2-line entries
    assert result.metadata["entry_count"] == 2
    assert result.totals["total_debit"] == result.totals["total_credit"]
    assert result.totals["difference"] == Decimal("0")


def test_journal_register_is_oldest_first(
    tenant, chart_of_accounts, open_period, owner_user
):
    """The book of original entry reads forwards. A register that lists newest
    first is a list, not a journal, and cannot be followed as a narrative."""
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    early = open_period.start_date
    _post(tenant, owner_user, bank, revenue, Decimal("10.00"), when=early + timedelta(days=5))
    _post(tenant, owner_user, bank, revenue, Decimal("20.00"), when=early)

    result = _run(tenant, "journal_register")
    dates = [ln.meta["date"] for ln in result.sections[0].lines]
    assert dates == sorted(dates)


# ---------------------------------------------------------------------------
# financial ratios
# ---------------------------------------------------------------------------

def test_financial_ratios_compute_margins_from_the_period(
    tenant, chart_of_accounts, open_period, owner_user
):
    """Revenue 1000, cost of sales 400 -> 60% gross and net margin."""
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    cogs = chart_of_accounts["cogs"]
    inventory = chart_of_accounts["inventory_asset"]
    _post(tenant, owner_user, bank, revenue, Decimal("1000.00"))
    _post(tenant, owner_user, cogs, inventory, Decimal("400.00"))

    result = _run(tenant, "financial_ratios")

    assert _line(result, "Gross margin").amount == Decimal("60.00")
    assert _line(result, "Net margin").amount == Decimal("60.00")
    net_sales = _line(result, "Net sales").amount
    assert net_sales == Decimal("1000.00")


def test_financial_ratio_is_na_when_the_denominator_is_zero(
    tenant, chart_of_accounts, open_period, owner_user
):
    """No current liabilities -> the current ratio is undefined, not 0.

    A current ratio of 0.00 reads as "cannot pay its bills"; a company with no
    bills is the opposite of that. Undefined must look undefined.
    """
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    _post(tenant, owner_user, bank, revenue, Decimal("500.00"))

    result = _run(tenant, "financial_ratios")

    current_ratio = _line(result, "Current ratio")
    assert current_ratio.meta.get("na") is True
    assert current_ratio.amount == Decimal("0")     # sentinel, shown as n/a


# ---------------------------------------------------------------------------
# party statement
# ---------------------------------------------------------------------------

def test_party_statement_follows_only_the_control_account(
    tenant, chart_of_accounts, open_period, owner_user
):
    """The statement is the party's receivable, not every line tagged to them.

    ACHR stamps the party onto every line of a sales entry (revenue and tax as
    well as the receivable), so a statement that summed them all would net to
    zero. Restricting to the AR control account is what makes the closing
    balance equal what the customer owes.
    """
    partner_id = uuid.uuid4()
    _post_invoice(tenant, owner_user, chart_of_accounts, Decimal("1000.00"), partner_id)

    result = _run(
        tenant, "party_statement",
        options={"partner_type": "customer", "partner_id": str(partner_id)},
    )

    assert result.totals["closing_balance"] == Decimal("1000.00")
    movements = [ln for ln in result.sections[0].lines if ln.meta.get("kind") == "movement"]
    assert len(movements) == 1                       # the AR line only, not revenue
    assert movements[0].meta["account_code"] == chart_of_accounts["ar_control"].code


def test_party_statement_opening_balance_is_brought_forward(
    tenant, chart_of_accounts, open_period, owner_user
):
    partner_id = uuid.uuid4()
    early = open_period.start_date
    _post_invoice(tenant, owner_user, chart_of_accounts, Decimal("300.00"), partner_id, when=early)
    _post_invoice(
        tenant, owner_user, chart_of_accounts, Decimal("200.00"), partner_id,
        when=early + timedelta(days=2),
    )

    result = get_generator("party_statement").run(
        ReportContext(
            tenant_id=tenant.id, date_from=early + timedelta(days=1), date_to=TODAY,
            options={"partner_type": "customer", "partner_id": str(partner_id)},
        )
    )

    assert result.totals["opening_balance"] == Decimal("300.00")
    assert result.totals["closing_balance"] == Decimal("500.00")


def test_party_statement_requires_a_partner(tenant, chart_of_accounts, open_period):
    """A statement with no party is not a statement — refuse it, don't guess."""
    from apps.reporting.generators.base import ReportError

    with pytest.raises(ReportError):
        _run(tenant, "party_statement")
