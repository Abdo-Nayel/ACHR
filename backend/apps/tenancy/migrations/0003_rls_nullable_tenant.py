"""
Correct the RLS policy for tables whose ``tenant_id`` is nullable.

``0002_row_level_security`` applied one policy shape to every tenant-scoped
table::

    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid ...)

That is right for the 84 tables where ``tenant_id`` is ``NOT NULL``, and
silently wrong for the two where it is not:

* ``iam_role`` — a NULL tenant means a **system role** shipped with the
  product (Owner, Admin, Accountant, HR Manager, Employee, Auditor).
* ``tenancy_audit_log`` — a NULL tenant means an event recorded before any
  tenant was established (a failed login, a platform-admin action).

In SQL, ``NULL = <uuid>`` evaluates to NULL, not to true — so those rows were
invisible to every query. The visible symptom was spectacular and misleading:
every user authenticated successfully and then received
``permission_denied: Missing permission: sales.invoice.read``, because
``Role.permissions`` resolved through a role row the database refused to
return. The permission catalogue was seeded and correct the whole time.

The fix adds an explicit ``tenant_id IS NULL`` arm. This does not widen
tenant isolation: a NULL tenant row belongs to no tenant by definition, and
these are precisely the two tables where "global row" is a modelled state.
Tables with ``NOT NULL tenant_id`` keep the strict policy, so the escape
hatch cannot be reached by inserting a NULL elsewhere.
"""

from __future__ import annotations

from django.db import migrations

NULLABLE_TENANT_TABLES = ("iam_role", "tenancy_audit_log")

_PREDICATE = (
    "(tenant_id IS NULL "
    " OR tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid "
    " OR current_setting('app.rls_bypass', true) = 'on')"
)

_STRICT_PREDICATE = (
    "(tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid "
    " OR current_setting('app.rls_bypass', true) = 'on')"
)


def _sql(predicate: str) -> str:
    return "\n".join(
        f"DROP POLICY IF EXISTS tenant_isolation ON {table};\n"
        f"CREATE POLICY tenant_isolation ON {table}\n"
        f"    USING {predicate}\n"
        f"    WITH CHECK {predicate};"
        for table in NULLABLE_TENANT_TABLES
    )


class Migration(migrations.Migration):

    dependencies = [("tenancy", "0002_row_level_security")]

    operations = [
        migrations.RunSQL(sql=_sql(_PREDICATE), reverse_sql=_sql(_STRICT_PREDICATE)),
    ]
