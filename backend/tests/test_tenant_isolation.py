"""Tenant isolation — the property a shared-database SaaS lives or dies by.

Isolation here is defence in depth, and the two layers are tested separately
because they fail separately:

* **The ORM manager** (``apps.core.models.TenantManager``) filters every
  queryset to the tenant bound in a ``ContextVar``, and returns ``.none()``
  when nothing is bound. It gives clean errors and good query plans. It is
  bypassed by ``.raw()``, by ``all_tenants`` and by any code that forgets.
* **PostgreSQL Row-Level Security** (``tenancy/migrations/0002``) refuses the
  row in the database, under ``FORCE ROW LEVEL SECURITY`` so the table owner
  is subject too. It is the backstop that still holds when a Celery task
  forgets to bind context or an analyst opens psql.

A test that only exercised the manager would pass on a database with the
policies dropped. The RLS assertions therefore carry ``@pytest.mark.rls`` and
skip — loudly, by name — on any backend that has no policies, rather than
silently reporting a pass they never earned.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection

from apps.accounting.models import Account, AccountType
from apps.core.models import TenantQuerySet
from apps.core.tenancy_context import (
    get_current_tenant_id,
    platform_admin_context,
    tenant_context,
)
from apps.hr.models import Department
from apps.iam.permissions import (
    CACHE_SCHEMA_VERSION,
    permission_cache_key,
    reauth_cache_key,
    scope_cache_key,
)
from apps.sales.models import Customer
from tests.conftest import running_on_postgres

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account_for(tenant, *, code: str, name: str) -> Account:
    """Create an account for an explicit tenant, past the ambient context.

    ``all_tenants`` plus an explicit ``tenant_id`` is the sanctioned escape
    hatch (see ``AllTenantsManager``); it is used here to *plant* the other
    tenant's row so that the test can then prove it is invisible.
    """
    return Account.all_tenants.create(
        tenant_id=tenant.id,
        code=code,
        name=name,
        type=AccountType.ASSET,
        is_postable=True,
    )


# ---------------------------------------------------------------------------
# ORM manager
# ---------------------------------------------------------------------------

def test_queryset_under_tenant_a_never_returns_tenant_b_rows(
    tenant, other_tenant, db_no_rls
):
    """The headline assertion. Both rows exist; only one is visible."""
    mine = _account_for(tenant, code="9001", name="Mine")
    theirs = _account_for(other_tenant, code="9001", name="Theirs")

    visible_ids = set(Account.objects.values_list("id", flat=True))

    assert mine.id in visible_ids
    assert theirs.id not in visible_ids, (
        "A row belonging to another tenant surfaced through the default manager."
    )
    assert Account.objects.filter(pk=theirs.pk).first() is None, (
        "Filtering by another tenant's primary key must still return nothing; "
        "an id is not an authorisation."
    )


def test_the_same_natural_key_may_exist_in_both_tenants(tenant, other_tenant, db_no_rls):
    """Codes are unique *per tenant*, never globally.

    A global unique index would leak the existence of another customer's
    accounts through constraint violations, and would break onboarding for any
    tenant using a standard chart.
    """
    _account_for(tenant, code="1100", name="Bank — mine")
    _account_for(other_tenant, code="1100", name="Bank — theirs")

    assert Account.all_tenants.filter(code="1100").count() == 2
    assert Account.objects.filter(code="1100").count() == 1


def test_switching_context_switches_the_visible_rows(tenant, other_tenant, db_no_rls):
    mine = _account_for(tenant, code="9100", name="Mine")
    theirs = _account_for(other_tenant, code="9100", name="Theirs")

    assert list(Account.objects.values_list("id", flat=True)).count(mine.id) == 1

    with tenant_context(other_tenant.id):
        visible = set(Account.objects.values_list("id", flat=True))
        assert theirs.id in visible
        assert mine.id not in visible

    # And the previous binding is restored on the way out.
    assert get_current_tenant_id() == tenant.id


def test_queryset_with_no_tenant_bound_returns_nothing_not_everything(
    tenant, other_tenant, db_no_rls
):
    """Fail closed. ``TenantManager`` returns ``.none()`` rather than an
    unfiltered queryset when the context is empty — the opposite default would
    turn one forgotten ``bind`` into a full cross-tenant dump."""
    _account_for(tenant, code="9200", name="Mine")

    with tenant_context(None):
        assert Account.objects.count() == 0
        assert list(Account.objects.all()) == []


def test_saving_without_a_tenant_context_raises_permission_denied(db):
    """``TenantScopedModel.save`` refuses to guess.

    An unscoped write is worse than an unscoped read: the row lands somewhere
    and is discovered later, by someone else's report.
    """
    with tenant_context(None):
        account = Account(
            code="9300", name="Orphan", type=AccountType.ASSET, is_postable=True
        )
        with pytest.raises(PermissionDenied) as exc:
            account.save()
    assert "without a tenant context" in str(exc.value)


def test_saving_picks_up_the_ambient_tenant_when_one_is_bound(tenant):
    """The mirror of the test above: with a context bound, ``tenant_id`` is
    filled in automatically, so callers never have to pass it."""
    department = Department(code="ops", name="Operations", depth=0)
    department.save()
    assert department.tenant_id == tenant.id


def test_bulk_delete_on_a_tenant_queryset_raises(tenant, chart_of_accounts):
    """``TenantQuerySet.delete`` is disabled outright.

    Business documents are archived, voided or reversed. A bulk delete is
    never the right answer, and the one place in the product that genuinely
    needs it (``payroll.engine._discard_payslips``, for a never-posted run)
    steps around this guard explicitly and says why.
    """
    with pytest.raises(PermissionDenied) as exc:
        Account.objects.filter(code__startswith="9").delete()
    assert "Bulk delete is disabled" in str(exc.value)

    with pytest.raises(PermissionDenied):
        Account.all_tenants.filter(tenant_id=tenant.id).delete()


def test_all_tenants_manager_still_uses_the_guarded_queryset():
    """The escape hatch widens *visibility*, not destructiveness."""
    assert isinstance(Account.all_tenants.all(), TenantQuerySet)
    assert isinstance(Account.objects.all(), TenantQuerySet)


def test_platform_admin_context_does_not_widen_the_orm_manager(
    tenant, other_tenant, db_no_rls
):
    """``platform_admin_context`` flips the RLS bypass flag, not the manager.

    Worth pinning: a reader could reasonably assume it makes ``objects``
    return everything. It does not — cross-tenant reads still go through
    ``all_tenants``, which is greppable and reviewable.
    """
    theirs = _account_for(other_tenant, code="9400", name="Theirs")
    with platform_admin_context():
        assert Account.objects.filter(pk=theirs.pk).first() is None
        assert Account.all_tenants.filter(pk=theirs.pk).exists()


# ---------------------------------------------------------------------------
# Row-Level Security
# ---------------------------------------------------------------------------

@pytest.mark.rls
def test_rls_and_the_orm_manager_agree(tenant, other_tenant, db_no_rls):
    """Both layers must hide exactly the same rows.

    ``db_no_rls`` plants the other tenant's row (the policy would otherwise
    refuse the INSERT, which is itself the policy working). The comparison is
    then made with the bypass off: raw SQL and the ORM must return the same
    set. If they diverge, one of the two layers has a hole and the other is
    masking it.
    """
    mine = _account_for(tenant, code="9500", name="Mine")
    theirs = _account_for(other_tenant, code="9500", name="Theirs")

    orm_ids = set(Account.objects.values_list("id", flat=True))

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.rls_bypass', 'off', true)")
        cursor.execute("SELECT id FROM accounting_account")
        raw_ids = {row[0] for row in cursor.fetchall()}
    raw_ids = {uuid.UUID(str(value)) for value in raw_ids}

    assert mine.id in raw_ids
    assert theirs.id not in raw_ids, (
        "PostgreSQL returned another tenant's row: the RLS policy is missing "
        "or the connection role is a superuser (see dev.py — tests must not "
        "run as the database owner)."
    )
    assert orm_ids == raw_ids, (
        f"ORM manager and RLS disagree. ORM-only: {orm_ids - raw_ids}; "
        f"RLS-only: {raw_ids - orm_ids}."
    )


@pytest.mark.rls
def test_rls_refuses_a_raw_insert_for_another_tenant(tenant, other_tenant):
    """The ``WITH CHECK`` half of the policy.

    Reading is only half of isolation. A worker that binds tenant A must not
    be able to *write* a row stamped tenant B, whatever the ORM was told.
    """
    from django.db import transaction
    from django.db.utils import Error as DjangoDatabaseError

    # Wrapped in a savepoint: a refused statement poisons the surrounding
    # transaction, and without the savepoint every later query in this test
    # (and pytest-django's own rollback) would fail with a confusing
    # "current transaction is aborted".
    with pytest.raises(DjangoDatabaseError):
        with transaction.atomic():
            Account.all_tenants.create(
                tenant_id=other_tenant.id,
                code="9600",
                name="Smuggled",
                type=AccountType.ASSET,
            )


@pytest.mark.rls
def test_rls_is_forced_on_every_tenant_scoped_table(tenant):
    """``FORCE ROW LEVEL SECURITY``, not merely ``ENABLE``.

    Without FORCE, the table owner — which is the role migrations run as, and
    on many small deployments the role the app runs as too — is exempt from
    its own policies. The isolation then holds in staging and evaporates in
    whichever environment took the shortcut.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relkind = 'r'
              AND c.relname IN (
                  'accounting_account', 'accounting_journal_entry',
                  'accounting_journal_line', 'sales_invoice',
                  'hr_employee', 'payroll_payslip'
              )
            """
        )
        rows = cursor.fetchall()

    assert rows, "None of the expected tenant-scoped tables exist."
    unprotected = [
        name for name, enabled, forced in rows if not (enabled and forced)
    ]
    assert not unprotected, (
        f"These tenant-scoped tables do not FORCE row level security: "
        f"{unprotected}. `make rls-verify` should have caught this."
    )


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "builder",
    [
        pytest.param(lambda t, u: permission_cache_key(t, u), id="permissions"),
        pytest.param(lambda t, u: scope_cache_key(t, u), id="scopes"),
        pytest.param(lambda t, u: reauth_cache_key(t, u, "jti-1"), id="reauth"),
    ],
)
def test_every_cache_key_contains_the_tenant_id(builder):
    """A per-user cache key without the tenant in it is a privilege escalation.

    The same ``User`` row is an Accountant in tenant A and a Read-Only Auditor
    in tenant B — that is the entire point of ``TenantMembership``. A key of
    ``perms:v1:{user_id}`` caches whichever tenant they opened first and then
    serves it to the other, granting ``journal_entry.post`` to an auditor and
    disclosing which modules tenant A has licensed. It fails silently: the
    user sees buttons that work.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()

    key = builder(tenant_id, user_id)

    assert str(tenant_id) in key, f"Cache key {key!r} omits the tenant id."
    assert str(user_id) in key


