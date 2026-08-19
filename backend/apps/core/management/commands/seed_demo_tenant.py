"""Provision one complete, working demo tenant — the fixture a new developer runs.

    python manage.py seed_demo_tenant --name "Acme Trading" --country EG --currency EGP

What "complete" means here is deliberate: every object created below is one
that some *other* module refuses to work without. A tenant with a chart of
accounts but no ``SalaryRevision`` cannot run payroll; one with employees but
no ``TaxBracket`` cannot compute withholding; one with a three-level org chart
is the only kind that exercises the ``Department.path`` subtree logic the ABAC
layer depends on. So this command is also a smoke test of the whole write
path — it issues an invoice, applies a payment and calculates a payroll run
through the real services, not through fixtures.

Order of construction (each step needs the one above it)::

    Tenant -> chart of accounts + journals + fiscal calendar   (seed_chart_of_accounts)
           -> system roles                                     (seed_permissions)
           -> users + memberships + role assignments
           -> departments (3 levels: HQ / Engineering / Backend)
           -> job titles, work schedule, employees, salary revisions
           -> payroll components, employee profiles, tax brackets
           -> customer, tax rate, items, warehouse, opening stock
           -> invoice (issued through issue_invoice -> posts to the GL)
           -> payment + application (recomputed through apply_payment)
           -> expense
           -> payroll run (calculated through calculate_run)

Everything runs inside ``tenant_context`` and one ``transaction.atomic``
block: a half-built demo tenant looks plausible and fails three modules later,
which costs far more time than a clean rollback.

Idempotency
-----------
Re-running against an existing slug is refused unless ``--reset`` is passed,
because the documents this command creates are ``ImmutableFinancialModel``
rows — they cannot be deleted, and re-issuing an invoice against the same
sequence would look like tampering. ``--reset`` creates a *new* slug
(``acme-2`` …) instead of destroying anything.

Notes on what this command exercises
------------------------------------
* The demo invoice uses free-text service lines (``item=None``). This was
  once justified by a claim that
  ``apps.inventory.services.stock.issue_stock`` "does not exist in this
  revision" — it does: ``fulfilment`` defines it and ``stock`` re-exports it
  (see the block at the foot of ``stock.py``). ``tests/
  test_invoice_stock_integration.py`` now covers that path end to end —
  stock decrement, movement, COGS at cost. Switching this command's invoice
  to an item line would be a reasonable next step; it is left as-is only so
  that the demo tenant keeps a service-invoice example alongside the stock
  data it already seeds.
* The demo expense is created in ``APPROVED`` state directly, so it carries no
  journal entry. ``apps.expenses.services.posting`` now exists and the
  ``approve`` transition posts through it, but this command writes the row
  rather than driving the HTTP transition, so nothing calls the service. Use
  ``POST /api/v1/expenses/{id}/approve`` to see the accrual.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounting.models import Account, TaxRate
from apps.core.fields import ZERO, quantize_currency
from apps.core.models import Currency
from apps.core.tenancy_context import bind_database_session, tenant_context
from apps.expenses.models import Expense, ExpenseCategory
from apps.hr.models import Department, Employee, JobTitle, SalaryRevision, WorkSchedule
from apps.iam.models import Role, RoleAssignment, TenantMembership, User
from apps.inventory.models import (
    Item,
    ItemCategory,
    StockMovement,
    UnitOfMeasure,
    Warehouse,
)
from apps.inventory.services.stock import apply_movement
from apps.payments.models import Payment, PaymentApplication
from apps.payroll.models import (
    EmployeeComponent,
    EmployeePayrollProfile,
    PayrollComponent,
    PayrollRun,
    TaxBracket,
)
from apps.payroll.services.engine import calculate_run
from apps.sales.models import Customer, Invoice, InvoiceLine
from apps.sales.services.invoice_workflow import apply_payment, issue_invoice
from apps.tenancy.models import Tenant

DEFAULT_PASSWORD = "demo-password-not-for-production"

#: Egyptian personal income tax scale, annual basis, expressed as fractions.
#: Stored per tenant (``TaxBracket`` is tenant-scoped) because a group may be
#: told by its auditor to apply a different published scale.
EG_BRACKETS: tuple[tuple[str, Optional[str], str], ...] = (
    ("0", "40000", "0"),
    ("40000", "55000", "0.100000"),
    ("55000", "70000", "0.150000"),
    ("70000", "200000", "0.200000"),
    ("200000", "400000", "0.225000"),
    ("400000", "1200000", "0.250000"),
    ("1200000", None, "0.275000"),
)

#: A deliberately plain three-slab scale for non-EG demos, so that the
#: marginal arithmetic is easy to verify by hand.
GENERIC_BRACKETS: tuple[tuple[str, Optional[str], str], ...] = (
    ("0", "50000", "0"),
    ("50000", "150000", "0.150000"),
    ("150000", None, "0.250000"),
)


class Command(BaseCommand):
    help = (
        "Create a complete demo tenant: chart of accounts, roles, org chart, "
        "employees, an issued invoice, a payment, an expense and a calculated "
        "payroll run."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--name", default="Acme Trading")
        parser.add_argument("--slug", default=None, help="Defaults to a slug of --name.")
        parser.add_argument("--country", default="EG", help="ISO-3166 alpha-2.")
        parser.add_argument("--currency", default=Currency.EGP)
        parser.add_argument("--owner-email", default=None)
        parser.add_argument("--password", default=DEFAULT_PASSWORD)
        parser.add_argument(
            "--reset",
            action="store_true",
            help="If the slug is taken, create the next free one instead of failing. "
                 "Nothing is ever deleted: the documents here are immutable.",
        )

    # -- entry point ------------------------------------------------------

    def handle(self, *args, **options) -> None:
        country = (options["country"] or "EG").upper()
        currency = (options["currency"] or Currency.EGP).upper()
        if currency not in Currency.values:
            raise CommandError(
                f"Currency '{currency}' is not in apps.core.models.Currency. "
                f"Add it there first — an unlisted currency would fail the "
                f"choices validation on every monetary model."
            )

        slug = self._pick_slug(options["slug"] or _slugify(options["name"]), options["reset"])
        today = timezone.localdate()

        with transaction.atomic():
            tenant = Tenant.objects.create(
                name=options["name"],
                legal_name=f"{options['name']} LLC",
                slug=slug,
                status=Tenant.Status.ACTIVE,
                country=country,
                timezone="Africa/Cairo" if country == "EG" else "UTC",
                base_currency=currency,
                tax_registration_number="100-200-300",
                fiscal_year_start_month=1,
                # Rates are strings on purpose: a JSON float here is rejected
                # by ``engine._rate_from_settings`` rather than silently
                # reintroducing binary floating point into payroll.
                settings={
                    "payroll": {
                        "social_insurance_employee_rate": "0.110000",
                        "social_insurance_employer_rate": "0.187500",
                    },
                    "demo": True,
                },
            )

        # The chart, the journals and the fiscal calendar must exist before
        # anything can post; the permission catalogue before anyone can be
        # granted a role.
        call_command(
            "seed_chart_of_accounts",
            tenant=str(tenant.id),
            country="EG" if country == "EG" else "GENERIC",
            year=today.year,
        )
        call_command("seed_permissions")

        owner_email = options["owner_email"] or f"owner@{slug}.example.com"
        owner = self._user(owner_email, "Owner Demo", options["password"])
        accountant = self._user(
            f"accountant@{slug}.example.com", "Nadia Accountant", options["password"]
        )
        staff = self._user(
            f"employee@{slug}.example.com", "Karim Employee", options["password"]
        )

        with tenant_context(tenant.id, owner.id), transaction.atomic():
            # RLS reads ``app.current_tenant``; ``SET LOCAL`` needs this
            # transaction, so the bind happens after atomic() opens.
            bind_database_session(tenant.id)

            accounts = self._accounts(tenant)
            departments = self._departments(tenant, owner)
            schedule = self._work_schedule(tenant, country)
            titles = self._job_titles(tenant, departments, currency)
            employees = self._employees(
                tenant, departments, titles, schedule, currency, today
            )
            self._memberships(tenant, owner, accountant, staff, employees, departments)
            self._tax_brackets(tenant, country, currency, today)
            components = self._payroll_components(tenant, accounts, currency)
            self._payroll_profiles(tenant, employees, components, currency, today)

            tax_rate = self._tax_rate(tenant, accounts, country)
            customer = self._customer(tenant, accounts, currency)
            items = self._catalogue(tenant, accounts, currency)
            warehouse = self._warehouse(tenant)
            self._opening_stock(tenant, items, warehouse, owner)

            invoice = self._invoice(
                tenant, customer, accounts, tax_rate, currency, owner, today
            )
            payment = self._payment(
                tenant, customer, invoice, accounts, currency, owner, today
            )
            expense = self._expense(tenant, accounts, employees, currency, owner, today)
            run = self._payroll_run(tenant, currency, owner, today)

        self._report(
            tenant=tenant,
            owner=owner,
            accountant=accountant,
            staff=staff,
            password=options["password"],
            invoice=invoice,
            payment=payment,
            expense=expense,
            run=run,
        )

    # -- tenant / users ---------------------------------------------------

    def _pick_slug(self, base: str, reset: bool) -> str:
        if not Tenant.objects.filter(slug=base).exists():
            return base
        if not reset:
            raise CommandError(
                f"Tenant slug '{base}' already exists. This command creates "
                f"immutable financial documents, so it will not re-seed on top "
                f"of them. Pass --reset to create the next free slug, or --slug."
            )
        for suffix in range(2, 100):
            candidate = f"{base}-{suffix}"
            if not Tenant.objects.filter(slug=candidate).exists():
                return candidate
        raise CommandError(f"Could not find a free slug based on '{base}'.")

    def _user(self, email: str, full_name: str, password: str) -> User:
        user = User.objects.filter(email=email.lower()).first()
        if user is not None:
            return user
        return User.objects.create_user(
            email=email, password=password, full_name=full_name, is_active=True
        )

    def _memberships(
        self,
        tenant: Tenant,
        owner: User,
        accountant: User,
        staff: User,
        employees: dict[str, Employee],
        departments: dict[str, Department],
    ) -> None:
        """Three memberships, three different shapes of authority.

        The employee membership is linked to an ``hr.Employee`` and the
        accountant's is not — that asymmetry is the point of the model (see
        ``hr.Employee``'s docstring): not every login is an employee.
        The department manager assignment is *scoped* to the Engineering
        subtree, which is what makes ``DEPARTMENT_SUBTREE`` exercisable.
        """
        roles = {
            role.code: role
            for role in Role.objects.filter(tenant__isnull=True, is_system=True)
        }
        missing = {"owner", "accountant", "employee", "department_manager"} - roles.keys()
        if missing:
            raise CommandError(
                f"System roles {sorted(missing)} are absent. Run "
                f"`manage.py seed_permissions` first."
            )

        owner_membership, _ = TenantMembership.objects.get_or_create(
            tenant=tenant, user=owner,
            defaults={"is_owner": True, "invitation_accepted_at": timezone.now()},
        )
        accountant_membership, _ = TenantMembership.objects.get_or_create(
            tenant=tenant, user=accountant,
            defaults={"invitation_accepted_at": timezone.now()},
        )
        staff_membership, _ = TenantMembership.objects.get_or_create(
            tenant=tenant, user=staff,
            defaults={
                "employee": employees["engineer"],
                "invitation_accepted_at": timezone.now(),
            },
        )

        RoleAssignment.objects.get_or_create(
            membership=owner_membership, role=roles["owner"],
            department=None, project=None,
        )
        RoleAssignment.objects.get_or_create(
            membership=accountant_membership, role=roles["accountant"],
            department=None, project=None,
        )
        RoleAssignment.objects.get_or_create(
            membership=staff_membership, role=roles["employee"],
            department=None, project=None,
        )
        # Scoped to a subtree, not to the whole tenant: this is the assignment
        # ``ScopeRule.Strategy.DEPARTMENT_SUBTREE`` compiles against.
        RoleAssignment.objects.get_or_create(
            membership=accountant_membership, role=roles["department_manager"],
            department=departments["engineering"], project=None,
        )

    # -- accounting -------------------------------------------------------

    def _accounts(self, tenant: Tenant) -> dict[str, Account]:
        """Index the seeded chart by ``system_key`` — never by code."""
        by_key = {
            account.system_key: account
            for account in Account.all_tenants.filter(tenant_id=tenant.id).exclude(
                system_key=""
            )
        }
        required = {
            "ar_control", "bank_main", "output_vat", "input_vat", "inventory_asset",
            "cogs", "sales_revenue", "service_revenue", "office_expense",
            "opening_balance_equity",
        }
        missing = sorted(required - by_key.keys())
        if missing:
            raise CommandError(
                f"Chart of accounts is missing {missing}; seed_chart_of_accounts "
                f"did not complete."
            )
        return by_key

    def _tax_rate(self, tenant: Tenant, accounts: dict[str, Account], country: str) -> TaxRate:
        rate = Decimal("0.140000") if country == "EG" else Decimal("0.200000")
        obj, _ = TaxRate.objects.get_or_create(
            code="VAT-STD",
            effective_from=date(timezone.localdate().year, 1, 1),
            defaults={
                "tenant": tenant,
                "name": f"Standard VAT {rate * 100:.0f}%",
                "rate": rate,
                "collected_account": accounts["output_vat"],
                "paid_account": accounts["input_vat"],
                "is_recoverable": True,
            },
        )
        return obj

    # -- org chart --------------------------------------------------------

    def _departments(self, tenant: Tenant, owner: User) -> dict[str, Department]:
        """Three levels, so ``path`` prefix matching has something to match.

        ``/hq/`` -> ``/hq/eng/`` -> ``/hq/eng/backend/``. A two-level chart
        would let a broken subtree query pass by accident: with one level of
        nesting, "my department" and "my subtree" return the same rows.
        """
        hq = Department.objects.create(
            tenant=tenant, code="hq", name="Head office", parent=None,
            depth=0, created_by=owner,
        )
        hq.path = hq.build_path()
        hq.save(update_fields=["path", "updated_at"])

        engineering = Department.objects.create(
            tenant=tenant, code="eng", name="Engineering", parent=hq,
            depth=1, created_by=owner,
        )
        engineering.path = engineering.build_path()
        engineering.save(update_fields=["path", "updated_at"])

        backend = Department.objects.create(
            tenant=tenant, code="backend", name="Backend team", parent=engineering,
            depth=2, created_by=owner,
        )
        backend.path = backend.build_path()
        backend.save(update_fields=["path", "updated_at"])

        finance = Department.objects.create(
            tenant=tenant, code="fin", name="Finance", parent=hq,
            depth=1, created_by=owner,
        )
        finance.path = finance.build_path()
        finance.save(update_fields=["path", "updated_at"])

        return {
            "hq": hq,
            "engineering": engineering,
            "backend": backend,
            "finance": finance,
        }

    def _work_schedule(self, tenant: Tenant, country: str) -> WorkSchedule:
        # Sunday–Thursday in Egypt, Monday–Friday elsewhere. ISO weekdays.
        working_days = [7, 1, 2, 3, 4] if country == "EG" else [1, 2, 3, 4, 5]
        schedule = WorkSchedule.objects.create(
            tenant=tenant,
            code="std",
            name="Standard week",
            working_days=working_days,
            expected_hours_per_week=Decimal("40"),
            is_default=True,
        )
        return schedule

    def _job_titles(
        self, tenant: Tenant, departments: dict[str, Department], currency: str
    ) -> dict[str, JobTitle]:
        return {
            "engineer": JobTitle.objects.create(
                tenant=tenant, code="ENG-2", name="Senior Backend Engineer",
                department=departments["backend"], grade="L4",
                min_salary=Decimal("18000.000000"),
                max_salary=Decimal("42000.000000"), currency=currency,
            ),
            "manager": JobTitle.objects.create(
                tenant=tenant, code="ENG-M", name="Engineering Manager",
                department=departments["engineering"], grade="L6",
                min_salary=Decimal("35000.000000"),
                max_salary=Decimal("70000.000000"), currency=currency,
            ),
            "accountant": JobTitle.objects.create(
                tenant=tenant, code="FIN-1", name="Accountant",
                department=departments["finance"], grade="L3",
                min_salary=Decimal("12000.000000"),
                max_salary=Decimal("28000.000000"), currency=currency,
            ),
        }

    def _employees(
        self,
        tenant: Tenant,
        departments: dict[str, Department],
        titles: dict[str, JobTitle],
        schedule: WorkSchedule,
        currency: str,
        today: date,
    ) -> dict[str, Employee]:
        hire_date = date(today.year - 2, 1, 15)

        manager = Employee.objects.create(
            tenant=tenant, employee_code="E-0001",
            first_name="Mona", last_name="Farid", arabic_name="منى فريد",
            department=departments["engineering"], job_title=titles["manager"],
            work_schedule=schedule, hire_date=hire_date,
            base_salary=Decimal("52000.000000"), salary_currency=currency,
            pay_frequency=Employee.PayFrequency.MONTHLY,
            status=Employee.Status.ACTIVE, work_email="mona.farid@example.com",
        )
        engineer = Employee.objects.create(
            tenant=tenant, employee_code="E-0002",
            first_name="Karim", last_name="Saleh", arabic_name="كريم صالح",
            department=departments["backend"], job_title=titles["engineer"],
            manager=manager, work_schedule=schedule, hire_date=hire_date,
            base_salary=Decimal("31000.000000"), salary_currency=currency,
            pay_frequency=Employee.PayFrequency.MONTHLY,
            status=Employee.Status.ACTIVE, work_email="karim.saleh@example.com",
            bank_account_iban="EG380019000500000000263180002",
            bank_name="Demo Bank",
        )
        accountant = Employee.objects.create(
            tenant=tenant, employee_code="E-0003",
            first_name="Nadia", last_name="Hassan", arabic_name="نادية حسن",
            department=departments["finance"], job_title=titles["accountant"],
            manager=manager, work_schedule=schedule, hire_date=hire_date,
            base_salary=Decimal("22000.000000"), salary_currency=currency,
            pay_frequency=Employee.PayFrequency.MONTHLY,
            status=Employee.Status.ACTIVE, work_email="nadia.hassan@example.com",
        )

        Department.objects.filter(pk=departments["engineering"].pk).update(manager=manager)
        Department.objects.filter(pk=departments["backend"].pk).update(manager=engineer)

        # THE payroll prerequisite: the engine reads effective-dated history,
        # never ``Employee.base_salary``. Without a HIRE revision on or before
        # the period end, ``effective_salary`` raises and the run fails.
        for employee in (manager, engineer, accountant):
            SalaryRevision.objects.create(
                tenant=tenant, employee=employee,
                change_type=SalaryRevision.ChangeType.HIRE,
                effective_date=hire_date,
                previous_salary=ZERO,
                new_salary=employee.base_salary,
                currency=currency,
                reason="Initial salary on hire.",
                approved_at=timezone.now(),
            )

        return {"manager": manager, "engineer": engineer, "accountant": accountant}

    # -- payroll configuration -------------------------------------------

    def _tax_brackets(
        self, tenant: Tenant, country: str, currency: str, today: date
    ) -> None:
        table = EG_BRACKETS if country == "EG" else GENERIC_BRACKETS
        effective_from = date(today.year, 1, 1)
        for sequence, (lower, upper, rate) in enumerate(table):
            TaxBracket.objects.get_or_create(
                tenant=tenant,
                country=country,
                effective_from=effective_from,
                lower_bound=Decimal(lower),
                defaults={
                    "upper_bound": Decimal(upper) if upper is not None else None,
                    "rate": Decimal(rate),
                    "fixed_deduction": ZERO,
                    "currency": currency,
                    "is_annual_basis": True,
                    "sequence": sequence,
                },
            )

    def _payroll_components(
        self, tenant: Tenant, accounts: dict[str, Account], currency: str
    ) -> dict[str, PayrollComponent]:
        """Two earnings and one deduction, in the sequence bands the model
        documents: earnings 100–499, statutory 500–799, voluntary 800–999."""
        housing = PayrollComponent.objects.create(
            tenant=tenant, code="HOUSING", name="Housing allowance",
            component_type=PayrollComponent.ComponentType.EARNING,
            calculation_type=PayrollComponent.CalculationType.PERCENTAGE_OF_BASE,
            rate=Decimal("0.100000"), currency=currency, sequence=110,
            expense_account=accounts["payroll_salary_expense"],
        )
        transport = PayrollComponent.objects.create(
            tenant=tenant, code="TRANSPORT", name="Transport allowance",
            component_type=PayrollComponent.ComponentType.EARNING,
            calculation_type=PayrollComponent.CalculationType.FIXED,
            amount=Decimal("800.000000"), currency=currency, sequence=120,
            is_subject_to_social_insurance=False,
            expense_account=accounts["payroll_salary_expense"],
        )
        loan = PayrollComponent.objects.create(
            tenant=tenant, code="LOAN", name="Staff loan repayment",
            component_type=PayrollComponent.ComponentType.DEDUCTION,
            calculation_type=PayrollComponent.CalculationType.FIXED,
            amount=Decimal("500.000000"), currency=currency, sequence=850,
            is_taxable=False, is_subject_to_social_insurance=False,
            liability_account=accounts["payroll_other_deductions_payable"],
        )
        return {"housing": housing, "transport": transport, "loan": loan}

    def _payroll_profiles(
        self,
        tenant: Tenant,
        employees: dict[str, Employee],
        components: dict[str, PayrollComponent],
        currency: str,
        today: date,
    ) -> None:
        effective_from = date(today.year, 1, 1)
        for employee in employees.values():
            EmployeePayrollProfile.objects.create(
                tenant=tenant, employee=employee, currency=currency,
                # Capped insurable wage: most schemes insure a statutory
                # maximum rather than actual pay.
                insurable_wage=Decimal("12600.000000"),
                tax_exemption_amount=Decimal("20000.000000"),
            )
            for code in ("housing", "transport"):
                EmployeeComponent.objects.create(
                    tenant=tenant, employee=employee,
                    component=components[code], effective_from=effective_from,
                )
        EmployeeComponent.objects.create(
            tenant=tenant, employee=employees["engineer"],
            component=components["loan"], effective_from=effective_from,
        )

    # -- sales / inventory ------------------------------------------------

    def _customer(
        self, tenant: Tenant, accounts: dict[str, Account], currency: str
    ) -> Customer:
        return Customer.objects.create(
            tenant=tenant, code="C-0001", name="Nile Retail Group",
            display_name="Nile Retail", email="ap@nile-retail.example.com",
            tax_number="200-300-400", currency=currency,
            receivable_account=accounts["ar_control"],
            payment_terms_days=30,
        )

    def _catalogue(
        self, tenant: Tenant, accounts: dict[str, Account], currency: str
    ) -> dict[str, Item]:
        uom = UnitOfMeasure.objects.create(
            tenant=tenant, code="EA", name="Each", symbol="ea",
            kind=UnitOfMeasure.Kind.UNIT, decimal_places=0,
        )
        hours = UnitOfMeasure.objects.create(
            tenant=tenant, code="HR", name="Hour", symbol="h",
            kind=UnitOfMeasure.Kind.TIME, decimal_places=2,
        )
        category = ItemCategory.objects.create(
            tenant=tenant, code="GEN", name="General",
            default_income_account=accounts["sales_revenue"],
            default_expense_account=accounts["cogs"],
            default_inventory_account=accounts["inventory_asset"],
        )
        widget = Item.objects.create(
            tenant=tenant, sku="SKU-001", name="Widget, blue", type=Item.Type.INVENTORY,
            uom=uom, category=category, currency=currency,
            sales_price=Decimal("450.000000"), purchase_price=Decimal("300.000000"),
            income_account=accounts["sales_revenue"],
            expense_account=accounts["cogs"],
            inventory_account=accounts["inventory_asset"],
            track_inventory=True,
            reorder_point=Decimal("20.000000"),
            reorder_quantity=Decimal("100.000000"),
        )
        consulting = Item.objects.create(
            tenant=tenant, sku="SVC-001", name="Consulting hour", type=Item.Type.SERVICE,
            uom=hours, category=category, currency=currency,
            sales_price=Decimal("1200.000000"),
            income_account=accounts["service_revenue"],
            expense_account=accounts["cogs"],
            # A SERVICE item must not be stocked; ``ck_item_service_not_stocked``
            # would reject the row otherwise.
            track_inventory=False,
        )
        return {"widget": widget, "consulting": consulting}

    def _warehouse(self, tenant: Tenant) -> Warehouse:
        return Warehouse.objects.create(
            tenant=tenant, code="WH-MAIN", name="Main warehouse",
            address="6th of October, Giza", is_default=True,
        )

    def _opening_stock(
        self,
        tenant: Tenant,
        items: dict[str, Item],
        warehouse: Warehouse,
        owner: User,
    ) -> None:
        """Opening balance, posted through the real service.

        ``Dr Inventory / Cr Opening balance equity`` — the balancing side of a
        migration. Going through ``apply_movement`` rather than writing a
        ``StockLevel`` row directly is the whole point: the demo data then has
        the same provenance (a movement, a level and a journal entry that all
        agree) as production data.
        """
        apply_movement(
            tenant_id=tenant.id,
            item=items["widget"],
            warehouse=warehouse,
            movement_type=StockMovement.MovementType.OPENING,
            quantity_delta=Decimal("200.000000"),
            unit_cost=Decimal("300.000000"),
            # ``apply_movement`` derives the journal entry date from this, so
            # it must land inside an OPEN period. "now" always does; a
            # back-dated opening balance would need the period reopened.
            occurred_at=timezone.now(),
            reference_type="seed",
            idempotency_key=f"seed:opening:{items['widget'].id}",
            notes="Demo opening stock.",
            user_id=owner.id,
        )

    def _invoice(
        self,
        tenant: Tenant,
        customer: Customer,
        accounts: dict[str, Account],
        tax_rate: TaxRate,
        currency: str,
        owner: User,
        today: date,
    ) -> Invoice:
        """A draft invoice, then issued through the real workflow.

        The lines are free-text (``item=None``) deliberately — see the "known
        gaps" note in the module docstring: ``_release_stock_for_invoice``
        imports a function this revision of the inventory service does not
        export, so a stock-bearing line cannot be issued yet.
        """
        issue_date = today.replace(day=min(today.day, 28))
        quantity = Decimal("10.000000")
        unit_price = Decimal("1200.000000")
        line_subtotal = quantize_currency(quantity * unit_price, currency)
        line_tax = quantize_currency(line_subtotal * tax_rate.rate, currency)
        line_total = line_subtotal + line_tax

        invoice = Invoice.objects.create(
            tenant=tenant, customer=customer, issue_date=issue_date,
            due_date=issue_date + timedelta(days=customer.payment_terms_days),
            currency=currency, exchange_rate=Decimal("1"),
            subtotal_amount=line_subtotal, discount_amount=ZERO,
            tax_amount=line_tax, total_amount=line_total,
            amount_paid=ZERO, amount_due=line_total,
            status=Invoice.Status.DRAFT, created_by=owner,
            notes="Demo invoice generated by seed_demo_tenant.",
        )
        InvoiceLine.objects.create(
            tenant=tenant, invoice=invoice, line_number=1, item=None,
            description="Consulting — implementation sprint",
            quantity=quantity, unit_price=unit_price, discount_rate=ZERO,
            tax_rate=tax_rate, line_subtotal=line_subtotal, line_tax=line_tax,
            line_total=line_total, income_account=accounts["service_revenue"],
        )
        return issue_invoice(invoice.id, tenant_id=tenant.id, user_id=owner.id)

    def _payment(
        self,
        tenant: Tenant,
        customer: Customer,
        invoice: Invoice,
        accounts: dict[str, Account],
        currency: str,
        owner: User,
        today: date,
    ) -> Payment:
        """A part payment, applied through ``apply_payment``.

        The invoice's ``amount_paid`` is *recomputed* from the applications
        rather than incremented — that is the service's contract, and the
        demo data must not contradict it.
        """
        amount = quantize_currency(invoice.total_amount / Decimal("2"), currency)
        payment = Payment.objects.create(
            tenant=tenant, customer=customer, number=f"PMT-{today.year}-000001",
            payment_date=today, currency=currency, amount=amount,
            unapplied_amount=ZERO, fee_amount=ZERO,
            method=Payment.Method.BANK_TRANSFER,
            status=Payment.Status.SETTLED,
            deposit_account=accounts["bank_main"],
            reference="Demo wire", created_by=owner,
            settled_at=timezone.now(),
        )
        PaymentApplication.objects.create(
            tenant=tenant, payment=payment, invoice=invoice,
            amount=amount, applied_on=today, created_by=owner,
        )
        apply_payment(invoice.id, tenant_id=tenant.id, user_id=owner.id)
        return payment

    def _expense(
        self,
        tenant: Tenant,
        accounts: dict[str, Account],
        employees: dict[str, Employee],
        currency: str,
        owner: User,
        today: date,
    ) -> Expense:
        category = ExpenseCategory.objects.create(
            tenant=tenant, code="OFFICE", name="Office supplies",
            expense_account=accounts["office_expense"],
            approval_threshold_amount=Decimal("5000.000000"),
        )
        amount = Decimal("1500.000000")
        tax = quantize_currency(amount * Decimal("0.140000"), currency)
        return Expense.objects.create(
            tenant=tenant, number=f"EXP-{today.year}-000001", category=category,
            description="Office chairs", expense_date=today, currency=currency,
            amount=amount, tax_amount=tax, total_amount=amount + tax,
            payment_method=Expense.PaymentMethod.COMPANY_CARD,
            paid_from_account=accounts["bank_main"],
            status=Expense.Status.APPROVED,
            submitted_at=timezone.now(), approved_at=timezone.now(),
            approved_by=owner, created_by=owner,
        )

    def _payroll_run(
        self, tenant: Tenant, currency: str, owner: User, today: date
    ) -> PayrollRun:
        """One monthly run, calculated (not approved, not posted).

        It stops at CALCULATED on purpose: approving it here would be the
        same user who calculated it, which ``approve_run`` correctly refuses
        (segregation of duties). A developer exercising the approval path
        needs two logins, and the demo tenant provides them.
        """
        period_start = today.replace(day=1)
        period_end = _month_end(period_start)
        run = PayrollRun.objects.create(
            tenant=tenant,
            name=f"Payroll {period_start:%B %Y}",
            period_start=period_start,
            period_end=period_end,
            pay_date=period_end,
            frequency=PayrollRun.Frequency.MONTHLY,
            currency=currency,
            created_by=owner,
        )
        return calculate_run(run, user_id=owner.id)

    # -- output -----------------------------------------------------------

    def _report(self, **ctx) -> None:
        tenant: Tenant = ctx["tenant"]
        invoice: Invoice = ctx["invoice"]
        run: PayrollRun = ctx["run"]
        write = self.stdout.write

        write(self.style.MIGRATE_HEADING(f"Demo tenant '{tenant.name}' ready"))
        write(f"  slug            {tenant.slug}")
        write(f"  tenant id       {tenant.id}")
        write(f"  base currency   {tenant.base_currency}   country {tenant.country}")
        write("  logins (password below)")
        write(f"    owner         {ctx['owner'].email}")
        write(f"    accountant    {ctx['accountant'].email}  "
              f"(+ department_manager scoped to /hq/eng/)")
        write(f"    employee      {ctx['staff'].email}")
        write(f"  password        {ctx['password']}")
        write(f"  invoice         {invoice.number}  total {invoice.total_amount} "
              f"{invoice.currency}  due {invoice.amount_due}  status {invoice.status}")
        write(f"  payment         {ctx['payment'].number}  {ctx['payment'].amount}")
        write(f"  expense         {ctx['expense'].number}  "
              f"{ctx['expense'].total_amount}")
        write(f"  payroll run     {run.name}  status {run.status}  "
              f"{run.employee_count} payslips  gross {run.total_gross}  "
              f"net {run.total_net}")
        write(self.style.SUCCESS(
            "  Next: `manage.py runserver`, log in as the owner, and approve "
            "the payroll run as the accountant — the same user may not do both."
        ))


def _slugify(name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in name)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:63] or "demo"


def _month_end(day: date) -> date:
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])
