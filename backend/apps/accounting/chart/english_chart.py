"""The default English chart of accounts — a 5-level coded hierarchy.

Ported from the reference GL's ``accounts/starter_templates/default_eg.json``
(structure and coverage) and translated to English, then **extended** with the
leaves this ERP's automated postings resolve by ``system_key`` (payroll
payables/expenses, the payment-gateway clearing account, work-in-progress, the
FX gain/loss account, opening-balance equity, and the two current-group markers
the working-capital KPIs classify by).

Shape: five sections (Assets, Liabilities, Equity, Revenue, Expenses), each a
tree exactly five levels deep. Levels 1–4 are summaries; **level 5 is the only
postable level**. ``NODE``/``LEAF`` build the tree; :func:`build_default_chart`
walks it, allocates each node's segment/full code, and upserts the accounts.

The ``section`` (ACHR ``AccountType``) is set on each root and inherited down —
it drives the balance sheet and every module that resolves an account by role.
A leaf may override its normal side (a credit accumulated-depreciation under
Assets, a debit sales-discount under Revenue) and name its P&L ``income``
category and whether it ``requires_party``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class Node:
    name: str
    children: tuple["Node", ...] = ()
    #: Leaf attributes (ignored on summaries).
    system_key: str = ""
    normal_balance: str = ""     # "" = derive from the section
    income_category: str = "none"
    requires_party: bool = False
    is_reconcilable: bool = False


def N(name: str, *children: Node, system_key: str = "") -> Node:
    """A summary node (levels 1–4). May carry a ``system_key`` (the current-group
    markers are summaries)."""
    return Node(name=name, children=tuple(children), system_key=system_key)


def L(name: str, *, system_key: str = "", nb: str = "", inc: str = "none",
      party: bool = False, recon: bool = False) -> Node:
    """A postable leaf (level 5)."""
    return Node(name=name, system_key=system_key, normal_balance=nb,
                income_category=inc, requires_party=party, is_reconcilable=recon)


# Each entry: (section AccountType, root Node).
DEFAULT_CHART: tuple[tuple[str, Node], ...] = (
    ("asset", N("Assets",
        N("Current assets",
            N("Cash & cash equivalents",
                N("Cash on hand", L("Main cash box", system_key="cash_on_hand", recon=True)),
                N("Banks",
                    L("Bank — current account", system_key="bank_main", recon=True),
                    L("Bank — savings account", recon=True)),
                N("Payment gateways",
                    L("Payment gateway clearing", system_key="gateway_clearing"))),
            N("Receivables & notes receivable",
                N("Customer accounts",
                    L("Accounts receivable — control", system_key="ar_control", party=True)),
                N("Notes receivable", L("Notes receivable"))),
            N("Inventory",
                N("Merchandise inventory",
                    L("Finished goods inventory", system_key="inventory_asset")),
                N("Work in progress", L("Work in progress", system_key="work_in_progress"))),
            N("Prepaid expenses & other current assets",
                N("Prepaid expenses", L("Prepaid rent")),
                N("Recoverable VAT", L("Input VAT — purchases", system_key="input_vat"))),
            system_key="grp_current_assets"),
        N("Fixed assets",
            N("Tangible fixed assets",
                N("Land & buildings", L("Land"), L("Buildings")),
                N("Machinery & equipment", L("Plant machinery & equipment")),
                N("Furniture & fixtures", L("Furniture & fittings"), L("Computers")),
                N("Vehicles", L("Vehicles"))),
            N("Accumulated depreciation",
                N("Accumulated depreciation of fixed assets",
                    L("Accumulated depreciation — buildings", nb="credit"),
                    L("Accumulated depreciation — machinery", nb="credit"),
                    L("Accumulated depreciation — vehicles", nb="credit")))))),
    ("liability", N("Liabilities",
        N("Current liabilities",
            N("Suppliers & notes payable",
                N("Supplier accounts",
                    L("Accounts payable — control", system_key="ap_control",
                      nb="credit", party=True)),
                N("Notes payable", L("Notes payable", nb="credit"))),
            N("Accruals & taxes",
                N("Accrued expenses", L("Accrued salaries", nb="credit")),
                N("Taxes payable",
                    L("Output VAT — sales", system_key="output_vat", nb="credit"),
                    L("Income tax payable", nb="credit"))),
            N("Payroll liabilities",
                N("Payroll payables",
                    L("Salaries payable", system_key="payroll_salaries_payable", nb="credit"),
                    L("Payroll income tax payable", system_key="payroll_income_tax_payable",
                      nb="credit"),
                    L("Social insurance payable", system_key="payroll_social_insurance_payable",
                      nb="credit"),
                    L("Other payroll deductions payable",
                      system_key="payroll_other_deductions_payable", nb="credit"),
                    L("Employee reimbursements payable",
                      system_key="employee_reimbursements_payable", nb="credit"))),
            N("Short-term loans",
                N("Short-term bank loans", L("Short-term bank loan", nb="credit"))),
            system_key="grp_current_liabilities"),
        N("Long-term liabilities",
            N("Long-term loans",
                N("Long-term bank loans", L("Long-term bank loan", nb="credit")))))),
    ("equity", N("Equity",
        N("Capital & reserves",
            N("Capital",
                N("Paid-in capital",
                    L("Issued & paid-in capital", system_key="share_capital", nb="credit"))),
            N("Reserves & retained earnings",
                N("Legal reserve", L("Legal reserve", nb="credit")),
                N("Retained earnings",
                    L("Retained earnings", system_key="retained_earnings", nb="credit")),
                N("Opening balance equity",
                    L("Opening balance equity", system_key="opening_balance_equity",
                      nb="credit")))))),
    ("income", N("Revenue",
        N("Operating revenue",
            N("Sales revenue",
                N("Local sales",
                    L("Local sales revenue", system_key="sales_revenue", nb="credit",
                      inc="revenue")),
                N("Export sales", L("Export sales revenue", nb="credit", inc="revenue")),
                N("Service revenue",
                    L("Service revenue", system_key="service_revenue", nb="credit",
                      inc="revenue"))),
            N("Sales returns & allowances",
                N("Sales returns", L("Local sales returns", inc="returns")),
                N("Discounts allowed",
                    L("Cash discount allowed", system_key="sales_discount", inc="discount"))),
            N("Other income",
                N("Miscellaneous income",
                    L("Other miscellaneous income", nb="credit", inc="revenue"),
                    L("Foreign exchange gain/(loss)", system_key="fx_gain_loss",
                      inc="revenue")))))),
    ("expense", N("Expenses",
        N("Cost of sales",
            N("Cost of goods",
                N("Cost of sales",
                    L("Cost of goods sold", system_key="cogs", inc="cogs"),
                    L("Inventory adjustment", system_key="inventory_adjustment", inc="cogs")))),
        N("Operating expenses",
            N("Selling & distribution",
                N("Sales salaries", L("Sales team salaries", inc="operating")),
                N("Marketing & advertising", L("Advertising & promotion", inc="operating")),
                N("Freight & shipping", L("Shipping & freight", inc="operating")))),
        N("General & administrative",
            N("Administrative salaries",
                N("Management salaries",
                    L("Salaries & wages", system_key="payroll_salary_expense", inc="admin"),
                    L("Employer social insurance contribution",
                      system_key="payroll_employer_social_insurance_expense", inc="admin"))),
            N("General expenses",
                N("Rent & utilities",
                    L("Office rent", inc="admin"),
                    L("Electricity & water", inc="admin")),
                N("Communications & stationery",
                    L("Communications & internet", inc="admin"),
                    L("Stationery & printing", inc="admin")),
                N("Maintenance & miscellaneous",
                    L("General maintenance", inc="admin"),
                    L("Office & general expense", system_key="office_expense", inc="admin"),
                    L("Bank & payment fees", system_key="bank_fees", inc="admin"),
                    L("Bad debt expense", system_key="bad_debt_expense", inc="admin")))),
        N("Depreciation & taxes",
            N("Depreciation expense",
                N("Fixed-asset depreciation",
                    L("Depreciation of fixed assets", inc="depreciation_tax"))),
            N("Income tax",
                N("Income tax expense",
                    L("Income tax expense for the period", inc="depreciation_tax")))))),
)


def required_system_keys() -> set[str]:
    """Every ``system_key`` the default chart defines — the set the seed proves
    present before it commits."""
    keys: set[str] = set()

    def walk(node: Node) -> None:
        if node.system_key:
            keys.add(node.system_key)
        for child in node.children:
            walk(child)

    for _section, root in DEFAULT_CHART:
        walk(root)
    return keys


def build_default_chart(
    tenant_id: uuid.UUID, *, user_id: Optional[uuid.UUID] = None
) -> tuple[dict, dict]:
    """Upsert the whole default chart for ``tenant_id``. Idempotent.

    Keyed on ``(tenant, full_code)`` — an account's full code is fixed by its
    place in the tree, so re-running refreshes names/roles without moving a
    balance or duplicating a row. Returns ``({"created": n, "updated": m},
    {system_key: Account})`` — the second map lets the seed wire journal default
    accounts by role.
    """
    from apps.accounting.models import Account
    from apps.accounting.services.coding import compute_full_code, format_full_code

    counts = {"created": 0, "updated": 0}
    by_key: dict[str, object] = {}

    def upsert(node: Node, *, section: str, level: int, parent, parent_full,
               segment: int) -> None:
        full_code = compute_full_code(parent_full, segment, level)
        is_postable = level == 5
        defaults = {
            "parent": parent,
            "level": level,
            "segment_code": segment,
            "code": format_full_code(full_code, level),
            "name": node.name,
            "type": section,
            "is_postable": is_postable,
            "system_key": node.system_key,
            "normal_balance_override": node.normal_balance if is_postable else "",
            "income_category": node.income_category if is_postable else "none",
            "requires_party": node.requires_party and is_postable,
            "is_reconcilable": node.is_reconcilable and is_postable,
            "updated_by_id": user_id,
        }
        obj, created = Account.all_tenants.update_or_create(
            tenant_id=tenant_id, full_code=full_code,
            defaults=defaults,
            create_defaults={**defaults, "created_by_id": user_id},
        )
        counts["created" if created else "updated"] += 1
        if node.system_key:
            by_key[node.system_key] = obj
        for index, child in enumerate(node.children, start=1):
            upsert(child, section=section, level=level + 1, parent=obj,
                   parent_full=full_code, segment=index)

    for root_index, (section, root) in enumerate(DEFAULT_CHART, start=1):
        upsert(root, section=section, level=1, parent=None, parent_full=None,
               segment=root_index)
    return counts, by_key