def test_cache_keys_differ_across_tenants_for_the_same_user():
    user_id = uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()
    assert permission_cache_key(first, user_id) != permission_cache_key(second, user_id)
    assert scope_cache_key(first, user_id) != scope_cache_key(second, user_id)


def test_cache_keys_are_schema_versioned():
    """The version prefix is what makes a payload-shape change safe to deploy:
    old dicts are simply never read again, instead of KeyError-ing on every
    request until the TTL expires."""
    key = permission_cache_key(uuid.uuid4(), uuid.uuid4())
    assert key.startswith(f"perms:{CACHE_SCHEMA_VERSION}:")


# ---------------------------------------------------------------------------
# Cross-module spot checks
# ---------------------------------------------------------------------------

def test_related_managers_are_tenant_filtered_too(
    tenant, other_tenant, chart_of_accounts, db_no_rls
):
    """Reverse accessors inherit the tenant-filtered default manager.

    This is the subtlety that makes ``invoice_workflow.build_invoice_entry``
    use ``InvoiceLine.all_tenants`` with an explicit filter: in a Celery task
    with no ambient tenant, ``invoice.lines.all()`` returns nothing at all —
    silently, and with a perfectly plausible empty result.
    """
    customer = Customer.objects.create(
        tenant=tenant,
        code="C-ISO-1",
        name="Visible Co",
        currency=tenant.base_currency,
        receivable_account=chart_of_accounts["ar_control"],
    )
    assert Customer.objects.filter(pk=customer.pk).exists()

    with tenant_context(None):
        assert Customer.objects.filter(pk=customer.pk).first() is None


