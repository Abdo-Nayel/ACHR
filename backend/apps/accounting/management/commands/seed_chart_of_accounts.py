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

The chart itself
----------------
One English, 5-level, positionally-coded tree, defined in
``apps.accounting.chart.english_chart`` (ported from the reference GL's Egyptian
starter template and extended with the roles above). ``--country`` is still
accepted for backward compatibility but no longer branches the layout.

Idempotency
-----------
The chart is keyed on ``(tenant, full_code)`` — an account's full code is fixed
by its place in the tree — and journals/periods on ``(tenant, code)`` /
``(tenant, start_date)``, the natural keys their unique constraints use.
Re-running refreshes names/roles only; it never touches ``cached_balance``,
``is_active`` on an account a tenant has archived, or a period whose status has
moved away from OPEN.

The whole run is one ``transaction.atomic`` block: a chart that is half
seeded is worse than one that is absent, because the missing half only
surfaces at the first posting.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from apps.accounting.chart.english_chart import (
    build_default_chart,
    required_system_keys,
)
from apps.accounting.models import Account, FiscalPeriod, FiscalYear, Journal
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
        """Build the default English 5-level coded chart.

        ``country`` is accepted for backward compatibility but no longer
        branches the layout: the chart is one English, positionally-coded tree
        (``apps.accounting.chart.english_chart``). The returned dict is keyed by
        ``system_key`` so :meth:`_seed_journals` can wire default accounts by
        role.
        """
        counts, by_key = build_default_chart(tenant.id)
        return by_key, counts["created"], counts["updated"]

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
        required = required_system_keys()
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
              f"total {ctx['created_accounts'] + ctx['updated_accounts']:>3}")
        write(f"  journals   created {ctx['created_journals']:>3}  "
              f"updated {ctx['updated_journals']:>3}")
        if ctx["fiscal_year"] is not None:
            write(f"  calendar   {ctx['fiscal_year'].name} "
                  f"({ctx['fiscal_year'].start_date} .. {ctx['fiscal_year'].end_date}), "
                  f"{ctx['created_periods']} new monthly period(s)")
        else:
            write("  calendar   skipped (--skip-calendar)")
        write(self.style.SUCCESS(
            f"  system keys {len(required_system_keys())} present; "
            f"the ledger is postable."
        ))
