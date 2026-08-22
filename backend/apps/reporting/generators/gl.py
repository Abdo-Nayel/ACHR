"""
General-ledger detail reports, plus the derived financial-ratios report.

This module houses the reports that read the ledger *line by line* — the
general ledger, the journal register and the per-party statement — and the
financial-ratios report, which reads no lines of its own but is derived
entirely from balance-sheet and profit-and-loss figures.

They sit apart from ``financial.py`` (the statutory statements) because they
are a different *shape*. A statement aggregates the ledger into a handful of
totals; these walk it transaction by transaction and carry a **running
balance**. A running balance is a property of an *ordered sequence* of lines,
not of a GROUP BY, so each report sorts its lines deterministically and
accumulates in Python — exactly as the per-account
``/accounts/{id}/ledger/`` endpoint does, and by the same sign convention
(:func:`net_balance`: positive on the account's normal side). Doing it any
other way is how a general ledger and the per-account ledger it should equal
end up disagreeing.

Every amount is the base-currency ``base_debit`` / ``base_credit`` frozen at
posting time — never re-converted at report time — so these detail reports
tie, line for line, to the trial balance built from the same rows.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from django.db.models import F, Sum

from apps.accounting.models import (
    NORMAL_BALANCE,
    Account,
    AccountType,
    JournalEntry,
    JournalLine,
)
from apps.core.fields import ZERO
from apps.reporting.generators.base import (
    ReportContext,
    ReportError,
    ReportGenerator,
    ReportLine,
    ReportResult,
    ReportSection,
    ledger_query,
    net_balance,
    register_report,
)
from apps.reporting.generators.financial import _AccountBook, _aggregate_by_account
from apps.reporting.models import ReportType


# ---------------------------------------------------------------------------
# Shared partner-name resolution
# ---------------------------------------------------------------------------

def _partner_names(
    tenant_id: uuid.UUID, keys: Iterable[tuple[str, uuid.UUID]]
) -> dict[tuple[str, uuid.UUID], str]:
    """``{(partner_type, partner_id): display_name}`` for the pairs given.

    Resolved in one query per partner type rather than one per line: the
    journal register of a busy month names the same few dozen customers
    thousands of times, and a per-line lookup turns a report into an N+1
    storm. Types this ERP does not model are skipped rather than raised on —
    a statement is still useful with an id where a name could not be found,
    and refusing the whole report because one line names a partner type the
    chart never expected to see helps nobody.
    """
    want: dict[str, set[uuid.UUID]] = defaultdict(set)
    for partner_type, partner_id in keys:
        if partner_type and partner_id:
            want[partner_type].add(partner_id)

    out: dict[tuple[str, uuid.UUID], str] = {}

    if want.get("customer"):
        from apps.sales.models import Customer  # local: avoids an app cycle

        for pid, name in Customer.all_tenants.filter(
            tenant_id=tenant_id, id__in=want["customer"]
        ).values_list("id", "name"):
            out[("customer", pid)] = name

    if want.get("vendor"):
        from apps.expenses.models import Vendor

        for pid, name in Vendor.all_tenants.filter(
            tenant_id=tenant_id, id__in=want["vendor"]
        ).values_list("id", "name"):
            out[("vendor", pid)] = name

    if want.get("employee"):
        try:
            from apps.hr.models import Employee

            for row in Employee.all_tenants.filter(
                tenant_id=tenant_id, id__in=want["employee"]
            ).values("id", "first_name", "last_name"):
                out[("employee", row["id"])] = (
                    f"{row['first_name']} {row['last_name']}".strip()
                )
        except Exception:  # pragma: no cover - HR app optional at report time
            pass

    return out


#: The control-account ``system_key``\ s a party statement follows, per partner
#: type. Kept in step with ``operational.AR_CONTROL_KEYS`` / ``AP_CONTROL_KEYS``
#: so the statement and the aging report age the same accounts.
_CONTROL_KEYS: dict[str, tuple[str, ...]] = {
    "customer": ("ar_control", "accounts_receivable"),
    "vendor": ("ap_control", "accounts_payable"),
}


def _control_account_ids(
    tenant_id: uuid.UUID, partner_type: str
) -> Optional[set[uuid.UUID]]:
    """Control accounts to scope a party statement to, or None to not scope.

    Resolved from ``system_key`` *and* from the per-partner override accounts
    (``Customer.receivable_account`` / ``Vendor.payable_account``), exactly as
    the aging report does — a tenant that gives an intercompany customer its
    own receivable account must still see it here. Returns None for a partner
    type with no known control set (an employee, say), which the caller treats
    as "show every line for this party" rather than "show nothing".
    """
    keys = _CONTROL_KEYS.get(partner_type)
    if not keys:
        return None
    ids = set(
        Account.all_tenants.filter(
            tenant_id=tenant_id, system_key__in=keys
        ).values_list("id", flat=True)
    )
    if partner_type == "customer":
        from apps.sales.models import Customer

        ids |= set(
            Customer.all_tenants.filter(
                tenant_id=tenant_id, receivable_account__isnull=False
            ).values_list("receivable_account_id", flat=True)
        )
    elif partner_type == "vendor":
        from apps.expenses.models import Vendor

        ids |= set(
            Vendor.all_tenants.filter(
                tenant_id=tenant_id, payable_account__isnull=False
            ).values_list("payable_account_id", flat=True)
        )
    return ids or None


# ---------------------------------------------------------------------------
# General ledger — every account, line by line, with a running balance
# ---------------------------------------------------------------------------

@register_report(ReportType.GENERAL_LEDGER)
class GeneralLedgerGenerator(ReportGenerator):
    """Each account's movements over the period, with a running balance.

    The whole-ledger companion to the per-account ``/accounts/{id}/ledger/``
    endpoint: one section per account, opened with its brought-forward
    balance, then a line per posting, closed with its carried-forward balance.
    Pass ``options.account`` to scope it to a single account **and its
    descendants** (so a level-3 roll-up shows the aggregate ledger of the
    postable leaves beneath it); omit it for the entire chart.

    Only accounts that carry an opening balance or moved in the period appear.
    Printing an account that is flat at zero throughout is noise on a document
    whose whole job is to let a reader follow the money.
    """

    title = "General ledger"
    is_as_of = False

    def generate(self, context: ReportContext) -> ReportResult:
        book = _AccountBook(context.tenant_id)
        account_ids = self._selected_accounts(context, book)

        # Opening balance = everything strictly before the period start. A
        # date_to on the day before date_from turns the shared ledger query
        # (which is inclusive) into a strictly-earlier one, so the opening and
        # the first period line never double-count the boundary date.
        opening_qs = ledger_query(
            context, ignore_period=True,
            date_to=context.date_from - timedelta(days=1),
        )
        period_qs = ledger_query(context)
        if account_ids is not None:
            opening_qs = opening_qs.filter(account_id__in=account_ids)
            period_qs = period_qs.filter(account_id__in=account_ids)

        opening = _aggregate_by_account(opening_qs)
        rows = list(
            period_qs.values(
                "account_id", "base_debit", "base_credit", "description",
                _date=F("entry__entry_date"),
                _num=F("entry__number"),
                _jcode=F("entry__journal__code"),
                _line=F("line_number"),
            )
        )
        by_account: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_account[row["account_id"]].append(row)

        result = ReportResult(
            report_type=self.report_type, currency=context.currency
        )
        sections: list[ReportSection] = []
        grand_debit = ZERO
        grand_credit = ZERO
        sequence = 0

        for account_id, meta in sorted(
            book.by_id.items(), key=lambda kv: kv[1]["code"]
        ):
            normal = NORMAL_BALANCE.get(meta["type"], "debit")
            odr, ocr = opening.get(account_id, (ZERO, ZERO))
            opening_bal = net_balance(odr, ocr, normal)
            account_rows = by_account.get(account_id, [])
            if opening_bal == ZERO and not account_rows:
                continue

            section = ReportSection(
                key=meta["code"] or str(account_id),
                title=f"{meta['code']} — {meta['name']}",
                sequence=sequence,
            )
            sequence += 1
            section.add(
                ReportLine(
                    label="Opening balance", amount=opening_bal, is_bold=True,
                    account_id=account_id, account_code=meta["code"],
                    account_type=meta["type"], meta={"kind": "opening"},
                )
            )

            running = opening_bal
            account_rows.sort(
                key=lambda r: (r["_date"], r["_num"] or "", r["_line"])
            )
            for row in account_rows:
                debit = row["base_debit"] or ZERO
                credit = row["base_credit"] or ZERO
                grand_debit += debit
                grand_credit += credit
                running += net_balance(debit, credit, normal)
                section.add(
                    ReportLine(
                        label=row["description"] or "",
                        debit=debit, credit=credit, amount=running,
                        account_id=account_id, account_code=meta["code"],
                        account_type=meta["type"],
                        meta={
                            "kind": "movement",
                            "date": row["_date"].isoformat(),
                            "number": row["_num"] or "",
                            "journal": row["_jcode"] or "",
                        },
                    )
                )

            section.add(
                ReportLine(
                    label="Closing balance", amount=running,
                    is_subtotal=True, is_bold=True, account_id=account_id,
                    account_code=meta["code"], account_type=meta["type"],
                    meta={"kind": "closing"},
                )
            )
            section.total = running
            sections.append(section)

        result.sections = sections
        result.totals = {
            "total_debit": grand_debit,
            "total_credit": grand_credit,
            "difference": grand_debit - grand_credit,
        }
        result.metadata["account_count"] = len(sections)
        return result

    def _selected_accounts(
        self, context: ReportContext, book: _AccountBook
    ) -> Optional[set[uuid.UUID]]:
        """The account subtree to report, or None for the whole chart."""
        raw = (context.options or {}).get("account")
        if not raw:
            return None
        try:
            target = uuid.UUID(str(raw))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ReportError(
                f"'account' must be an account id; got {raw!r}."
            ) from exc
        if target not in book.by_id:
            raise ReportError(
                "The requested account is not in this tenant's chart."
            )
        return _subtree_ids(context.tenant_id, target)


def _subtree_ids(tenant_id: uuid.UUID, root: uuid.UUID) -> set[uuid.UUID]:
    """``root`` and every account beneath it, following ``parent_id``.

    One query for the whole ``(id, parent_id)`` map, then a walk in Python: a
    recursive CTE would be one round trip fewer, but the chart is a few
    hundred rows and this keeps the report free of raw SQL.
    """
    children: dict[Optional[uuid.UUID], list[uuid.UUID]] = defaultdict(list)
    for row in Account.all_tenants.filter(tenant_id=tenant_id).values(
        "id", "parent_id"
    ):
        children[row["parent_id"]].append(row["id"])

    out: set[uuid.UUID] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node in out:
            continue
        out.add(node)
        stack.extend(children.get(node, ()))
    return out


# ---------------------------------------------------------------------------
# Journal register — every posting in date order
# ---------------------------------------------------------------------------

@register_report(ReportType.JOURNAL_REGISTER)
class JournalRegisterGenerator(ReportGenerator):
    """Every posted journal line in the period, oldest first.

    The chronological counterpart to the general ledger: where the ledger
    groups by account, the register keeps the ledger's own order — the book of
    original entry. It is what an auditor reads to walk the postings of a
    period in the order they happened, so it is ordered by
    ``(entry_date, number, line)`` rather than newest-first: a register that
    reads backwards is a list, not a journal.

    Debits equal credits by construction — every posted entry balances — so
    ``difference`` is zero on any register this application produced, and a
    non-zero value is the same out-of-band-write signal the trial balance
    raises on.
    """

    title = "Journal register"
    is_as_of = False

    def generate(self, context: ReportContext) -> ReportResult:
        book = _AccountBook(context.tenant_id)
        rows = list(
            ledger_query(context).values(
                "account_id", "base_debit", "base_credit", "description",
                "partner_type", "partner_id",
                _date=F("entry__entry_date"),
                _num=F("entry__number"),
                _memo=F("entry__memo"),
                _jcode=F("entry__journal__code"),
                _line=F("line_number"),
            )
        )
        rows.sort(key=lambda r: (r["_date"], r["_num"] or "", r["_line"]))

        names = _partner_names(
            context.tenant_id,
            ((r["partner_type"], r["partner_id"]) for r in rows),
        )

        section = ReportSection(key="entries", title="Journal register", sequence=0)
        grand_debit = ZERO
        grand_credit = ZERO
        numbers: set[str] = set()

        for row in rows:
            debit = row["base_debit"] or ZERO
            credit = row["base_credit"] or ZERO
            grand_debit += debit
            grand_credit += credit
            if row["_num"]:
                numbers.add(row["_num"])
            meta = book.meta(row["account_id"])
            partner = names.get(
                (row["partner_type"], row["partner_id"]),
                str(row["partner_id"]) if row["partner_id"] else "",
            )
            section.add(
                ReportLine(
                    label=f"{meta['code']} — {meta['name']}",
                    debit=debit, credit=credit,
                    account_id=row["account_id"], account_code=meta["code"],
                    account_type=meta["type"],
                    meta={
                        "date": row["_date"].isoformat(),
                        "number": row["_num"] or "",
                        "journal": row["_jcode"] or "",
                        "memo": row["_memo"] or "",
                        "description": row["description"] or "",
                        "partner_type": row["partner_type"] or "",
                        "partner": partner,
                    },
                )
            )

        section.total = grand_debit
        return ReportResult(
            report_type=self.report_type,
            sections=[section],
            totals={
                "total_debit": grand_debit,
                "total_credit": grand_credit,
                "difference": grand_debit - grand_credit,
            },
            metadata={"line_count": len(rows), "entry_count": len(numbers)},
            currency=context.currency,
        )


# ---------------------------------------------------------------------------
# Party statement — one partner's account, line by line
# ---------------------------------------------------------------------------

@register_report(ReportType.PARTY_STATEMENT)
class PartyStatementGenerator(ReportGenerator):
    """A single customer's or supplier's ledger, with a running balance.

    The same running-balance mechanic as the general ledger, grouped by
    *partner* instead of account — so each row names the account it hit,
    because a party's activity spans many accounts (the invoice, the payment,
    the credit note). The balance is presented debit-positive: charges raise
    it, settlements lower it, which reads correctly for a customer statement
    (a positive balance is what they owe) and, mirrored, for a supplier.

    Requires ``options.partner_id`` (and, to disambiguate, ``partner_type``):
    a statement with no party is not a statement.
    """

    title = "Party statement"
    is_as_of = False

    def validate_context(self, context: ReportContext) -> None:
        super().validate_context(context)
        if not (context.options or {}).get("partner_id"):
            raise ReportError(
                "A party statement needs `partner_id` (and `partner_type`) in "
                "options — it is one partner's account, not a list."
            )

    def generate(self, context: ReportContext) -> ReportResult:
        options = context.options or {}
        partner_type = (options.get("partner_type") or "").strip().lower()
        try:
            partner_id = uuid.UUID(str(options.get("partner_id")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ReportError("`partner_id` must be a UUID.") from exc

        book = _AccountBook(context.tenant_id)

        base = JournalLine.all_tenants.filter(
            tenant_id=context.tenant_id,
            entry__tenant_id=context.tenant_id,
            entry__status=JournalEntry.Status.POSTED,
            partner_id=partner_id,
        )
        if partner_type:
            base = base.filter(partner_type=partner_type)

        # A statement is the party's *control-account* movements — invoices
        # raise the balance, receipts lower it — not every line an invoice
        # happened to tag with them. ACHR stamps the partner onto every line of
        # a sales/purchase entry (revenue, tax and the receivable alike), so
        # without this filter a customer's statement nets to zero: their own
        # revenue credit cancels their receivable debit. Restricting to the AR
        # / AP control accounts (the same set the aging report uses) is what
        # makes the closing balance equal what they actually owe.
        control_ids = _control_account_ids(context.tenant_id, partner_type)
        fell_back = control_ids is None
        if control_ids:
            base = base.filter(account_id__in=control_ids)

        opening_agg = base.filter(
            entry__entry_date__lt=context.date_from
        ).aggregate(debit=Sum("base_debit"), credit=Sum("base_credit"))
        opening_bal = (opening_agg["debit"] or ZERO) - (opening_agg["credit"] or ZERO)

        rows = list(
            base.filter(
                entry__entry_date__gte=context.date_from,
                entry__entry_date__lte=context.date_to,
            ).values(
                "account_id", "base_debit", "base_credit", "description",
                _date=F("entry__entry_date"),
                _num=F("entry__number"),
                _line=F("line_number"),
            )
        )
        rows.sort(key=lambda r: (r["_date"], r["_num"] or "", r["_line"]))

        section = ReportSection(key="statement", title="Statement of account", sequence=0)
        section.add(
            ReportLine(
                label="Opening balance", amount=opening_bal, is_bold=True,
                meta={"kind": "opening"},
            )
        )

        running = opening_bal
        grand_debit = ZERO
        grand_credit = ZERO
        for row in rows:
            debit = row["base_debit"] or ZERO
            credit = row["base_credit"] or ZERO
            grand_debit += debit
            grand_credit += credit
            running += debit - credit
            meta = book.meta(row["account_id"])
            section.add(
                ReportLine(
                    label=row["description"] or "",
                    debit=debit, credit=credit, amount=running,
                    account_id=row["account_id"], account_code=meta["code"],
                    meta={
                        "kind": "movement",
                        "date": row["_date"].isoformat(),
                        "number": row["_num"] or "",
                        "account_code": meta["code"],
                        "account_name": meta["name"],
                    },
                )
            )

        section.add(
            ReportLine(
                label="Closing balance", amount=running,
                is_subtotal=True, is_bold=True, meta={"kind": "closing"},
            )
        )
        section.total = running

        name = _partner_names(context.tenant_id, [(partner_type, partner_id)]).get(
            (partner_type, partner_id), ""
        )
        result = ReportResult(
            report_type=self.report_type,
            sections=[section],
            totals={
                "opening_balance": opening_bal,
                "closing_balance": running,
                "total_debit": grand_debit,
                "total_credit": grand_credit,
            },
            metadata={
                "partner": {
                    "type": partner_type,
                    "id": str(partner_id),
                    "name": name,
                },
                "line_count": len(rows),
            },
            currency=context.currency,
        )
        if fell_back:
            result.warn(
                f"No control account is configured for partner type "
                f"'{partner_type or '(unset)'}', so this statement lists every "
                f"line tagged with the party rather than only its control-"
                f"account movements. The closing balance may net to zero where "
                f"revenue and receivable lines carry the same party."
            )
        return result


# ---------------------------------------------------------------------------
# Financial ratios — derived from the balance sheet and the P&L
# ---------------------------------------------------------------------------

def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Optional[Decimal]:
    """The ratio, or ``None`` when the denominator is zero.

    ``None``, never ``0`` and never a substituted denominator: a current ratio
    of ``0.00`` on a company with no current liabilities reads as "cannot pay
    its bills", which is the opposite of "has no bills". The undefined case has
    to look undefined.
    """
    if denominator == ZERO:
        return None
    try:
        return (numerator / denominator).quantize(Decimal("0.0001"))
    except (InvalidOperation, ZeroDivisionError):  # pragma: no cover - defensive
        return None


@register_report(ReportType.FINANCIAL_RATIOS)
class FinancialRatiosGenerator(ReportGenerator):
    """Liquidity, solvency, profitability and efficiency ratios.

    Reads no journal lines of its own for the balance-sheet inputs: those come
    from :func:`apps.reporting.services.kpis.compute_kpis`, the same classifier
    the dashboard uses, so a ratio here and the working-capital figure on the
    home screen can never disagree about what "current" means. The P&L inputs
    (net sales, gross and operating profit) are split by ``income_category``
    over the period, exactly as the profit-and-loss statement groups them.

    Balances are drawn **as at ``date_to``** (a position) while margins are
    computed **over ``date_from..date_to``** (a rate). Mixing the two is the
    classic ratio bug — a quick ratio built from one month's asset *movements*
    looks catastrophic in January and fine in December for a company whose
    liquidity never changed — so the two live on different time axes here by
    construction.

    Every ratio that cannot be computed prints as "n/a", never as a fabricated
    zero.
    """

    title = "Financial ratios"
    is_as_of = False

    # Statutory ratio set, grouped. (key, label, formula text, kind).
    _LIQUIDITY = "Liquidity"
    _SOLVENCY = "Solvency"
    _PROFITABILITY = "Profitability"
    _EFFICIENCY = "Efficiency"

    def generate(self, context: ReportContext) -> ReportResult:
        from apps.reporting.services.kpis import compute_kpis

        kpis = compute_kpis(
            context.tenant_id,
            date_from=context.date_from,
            date_to=context.date_to,
        )
        comp = kpis["components"]

        def amount(key: str) -> Decimal:
            return Decimal(comp[key])

        total_assets = amount("total_assets")
        current_assets = amount("current_assets")
        current_liabilities = amount("current_liabilities")
        inventory = amount("inventory")
        total_liabilities = amount("total_liabilities")
        total_equity = amount("total_equity")  # posted equity + current earnings

        pl = self._pl_buckets(context)
        net_sales = pl["revenue"] - pl["discount"] - pl["returns"]
        gross_profit = net_sales - pl["cogs"]
        operating_profit = gross_profit - pl["operating"]
        net_profit = operating_profit - pl["admin"] - pl["deptax"]

        result = ReportResult(
            report_type=self.report_type, currency=context.currency
        )

        liquidity = ReportSection(key="liquidity", title=self._LIQUIDITY, sequence=0)
        self._ratio_line(liquidity, "Current ratio",
                         _safe_ratio(current_assets, current_liabilities),
                         "current assets / current liabilities", "ratio")
        self._ratio_line(liquidity, "Quick ratio",
                         _safe_ratio(current_assets - inventory, current_liabilities),
                         "(current assets - inventory) / current liabilities", "ratio")

        solvency = ReportSection(key="solvency", title=self._SOLVENCY, sequence=1)
        self._ratio_line(solvency, "Debt ratio",
                         _safe_ratio(total_liabilities, total_assets),
                         "total liabilities / total assets", "percent")
        self._ratio_line(solvency, "Debt to equity",
                         _safe_ratio(total_liabilities, total_equity),
                         "total liabilities / total equity", "ratio")
        self._ratio_line(solvency, "Equity ratio",
                         _safe_ratio(total_equity, total_assets),
                         "total equity / total assets", "percent")

        profitability = ReportSection(
            key="profitability", title=self._PROFITABILITY, sequence=2
        )
        self._ratio_line(profitability, "Gross margin",
                         _safe_ratio(gross_profit, net_sales),
                         "gross profit / net sales", "percent")
        self._ratio_line(profitability, "Operating margin",
                         _safe_ratio(operating_profit, net_sales),
                         "operating profit / net sales", "percent")
        self._ratio_line(profitability, "Net margin",
                         _safe_ratio(net_profit, net_sales),
                         "net profit / net sales", "percent")
        self._ratio_line(profitability, "Return on assets",
                         _safe_ratio(net_profit, total_assets),
                         "net profit / total assets", "percent")
        self._ratio_line(profitability, "Return on equity",
                         _safe_ratio(net_profit, total_equity),
                         "net profit / total equity", "percent")

        efficiency = ReportSection(
            key="efficiency", title=self._EFFICIENCY, sequence=3
        )
        self._ratio_line(efficiency, "Asset turnover",
                         _safe_ratio(net_sales, total_assets),
                         "net sales / total assets", "ratio")

        # The inputs, so every ratio above is auditable without leaving the
        # page: the same "every amount is reachable" stance the statements take.
        figures = ReportSection(key="figures", title="Figures", sequence=4)
        for label, value in (
            ("Total assets", total_assets),
            ("Current assets", current_assets),
            ("Inventory", inventory),
            ("Total liabilities", total_liabilities),
            ("Current liabilities", current_liabilities),
            ("Total equity", total_equity),
            ("Net sales", net_sales),
            ("Gross profit", gross_profit),
            ("Operating profit", operating_profit),
            ("Net profit", net_profit),
        ):
            figures.add(ReportLine(label=label, amount=value, meta={"kind": "figure"}))
        figures.total = ZERO

        result.sections = [
            liquidity, solvency, profitability, efficiency, figures,
        ]
        result.metadata["as_of"] = context.date_to.isoformat()
        for note in kpis.get("assumptions", ()):  # carry the classifier's caveats
            result.warn(note)
        return result

    def _ratio_line(
        self, section: ReportSection, label: str,
        value: Optional[Decimal], formula: str, kind: str,
    ) -> None:
        if value is None:
            section.add(
                ReportLine(
                    label=f"{label} ({'%' if kind == 'percent' else '×'})",
                    amount=ZERO, note="n/a — zero denominator",
                    meta={"kind": kind, "na": True, "formula": formula},
                )
            )
            return
        display = value * Decimal("100") if kind == "percent" else value
        section.add(
            ReportLine(
                label=f"{label} ({'%' if kind == 'percent' else '×'})",
                amount=display.quantize(Decimal("0.01")),
                note=formula, meta={"kind": kind, "formula": formula},
            )
        )

    def _pl_buckets(self, context: ReportContext) -> dict[str, Decimal]:
        """P&L totals bucketed by ``income_category`` over the period.

        Every income/expense account falls in exactly one bucket, so
        ``revenue − discount − returns − cogs − operating − admin − deptax``
        equals income minus expense — the same net profit the P&L reports.
        Uncategorised income counts as revenue and uncategorised expense as
        admin, so a chart that never set ``income_category`` still nets to the
        right bottom line; only the *margin* split degrades, not the total.
        """
        meta = {
            row["id"]: (row["type"], row["income_category"])
            for row in Account.all_tenants.filter(
                tenant_id=context.tenant_id,
                type__in=[AccountType.INCOME, AccountType.EXPENSE],
            ).values("id", "type", "income_category")
        }
        buckets: dict[str, Decimal] = {
            k: ZERO for k in
            ("revenue", "discount", "returns", "cogs", "operating", "admin", "deptax")
        }
        aggregated = (
            ledger_query(context)
            .filter(account_id__in=meta.keys())
            .values("account_id")
            .annotate(debit=Sum("base_debit"), credit=Sum("base_credit"))
        )
        for row in aggregated:
            account_type, category = meta[row["account_id"]]
            value = net_balance(
                row["debit"] or ZERO, row["credit"] or ZERO,
                NORMAL_BALANCE.get(account_type, "debit"),
            )
            if account_type == AccountType.INCOME:
                if category == "discount":
                    buckets["discount"] += value
                elif category == "returns":
                    buckets["returns"] += value
                else:
                    buckets["revenue"] += value
            else:  # expense
                if category == "cogs":
                    buckets["cogs"] += value
                elif category == "operating":
                    buckets["operating"] += value
                elif category == "depreciation_tax":
                    buckets["deptax"] += value
                else:
                    buckets["admin"] += value
        return buckets