def test_department_subtree_query_is_scoped_to_one_tenant(
    tenant, other_tenant, db_no_rls
):
    """``Department.path`` prefixes are only unique inside a tenant.

    Two tenants both having ``/hq/eng/`` is normal. The ABAC subtree query is
    a ``path__startswith`` and would match across tenants if it ever ran
    without the manager's filter — which is exactly why the filter is on the
    default manager and not on the call site.
    """
    mine = Department.objects.create(tenant=tenant, code="hq", name="HQ", depth=0)
    mine.path = mine.build_path()
    mine.save(update_fields=["path", "updated_at"])

    theirs = Department.all_tenants.create(
        tenant_id=other_tenant.id, code="hq", name="HQ", depth=0, path="/hq/"
    )

    matches = list(Department.objects.filter(path__startswith="/hq/"))
    assert [department.id for department in matches] == [mine.id]
    assert theirs.id not in {department.id for department in matches}


def test_money_in_isolation_fixtures_is_decimal(tenant, chart_of_accounts):
    """A guard on the fixtures themselves: a float would make every downstream
    assertion in this suite meaningless."""
    for account in Account.objects.all()[:5]:
        assert isinstance(account.cached_balance, Decimal)


def test_running_on_postgres_reports_the_real_backend():
    """The RLS skip decision is only as good as this predicate."""
    assert running_on_postgres() == (connection.vendor == "postgresql")
