"""Shared fixtures.

Two properties of this system make its fixtures unusual, and both are
encoded here so that no individual test has to remember them:

1. **Nothing is visible without a tenant context.**
   ``TenantManager.get_queryset`` returns ``.none()`` when no tenant is
   bound, and ``TenantScopedModel.save`` raises ``PermissionDenied``. A test
   that forgets to bind therefore does not fail with "wrong data" — it fails
   with "no data", which reads like a broken fixture. The autouse
   :func:`bind_tenant` fixture removes the whole class of confusion by
   wrapping every test that asks for a ``tenant`` in ``tenant_context``.

2. **The database enforces more than the ORM does.**
   Row-Level Security, the balance trigger and the immutability triggers are
   PostgreSQL objects. Assertions about them are marked ``@pytest.mark.rls``
   and skip automatically on any other backend, so the suite still runs on a
   developer's SQLite scratch database without silently *passing* checks it
   never made.

Money in fixtures is always ``Decimal``. Never a float literal, not even
``0.0`` — see ``apps/core/fields.py``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Iterator, Optional

import pytest
from django.core.management import call_command
from django.db import connection

from apps.accounting.models import (
    Account,
    FiscalPeriod,
    FiscalYear,
    Journal,
    JournalEntry,
    JournalLine,
)
from apps.accounting.services.posting import JournalEntryDraft, LineDraft
from apps.core.models import Currency
from apps.core.tenancy_context import (
    bind_database_session,
    get_current_tenant_id,
    tenant_context,
)
from apps.iam.models import Role, RoleAssignment, TenantMembership, User
from apps.tenancy.models import Tenant

#: Every fixture below deals in this currency unless a test says otherwise.
#: Two minor units, so a rounding bug is visible at the cent.
TEST_CURRENCY = Currency.EGP


# ---------------------------------------------------------------------------
# Backend capability helpers
# ---------------------------------------------------------------------------

def running_on_postgres() -> bool:
    return connection.vendor == "postgresql"


@pytest.fixture
def db_no_rls(db) -> Iterator[object]:
    """Run a block with PostgreSQL Row-Level Security bypassed.

    The RLS policy in ``tenancy/migrations/0002_row_level_security.py`` admits
    a row when ``tenant_id`` matches ``app.current_tenant`` **or** when
    ``app.rls_bypass`` is ``'on'``. Tests that need to see the true contents
    of a table across tenants — to prove that the ORM manager hid rows that
    really are there, rather than rows that were never written — use this.

    It is a fixture and not a helper function because it must not leak: the
    bypass flag is set with ``set_config(..., true)``, i.e. transaction-local,
    and pytest-django wraps each test in a transaction that is rolled back.

    On a non-PostgreSQL backend it is a no-op that yields ``None``; the tests
    that depend on the policy itself carry ``@pytest.mark.rls`` and skip.
    """
    if not running_on_postgres():
        yield None
        return

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.rls_bypass', 'on', true)")
        try:
            yield cursor
        finally:
            cursor.execute("SELECT set_config('app.rls_bypass', 'off', true)")


@pytest.fixture(autouse=True)
def _skip_rls_marked_tests_off_postgres(request) -> None:
    """Honour ``@pytest.mark.rls`` without every test file repeating the guard."""
    if request.node.get_closest_marker("rls") and not running_on_postgres():
        pytest.skip("RLS assertions require PostgreSQL; this backend has no policies.")


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

def make_tenant(
    *,
    name: str = "Test Tenant",
    slug: Optional[str] = None,
    country: str = "EG",
    currency: str = TEST_CURRENCY,
) -> Tenant:
    return Tenant.objects.create(
        name=name,
        legal_name=f"{name} LLC",
        slug=slug or f"t-{uuid.uuid4().hex[:12]}",
        status=Tenant.Status.ACTIVE,
        country=country,
        base_currency=currency,
        fiscal_year_start_month=1,
        settings={
            # Strings, not JSON floats: ``engine._rate_from_settings`` rejects
            # a float outright rather than reintroducing binary error.
            "payroll": {
                "social_insurance_employee_rate": "0.110000",
                "social_insurance_employer_rate": "0.187500",
            }
        },
    )


@pytest.fixture
def tenant(db) -> Tenant:
    """The tenant every test operates inside."""
    return make_tenant(name="Acme Trading", slug="acme-test")


@pytest.fixture
def other_tenant(db) -> Tenant:
    """A second tenant, used only to prove that its rows never leak into the
    first one's querysets. Nothing in a test should ever *want* both bound."""
    return make_tenant(name="Globex Industrial", slug="globex-test", currency=Currency.USD)


