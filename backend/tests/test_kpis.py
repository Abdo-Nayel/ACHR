"""The dashboard's ratio block.

Ratios are the numbers people act on without reading the statement underneath,
so the failure mode that matters is not "wrong by a bit" — it is a ratio that
looks plausible and answers a different question. The tests below pin the three
ways that happens:

* mixing a period movement into a position (working capital computed from the
  window's asset movements rather than the balance at ``date_to``);
* substituting a denominator instead of admitting the ratio is undefined;
* classifying every asset as current, which flatters liquidity on any chart
  that has fixed assets.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.accounting.models import Account
from apps.reporting.services.kpis import compute_kpis
from tests.conftest import make_draft

pytestmark = pytest.mark.django_db

TODAY = date.today()
YEAR_START = date(TODAY.year, 1, 1)


def _post(tenant, owner_user, debit, credit, amount, when=None):
    from apps.accounting.services.posting import post_entry  # noqa: PLC0415

    draft = make_draft(debit_account=debit, credit_account=credit, amount=amount)
    if when is not None:
        draft.entry_date = when
    return post_entry(draft, tenant_id=tenant.id, user_id=owner_user.id)


def test_revenue_in_the_window_drives_margin(
    tenant, chart_of_accounts, open_period, owner_user
):
    _post(tenant, owner_user, chart_of_accounts["bank_main"],
          chart_of_accounts["sales_revenue"], Decimal("1000.00"))

    k = compute_kpis(tenant.id, date_from=YEAR_START, date_to=TODAY)

    assert k["components"]["revenue"] == "1000.000000"
    # No expenses posted, so every pound of revenue is profit.
    assert Decimal(k["metrics"]["net_profit_margin"]) == Decimal("1.0000")


def test_margin_is_none_not_zero_when_there_is_no_revenue(
    tenant, chart_of_accounts, open_period, owner_user
):
    """A margin of 0.00 reads as "we broke even". Undefined is not that."""
    k = compute_kpis(tenant.id, date_from=YEAR_START, date_to=TODAY)
    assert k["metrics"]["net_profit_margin"] is None


def test_debt_to_equity_is_none_when_no_equity_is_recorded(
    tenant, chart_of_accounts, open_period, owner_user
):
    """0.00 would read as "no debt", which is the opposite of the truth."""
    k = compute_kpis(tenant.id, date_from=YEAR_START, date_to=TODAY)
    assert k["metrics"]["debt_to_equity"] is None


def test_working_capital_is_a_position_not_a_window_movement(
    tenant, chart_of_accounts, owner_user, open_period
):
    """Cash banked before the window still counts as an asset inside it.

    This is the bug the two time treatments exist to prevent: computing
    working capital from ``[date_from, date_to]`` movements makes a solvent
    company look broke every January.
    """
    early = open_period.start_date
    _post(tenant, owner_user, chart_of_accounts["bank_main"],
          chart_of_accounts["sales_revenue"], Decimal("5000.00"), when=early)

    # A window that starts *after* the entry.
    k = compute_kpis(tenant.id, date_from=early + timedelta(days=1), date_to=TODAY)

    assert Decimal(k["components"]["current_assets"]) == Decimal("5000.00")
    assert Decimal(k["metrics"]["working_capital"]) == Decimal("5000.00")
    # ...but the revenue is outside the window, so it is not in this period's
    # profit. Position and rate must disagree here; that is the point.
    assert Decimal(k["components"]["revenue"]) == Decimal("0")


def test_current_assets_exclude_anything_outside_the_current_group(
    tenant, chart_of_accounts, owner_user, open_period
):
    """A fixed asset must not inflate the quick ratio."""
    fixed = Account.objects.create(
        tenant=tenant, code="1900", name="Motor vehicles",
        type="asset", is_postable=True, currency=tenant.base_currency,
    )
    _post(tenant, owner_user, fixed, chart_of_accounts["sales_revenue"],
          Decimal("8000.00"))

    k = compute_kpis(tenant.id, date_from=YEAR_START, date_to=TODAY)

    assert Decimal(k["components"]["total_assets"]) == Decimal("8000.00")
    # 1900 hangs off no group, so it is not current.
    assert Decimal(k["components"]["current_assets"]) == Decimal("0")


def test_inventory_is_stripped_out_of_the_quick_ratio(
    tenant, chart_of_accounts, owner_user, open_period
):
    """"Quick" means assets convertible to cash now; stock is not one."""
    _post(tenant, owner_user, chart_of_accounts["inventory_asset"],
          chart_of_accounts["ap_control"], Decimal("400.00"))
    _post(tenant, owner_user, chart_of_accounts["bank_main"],
          chart_of_accounts["ap_control"], Decimal("100.00"))

    k = compute_kpis(tenant.id, date_from=YEAR_START, date_to=TODAY)

    # Current assets 500 (400 stock + 100 cash), current liabilities 500.
    # Including stock would give 1.00; excluding it gives 0.20.
    assert Decimal(k["components"]["inventory"]) == Decimal("400.00")
    assert Decimal(k["metrics"]["quick_ratio"]) == Decimal("0.2000")


def test_only_posted_entries_count(
    tenant, chart_of_accounts, owner_user, open_period
):
    """A draft is a proposal. If drafts counted, anyone could move a covenant
    ratio by typing."""
    from apps.accounting.models import JournalEntry  # noqa: PLC0415

    entry = _post(tenant, owner_user, chart_of_accounts["bank_main"],
                  chart_of_accounts["sales_revenue"], Decimal("700.00"))
    before = compute_kpis(tenant.id, date_from=YEAR_START, date_to=TODAY)
    assert Decimal(before["components"]["revenue"]) == Decimal("700.00")

    JournalEntry.all_tenants.filter(pk=entry.pk).update(
        status=JournalEntry.Status.DRAFT
    )
    after = compute_kpis(tenant.id, date_from=YEAR_START, date_to=TODAY)
    assert Decimal(after["components"]["revenue"]) == Decimal("0")


def test_ebitda_is_flagged_inexact_without_add_back_accounts(
    tenant, chart_of_accounts, owner_user, open_period
):
    """Reporting net profit as EBITDA without saying so is the lie of omission
    this flag exists to prevent."""
    _post(tenant, owner_user, chart_of_accounts["bank_main"],
          chart_of_accounts["sales_revenue"], Decimal("300.00"))

    k = compute_kpis(tenant.id, date_from=YEAR_START, date_to=TODAY)

    assert k["flags"]["ebitda_is_exact"] is False
    assert k["metrics"]["ebitda"] == k["components"]["net_profit"]
    assert any("EBITDA" in a for a in k["assumptions"])


def test_cash_burn_is_positive_when_cash_leaves(
    tenant, chart_of_accounts, owner_user, open_period
):
    """Sign convention: burn is money *going out*, so spending is positive."""
    _post(tenant, owner_user, chart_of_accounts["bank_main"],
          chart_of_accounts["sales_revenue"], Decimal("1200.00"),
          when=open_period.start_date)
    _post(tenant, owner_user, chart_of_accounts["office_expense"],
          chart_of_accounts["bank_main"], Decimal("300.00"))

    k = compute_kpis(tenant.id, date_from=YEAR_START, date_to=TODAY)

    # Cash rose overall across the window, so the burn is negative.
    assert Decimal(k["components"]["cash_change"]) == Decimal("900.00")
    assert Decimal(k["metrics"]["cash_burn_rate"]) < 0


def test_kpis_do_not_leak_across_tenants(
    tenant, other_tenant, chart_of_accounts, owner_user, open_period
):
    _post(tenant, owner_user, chart_of_accounts["bank_main"],
          chart_of_accounts["sales_revenue"], Decimal("2500.00"))

    theirs = compute_kpis(other_tenant.id, date_from=YEAR_START, date_to=TODAY)

    assert Decimal(theirs["components"]["revenue"]) == Decimal("0")
    assert Decimal(theirs["components"]["total_assets"]) == Decimal("0")
