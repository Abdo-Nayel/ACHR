"""Boot/CI assertions that tenant isolation is actually switched on.

The whole isolation model — ``FORCE ROW LEVEL SECURITY`` and the tenant policy
on every business table, plus the app connecting as a non-superuser role — is
inert if any one piece is missing, and it fails *silently* in the safe-looking
direction: the ORM still filters, the UI looks right, and the first evidence is
one customer reading another's ledger through a ``.raw()`` query. ``docs`` and
``apps/core/rls.py`` both promised these checks existed; they did not. These are
them, as Django system checks (``deploy=True``), so they run on
``manage.py check --deploy``, in CI and at deploy — never during ``migrate``
(which runs the non-deploy checks as the owner role, where "not a superuser"
would rightly fail).

The set of tables that must be protected is **derived from the model registry**,
not a hand-maintained list: any model with a ``tenant_id`` column must have RLS
forced on its table. That is what makes forgetting a new tenant table impossible
rather than merely discouraged.
"""

from __future__ import annotations

from django.core.checks import Error, Tags, Warning, register
from django.db import OperationalError, ProgrammingError, connection


def tenant_scoped_tables() -> list[str]:
    """Every DB table carrying a ``tenant_id`` column, from the model registry.

    This is the RLS-protected set. Deriving it here means a new tenant-scoped
    model is covered automatically, and a table that slips through the RLS
    migration is caught by :func:`check_rls_forced_on_tenant_tables` rather than
    discovered in production.
    """
    from django.apps import apps

    return sorted(
        {
            model._meta.db_table
            for model in apps.get_models()
            if any(getattr(f, "attname", None) == "tenant_id"
                   for f in model._meta.concrete_fields)
        }
    )


@register(Tags.security, deploy=True)
def check_app_role_is_not_privileged(app_configs, **kwargs):
    """The runtime role must not be able to bypass Row-Level Security.

    ``rolsuper`` and ``rolbypassrls`` both skip policy evaluation entirely, so a
    deployment connecting as either has every policy installed and none in
    effect. Only meaningful on PostgreSQL.
    """
    if connection.vendor != "postgresql":
        return []
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
            row = cur.fetchone()
    except (OperationalError, ProgrammingError):
        return []  # no database to inspect (e.g. building an image); not this check's job
    if row is None:
        return []
    is_super, is_bypass = row
    errors = []
    if is_super:
        errors.append(Error(
            "The application connects to PostgreSQL as a SUPERUSER, which "
            "bypasses Row-Level Security unconditionally — tenant isolation is "
            "not enforced.",
            hint="Run as the non-superuser role from `manage.py provision_db_roles` "
                 "(erp_app). Migrations may still run as the owner.",
            id="tenancy.E001",
        ))
    if is_bypass:
        errors.append(Error(
            "The application role has BYPASSRLS, which disables tenant isolation.",
            hint="ALTER ROLE <app_role> NOBYPASSRLS.",
            id="tenancy.E002",
        ))
    return errors


@register(Tags.database, deploy=True)
def check_rls_forced_on_tenant_tables(app_configs, **kwargs):
    """Every table with a ``tenant_id`` column must have RLS enabled *and*
    forced (forced so the table owner is subject to it too)."""
    if connection.vendor != "postgresql":
        return []
    expected = tenant_scoped_tables()
    if not expected:
        return []
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT relname FROM pg_class "
                "WHERE relkind = 'r' AND relname = ANY(%s) "
                "AND NOT (relrowsecurity AND relforcerowsecurity)",
                [expected],
            )
            unprotected = sorted(r[0] for r in cur.fetchall())
    except (OperationalError, ProgrammingError):
        return []
    if not unprotected:
        return []
    return [Error(
        f"{len(unprotected)} tenant-scoped table(s) do not have FORCE ROW LEVEL "
        f"SECURITY: {', '.join(unprotected)}.",
        hint="Add apps.core.rls.rls_operations(<table>) to the migration that "
             "creates the table. A tenant table without forced RLS leaks across "
             "tenants on any query that bypasses the ORM.",
        id="tenancy.E003",
    )]


@register(Tags.database, deploy=True)
def check_rls_policies_present(app_configs, **kwargs):
    """A forced-RLS table with no policy denies everything; without FORCE it
    would allow everything. Warn if the two counts diverge from the table set."""
    if connection.vendor != "postgresql":
        return []
    expected = tenant_scoped_tables()
    if not expected:
        return []
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = ANY(%s)",
                [expected],
            )
            with_policy = {r[0] for r in cur.fetchall()}
    except (OperationalError, ProgrammingError):
        return []
    missing = sorted(set(expected) - with_policy)
    if not missing:
        return []
    return [Warning(
        f"{len(missing)} tenant-scoped table(s) have no RLS policy: "
        f"{', '.join(missing)}.",
        hint="A forced-RLS table with no permissive policy rejects every row.",
        id="tenancy.W001",
    )]