@pytest.fixture(autouse=True)
def bind_tenant(request) -> Iterator[Optional[Tenant]]:
    """Wrap each test in ``tenant_context(tenant.id)``.

    Autouse, but inert unless the test actually asked for a ``tenant``: a
    pure-Decimal unit test (``to_money``, ``allocate``) must not pay for a
    database or a tenant it does not use.

    ``tenant_context`` restores the previous ContextVar on the way out, including
    on failure, so a test that raises cannot leave a tenant bound for the next
    one — which under ``pytest-randomly`` would produce a failure that only
    reproduces in one seed order.

    It also pushes the tenant onto the PostgreSQL *session*
    (``app.current_tenant``). The ORM manager reads a ContextVar; Row-Level
    Security reads a session variable, and they are genuinely two different
    mechanisms. Binding only the first would make every INSERT in the suite
    fail the policy's ``WITH CHECK`` — which is the policy working correctly,
    and an extremely confusing way to learn it.
    """
    if "tenant" not in request.fixturenames:
        yield None
        return

    bound: Tenant = request.getfixturevalue("tenant")

    # Bind the PostgreSQL session *before* resolving the identity fixtures,
    # not after. ``owner_user`` and friends INSERT a ``TenantMembership`` and
    # a ``RoleAssignment``, and the RLS policy's WITH CHECK runs on those
    # inserts exactly as it does on any other write. Resolving them first
    # means they execute with ``app.current_tenant`` unset, the predicate is
    # NULL, and PostgreSQL refuses the row -- so every test that asks for a
    # user dies in setup with "new row violates row-level security policy".
    #
    # That ordering was invisible while the suite connected as a superuser,
    # because superusers skip policy evaluation entirely. It becomes load
    # bearing the moment the connection is the non-superuser role that
    # ``manage.py provision_db_roles`` creates -- which is the only
    # configuration in which these tests prove anything about isolation.
    if running_on_postgres():
        bind_database_session(bound.id)

    user = None
    for name in ("owner_user", "accountant_user", "employee_user"):
        if name in request.fixturenames:
            user = request.getfixturevalue(name)
            break

    with tenant_context(bound.id, user.id if user is not None else None):
        if running_on_postgres():
            # ``SET LOCAL``: scoped to pytest-django's per-test transaction,
            # so it cannot leak onto a pooled connection.
            bind_database_session(bound.id)
        yield bound


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------

@pytest.fixture
def system_roles(db) -> dict[str, Role]:
    """The system roles, created directly rather than through the JSON.

    A test that cares about the *catalogue* asks for :func:`permission_catalogue`
    instead; most tests only need a role object to hang a ``RoleAssignment``
    off, and loading 187 permissions for that is a needless second of setup.

    ``tenant=NULL`` + ``is_system=True`` is not a choice: the
    ``ck_role_system_has_no_tenant`` constraint requires exactly that pairing.
    """
    wanted = {
        "owner": (0, "Owner"),
        "admin": (10, "Admin"),
        "accountant": (20, "Accountant"),
        "hr_manager": (20, "HR Manager"),
        "department_manager": (30, "Department Manager"),
        "employee": (50, "Employee"),
    }
    roles: dict[str, Role] = {}
    for code, (rank, name) in wanted.items():
        roles[code], _ = Role.objects.get_or_create(
            tenant=None,
            code=code,
            defaults={"name": name, "rank": rank, "is_system": True},
        )
    return roles


