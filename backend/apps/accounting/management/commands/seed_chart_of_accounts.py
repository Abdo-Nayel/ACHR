"""Seed a tenant's chart of accounts, journals and fiscal calendar.

    python manage.py seed_chart_of_accounts --tenant acme --country EG

Why this command exists
----------------------
Automated postings never resolve an account by ``code`` — codes differ
between national standard charts and between two tenants using the same
chart. They resolve by :attr:`accounting.Account.system_key`, which is a
*role* ("the account net salaries are owed from"), not a name. A tenant whose
chart is missing one of those roles cannot post the corresponding document at
all: ``payroll.services.engine._account_id`` and
``inventory.services.stock._system_account`` raise rather than guess.

So this command is part of tenant provisioning, not a convenience: it is the
step that makes the ledger usable.

Every ``system_key`` the codebase looks up
------------------------------------------
Grepped from ``apps/*/services/*.py`` and ``apps/*/models.py``. The left
column is the literal the code passes; anything in "also known as" is the
name used by ``docs/03-data-model.md`` §5.2 (and by the provisioning
checklist) for the *same* role — see ALIAS NOTE below.

===================================================  =========  ==============================
``system_key`` (authoritative literal)               Type       Resolved by
===================================================  =========  ==============================
``ar_control``                                       asset      sales.Customer.receivable_account
``ap_control``                                       liability  inventory.services.stock.AP_CONTROL
``inventory_asset``                                  asset      inventory.Item.inventory_account
``cogs``                                             expense    inventory.Item.expense_account
``output_vat``                                       liability  accounting.TaxRate.collected_account
``input_vat``                                        asset      accounting.TaxRate.paid_account
``retained_earnings``                                equity     year-end close
``opening_balance_equity``                           equity     stock.OPENING_BALANCE_EQUITY
``work_in_progress``                                 asset      stock.WORK_IN_PROGRESS
``inventory_adjustment``                             expense    stock.INVENTORY_ADJUSTMENT
``sales_discount``                                   income     invoice_workflow.SYSTEM_KEY_SALES_DISCOUNT
``bad_debt_expense``                                 expense    invoice_workflow.SYSTEM_KEY_BAD_DEBT
``payroll_salary_expense``                           expense    payroll.engine.SALARY_EXPENSE
``payroll_employer_social_insurance_expense``        expense    payroll.engine.EMPLOYER_SI_EXPENSE
``payroll_salaries_payable``                         liability  payroll.engine.SALARIES_PAYABLE
``payroll_income_tax_payable``                       liability  payroll.engine.INCOME_TAX_PAYABLE
``payroll_social_insurance_payable``                 liability  payroll.engine.SOCIAL_INSURANCE_PAYABLE
``payroll_other_deductions_payable``                 liability  payroll.engine.OTHER_DEDUCTIONS_PAYABLE
``bank_main``                                        asset      payroll.engine.DEFAULT_BANK
``gateway_clearing``                                 asset      payments.PaymentGatewayConfig.clearing_account
``bank_fees``                                        expense    payments.PaymentGatewayConfig.fee_account
``fx_gain_loss``                                     income     payments.PaymentApplication.fx_gain_loss_amount
===================================================  =========  ==============================

Convenience keys created as well (nothing resolves them today; they exist so
the demo seed and the API can find a default revenue account by role rather
than by code): ``sales_revenue``, ``service_revenue``.

ALIAS NOTE — read before "fixing" a key
---------------------------------------
``docs/03-data-model.md`` names five payroll roles without the ``payroll_``
prefix (``salaries_payable``, ``income_tax_payable``,
``social_insurance_payable``, ``other_deductions_payable``,
``employer_si_expense``) while ``apps/payroll/services/engine.py`` — the code
that actually performs the lookup — uses the prefixed form. Seeding *both*
would split one liability across two accounts, so exactly one account is
created per role, carrying the literal the engine passes, and
:data:`SYSTEM_KEY_ALIASES` records the other spelling. If a chart seeded by an
older revision already carries an alias key, the command **re-keys that
account in place** rather than creating a duplicate; no balance moves.

Idempotency
-----------
Everything is ``update_or_create``-shaped and keyed on ``(tenant, code)`` for
accounts, ``(tenant, code)`` for journals and ``(tenant, start_date)`` for
periods — the same natural keys their unique constraints use. Re-running the
command is a no-op apart from refreshing names, and it never touches
``cached_balance``, ``is_active`` on an account a tenant has archived, or a
period whose status has moved away from OPEN.

The whole run is one ``transaction.atomic`` block: a chart that is half
seeded is worse than one that is absent, because the missing half only
surfaces at the first posting.
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from django.core.management.base import BaseCommand, CommandError

from apps.accounting.models import Account, AccountType, FiscalPeriod, FiscalYear, Journal
from apps.core.tenancy_context import tenant_context
from apps.tenancy.models import Tenant

#: Supported chart layouts. "EG" follows the Egyptian unified-chart digit
#: grouping; "GENERIC" is the 1000/2000/3000 layout described in
#: ``docs/03-data-model.md`` §5.2.
COUNTRIES = ("EG", "GENERIC")

#: doc / historical spelling -> the literal the service layer passes.
#: See the ALIAS NOTE in the module docstring.
SYSTEM_KEY_ALIASES: dict[str, str] = {
    "salaries_payable": "payroll_salaries_payable",
    "income_tax_payable": "payroll_income_tax_payable",
    "social_insurance_payable": "payroll_social_insurance_payable",
    "other_deductions_payable": "payroll_other_deductions_payable",
    "employer_si_expense": "payroll_employer_social_insurance_expense",
}


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """One node of the seeded chart.

    ``ref`` is an internal identifier used to wire parents together; it is not
    written to the database. ``system_key`` is written and is what automated
    postings resolve. Structural (header/group) nodes carry an empty
    ``system_key`` and ``is_postable=False`` — posting to a roll-up makes its
    balance ambiguous, which is why ``Account.is_postable`` exists.
    """

    ref: str
    name: str
    type: str
    codes: dict[str, str]
    parent_ref: Optional[str] = None
    system_key: str = ""
    is_postable: bool = True
    is_reconcilable: bool = False
    #: Arabic label, stored in ``description`` for the EG chart.
    name_ar: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def code_for(self, country: str) -> str:
        return self.codes.get(country) or self.codes["GENERIC"]


def _c(generic: str, eg: str) -> dict[str, str]:
    return {"GENERIC": generic, "EG": eg}


#: The chart itself: header (non-postable) -> group (non-postable) -> detail
#: (postable), three levels, exactly as ``docs/03-data-model.md`` §5.2
#: prescribes. Order matters only in that a parent must precede its children.
CHART: tuple[AccountSpec, ...] = (
    # --- 1 Assets ---------------------------------------------------------
    AccountSpec("hdr_assets", "Assets", AccountType.ASSET, _c("1000", "1"),
                is_postable=False, name_ar="الأصول"),
    # Carries a system_key even though it is a structural node: the
    # current/non-current split is a *fact about the chart* that
    # apps.reporting.services.kpis needs to compute working capital and the
    # quick ratio, and code ranges differ per national chart — which is the
    # whole reason system_key exists. It stays is_postable=False.
    AccountSpec("grp_current_assets", "Current assets", AccountType.ASSET,
                _c("1100", "11"), parent_ref="hdr_assets", is_postable=False,
                system_key="grp_current_assets",
                name_ar="الأصول المتداولة"),
    AccountSpec("bank_main", "Main bank account", AccountType.ASSET,
                _c("1110", "1110"), parent_ref="grp_current_assets",
                system_key="bank_main", is_reconcilable=True,
                name_ar="البنك — الحساب الجاري"),
    AccountSpec("cash_on_hand", "Cash on hand", AccountType.ASSET,
                _c("1120", "1120"), parent_ref="grp_current_assets",
                system_key="cash_on_hand", is_reconcilable=True,
                name_ar="النقدية بالخزينة"),
    AccountSpec("gateway_clearing", "Payment gateway clearing", AccountType.ASSET,
                _c("1130", "1130"), parent_ref="grp_current_assets",
                system_key="gateway_clearing", name_ar="حساب تسوية بوابات الدفع"),
    AccountSpec("ar_control", "Accounts receivable (control)", AccountType.ASSET,
                _c("1200", "1210"), parent_ref="grp_current_assets",
                system_key="ar_control", name_ar="العملاء"),
    AccountSpec("inventory_asset", "Inventory", AccountType.ASSET,
                _c("1300", "1310"), parent_ref="grp_current_assets",
                system_key="inventory_asset", name_ar="المخزون"),
    AccountSpec("work_in_progress", "Work in progress", AccountType.ASSET,
                _c("1350", "1320"), parent_ref="grp_current_assets",
                system_key="work_in_progress", name_ar="إنتاج تحت التشغيل"),
    AccountSpec("input_vat", "Input VAT (recoverable)", AccountType.ASSET,
                _c("1400", "1410"), parent_ref="grp_current_assets",
                system_key="input_vat", name_ar="ضريبة القيمة المضافة — مشتريات"),

    # --- 2 Liabilities ----------------------------------------------------
    AccountSpec("hdr_liabilities", "Liabilities", AccountType.LIABILITY,
                _c("2000", "2"), is_postable=False, name_ar="الالتزامات"),
    AccountSpec("grp_current_liabilities", "Current liabilities",
                AccountType.LIABILITY, _c("2100", "21"),
                parent_ref="hdr_liabilities", is_postable=False,
                system_key="grp_current_liabilities",
                name_ar="الالتزامات المتداولة"),
    AccountSpec("ap_control", "Accounts payable (control)", AccountType.LIABILITY,
                _c("2110", "2110"), parent_ref="grp_current_liabilities",
                system_key="ap_control", name_ar="الموردون"),
    AccountSpec("output_vat", "Output VAT (collected)", AccountType.LIABILITY,
                _c("2200", "2210"), parent_ref="grp_current_liabilities",
                system_key="output_vat", name_ar="ضريبة القيمة المضافة — مبيعات"),
    AccountSpec("grp_payroll_liabilities", "Payroll liabilities",
                AccountType.LIABILITY, _c("2300", "23"),
                parent_ref="hdr_liabilities", is_postable=False,
                name_ar="التزامات الأجور"),
    AccountSpec("payroll_salaries_payable", "Salaries payable",
                AccountType.LIABILITY, _c("2310", "2310"),
                parent_ref="grp_payroll_liabilities",
                system_key="payroll_salaries_payable",
                aliases=("salaries_payable",), name_ar="أجور مستحقة"),
    AccountSpec("payroll_income_tax_payable", "Income tax payable",
                AccountType.LIABILITY, _c("2320", "2320"),
                parent_ref="grp_payroll_liabilities",
                system_key="payroll_income_tax_payable",
                aliases=("income_tax_payable",), name_ar="ضريبة كسب العمل المستحقة"),
    AccountSpec("payroll_social_insurance_payable", "Social insurance payable",
                AccountType.LIABILITY, _c("2330", "2330"),
                parent_ref="grp_payroll_liabilities",
                system_key="payroll_social_insurance_payable",
                aliases=("social_insurance_payable",),
                name_ar="التأمينات الاجتماعية المستحقة"),
    AccountSpec("payroll_other_deductions_payable", "Other payroll deductions payable",
                AccountType.LIABILITY, _c("2340", "2340"),
                parent_ref="grp_payroll_liabilities",
                system_key="payroll_other_deductions_payable",
                aliases=("other_deductions_payable",), name_ar="استقطاعات أخرى مستحقة"),
    # Money owed to staff for expenses they paid out of their own pocket.
    # Deliberately its own account rather than folded into salaries payable:
    # the two settle on different cycles (payroll runs monthly, expense
    # reimbursements when finance processes them) and a single balance that
    # mixes them cannot be reconciled against either. Read by
    # ``apps.expenses.services.posting``.
    AccountSpec("employee_reimbursements_payable", "Employee reimbursements payable",
                AccountType.LIABILITY, _c("2350", "2350"),
                parent_ref="grp_payroll_liabilities",
                system_key="employee_reimbursements_payable",
                name_ar="مصروفات مستحقة للموظفين"),

    # --- 3 Equity ---------------------------------------------------------
    AccountSpec("hdr_equity", "Equity", AccountType.EQUITY, _c("3000", "3"),
                is_postable=False, name_ar="حقوق الملكية"),
    AccountSpec("share_capital", "Share capital", AccountType.EQUITY,
                _c("3100", "3110"), parent_ref="hdr_equity",
                system_key="share_capital", name_ar="رأس المال"),
    AccountSpec("retained_earnings", "Retained earnings", AccountType.EQUITY,
                _c("3200", "3210"), parent_ref="hdr_equity",
                system_key="retained_earnings", name_ar="الأرباح المرحلة"),
    AccountSpec("opening_balance_equity", "Opening balance equity",
                AccountType.EQUITY, _c("3900", "3910"), parent_ref="hdr_equity",
                system_key="opening_balance_equity", name_ar="أرصدة افتتاحية"),

    # --- 4 Income ---------------------------------------------------------
    AccountSpec("hdr_income", "Income", AccountType.INCOME, _c("4000", "4"),
                is_postable=False, name_ar="الإيرادات"),
    AccountSpec("sales_revenue", "Sales revenue", AccountType.INCOME,
                _c("4100", "4110"), parent_ref="hdr_income",
                system_key="sales_revenue", name_ar="إيرادات المبيعات"),
    AccountSpec("service_revenue", "Service revenue", AccountType.INCOME,
                _c("4200", "4120"), parent_ref="hdr_income",
                system_key="service_revenue", name_ar="إيرادات الخدمات"),
    # Contra-revenue: debited, deliberately typed INCOME so the P&L nets it
    # against gross sales instead of inflating operating expenses.
    AccountSpec("sales_discount", "Sales discounts (contra-revenue)",
                AccountType.INCOME, _c("4900", "4190"), parent_ref="hdr_income",
                system_key="sales_discount", name_ar="خصم مسموح به"),

    # --- 5 Cost of sales --------------------------------------------------
    AccountSpec("hdr_cost_of_sales", "Cost of sales", AccountType.EXPENSE,
                _c("5000", "5"), is_postable=False, name_ar="تكلفة المبيعات"),
    AccountSpec("cogs", "Cost of goods sold", AccountType.EXPENSE,
                _c("5100", "5110"), parent_ref="hdr_cost_of_sales",
                system_key="cogs", name_ar="تكلفة البضاعة المباعة"),
    AccountSpec("inventory_adjustment", "Inventory adjustment (shrinkage / gain)",
                AccountType.EXPENSE, _c("5200", "5120"),
                parent_ref="hdr_cost_of_sales", system_key="inventory_adjustment",
                name_ar="تسويات المخزون"),

    # --- 6 Operating expenses --------------------------------------------
    AccountSpec("hdr_operating_expenses", "Operating expenses", AccountType.EXPENSE,
                _c("6000", "6"), is_postable=False, name_ar="المصروفات التشغيلية"),
    AccountSpec("grp_payroll_expenses", "Payroll expenses", AccountType.EXPENSE,
                _c("6100", "61"), parent_ref="hdr_operating_expenses",
                is_postable=False, name_ar="مصروفات الأجور"),
    AccountSpec("payroll_salary_expense", "Salaries and wages", AccountType.EXPENSE,
                _c("6110", "6110"), parent_ref="grp_payroll_expenses",
                system_key="payroll_salary_expense", name_ar="الأجور والمرتبات"),
    AccountSpec("payroll_employer_social_insurance_expense",
                "Employer social insurance contribution", AccountType.EXPENSE,
                _c("6120", "6120"), parent_ref="grp_payroll_expenses",
                system_key="payroll_employer_social_insurance_expense",
                aliases=("employer_si_expense",),
                name_ar="حصة صاحب العمل في التأمينات"),
    AccountSpec("grp_admin_expenses", "General and administrative",
                AccountType.EXPENSE, _c("6500", "65"),
                parent_ref="hdr_operating_expenses", is_postable=False,
                name_ar="مصروفات عمومية وإدارية"),
    AccountSpec("bad_debt_expense", "Bad debt expense", AccountType.EXPENSE,
                _c("6510", "6510"), parent_ref="grp_admin_expenses",
                system_key="bad_debt_expense", name_ar="ديون معدومة"),
    AccountSpec("bank_fees", "Bank and payment processing fees", AccountType.EXPENSE,
                _c("6520", "6520"), parent_ref="grp_admin_expenses",
                system_key="bank_fees", name_ar="مصروفات بنكية"),
    AccountSpec("office_expense", "Office and general expense", AccountType.EXPENSE,
                _c("6530", "6530"), parent_ref="grp_admin_expenses",
                system_key="office_expense", name_ar="مصروفات مكتبية"),

    # --- 8 Other income and expense --------------------------------------
    AccountSpec("hdr_other", "Other income and expense", AccountType.INCOME,
                _c("8000", "8"), is_postable=False, name_ar="إيرادات ومصروفات أخرى"),
    # One account carrying both directions: a separate gain and loss pair
    # makes the net FX result a two-account subtraction on every report, and
    # the two are the same economic event measured on different days.
    AccountSpec("fx_gain_loss", "Foreign exchange gain / (loss)", AccountType.INCOME,
                _c("8100", "8110"), parent_ref="hdr_other",
                system_key="fx_gain_loss", name_ar="فروق عملة"),
)

#: ``(code, name, kind, sequence_prefix)``. The codes are the ones services
#: hard-code: ``invoice_workflow.SALES_JOURNAL_CODE == "SAL"``, and the
#: payroll/inventory services resolve theirs by ``kind``.
JOURNALS: tuple[tuple[str, str, str, str], ...] = (
    ("SAL", "Sales journal", Journal.Kind.SALES, "SAL"),
    ("PUR", "Purchases journal", Journal.Kind.PURCHASE, "PUR"),
    ("CASH", "Cash and bank journal", Journal.Kind.CASH, "CSH"),
    ("PAY", "Payroll journal", Journal.Kind.PAYROLL, "PAY"),
    ("INV", "Inventory journal", Journal.Kind.INVENTORY, "INV"),
    ("GEN", "General journal", Journal.Kind.GENERAL, "GEN"),
)

#: The journal each system account defaults to, by ``ref``. Only set where it
#: removes a decision from the user; the rest stay NULL.
JOURNAL_DEFAULT_ACCOUNT: dict[str, str] = {
    "CASH": "bank_main",
    "SAL": "ar_control",
    "PUR": "ap_control",
    "PAY": "payroll_salary_expense",
    "INV": "inventory_asset",
}


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _add_months(anchor: date, months: int) -> date:
    """First day of the month ``months`` after ``anchor``'s month."""
    index = (anchor.year * 12 + (anchor.month - 1)) + months
    return date(index // 12, index % 12 + 1, 1)


class Command(BaseCommand):
    help = (
        "Seed a tenant's chart of accounts (every system_key the codebase "
        "resolves), its journals and one fiscal year of monthly periods. "
        "Idempotent."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--tenant",
            required=True,
            help="Tenant slug or UUID. The chart is per tenant; there is no global one.",
        )
        parser.add_argument(
            "--country",
            default=None,
            choices=[*COUNTRIES, *(c.lower() for c in COUNTRIES)],
            help="Chart layout. Defaults to 'EG' when Tenant.country == 'EG', "
                 "else 'GENERIC'.",
        )
        parser.add_argument(
            "--year",
            type=int,
            default=None,
            help="Fiscal year to create periods for. Defaults to the current year.",
        )
        parser.add_argument(
            "--skip-calendar",
            action="store_true",
            help="Seed accounts and journals only; leave the fiscal calendar alone.",
        )

    # -- entry point ------------------------------------------------------

    def handle(self, *args, **options) -> None:
        tenant = self._resolve_tenant(options["tenant"])
        country = (options["country"] or "").upper() or (
            "EG" if (tenant.country or "").upper() == "EG" else "GENERIC"
        )
        if country not in COUNTRIES:
            raise CommandError(f"Unsupported country '{country}'. Choose one of {COUNTRIES}.")

        year = options["year"] or date.today().year

        # One transaction for the whole chart: a partially seeded chart is
        # worse than an absent one, because the missing half only surfaces at
        # the first posting, in front of a user.
        with tenant_context(tenant.id):

            realigned = self._realign_aliases(tenant)
            accounts, created_accounts, updated_accounts = self._seed_accounts(
                tenant, country
            )
            created_journals, updated_journals = self._seed_journals(tenant, accounts)
            if options["skip_calendar"]:
                fiscal_year = None
                created_periods = 0
            else:
                fiscal_year, created_periods = self._seed_calendar(tenant, year)

            self._assert_system_keys(tenant)

        if int(options.get("verbosity", 1)) == 0:
            return
        self._report(
            tenant=tenant,
            country=country,
            realigned=realigned,
            created_accounts=created_accounts,
            updated_accounts=updated_accounts,
            created_journals=created_journals,
            updated_journals=updated_journals,
            fiscal_year=fiscal_year,
            created_periods=created_periods,
        )

    # -- steps ------------------------------------------------------------

    def _resolve_tenant(self, reference: str) -> Tenant:
        try:
            tenant_id = uuid.UUID(str(reference))
        except (ValueError, AttributeError, TypeError):
            tenant = Tenant.objects.filter(slug=reference).first()
        else:
            tenant = Tenant.objects.filter(pk=tenant_id).first()
        if tenant is None:
            raise CommandError(f"No tenant matches '{reference}'.")
        return tenant

    def _realign_aliases(self, tenant: Tenant) -> list[str]:
        """Re-key accounts seeded under a doc-spelling alias.

        Creating a second account for the alias would split one liability
        across two rows, so the existing account is renamed to the literal the
        service layer resolves. No balance moves; only ``system_key`` changes.
        """
        realigned: list[str] = []
        for alias, canonical in SYSTEM_KEY_ALIASES.items():
            stale = Account.all_tenants.filter(
                tenant_id=tenant.id, system_key=alias
            ).first()
            if stale is None:
                continue
            if Account.all_tenants.filter(
                tenant_id=tenant.id, system_key=canonical
            ).exclude(pk=stale.pk).exists():
                raise CommandError(
                    f"Tenant {tenant.slug} has both '{alias}' and '{canonical}' "
                    f"mapped to different accounts. Merge them by hand — this "
                    f"command will not decide which balance is real."
                )
            Account.all_tenants.filter(pk=stale.pk).update(system_key=canonical)
            realigned.append(f"{alias} -> {canonical}")
        return realigned

    def _seed_accounts(
        self, tenant: Tenant, country: str
    ) -> tuple[dict[str, Account], int, int]:
        by_ref: dict[str, Account] = {}
        created = updated = 0

        for spec in CHART:
            code = spec.code_for(country)
            parent = by_ref[spec.parent_ref] if spec.parent_ref else None
            defaults = {
                "name": spec.name,
                "type": spec.type,
                "parent": parent,
                "system_key": spec.system_key,
                "is_postable": spec.is_postable,
                "is_reconcilable": spec.is_reconcilable,
                "description": spec.name_ar if country == "EG" else "",
            }
            account = Account.all_tenants.filter(
                tenant_id=tenant.id, code=code
            ).first()
            if account is None:
                # An earlier run may have used a different chart layout; match
                # on the role before deciding it is missing.
                if spec.system_key:
                    account = Account.all_tenants.filter(
                        tenant_id=tenant.id, system_key=spec.system_key
                    ).first()

            if account is None:
                account = Account(tenant=tenant, code=code, **defaults)
                account.save()
                created += 1
            else:
                account.code = code
                for name, value in defaults.items():
                    setattr(account, name, value)
                # ``is_active`` and ``cached_balance`` are deliberately absent
                # from ``defaults``: a tenant who archived an account they do
                # not use must not have it resurrected by a re-run, and a
                # balance is never rewritten by anything but the posting
                # service.
                account.save(update_fields=["code", *defaults.keys(), "updated_at"])
                updated += 1
            by_ref[spec.ref] = account

        return by_ref, created, updated

    def _seed_journals(
        self, tenant: Tenant, accounts: dict[str, Account]
    ) -> tuple[int, int]:
        created = updated = 0
        for code, name, kind, prefix in JOURNALS:
            default_ref = JOURNAL_DEFAULT_ACCOUNT.get(code)
            default_account = accounts.get(default_ref) if default_ref else None
            journal, was_created = Journal.all_tenants.update_or_create(
                tenant_id=tenant.id,
                code=code,
                defaults={
                    "name": name,
                    "kind": kind,
                    "sequence_prefix": prefix,
                    "default_account": default_account,
                },
            )
            created += int(was_created)
            updated += int(not was_created)
        return created, updated

    def _seed_calendar(self, tenant: Tenant, year: int) -> tuple[FiscalYear, int]:
        """One fiscal year plus its twelve monthly periods.

        The year starts at ``Tenant.fiscal_year_start_month``, so a July–June
        tenant gets July..June and not January..December — periods are the unit
        the books are locked at, and a calendar that disagrees with the
        tenant's year makes every close land in the wrong place.
        """
        start_month = tenant.fiscal_year_start_month or 1
        start = date(year, start_month, 1)
        end = _add_months(start, 12) - timedelta(days=1)

        fiscal_year, _ = FiscalYear.all_tenants.update_or_create(
            tenant_id=tenant.id,
            name=f"FY{year}",
            defaults={"start_date": start, "end_date": end},
        )

        created = 0
        for offset in range(12):
            period_start = _add_months(start, offset)
            period_end = _month_end(period_start.year, period_start.month)
            existing = FiscalPeriod.all_tenants.filter(
                tenant_id=tenant.id, start_date=period_start
            ).first()
            if existing is not None:
                # Never reopen: a period the tenant has closed stays closed.
                continue
            FiscalPeriod.all_tenants.create(
                tenant_id=tenant.id,
                fiscal_year=fiscal_year,
                name=period_start.strftime("%Y-%m"),
                start_date=period_start,
                end_date=period_end,
                status=FiscalPeriod.Status.OPEN,
            )
            created += 1
        return fiscal_year, created

    def _assert_system_keys(self, tenant: Tenant) -> None:
        """Refuse to finish with a role missing.

        Provisioning that "mostly" succeeded is how a tenant discovers at
        month-end that payroll cannot post.
        """
        required = {spec.system_key for spec in CHART if spec.system_key}
        present = set(
            Account.all_tenants.filter(tenant_id=tenant.id)
            .exclude(system_key="")
            .values_list("system_key", flat=True)
        )
        missing = sorted(required - present)
        if missing:
            raise CommandError(
                f"Chart seeded but these system keys are still missing for "
                f"{tenant.slug}: {missing}. Refusing to commit a chart that "
                f"cannot post."
            )

    # -- output -----------------------------------------------------------

    def _report(self, **ctx) -> None:
        tenant = ctx["tenant"]
        write = self.stdout.write
        write(self.style.MIGRATE_HEADING(
            f"Chart of accounts — {tenant.name} ({tenant.slug}), layout {ctx['country']}"
        ))
        for line in ctx["realigned"]:
            write(self.style.WARNING(f"  re-keyed alias  {line}"))
        write(f"  accounts   created {ctx['created_accounts']:>3}  "
              f"updated {ctx['updated_accounts']:>3}  "
              f"total {len(CHART):>3}")
        write(f"  journals   created {ctx['created_journals']:>3}  "
              f"updated {ctx['updated_journals']:>3}")
        if ctx["fiscal_year"] is not None:
            write(f"  calendar   {ctx['fiscal_year'].name} "
                  f"({ctx['fiscal_year'].start_date} .. {ctx['fiscal_year'].end_date}), "
                  f"{ctx['created_periods']} new monthly period(s)")
        else:
            write("  calendar   skipped (--skip-calendar)")
        write(self.style.SUCCESS(
            f"  system keys {len([s for s in CHART if s.system_key])} present; "
            f"the ledger is postable."
        ))
