"""Row-Level Security SQL for a new tenant-scoped table.

Every migration that creates a table with a ``tenant_id`` column must install
the policy, and ``apps.tenancy.checks`` asserts at boot that no such table is
missing it — a forgotten table fails deployment rather than leaking. That
check is the safety net; this module is the thing that stops you needing it,
by making the correct SQL one call instead of forty lines copied from the
previous migration.

The copy-paste version already existed twice (``tenancy.0002_row_level_security``
and ``iam.0003_invitation``) and had begun to drift: the second omitted the
``GRANT`` block's counterpart on reverse. Three copies is where that becomes a
real problem, so this is the third.

Usage in a migration::

    from apps.core.rls import rls_operations

    class Migration(migrations.Migration):
        operations = [
            migrations.CreateModel(...),
            *rls_operations("hr_overtime_slip", "hr_shift_assignment"),
        ]

What the SQL does and why, in one place
---------------------------------------
``ENABLE`` turns policies on. ``FORCE`` subjects the *owner* to them too —
without it the role that owns the table (in a default Django deployment, the
role the app connects as) is exempt, and a team can enable RLS, watch their
tenant tests pass, and ship a system with no isolation at all.

``USING`` filters reads and the existing row of UPDATE/DELETE. ``WITH CHECK``
filters the new row of INSERT/UPDATE and **is not implied by USING**: without
it, a caller bound to tenant A can INSERT a row carrying tenant B's id. A
cannot read it back, which makes the write silent — B sees a phantom row
appear in their books with nothing in A's data to explain it.

``current_setting(..., true)`` returns NULL rather than raising when the
variable is unset, which is a legitimate state (migrations, health checks, a
connection pgbouncer has just handed over). NULL fails the comparison, the row
is filtered out, and the system fails closed.

``NULLIF(..., '')`` guards the empty string ``bind_database_session`` writes
when no tenant is bound: ``''::uuid`` raises, which would turn "not
authenticated" into a 500.

The ``::uuid`` cast is a performance requirement, not cosmetics —
``current_setting`` returns text, and comparing text to a uuid column stops
PostgreSQL using the btree index on ``tenant_id``.
"""

from __future__ import annotations

from django.db import migrations

#: Identical to ``TENANT_PREDICATE`` in ``tenancy.0002_row_level_security``.
PREDICATE = (
    "(tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid"
    " OR current_setting('app.rls_bypass', true) = 'on')"
)


def _forward(table: str) -> str:
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON {table};
CREATE POLICY tenant_isolation ON {table}
    AS PERMISSIVE
    FOR ALL
    TO PUBLIC
    USING {PREDICATE}
    WITH CHECK {PREDICATE};
"""


def _reverse(table: str) -> str:
    return f"""
DROP POLICY IF EXISTS tenant_isolation ON {table};
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
"""


def _grant(table: str, revoke: bool = False) -> str:
    """Grant DML on ``table`` to the runtime role, if one is configured.

    Guarded on ``app.app_role`` because the role name is deployment-specific
    (``erp_app`` under docker-compose, whatever ``provision_db_roles`` was
    told locally) and a migration must not fail on a database where it has not
    been set — a developer running against a single-role local PostgreSQL is a
    supported setup, just not an isolated one.

    ``ALTER DEFAULT PRIVILEGES`` covers most tables automatically; this exists
    for the ones created after the role was granted, where the default only
    applies to objects made by the role that owns the default.
    """
    verb = "REVOKE" if revoke else "GRANT"
    direction = "FROM" if revoke else "TO"
    return f"""
DO $$
DECLARE
    app_role text := current_setting('app.app_role', true);
BEGIN
    IF app_role IS NOT NULL AND app_role <> ''
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
        EXECUTE format(
            '{verb} SELECT, INSERT, UPDATE, DELETE ON {table} {direction} %I', app_role
        );
    END IF;
END
$$;
"""


def rls_operations(*tables: str) -> list:
    """``RunSQL`` operations installing the policy and grants on each table.

    Reversible: rolling the migration back drops the policy rather than
    leaving a table enabled with no way to read it.
    """
    operations: list = []
    for table in tables:
        operations.append(
            migrations.RunSQL(sql=_forward(table), reverse_sql=_reverse(table))
        )
        operations.append(
            migrations.RunSQL(
                sql=_grant(table), reverse_sql=_grant(table, revoke=True)
            )
        )
    return operations


__all__ = ["PREDICATE", "rls_operations"]