@pytest.fixture
def permission_catalogue(db) -> None:
    """Load the real ``config/permissions.json`` through the seeder.

    Used by tests that assert on the catalogue itself (codename grammar,
    role -> permission edges). Deliberately not autouse.
    """
    call_command("seed_permissions")


def _make_user(email: str, full_name: str) -> User:
    return User.objects.create_user(
        email=email, password="test-password", full_name=full_name, is_active=True
    )


def _grant(tenant: Tenant, user: User, role: Role, *, is_owner: bool = False) -> TenantMembership:
    membership, _ = TenantMembership.objects.get_or_create(
        tenant=tenant, user=user, defaults={"is_owner": is_owner}
    )
    RoleAssignment.objects.get_or_create(
        membership=membership, role=role, department=None, project=None
    )
    return membership


@pytest.fixture
def owner_user(db, tenant, system_roles) -> User:
    user = _make_user("owner@acme-test.example.com", "Owner Demo")
    _grant(tenant, user, system_roles["owner"], is_owner=True)
    return user


@pytest.fixture
def accountant_user(db, tenant, system_roles) -> User:
    """A second authorised human.

    Exists mainly so that segregation-of-duties paths (calculate vs approve
    a payroll run) have two distinct actors to work with. A test that uses one
    user for both is testing nothing.
    """
    user = _make_user("accountant@acme-test.example.com", "Nadia Accountant")
    _grant(tenant, user, system_roles["accountant"])
    return user


@pytest.fixture
def employee_user(db, tenant, system_roles) -> User:
    user = _make_user("employee@acme-test.example.com", "Karim Employee")
    _grant(tenant, user, system_roles["employee"])
    return user


@pytest.fixture
def iam_permission_stub(monkeypatch) -> None:
    """Provide ``apps.iam.services.permissions.assert_permission``.

    ``payroll.services.engine.approve_run`` imports that module lazily. It does
    not exist in this revision of the repository (``apps/iam`` ships
    ``permissions.py``, not ``services/permissions.py``), so without this stub
    the approval path raises ``ModuleNotFoundError`` and the segregation-of-
    duties assertion can never be reached.

    The stub is permissive on purpose: these tests assert the *segregation*
    control, which is enforced by ``approve_run`` itself, not the RBAC gate.
    """
    import sys
    import types

    module = types.ModuleType("apps.iam.services.permissions")

    def assert_permission(user, codename, *, tenant_id=None):  # noqa: ANN001
        return True

    module.assert_permission = assert_permission

    package = sys.modules.get("apps.iam.services")
    if package is None:
        package = types.ModuleType("apps.iam.services")
        package.__path__ = []  # mark as a package so submodule import resolves
        monkeypatch.setitem(sys.modules, "apps.iam.services", package)
    monkeypatch.setitem(sys.modules, "apps.iam.services.permissions", module)
    monkeypatch.setattr(package, "permissions", module, raising=False)


# ---------------------------------------------------------------------------
# Accounting scaffolding
# ---------------------------------------------------------------------------

@pytest.fixture
def chart_of_accounts(db, tenant) -> dict[str, Account]:
    """Seed the tenant's chart, journals and fiscal calendar, keyed by role.

    Goes through the real management command rather than building accounts by
    hand: if ``seed_chart_of_accounts`` ever stops creating a ``system_key``
    the services resolve, every posting test in the suite fails, which is
    exactly the alarm that should sound.

    Returns ``{system_key: Account}``. Look accounts up by role, never by code
    — the codes differ per country chart, which is the whole reason
    ``system_key`` exists.
    """
    call_command(
        "seed_chart_of_accounts",
        tenant=str(tenant.id),
        country="EG",
        year=date.today().year,
        verbosity=0,
    )
    return {
        account.system_key: account
        for account in Account.all_tenants.filter(tenant_id=tenant.id).exclude(
            system_key=""
        )
    }


@pytest.fixture
def journals(db, tenant, chart_of_accounts) -> dict[str, Journal]:
    return {
        journal.code: journal
        for journal in Journal.all_tenants.filter(tenant_id=tenant.id)
    }


