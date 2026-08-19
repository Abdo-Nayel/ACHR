"""
The statutory financial statements: trial balance, P&L, balance sheet,
cash flow and tax summary.

Every generator in this file is **pure aggregation over ``JournalLine``**.
Specifically:

* Amounts come from ``base_debit`` / ``base_credit`` — the amounts already
  converted to the tenant's base currency and *stored* at posting time. They
  are never recomputed as ``debit * exchange_rate`` at report time. A rate
  table that is later corrected (they are, routinely) must not silently
  restate a P&L that has already been filed; storing the converted amount is
  what makes a historical report stable, and re-deriving it throws that away.
* Nothing here reads ``Account.cached_balance``. That column is a
  denormalisation maintained by the posting service for dashboards; the
  statements aggregate the lines, which is what makes them the thing the
  cached balance is checked *against*.
* Every consistency identity is asserted, and a failure names the difference.
  A trial balance that does not balance, or a balance sheet that does not,
  is not a cosmetic problem to be rounded away — it means the ledger has been
  written to outside the application, and every figure on the page is suspect.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional

from django.db.models import Sum

from apps.accounting.models import (
    NORMAL_BALANCE,
    Account,
    AccountType,
    FiscalYear,
    TaxRate,
)
from apps.core.fields import ZERO
from apps.reporting.generators.base import (
    ReportContext,
    ReportError,
    ReportGenerator,
    ReportImbalance,
    ReportLine,
    ReportResult,
    ReportSection,
    ledger_query,
    net_balance,
    register_report,
    sum_decimals,
)
from apps.reporting.models import AccountGrouping, ReportLineMapping, ReportType

__all__ = [
    "TrialBalanceGenerator",
    "ProfitLossGenerator",
    "BalanceSheetGenerator",
    "CashFlowGenerator",
    "TaxSummaryGenerator",
]


#: ``Account.system_key`` roles the cash-flow statement needs. Looked up by
#: system key rather than by code, exactly as the posting services do: the
#: code of "Accounts receivable" differs between every national standard chart
#: while its role in the indirect method does not.
AR_CONTROL_KEYS: frozenset[str] = frozenset({"ar_control", "accounts_receivable"})
AP_CONTROL_KEYS: frozenset[str] = frozenset({"ap_control", "accounts_payable"})
INVENTORY_CONTROL_KEYS: frozenset[str] = frozenset(
    {"inventory_control", "inventory_asset"}
)
ACCRUAL_KEYS: frozenset[str] = frozenset(
    {
        "accrued_expenses",
        "prepaid_expenses",
        "payroll_salaries_payable",
        "payroll_income_tax_payable",
        "payroll_social_insurance_payable",
        "payroll_other_deductions_payable",
    }
)
#: Non-cash charges added back to net profit. They reduced profit without any
#: money leaving; not adding them back understates operating cash by exactly
#: the depreciation charge, every single period.
NON_CASH_KEYS: frozenset[str] = frozenset(
    {
        "depreciation_expense",
        "accumulated_depreciation",
        "amortisation_expense",
        "provision_expense",
        "bad_debt_expense",
        "impairment_expense",
    }
)
FINANCING_KEYS: frozenset[str] = frozenset(
    {"share_capital", "owner_equity", "loans_payable", "dividends_payable",
     "owner_drawings", "retained_earnings"}
)


# ---------------------------------------------------------------------------
# Shared account metadata
# ---------------------------------------------------------------------------

class _AccountBook:
    """The tenant's chart of accounts, loaded once per report.

    Reports need an account's code, name, type and system key on every line.
    Fetching them through ``select_related`` on a million-row line queryset
    re-transmits the same few hundred accounts for every group; fetching them
    once into a dict is one small query and turns the per-line lookup into a
    hash hit. It also means the *presentation* layer can bucket accounts in
    Python instead of issuing one aggregate query per presentation line —
    which is the difference between a 20-line P&L costing one scan of the
    journal and costing twenty.
    """

    __slots__ = ("by_id", "by_system_key")

    def __init__(self, tenant_id: uuid.UUID) -> None:
        # Index used: uq_account_code (tenant, code) satisfies the tenant
        # predicate and returns rows already ordered by code, which is the
        # order every statement prints in.
        rows = (
            Account.all_tenants.filter(tenant_id=tenant_id)
            .values("id", "code", "name", "type", "system_key", "is_reconcilable")
            .order_by("code")
        )
        self.by_id: dict[uuid.UUID, dict[str, Any]] = {r["id"]: r for r in rows}
        self.by_system_key: dict[str, dict[str, Any]] = {
            r["system_key"]: r for r in rows if r["system_key"]
        }

    def ids_with_system_keys(self, keys: Iterable[str]) -> set[uuid.UUID]:
        return {
            row["id"]
            for key in keys
            if (row := self.by_system_key.get(key)) is not None
        }

    def ids_of_type(self, *types: str) -> set[uuid.UUID]:
        wanted = set(types)
        return {r["id"] for r in self.by_id.values() if r["type"] in wanted}

    def cash_account_ids(self) -> set[uuid.UUID]:
        """Bank and cash accounts, identified by ``is_reconcilable``.

        That flag is the one already used by the banking module to decide what
        can be reconciled against a statement, so cash-flow and reconciliation
        cannot disagree about what counts as cash — which they would if this
        report kept its own list of code prefixes.
        """
        return {r["id"] for r in self.by_id.values() if r["is_reconcilable"]}

    def meta(self, account_id: uuid.UUID) -> dict[str, Any]:
        return self.by_id.get(
            account_id,
            {"code": "", "name": "(unknown account)", "type": "", "system_key": "",
             "is_reconcilable": False},
        )


def _aggregate_by_account(qs) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    """``{account_id: (debit_total, credit_total)}`` for a line queryset.

    One GROUP BY in the database rather than a Python loop over lines: the
    journal is the largest table in the system and pulling it into the
    application to add it up is how a report becomes a 40-second query.

    Index used: ``ix_line_account`` (tenant, account) provides the grouping
    key, while ``ix_entry_status`` (tenant, status, entry_date) restricts the
    entry side before the join.
    """
    rows = qs.values("account_id").annotate(
        debit=Sum("base_debit"), credit=Sum("base_credit")
    )
    return {
        r["account_id"]: (r["debit"] or ZERO, r["credit"] or ZERO) for r in rows
    }


def _load_grouping(
    context: ReportContext, statement: str
) -> Optional[AccountGrouping]:
    """Resolve the layout to apply: explicitly requested, else the default.

    Returning None is legitimate and means "fall back to grouping by account
    type". The fallback is correct but unlabelled, which is exactly why the
    result carries a warning rather than pretending a configured layout was
    used.
    """
    code = (context.options or {}).get("grouping_code")
    qs = AccountGrouping.all_tenants.filter(
        tenant_id=context.tenant_id, statement=statement, is_active=True
    )
    if code:
        grouping = qs.filter(code=code).first()
        if grouping is None:
            raise ReportError(
                f"No active account grouping '{code}' for statement "
                f"'{statement}'. Refusing to substitute a different layout: a "
                f"statement laid out differently from the one that was asked "
                f"for is not the statement that was asked for."
            )
        return grouping
    return qs.filter(is_default=True).first()


def _grouping_lines(grouping: AccountGrouping) -> list[ReportLineMapping]:
    # Index used: ix_report_mapping_group (tenant, grouping, sequence) returns
    # the layout already in print order, so no sort is needed.
    return list(
        ReportLineMapping.all_tenants.filter(
            tenant_id=grouping.tenant_id, grouping_id=grouping.id
        ).order_by("sequence")
    )


# ---------------------------------------------------------------------------
# Trial balance
# ---------------------------------------------------------------------------

@register_report(ReportType.TRIAL_BALANCE)
class TrialBalanceGenerator(ReportGenerator):
    """Every account with its debit and credit totals for the period.

    The trial balance is not really a statement for readers; it is the *proof*
    that the other statements can be trusted. Double-entry guarantees that
    total debits equal total credits, so if they do not, the P&L and balance
    sheet built from the same rows are both wrong and there is no point
    presenting either. That is why this generator raises instead of printing a
    "difference" row: a trial balance that shows an imbalance and carries on
    invites someone to plug the gap, which destroys the evidence of how it got
    there.

    An imbalance here cannot be produced by this application — the DB check
    ``ck_entry_balanced`` and the posting service both forbid it. It therefore
    means the ledger was written outside the application: a restored backup, a
    data migration, a manual SQL fix. That is worth stopping the report for.
    """

    title = "Trial balance"
    is_as_of = False

    def generate(self, context: ReportContext) -> ReportResult:
        book = _AccountBook(context.tenant_id)
        totals = _aggregate_by_account(ledger_query(context))

        section = ReportSection(key="accounts", title="Trial balance", sequence=0)
        grand_debit = ZERO
        grand_credit = ZERO

        # Iterate the chart, not the aggregate, so accounts print in code order
        # and an account with movement on only one side still shows both
        # columns rather than being silently omitted.
        for account_id, meta in sorted(
            book.by_id.items(), key=lambda kv: kv[1]["code"]
        ):
            debit, credit = totals.get(account_id, (ZERO, ZERO))
            if debit == ZERO and credit == ZERO:
                continue  # No movement: printing it is noise, not information.
            grand_debit += debit
            grand_credit += credit
            section.add(
                ReportLine(
                    label=f"{meta['code']} — {meta['name']}",
                    account_id=account_id,
                    account_code=meta["code"],
                    account_type=meta["type"],
                    debit=debit,
                    credit=credit,
                    amount=net_balance(
                        debit, credit, NORMAL_BALANCE.get(meta["type"], "debit")
                    ),
                )
            )

        difference = grand_debit - grand_credit
        if difference != ZERO:
            raise ReportImbalance(
                f"TRIAL BALANCE DOES NOT BALANCE for tenant {context.tenant_id} "
                f"over {context.date_from}..{context.date_to}: "
                f"total debits {grand_debit} vs total credits {grand_credit}, "
                f"difference {difference} "
                f"({'debits' if difference > ZERO else 'credits'} exceed by "
                f"{abs(difference)}). Every statement derived from these rows "
                f"is unreliable. The application cannot create this state "
                f"(ck_entry_balanced); investigate out-of-band writes — a "
                f"restore, a data migration or a manual SQL fix — before "
                f"trusting any report for this period."
            )

        section.total = grand_debit
        result = ReportResult(
            report_type=self.report_type,
            sections=[section],
            totals={
                "total_debit": grand_debit,
                "total_credit": grand_credit,
                "difference": difference,
            },
            metadata={"account_count": len(section.lines)},
            currency=context.currency,
        )
        return result


# ---------------------------------------------------------------------------
# Profit & loss
# ---------------------------------------------------------------------------

@register_report(ReportType.PROFIT_LOSS)
class ProfitLossGenerator(ReportGenerator):
    """Income and expenses for a period, laid out by ``ReportLineMapping``.

    Income and expense accounts are *flow* accounts: their balance means
    "movement during this period", which is why this report is period-bounded
    and the balance sheet is not. Presenting a P&L cumulatively is the classic
    way to report a year's revenue as a month's.

    Subtotals (gross profit, operating profit, net profit) come from the
    layout, not from hard-coded account ranges — see
    :class:`apps.reporting.models.AccountGrouping` for why a country-specific
    ``if code.startswith("4")`` is a fork waiting to happen.

    The comparison column is produced by re-running *this same generator* over
    a context bound to the comparison period. Computing it with a separate,
    simpler query is how a comparison column ends up including drafts, or
    missing the department filter that the primary column applied — and the
    resulting variance is then attributed to the business rather than to the
    bug.
    """

    title = "Profit & loss"
    is_as_of = False

    def generate(self, context: ReportContext) -> ReportResult:
        book = _AccountBook(context.tenant_id)
        current = self._account_amounts(context, book)

        comparison_context = context.for_comparison()
        comparison = (
            self._account_amounts(comparison_context, book)
            if comparison_context is not None
            else {}
        )

        grouping = _load_grouping(context, "profit_loss")
        result = ReportResult(
            report_type=self.report_type, currency=context.currency
        )

        if grouping is None:
            sections = self._fallback_sections(book, current, comparison)
            result.warn(
                "No account grouping is configured for the profit & loss "
                "statement, so accounts were grouped by type. The figures are "
                "correct but unlabelled: gross profit and operating profit "
                "cannot be derived without knowing which expenses are cost of "
                "sales. Configure an AccountGrouping before filing this."
            )
        else:
            sections = self._mapped_sections(grouping, book, current, comparison)
            result.metadata["grouping_code"] = grouping.code

        result.sections = sections

        total_income = sum_decimals(
            amount
            for account_id, amount in current.items()
            if book.meta(account_id)["type"] == AccountType.INCOME
        )
        total_expense = sum_decimals(
            amount
            for account_id, amount in current.items()
            if book.meta(account_id)["type"] == AccountType.EXPENSE
        )
        net_profit = total_income - total_expense
        result.totals = {
            "total_income": total_income,
            "total_expense": total_expense,
            "net_profit": net_profit,
        }

        if comparison_context is not None:
            prior_income = sum_decimals(
                amount
                for account_id, amount in comparison.items()
                if book.meta(account_id)["type"] == AccountType.INCOME
            )
            prior_expense = sum_decimals(
                amount
                for account_id, amount in comparison.items()
                if book.meta(account_id)["type"] == AccountType.EXPENSE
            )
            result.totals["comparison_total_income"] = prior_income
            result.totals["comparison_total_expense"] = prior_expense
            result.totals["comparison_net_profit"] = prior_income - prior_expense
            result.metadata["comparison_period"] = [
                comparison_context.date_from.isoformat(),
                comparison_context.date_to.isoformat(),
            ]

        return result

    # -- data ---------------------------------------------------------------

    def _account_amounts(
        self, context: ReportContext, book: _AccountBook
    ) -> dict[uuid.UUID, Decimal]:
        """Signed period movement per income/expense account, positive on its
        normal side (revenue positive, expense positive).

        Index used: ``ix_entry_status`` (tenant, status, entry_date) restricts
        the journal to posted entries inside the period — the selective
        predicate — before ``ix_line_account`` (tenant, account) provides the
        grouping key. The account-type restriction happens in Python against
        the pre-loaded chart rather than as a join to ``accounting_account``,
        which keeps the aggregate a single index-driven pass over the lines.
        """
        pl_account_ids = book.ids_of_type(AccountType.INCOME, AccountType.EXPENSE)
        if not pl_account_ids:
            return {}
        totals = _aggregate_by_account(
            ledger_query(context).filter(account_id__in=pl_account_ids)
        )
        return {
            account_id: net_balance(
                debit, credit,
                NORMAL_BALANCE.get(book.meta(account_id)["type"], "debit"),
            )
            for account_id, (debit, credit) in totals.items()
        }

    # -- layout -------------------------------------------------------------

    def _mapped_sections(
        self,
        grouping: AccountGrouping,
        book: _AccountBook,
        current: dict[uuid.UUID, Decimal],
        comparison: dict[uuid.UUID, Decimal],
    ) -> list[ReportSection]:
        mappings = _grouping_lines(grouping)
        section = ReportSection(key="profit_loss", title=grouping.name, sequence=0)

        running = ZERO
        running_prior = ZERO
        #: Accumulated per subtotal start point, so "gross profit" can begin
        #: after the header lines while "net profit" runs from the top.
        by_sequence: dict[int, tuple[Decimal, Decimal]] = {}

        for mapping in mappings:
            if mapping.is_subtotal:
                start = mapping.subtotal_from_sequence
                base, base_prior = (
                    by_sequence.get(start, (ZERO, ZERO)) if start is not None
                    else (ZERO, ZERO)
                )
                amount = running - base
                prior = running_prior - base_prior
                section.add(
                    ReportLine(
                        label=mapping.label,
                        amount=amount,
                        comparison_amount=prior if comparison else None,
                        level=mapping.level,
                        is_subtotal=True,
                        is_bold=True,
                    )
                )
                by_sequence[mapping.sequence] = (running, running_prior)
                continue

            sign = Decimal(mapping.sign)
            amount = ZERO
            prior = ZERO
            children: list[ReportLine] = []
            for account_id, meta in book.by_id.items():
                if not mapping.matches_account(
                    meta["code"], meta["type"], meta["system_key"], account_id
                ):
                    continue
                account_amount = current.get(account_id, ZERO) * sign
                account_prior = comparison.get(account_id, ZERO) * sign
                if account_amount == ZERO and account_prior == ZERO:
                    continue
                amount += account_amount
                prior += account_prior
                children.append(
                    ReportLine(
                        label=f"{meta['code']} — {meta['name']}",
                        amount=account_amount,
                        comparison_amount=account_prior if comparison else None,
                        account_id=account_id,
                        account_code=meta["code"],
                        account_type=meta["type"],
                        level=mapping.level + 1,
                    )
                )

            if mapping.hide_if_zero and amount == ZERO and prior == ZERO:
                continue

            running += amount
            running_prior += prior
            by_sequence[mapping.sequence] = (running, running_prior)
            section.add(
                ReportLine(
                    label=mapping.label,
                    amount=amount,
                    comparison_amount=prior if comparison else None,
                    level=mapping.level,
                    is_bold=mapping.is_bold,
                    children=sorted(children, key=lambda line: line.account_code),
                )
            )

        section.total = running
        section.comparison_total = running_prior if comparison else None
        return [section]

    def _fallback_sections(
        self,
        book: _AccountBook,
        current: dict[uuid.UUID, Decimal],
        comparison: dict[uuid.UUID, Decimal],
    ) -> list[ReportSection]:
        """Group by account type when no layout is configured.

        Deliberately produces no gross/operating subtotals: without a mapping
        there is no way to know which expenses are cost of sales, and a "gross
        profit" invented by assuming all expenses are cost of sales would be
        wrong in a way that looks authoritative.
        """
        sections: list[ReportSection] = []
        for sequence, (account_type, title) in enumerate(
            ((AccountType.INCOME, "Income"), (AccountType.EXPENSE, "Expenses"))
        ):
            section = ReportSection(key=account_type, title=title, sequence=sequence)
            for account_id, meta in sorted(
                book.by_id.items(), key=lambda kv: kv[1]["code"]
            ):
                if meta["type"] != account_type:
                    continue
                amount = current.get(account_id, ZERO)
                prior = comparison.get(account_id, ZERO)
                if amount == ZERO and prior == ZERO:
                    continue
                section.add(
                    ReportLine(
                        label=f"{meta['code']} — {meta['name']}",
                        amount=amount,
                        comparison_amount=prior if comparison else None,
                        account_id=account_id,
                        account_code=meta["code"],
                        account_type=meta["type"],
                    )
                )
            section.recompute_total()
            sections.append(section)
        return sections


# ---------------------------------------------------------------------------
# Balance sheet
# ---------------------------------------------------------------------------

@register_report(ReportType.BALANCE_SHEET)
class BalanceSheetGenerator(ReportGenerator):
    """Assets, liabilities and equity at an instant, including current-year
    earnings.

    Why current-year earnings are computed and not read from an account
    -------------------------------------------------------------------
    Income and expense accounts are *flow* accounts. Their balances are rolled
    into equity — into Retained Earnings — by a single closing journal entry
    posted at year-end (``JournalEntry.Source.CLOSING``, see
    ``FiscalYear.status``). That entry does not exist yet on 31 March.

    So on any date before the year is closed, the ledger contains profit that
    is sitting in income and expense accounts and has *not* reached equity.
    A balance sheet that reports only the equity accounts is therefore short by
    exactly the year-to-date profit, and **does not balance**. The usual
    reaction to a balance sheet that is out by the profit figure is to assume
    the ledger is broken; it is not, the report is.

    This generator computes ``current_year_earnings`` as (income − expenses)
    from the start of the fiscal year containing ``as_of`` up to ``as_of``, and
    presents it as an equity line. That figure is not stored anywhere, and must
    not be: storing it would mean maintaining it on every posting, and it would
    then be a second source of truth that can disagree with the journal. It is
    cheap to derive and correct by construction.

    Prior years that were never closed produce a *different* shortfall, and the
    assertion below names it rather than absorbing it, because "the 2024 year
    was never closed" is an action for an accountant, not a rounding.
    """

    title = "Balance sheet"
    is_as_of = True

    def generate(self, context: ReportContext) -> ReportResult:
        as_of = context.effective_as_of
        book = _AccountBook(context.tenant_id)

        # Cumulative from the beginning of the ledger: a balance is a stock,
        # not a flow. `ignore_period=True` is what stops a caller's date_from
        # from silently turning this into a period-movement report that still
        # balances and means nothing.
        #
        # Index used: ix_entry_status (tenant, status, entry_date) answers
        # "posted, on or before as_of" as a range scan; ix_line_account
        # (tenant, account) supplies the grouping key.
        cumulative = _aggregate_by_account(
            ledger_query(context, date_to=as_of, ignore_period=True)
        )
        balances = {
            account_id: net_balance(
                debit, credit,
                NORMAL_BALANCE.get(book.meta(account_id)["type"], "debit"),
            )
            for account_id, (debit, credit) in cumulative.items()
        }

        result = ReportResult(
            report_type=self.report_type, currency=context.currency
        )

        total_assets = self._section_total(book, balances, AccountType.ASSET)
        total_liabilities = self._section_total(
            book, balances, AccountType.LIABILITY
        )
        posted_equity = self._section_total(book, balances, AccountType.EQUITY)

        fiscal_year = self._fiscal_year(context, as_of)
        current_year_earnings = self._current_year_earnings(
            context, book, fiscal_year, as_of, result
        )
        total_equity = posted_equity + current_year_earnings

        sections = [
            self._build_section(
                "assets", "Assets", AccountType.ASSET, book, balances, 0
            ),
            self._build_section(
                "liabilities", "Liabilities", AccountType.LIABILITY, book, balances, 1
            ),
        ]
        equity_section = self._build_section(
            "equity", "Equity", AccountType.EQUITY, book, balances, 2
        )
        equity_section.add(
            ReportLine(
                label="Current year earnings",
                amount=current_year_earnings,
                is_bold=True,
                note=(
                    "Income less expenses since the start of the fiscal year. "
                    "Computed, not stored: these amounts are only rolled into "
                    "retained earnings by the year-end closing entry, so a "
                    "mid-year balance sheet that omits them does not balance."
                ),
            )
        )
        equity_section.total = total_equity
        sections.append(equity_section)
        result.sections = sections

        difference = total_assets - (total_liabilities + total_equity)
        result.totals = {
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "equity_posted": posted_equity,
            "current_year_earnings": current_year_earnings,
            "total_equity": total_equity,
            "liabilities_and_equity": total_liabilities + total_equity,
            "difference": difference,
        }
        result.metadata["as_of"] = as_of.isoformat()
        if fiscal_year is not None:
            result.metadata["fiscal_year"] = {
                "name": fiscal_year.name,
                "start_date": fiscal_year.start_date.isoformat(),
                "status": fiscal_year.status,
            }

        if difference != ZERO:
            raise ReportImbalance(
                f"BALANCE SHEET DOES NOT BALANCE as at {as_of:%Y-%m-%d} for "
                f"tenant {context.tenant_id}: assets {total_assets} vs "
                f"liabilities {total_liabilities} + equity {total_equity} "
                f"(= {total_liabilities + total_equity}), difference "
                f"{difference}. Current-year earnings of "
                f"{current_year_earnings} are already included in equity, so "
                f"this is not the usual unclosed-period shortfall. The most "
                f"likely causes, in order: a prior fiscal year that was never "
                f"closed (its result is still sitting in income/expense "
                f"accounts outside this year's range), an account whose `type` "
                f"was changed after it had postings, or ledger rows written "
                f"outside the application. Run the trial balance first."
            )

        return result

    # -- helpers ------------------------------------------------------------

    def _fiscal_year(
        self, context: ReportContext, as_of
    ) -> Optional[FiscalYear]:
        """The fiscal year containing ``as_of``.

        Index used: ``uq_fiscal_year_name`` does not help here; the table is
        tiny (one row per year per tenant) and the tenant predicate is served
        by the ``(tenant, -created_at)`` index inherited from
        ``TenantScopedModel.Meta``. Deliberately not cached across calls — a
        year can be closed between two report runs and the second run must see
        it.
        """
        return (
            FiscalYear.all_tenants.filter(
                tenant_id=context.tenant_id,
                start_date__lte=as_of,
                end_date__gte=as_of,
            )
            .order_by("-start_date")
            .first()
        )

    def _current_year_earnings(
        self,
        context: ReportContext,
        book: _AccountBook,
        fiscal_year: Optional[FiscalYear],
        as_of,
        result: ReportResult,
    ) -> Decimal:
        """Income minus expenses since the fiscal year started. See the class
        docstring for why this is derived rather than stored."""
        if fiscal_year is None:
            result.warn(
                f"No fiscal year covers {as_of:%Y-%m-%d}, so current-year "
                f"earnings were computed from the whole ledger. Create the "
                f"fiscal year: without it, every prior year's result is being "
                f"counted as this year's."
            )
            year_start = None
        else:
            year_start = fiscal_year.start_date

        pl_ids = book.ids_of_type(AccountType.INCOME, AccountType.EXPENSE)
        if not pl_ids:
            return ZERO

        # Index used: ix_entry_status (tenant, status, entry_date) — the date
        # range is the selective predicate here, since a year's postings are a
        # small slice of a multi-year ledger.
        qs = ledger_query(
            context, date_from=year_start, date_to=as_of, ignore_period=year_start is None
        ).filter(account_id__in=pl_ids)
        totals = _aggregate_by_account(qs)

        income = ZERO
        expense = ZERO
        for account_id, (debit, credit) in totals.items():
            account_type = book.meta(account_id)["type"]
            balance = net_balance(
                debit, credit, NORMAL_BALANCE.get(account_type, "debit")
            )
            if account_type == AccountType.INCOME:
                income += balance
            elif account_type == AccountType.EXPENSE:
                expense += balance
        return income - expense

    def _section_total(
        self, book: _AccountBook, balances: dict[uuid.UUID, Decimal], account_type: str
    ) -> Decimal:
        return sum_decimals(
            amount
            for account_id, amount in balances.items()
            if book.meta(account_id)["type"] == account_type
        )

    def _build_section(
        self,
        key: str,
        title: str,
        account_type: str,
        book: _AccountBook,
        balances: dict[uuid.UUID, Decimal],
        sequence: int,
    ) -> ReportSection:
        section = ReportSection(key=key, title=title, sequence=sequence)
        for account_id, meta in sorted(
            book.by_id.items(), key=lambda kv: kv[1]["code"]
        ):
            if meta["type"] != account_type:
                continue
            amount = balances.get(account_id, ZERO)
            if amount == ZERO:
                continue
            section.add(
                ReportLine(
                    label=f"{meta['code']} — {meta['name']}",
                    amount=amount,
                    account_id=account_id,
                    account_code=meta["code"],
                    account_type=meta["type"],
                )
            )
        section.recompute_total()
        return section


# ---------------------------------------------------------------------------
# Cash flow (indirect method)
# ---------------------------------------------------------------------------

@register_report(ReportType.CASH_FLOW)
class CashFlowGenerator(ReportGenerator):
    """Cash flow statement, indirect method, reconciled against actual cash.

    The indirect method starts from net profit and undoes everything in it
    that was not cash:

    1. **Non-cash charges added back.** Depreciation, amortisation, provisions
       and bad-debt charges reduced profit without any money moving. Omitting
       the add-back understates operating cash by exactly those charges, every
       period, in a way that looks like a deteriorating business.
    2. **Working capital movements.** Profit is recognised when a sale is
       *invoiced*, not when it is *collected*. A period with strong profit and
       a large increase in receivables generated no cash at all — which is
       precisely the condition that puts profitable companies out of business,
       and precisely what this section exists to show. An increase in an asset
       (AR, inventory) consumes cash; an increase in a liability (AP, accruals)
       provides it. Getting that sign backwards inverts the entire statement.
    3. **Investing and financing** sections for the movements that never touch
       profit at all: buying equipment, drawing a loan, paying a dividend.

    The reconciliation is the point
    -------------------------------
    Cash is the one figure in accounting that can be independently verified:
    the bank knows what it is. So the statement's closing figure is compared
    against the *actual* movement on the cash and bank ledger accounts over
    the same period, and any difference is reported as its own line and as a
    warning. It is never silently distributed into "other movements", because
    a plug in a cash flow statement is indistinguishable from a real number to
    every reader, and hiding it removes the only signal that the classification
    of some account is wrong.
    """

    title = "Cash flow statement (indirect)"
    is_as_of = False

    def generate(self, context: ReportContext) -> ReportResult:
        book = _AccountBook(context.tenant_id)
        result = ReportResult(
            report_type=self.report_type, currency=context.currency
        )

        cash_ids = book.cash_account_ids()
        if not cash_ids:
            result.warn(
                "No account is flagged `is_reconcilable`, so there is nothing "
                "the statement can be reconciled against. Flag the bank and "
                "cash accounts in the chart of accounts: an unreconciled cash "
                "flow statement is an assertion nobody has checked."
            )

        # Period movement per account, signed on the account's normal side.
        # Index used: ix_entry_status (tenant, status, entry_date) — the period
        # predicate is the selective one for a statement of flows.
        movements = _aggregate_by_account(ledger_query(context))
        signed: dict[uuid.UUID, Decimal] = {
            account_id: net_balance(
                debit, credit,
                NORMAL_BALANCE.get(book.meta(account_id)["type"], "debit"),
            )
            for account_id, (debit, credit) in movements.items()
        }
        # Raw debit-minus-credit, needed for cash accounts where the direction
        # of money is what matters, not the account's normal side.
        raw: dict[uuid.UUID, Decimal] = {
            account_id: debit - credit for account_id, (debit, credit) in movements.items()
        }

        ar_ids = book.ids_with_system_keys(AR_CONTROL_KEYS)
        ap_ids = book.ids_with_system_keys(AP_CONTROL_KEYS)
        inventory_ids = book.ids_with_system_keys(INVENTORY_CONTROL_KEYS)
        accrual_ids = book.ids_with_system_keys(ACCRUAL_KEYS)
        non_cash_ids = book.ids_with_system_keys(NON_CASH_KEYS)
        financing_ids = book.ids_with_system_keys(FINANCING_KEYS)

        # -- operating ------------------------------------------------------
        income_total = sum_decimals(
            amount for account_id, amount in signed.items()
            if book.meta(account_id)["type"] == AccountType.INCOME
        )
        expense_total = sum_decimals(
            amount for account_id, amount in signed.items()
            if book.meta(account_id)["type"] == AccountType.EXPENSE
        )
        net_profit = income_total - expense_total

        operating = ReportSection(
            key="operating", title="Cash flows from operating activities", sequence=0
        )
        operating.add(
            ReportLine(label="Net profit for the period", amount=net_profit, is_bold=True)
        )

        non_cash_total = ZERO
        for account_id in sorted(non_cash_ids, key=lambda i: book.meta(i)["code"]):
            meta = book.meta(account_id)
            charge = signed.get(account_id, ZERO)
            if charge == ZERO:
                continue
            # Expense accounts carry a positive (debit) balance; adding the
            # charge back means adding a positive number to profit.
            add_back = charge if meta["type"] == AccountType.EXPENSE else -charge
            non_cash_total += add_back
            operating.add(
                ReportLine(
                    label=f"Add back non-cash: {meta['name']}",
                    amount=add_back,
                    account_id=account_id,
                    account_code=meta["code"],
                    level=1,
                )
            )

        working_capital = ZERO
        for label, account_ids, is_asset in (
            ("Movement in receivables", ar_ids, True),
            ("Movement in inventory", inventory_ids, True),
            ("Movement in payables", ap_ids, False),
            ("Movement in accruals and prepayments", accrual_ids, False),
        ):
            movement = sum_decimals(signed.get(a, ZERO) for a in account_ids)
            if movement == ZERO:
                continue
            # An increase in an asset consumes cash; an increase in a
            # liability provides it. This sign is the single most common
            # error in an indirect cash flow statement.
            effect = -movement if is_asset else movement
            working_capital += effect
            operating.add(ReportLine(label=label, amount=effect, level=1))

        operating_cash = net_profit + non_cash_total + working_capital
        operating.add(
            ReportLine(
                label="Net cash from operating activities",
                amount=operating_cash,
                is_subtotal=True,
                is_bold=True,
            )
        )
        operating.total = operating_cash

        # -- investing ------------------------------------------------------
        # Non-current assets that are not cash, receivables or inventory:
        # equipment, intangibles, investments. Their movement is investing.
        investing = ReportSection(
            key="investing", title="Cash flows from investing activities", sequence=1
        )
        investing_cash = ZERO
        excluded = cash_ids | ar_ids | inventory_ids | accrual_ids | non_cash_ids
        for account_id, meta in sorted(
            book.by_id.items(), key=lambda kv: kv[1]["code"]
        ):
            if meta["type"] != AccountType.ASSET or account_id in excluded:
                continue
            movement = signed.get(account_id, ZERO)
            if movement == ZERO:
                continue
            effect = -movement  # buying an asset consumes cash
            investing_cash += effect
            investing.add(
                ReportLine(
                    label=meta["name"],
                    amount=effect,
                    account_id=account_id,
                    account_code=meta["code"],
                    level=1,
                )
            )
        investing.total = investing_cash

        # -- financing ------------------------------------------------------
        financing = ReportSection(
            key="financing", title="Cash flows from financing activities", sequence=2
        )
        financing_cash = ZERO
        financing_candidates = set(financing_ids) | {
            account_id
            for account_id, meta in book.by_id.items()
            if meta["type"] == AccountType.EQUITY
        }
        for account_id in sorted(
            financing_candidates, key=lambda i: book.meta(i)["code"]
        ):
            if account_id in excluded or account_id in ap_ids:
                continue
            movement = signed.get(account_id, ZERO)
            if movement == ZERO:
                continue
            # Equity and loans are credit-normal: an increase provides cash.
            financing_cash += movement
            meta = book.meta(account_id)
            financing.add(
                ReportLine(
                    label=meta["name"],
                    amount=movement,
                    account_id=account_id,
                    account_code=meta["code"],
                    level=1,
                )
            )
        financing.total = financing_cash

        # -- reconciliation against real cash -------------------------------
        opening_cash = self._cash_balance(context, cash_ids, before=context.date_from)
        actual_movement = sum_decimals(raw.get(a, ZERO) for a in cash_ids)
        closing_cash_actual = opening_cash + actual_movement
        computed_movement = operating_cash + investing_cash + financing_cash
        closing_cash_computed = opening_cash + computed_movement
        discrepancy = closing_cash_computed - closing_cash_actual

        reconciliation = ReportSection(
            key="reconciliation", title="Reconciliation to cash and bank", sequence=3
        )
        reconciliation.add(ReportLine(label="Cash at start of period", amount=opening_cash))
        reconciliation.add(
            ReportLine(label="Net movement per this statement", amount=computed_movement)
        )
        reconciliation.add(
            ReportLine(
                label="Cash at end of period (per statement)",
                amount=closing_cash_computed,
                is_subtotal=True,
            )
        )
        reconciliation.add(
            ReportLine(
                label="Cash at end of period (per ledger)",
                amount=closing_cash_actual,
                is_bold=True,
                note="Sum of accounts flagged is_reconcilable — the same "
                     "accounts the banking module reconciles to statements.",
            )
        )
        reconciliation.add(
            ReportLine(
                label="Unexplained difference",
                amount=discrepancy,
                is_bold=True,
                note="Must be zero. A non-zero value means at least one "
                     "account is classified into the wrong section.",
            )
        )
        reconciliation.total = discrepancy

        if discrepancy != ZERO:
            result.warn(
                f"CASH FLOW DOES NOT RECONCILE: this statement explains a "
                f"movement of {computed_movement} while the cash and bank "
                f"ledger accounts actually moved {actual_movement} — an "
                f"unexplained difference of {discrepancy}. It is shown as its "
                f"own line rather than absorbed into 'other movements', "
                f"because a plug in a cash flow statement is indistinguishable "
                f"from a real figure to the reader. The usual cause is an "
                f"account with no `system_key` falling into the wrong section: "
                f"check that receivables, payables, inventory, accrual and "
                f"depreciation accounts all carry their system key."
            )

        result.sections = [operating, investing, financing, reconciliation]
        result.totals = {
            "net_profit": net_profit,
            "non_cash_adjustments": non_cash_total,
            "working_capital_movement": working_capital,
            "operating_cash": operating_cash,
            "investing_cash": investing_cash,
            "financing_cash": financing_cash,
            "opening_cash": opening_cash,
            "closing_cash_computed": closing_cash_computed,
            "closing_cash_actual": closing_cash_actual,
            "unexplained_difference": discrepancy,
        }
        return result

    def _cash_balance(
        self, context: ReportContext, cash_ids: set[uuid.UUID], *, before
    ) -> Decimal:
        """Cumulative cash balance immediately before ``before``.

        Cumulative, not period: an opening balance is everything that happened
        up to that moment. ``ignore_period=True`` is what prevents the caller's
        ``date_from`` from turning the opening balance into a movement.

        Index used: ``ix_entry_status`` (tenant, status, entry_date) for the
        ``<= date`` range scan, then ``ix_line_account`` for the account
        restriction.
        """
        if not cash_ids or before is None:
            return ZERO
        cutoff = before - timedelta(days=1)
        rows = (
            ledger_query(context, date_to=cutoff, ignore_period=True)
            .filter(account_id__in=cash_ids)
            .aggregate(debit=Sum("base_debit"), credit=Sum("base_credit"))
        )
        return (rows["debit"] or ZERO) - (rows["credit"] or ZERO)


# ---------------------------------------------------------------------------
# Tax summary
# ---------------------------------------------------------------------------

@register_report(ReportType.TAX_SUMMARY)
class TaxSummaryGenerator(ReportGenerator):
    """Output VAT collected, input VAT paid and the net payable, by tax rate.

    This is the report a VAT return is copied from, so it is broken down *per
    rate* rather than presented as one net figure: every VAT return in
    existence asks for the taxable base and the tax at each rate separately,
    and a single net number cannot be decomposed back into them. A tenant with
    a standard rate, a reduced rate and zero-rated exports has three lines on
    their return and would otherwise have to derive them by hand — which is
    where filing errors come from.

    The taxable base
    ----------------
    ``JournalLine.tax_rate`` is set on the tax line itself (see
    ``apps.sales.services.invoice_workflow``) and, where the posting service
    sets it, on the underlying revenue or expense line. So the base is read
    from those non-tax lines when they exist, and *derived* as ``tax / rate``
    only when they do not. Which method was used is reported per line, because
    a derived base is an inference and the person signing the return is
    entitled to know that.

    Recoverability matters: input VAT on a non-recoverable rate is a cost, not
    an asset, and must not be netted off the amount payable. Netting it is a
    real and expensive filing error — it understates the liability, and the
    authority's assessment arrives with interest.
    """

    title = "Tax summary"
    is_as_of = False

    def generate(self, context: ReportContext) -> ReportResult:
        # Index used: the tax-rate table is small and tenant-scoped; the
        # (tenant, -created_at) index from TenantScopedModel.Meta serves it.
        rates = list(
            TaxRate.all_tenants.filter(tenant_id=context.tenant_id).order_by("code")
        )
        if not rates:
            result = ReportResult(
                report_type=self.report_type, currency=context.currency
            )
            result.warn(
                "No tax rates are configured for this tenant, so no return can "
                "be prepared from this period's postings."
            )
            return result

        collected_ids = {r.collected_account_id for r in rates if r.collected_account_id}
        paid_ids = {r.paid_account_id for r in rates if r.paid_account_id}

        # One pass over the period's lines that carry a tax rate, grouped by
        # (tax_rate, account). Everything else is derived in Python from that
        # small result set.
        #
        # Index used: ix_entry_status (tenant, status, entry_date) restricts to
        # the period; the tax_rate_id predicate is highly selective on its own
        # FK index. Grouping by two columns avoids a second query per rate,
        # which at twelve rates would be twelve scans of the journal.
        rows = (
            ledger_query(context)
            .filter(tax_rate_id__isnull=False)
            .values("tax_rate_id", "account_id")
            .annotate(debit=Sum("base_debit"), credit=Sum("base_credit"))
        )

        per_rate: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(
            lambda: {
                "output_tax": ZERO,
                "input_tax": ZERO,
                "base_from_lines": ZERO,
            }
        )
        for row in rows:
            bucket = per_rate[row["tax_rate_id"]]
            debit = row["debit"] or ZERO
            credit = row["credit"] or ZERO
            account_id = row["account_id"]
            if account_id in collected_ids:
                # Output VAT is a liability: credits increase it, a credit
                # note debits it back. Net, not gross, or a month with returns
                # overstates what is owed.
                bucket["output_tax"] += credit - debit
            elif account_id in paid_ids:
                bucket["input_tax"] += debit - credit
            else:
                # A revenue or expense line carrying the rate: this is the
                # taxable base, and it is preferred over deriving it.
                bucket["base_from_lines"] += (credit - debit).copy_abs()

        output_section = ReportSection(
            key="output_tax", title="Output tax (collected on sales)", sequence=0
        )
        input_section = ReportSection(
            key="input_tax", title="Input tax (paid on purchases)", sequence=1
        )

        total_output = ZERO
        total_input_recoverable = ZERO
        total_input_non_recoverable = ZERO

        result = ReportResult(
            report_type=self.report_type, currency=context.currency
        )

        for rate in rates:
            bucket = per_rate.get(rate.id)
            if bucket is None:
                continue
            output_tax = bucket["output_tax"]
            input_tax = bucket["input_tax"]
            base = bucket["base_from_lines"]
            base_is_derived = False
            if base == ZERO and rate.rate and rate.rate > ZERO:
                # Only an inference — say so on the line.
                base = (output_tax + input_tax) / rate.rate
                base_is_derived = True

            label = f"{rate.code} — {rate.name} ({rate.rate * 100:.2f}%)"
            note = (
                "Taxable base derived as tax / rate; no base lines carried this "
                "tax rate." if base_is_derived else "Taxable base from posted lines."
            )

            if output_tax != ZERO:
                total_output += output_tax
                output_section.add(
                    ReportLine(
                        label=label,
                        amount=output_tax,
                        quantity=base,
                        note=note,
                        meta={
                            "tax_rate_id": str(rate.id),
                            "rate": rate.rate,
                            "taxable_base": base,
                            "base_is_derived": base_is_derived,
                        },
                    )
                )

            if input_tax != ZERO:
                if rate.is_recoverable:
                    total_input_recoverable += input_tax
                else:
                    total_input_non_recoverable += input_tax
                input_section.add(
                    ReportLine(
                        label=label
                        + ("" if rate.is_recoverable else "  [NOT RECOVERABLE]"),
                        amount=input_tax,
                        quantity=base,
                        note=(
                            note
                            if rate.is_recoverable
                            else note
                            + " Non-recoverable: this is a cost, and is "
                              "deliberately excluded from the net payable."
                        ),
                        meta={
                            "tax_rate_id": str(rate.id),
                            "rate": rate.rate,
                            "taxable_base": base,
                            "base_is_derived": base_is_derived,
                            "is_recoverable": rate.is_recoverable,
                        },
                    )
                )

        output_section.total = total_output
        input_section.total = total_input_recoverable + total_input_non_recoverable

        net_payable = total_output - total_input_recoverable
        summary = ReportSection(key="net", title="Net position", sequence=2)
        summary.add(ReportLine(label="Output tax collected", amount=total_output))
        summary.add(
            ReportLine(
                label="Input tax recoverable", amount=total_input_recoverable
            )
        )
        summary.add(
            ReportLine(
                label="Input tax NOT recoverable (expensed)",
                amount=total_input_non_recoverable,
                note="Excluded from the net payable; netting it off understates "
                     "the liability and the assessment arrives with interest.",
            )
        )
        summary.add(
            ReportLine(
                label="Net tax payable" if net_payable >= ZERO else "Net tax reclaimable",
                amount=net_payable,
                is_subtotal=True,
                is_bold=True,
            )
        )
        summary.total = net_payable

        result.sections = [output_section, input_section, summary]
        result.totals = {
            "output_tax": total_output,
            "input_tax_recoverable": total_input_recoverable,
            "input_tax_non_recoverable": total_input_non_recoverable,
            "net_payable": net_payable,
        }
        return result
