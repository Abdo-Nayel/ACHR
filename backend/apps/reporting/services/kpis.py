"""Headline financial ratios, computed from the ledger rather than from inputs.

Every figure here is derived from ``JournalLine.base_debit`` / ``base_credit``
— the tenant's reporting currency, frozen at posting time — for the same
reason the statement generators use them: a ratio recomputed at today's FX
rate would restate a filed quarter every time the rate table moved.

Two different time treatments, deliberately
-------------------------------------------
Balance-sheet accounts (asset / liability / equity) are cumulative from
inception to ``date_to``. Working capital is a *position*: what the company
owns and owes at a moment. Income and expense accounts are the movement
*within* ``[date_from, date_to]`` — margin is a *rate*, and asking "what was
the margin as at 31 March" is not a question.

Mixing the two is the classic ratio bug: a quick ratio computed from
period-only asset movements looks catastrophic in January and fine in
December, for a company whose liquidity never changed.

Current vs non-current
----------------------
The chart marks these with the ``grp_current_assets`` and
``grp_current_liabilities`` group accounts, so classification walks the parent
chain rather than guessing from code ranges (which differ per national chart —
the whole reason ``system_key`` exists). A chart with no such groups falls back
to treating all assets and liabilities as current, and says so in
``assumptions`` rather than quietly returning a ratio that means something
else.

Ratios that cannot be computed return ``None``, never 0 and never a fabricated
denominator. A debt-to-equity of ``0.00`` on a company with no equity recorded
reads as "no debt", which is the opposite of the truth.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.db.models import Sum

from apps.accounting.models import Account, AccountType, JournalEntry, JournalLine
from apps.core.fields import ZERO

#: Signed so that a positive number always means "more of what this account
#: is for". Assets and expenses grow on the debit side; the rest on credit.
_DEBIT_POSITIVE = {AccountType.ASSET, AccountType.EXPENSE}

#: Group accounts that mark the current section of the balance sheet.
CURRENT_ASSET_GROUP = "grp_current_assets"
CURRENT_LIABILITY_GROUP = "grp_current_liabilities"

#: EBITDA add-backs, by ``system_key``. Absent from the default chart, which
#: is why EBITDA is reported alongside ``ebitda_is_exact``: with none of these
#: present it equals operating profit, which is the same number only for a
#: company with no debt and no fixed assets.
DEPRECIATION_KEYS = ("depreciation_expense", "amortisation_expense",
                     "amortization_expense")
INTEREST_KEYS = ("interest_expense", "finance_costs")
TAX_KEYS = ("income_tax_expense", "corporate_tax_expense")

#: Accounts whose balance is "cash" for burn-rate purposes.
CASH_KEYS = ("bank_main", "cash_on_hand")


@dataclass(slots=True)
class _Chart:
    """Account metadata, loaded once per computation."""

    type_of: dict[uuid.UUID, str] = field(default_factory=dict)
    parent_of: dict[uuid.UUID, Optional[uuid.UUID]] = field(default_factory=dict)
    key_of: dict[uuid.UUID, str] = field(default_factory=dict)
    name_of: dict[uuid.UUID, str] = field(default_factory=dict)
    id_by_key: dict[str, uuid.UUID] = field(default_factory=dict)
    reconcilable: set[uuid.UUID] = field(default_factory=set)

    def resolve_group(self, group_key: str, *via: str) -> Optional[uuid.UUID]:
        """The group account for ``group_key``, by key or by inference.

        ``seed_chart_of_accounts`` now stamps ``grp_current_assets`` and
        ``grp_current_liabilities`` onto the structural nodes, but charts
        seeded before that carry an empty ``system_key`` on every roll-up —
        ``ref`` is not written to the database. Rather than tell those tenants
        their working capital is an upper bound forever, infer the group from
        the parent of an account that is unambiguously inside it: accounts
        receivable is a current asset in every chart of accounts ever drawn.
        """
        direct = self.id_by_key.get(group_key)
        if direct is not None:
            return direct
        for key in via:
            child = self.id_by_key.get(key)
            if child is not None:
                parent = self.parent_of.get(child)
                if parent is not None:
                    return parent
        return None

    def under(self, account_id: uuid.UUID, target: Optional[uuid.UUID]) -> bool:
        """Is ``account_id`` at or below ``target``?"""
        if target is None:
            return False
        seen: set[uuid.UUID] = set()
        node: Optional[uuid.UUID] = account_id
        # Guard against a cycle: a self-parenting row would otherwise spin
        # here forever and take the dashboard request with it.
        while node is not None and node not in seen:
            if node == target:
                return True
            seen.add(node)
            node = self.parent_of.get(node)
        return False


def _load_chart(tenant_id: uuid.UUID) -> _Chart:
    chart = _Chart()
    rows = Account.all_tenants.filter(tenant_id=tenant_id).values(
        "id", "type", "parent_id", "system_key", "name", "is_reconcilable"
    )
    for row in rows:
        chart.type_of[row["id"]] = row["type"]
        chart.parent_of[row["id"]] = row["parent_id"]
        chart.key_of[row["id"]] = row["system_key"]
        chart.name_of[row["id"]] = row["name"]
        if row["system_key"]:
            chart.id_by_key[row["system_key"]] = row["id"]
        if row["is_reconcilable"]:
            chart.reconcilable.add(row["id"])
    return chart


def _totals(tenant_id: uuid.UUID, *, upto: date,
            since: Optional[date] = None) -> dict[uuid.UUID, Decimal]:
    """Signed balance per account, in the tenant's base currency.

    ``since=None`` means from inception (a position). Otherwise the movement
    within the window (a rate). Only POSTED entries count — a draft is a
    proposal, and including it would let anyone move a covenant ratio by
    typing.
    """
    qs = JournalLine.all_tenants.filter(
        tenant_id=tenant_id,
        entry__status=JournalEntry.Status.POSTED,
        entry__entry_date__lte=upto,
    )
    if since is not None:
        qs = qs.filter(entry__entry_date__gte=since)

    rows = qs.values("account_id").annotate(
        debit=Sum("base_debit"), credit=Sum("base_credit")
    )
    return {
        row["account_id"]: (row["debit"] or ZERO) - (row["credit"] or ZERO)
        for row in rows
    }


def _signed(chart: _Chart, account_id: uuid.UUID, raw: Decimal) -> Decimal:
    """``raw`` is debit-minus-credit; flip it for credit-natured accounts."""
    account_type = chart.type_of.get(account_id)
    if account_type in _DEBIT_POSITIVE:
        return raw
    return -raw


def _ratio(numerator: Decimal, denominator: Decimal) -> Optional[Decimal]:
    """``None`` when undefined. Never 0, never a substituted denominator."""
    if denominator == ZERO:
        return None
    try:
        return (numerator / denominator).quantize(Decimal("0.0001"))
    except (InvalidOperation, ZeroDivisionError):  # pragma: no cover - defensive
        return None


def _months_between(start: date, end: date) -> Decimal:
    """Fractional months in the window, floored at one day's worth.

    Used only as a burn-rate denominator. A window shorter than a month must
    not be annualised into a burn figure ten times the real one, so the
    caller is told the window length and can decide.
    """
    days = Decimal((end - start).days + 1)
    if days <= ZERO:
        return Decimal("1")
    return days / Decimal("30.4375")  # mean Gregorian month


def compute_kpis(
    tenant_id: uuid.UUID,
    *,
    date_from: date,
    date_to: date,
) -> dict:
    """The dashboard's headline block. Amounts are strings; ratios are strings
    or ``None``.

    Strings for the same reason every other amount in this API is a string:
    JSON has one numeric type and it is an IEEE-754 double, which cannot hold
    ``numeric(19, 6)``.
    """
    chart = _load_chart(tenant_id)
    assumptions: list[str] = []

    position = _totals(tenant_id, upto=date_to)
    movement = _totals(tenant_id, upto=date_to, since=date_from)

    current_asset_group = chart.resolve_group(
        CURRENT_ASSET_GROUP, "ar_control", "bank_main", "cash_on_hand"
    )
    current_liability_group = chart.resolve_group(
        CURRENT_LIABILITY_GROUP, "ap_control", "output_vat"
    )
    has_current_groups = (
        current_asset_group is not None and current_liability_group is not None
    )
    if not has_current_groups:
        assumptions.append(
            "This chart has no current-asset/current-liability groups, so "
            "every asset and liability is treated as current. Working capital "
            "and the quick ratio are upper bounds."
        )

    current_assets = ZERO
    current_liabilities = ZERO
    total_assets = ZERO
    total_liabilities = ZERO
    total_equity = ZERO
    inventory = ZERO
    cash_close = ZERO

    inventory_id = chart.id_by_key.get("inventory_asset")
    cash_ids = {
        chart.id_by_key[k] for k in CASH_KEYS if k in chart.id_by_key
    } | chart.reconcilable

    for account_id, raw in position.items():
        account_type = chart.type_of.get(account_id)
        value = _signed(chart, account_id, raw)

        if account_type == AccountType.ASSET:
            total_assets += value
            if not has_current_groups or chart.under(account_id, current_asset_group):
                current_assets += value
            if account_id == inventory_id:
                inventory += value
            if account_id in cash_ids:
                cash_close += value
        elif account_type == AccountType.LIABILITY:
            total_liabilities += value
            if not has_current_groups or chart.under(account_id, current_liability_group):
                current_liabilities += value
        elif account_type == AccountType.EQUITY:
            total_equity += value

    revenue = ZERO
    expenses = ZERO
    depreciation = ZERO
    interest = ZERO
    tax = ZERO

    for account_id, raw in movement.items():
        account_type = chart.type_of.get(account_id)
        value = _signed(chart, account_id, raw)
        key = chart.key_of.get(account_id, "")
        if account_type == AccountType.INCOME:
            revenue += value
        elif account_type == AccountType.EXPENSE:
            expenses += value
            if key in DEPRECIATION_KEYS:
                depreciation += value
            elif key in INTEREST_KEYS:
                interest += value
            elif key in TAX_KEYS:
                tax += value

    net_profit = revenue - expenses

    # Current-year earnings are equity the moment they are earned, but they
    # only reach retained earnings at year-end close. Omitting them makes
    # debt-to-equity spike for any profitable company mid-year.
    equity_incl_earnings = total_equity + net_profit

    ebitda = net_profit + interest + tax + depreciation
    ebitda_is_exact = bool(depreciation or interest or tax)
    if not ebitda_is_exact:
        assumptions.append(
            "No depreciation, interest or tax accounts carry a balance in this "
            "window, so EBITDA equals net profit. It is a true EBITDA only for "
            "a company with no debt and no depreciating assets."
        )

    cash_open = ZERO
    opening = _totals(tenant_id, upto=date_from)
    for account_id, raw in opening.items():
        if account_id in cash_ids and chart.type_of.get(account_id) == AccountType.ASSET:
            cash_open += _signed(chart, account_id, raw)

    months = _months_between(date_from, date_to)
    cash_change = cash_close - cash_open
    # Positive burn = cash leaving. A company that grew its cash has a
    # negative burn, which is more honest than clamping it to zero.
    burn_rate = (-cash_change / months).quantize(Decimal("0.01"))

    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "window_months": str(months.quantize(Decimal("0.01"))),
        "metrics": {
            "working_capital": str(current_assets - current_liabilities),
            "quick_ratio": _s(_ratio(current_assets - inventory, current_liabilities)),
            "debt_to_equity": _s(_ratio(total_liabilities, equity_incl_earnings)),
            "net_profit_margin": _s(_ratio(net_profit, revenue)),
            "ebitda": str(ebitda),
            "cash_burn_rate": str(burn_rate),
        },
        "components": {
            "current_assets": str(current_assets),
            "current_liabilities": str(current_liabilities),
            "inventory": str(inventory),
            "total_assets": str(total_assets),
            "total_liabilities": str(total_liabilities),
            "total_equity": str(equity_incl_earnings),
            "revenue": str(revenue),
            "expenses": str(expenses),
            "net_profit": str(net_profit),
            "depreciation": str(depreciation),
            "interest": str(interest),
            "tax": str(tax),
            "cash_opening": str(cash_open),
            "cash_closing": str(cash_close),
            "cash_change": str(cash_change),
        },
        "flags": {"ebitda_is_exact": ebitda_is_exact},
        "assumptions": assumptions,
    }


def _s(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


__all__ = ["compute_kpis"]