@pytest.fixture
def fiscal_year(db, tenant, chart_of_accounts) -> FiscalYear:
    year = FiscalYear.all_tenants.filter(tenant_id=tenant.id).first()
    assert year is not None, "seed_chart_of_accounts did not create a fiscal year"
    return year


@pytest.fixture
def open_period(db, tenant, chart_of_accounts) -> FiscalPeriod:
    """The OPEN period containing today. Every posting test dates into it."""
    today = date.today()
    period = FiscalPeriod.all_tenants.filter(
        tenant_id=tenant.id, start_date__lte=today, end_date__gte=today
    ).first()
    assert period is not None, (
        "No fiscal period covers today; seed_chart_of_accounts should have "
        "created twelve."
    )
    if period.status != FiscalPeriod.Status.OPEN:
        FiscalPeriod.all_tenants.filter(pk=period.pk).update(
            status=FiscalPeriod.Status.OPEN
        )
        period.refresh_from_db()
    return period


@pytest.fixture
def next_period(db, tenant, open_period) -> FiscalPeriod:
    """The period after :func:`open_period` — where reversals land."""
    period = (
        FiscalPeriod.all_tenants.filter(
            tenant_id=tenant.id, start_date__gt=open_period.start_date
        )
        .order_by("start_date")
        .first()
    )
    assert period is not None, "Expected a period after the current one."
    return period


# ---------------------------------------------------------------------------
# Draft helpers
# ---------------------------------------------------------------------------

def make_draft(
    *,
    debit_account: Account,
    credit_account: Account,
    amount: Decimal,
    entry_date: Optional[date] = None,
    journal_code: str = "GEN",
    currency: str = TEST_CURRENCY,
    memo: str = "test entry",
    idempotency_key: str = "",
) -> JournalEntryDraft:
    """A minimal two-line balanced draft. Decimal amounts only."""
    draft = JournalEntryDraft(
        journal_code=journal_code,
        entry_date=entry_date or date.today(),
        currency=currency,
        memo=memo,
        idempotency_key=idempotency_key,
    )
    draft.add(LineDraft(account_id=debit_account.id, debit=amount, description="Dr"))
    draft.add(LineDraft(account_id=credit_account.id, credit=amount, description="Cr"))
    return draft


@pytest.fixture
def draft_factory(chart_of_accounts):
    """``draft_factory(Decimal("100.00"))`` -> a balanced Bank/Revenue draft."""

    def _factory(
        amount: Decimal = Decimal("100.00"),
        *,
        debit_key: str = "bank_main",
        credit_key: str = "sales_revenue",
        **kwargs,
    ) -> JournalEntryDraft:
        return make_draft(
            debit_account=chart_of_accounts[debit_key],
            credit_account=chart_of_accounts[credit_key],
            amount=amount,
            **kwargs,
        )

    return _factory


def ledger_row_counts(tenant_id: uuid.UUID) -> tuple[int, int]:
    """``(entries, lines)`` for a tenant, read past the tenant manager.

    Used by the "wrote NOTHING" assertions: counting through the tenant-scoped
    manager would be unable to distinguish "no rows written" from "rows written
    for a tenant we are not bound to", and the second is the bug.
    """
    return (
        JournalEntry.all_tenants.filter(tenant_id=tenant_id).count(),
        JournalLine.all_tenants.filter(tenant_id=tenant_id).count(),
    )


@pytest.fixture
def row_counts(tenant):
    """``row_counts()`` -> the current ``(entries, lines)`` tuple."""

    def _counts() -> tuple[int, int]:
        return ledger_row_counts(tenant.id)

    return _counts


@pytest.fixture
def assert_bound():
    """Guard against a test silently running with no tenant bound."""

    def _assert(expected_tenant_id: uuid.UUID) -> None:
        assert get_current_tenant_id() == expected_tenant_id, (
            "The tenant context is not bound to the tenant under test; every "
            "queryset in this test would return .none()."
        )

    return _assert


__all__ = [
    "TEST_CURRENCY",
    "ledger_row_counts",
    "make_draft",
    "make_tenant",
    "running_on_postgres",
]
