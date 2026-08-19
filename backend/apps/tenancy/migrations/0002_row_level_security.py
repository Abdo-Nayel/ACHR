"""
PostgreSQL Row-Level Security for every tenant-scoped table.

This migration is the load-bearing half of the isolation model. The ORM
manager in ``apps.core.models.TenantManager`` is the *convenient* half: it
gives good errors and good query plans, but it is bypassed by ``.raw()``, by
``connection.cursor()``, by a Celery task that forgot to bind context, by a
data migration, and by an analyst in psql. RLS is what still holds in all of
those cases, because the refusal happens in the database.

What each statement does, and what it prevents
==============================================

``ALTER TABLE x ENABLE ROW LEVEL SECURITY``
    Turns policies on. On its own this does nothing to the table owner or to
    a superuser — they still see every row.

``ALTER TABLE x FORCE ROW LEVEL SECURITY``
    THIS IS THE LINE PEOPLE FORGET. Without FORCE, the role that owns the
    table (which, in a default Django deployment, is the same role the app
    connects as) is exempt from every policy. Teams enable RLS, run their
    single-tenant test suite, see it pass, and ship a system with no
    isolation whatsoever. FORCE makes the owner subject to the policy too.
    Combined with the app connecting as a non-owner, non-superuser role
    (see ``config/settings/base.py``), there is no ordinary path around it.
    Note that FORCE still does not constrain a superuser or a role with
    BYPASSRLS — which is exactly why the application role has neither.

``USING (...)``  -- the READ half
    Applied to SELECT, and to the *existing* row of UPDATE/DELETE. A row that
    does not satisfy it is invisible: not an error, simply absent. That is
    the desired behaviour — an error would confirm the row's existence.

``WITH CHECK (...)``  -- the WRITE half
    Applied to the *new* row of INSERT and UPDATE. IT IS NOT IMPLIED BY
    ``USING`` FOR INSERT.

    Concretely, with ``USING`` alone and no ``WITH CHECK``:

        SET app.current_tenant = '<tenant A>';
        INSERT INTO sales_invoice (tenant_id, ...) VALUES ('<tenant B>', ...);

    succeeds. Tenant A has just written a row into tenant B's books. A cannot
    read it back (USING filters the SELECT), which makes the attack *quiet*:
    the victim sees a phantom invoice appear in their ledger and there is
    nothing in tenant A's data to explain it. The same hole lets an UPDATE
    move a row out of the current tenant ("tenant_id = other"), which is a
    silent data-destruction primitive.

    So every policy below repeats the predicate in ``WITH CHECK``.

``current_setting('app.current_tenant', true)``
    The second argument is ``missing_ok``. With it, an unset variable returns
    NULL; without it, PostgreSQL raises ``unrecognized configuration
    parameter``. That distinction matters because the setting is genuinely
    unset in several legitimate situations: the connection has just been
    handed over by pgbouncer, a health check is running, ``migrate`` is
    executing, or a background job is starting up. We want those sessions to
    see *zero rows* (fail closed), not to crash with a 500 that masks the
    real problem. NULL propagates through the comparison, the predicate is
    NULL (not true), and the row is filtered out. Fail-closed by construction.

``OR current_setting('app.rls_bypass', true) = 'on'``
    The platform-admin escape hatch, matching
    ``apps.core.tenancy_context.platform_admin_context()``. It is a *session
    variable set by the application*, not a role attribute, so:
      * it is set with ``set_config(..., is_local => true)``, i.e. SET LOCAL,
        and therefore dies at COMMIT — a pooled connection cannot inherit it;
      * every code path that sets it writes a ``TenantAuditLog`` row.
    A BYPASSRLS role would have been simpler and unauditable.

``::uuid`` cast
    ``current_setting`` returns text. ``tenant_id`` is uuid. Without the cast
    PostgreSQL cannot use the btree index on ``tenant_id`` and every query in
    the system degrades to a sequential scan. The cast is a performance
    requirement, not cosmetics. (The empty string written by
    ``bind_database_session`` when no tenant is bound would fail the cast, so
    the policy guards it with a NULLIF.)

Ordering
--------
This migration must run after every table it touches exists, hence the long
``dependencies`` list. New tenant-scoped tables MUST be added to
``TENANT_SCOPED_TABLES`` in a follow-up migration; ``apps.tenancy.checks``
asserts at boot that every table with a ``tenant_id`` column has
``relforcerowsecurity`` set, so a forgotten table fails deployment rather
than leaking.
"""

from __future__ import annotations

from django.db import migrations

# ---------------------------------------------------------------------------
# The tables
# ---------------------------------------------------------------------------
# Exactly the ``db_table`` values of every concrete ``TenantScopedModel`` /
# ``ImmutableFinancialModel`` subclass. Tables deliberately NOT in this list:
#   * tenancy_tenant / tenancy_domain / tenancy_subscription — they *are* the
#     scope; they are reached through the platform-admin path only.
#   * tenancy_audit_log — must be writable while the tenant context is being
#     established (login, tenant switch, impersonation) and must never be
#     deletable by tenant users.
#   * iam_user / iam_permission / iam_role — global identity and the
#     capability catalogue.
TENANT_SCOPED_TABLES: list[str] = [
    "accounting_account",
    "accounting_document_sequence",
    "accounting_exchange_rate",
    "accounting_fiscal_period",
    "accounting_fiscal_year",
    "accounting_journal",
    "accounting_journal_entry",
    "accounting_journal_line",
    "accounting_tax_rate",
    "banking_bank_account",
    "banking_bank_statement",
    "banking_bank_transaction",
    "banking_reconciliation_match",
    "banking_reconciliation_session",
    "expenses_bill",
    "expenses_bill_line",
    "expenses_bill_payment",
    "expenses_category",
    "expenses_expense",
    "expenses_receipt",
    "expenses_vendor",
    "hr_attendance_break",
    "hr_attendance_record",
    "hr_department",
    "hr_employee",
    "hr_employee_document",
    "hr_holiday",
    "hr_job_title",
    "hr_leave_approval",
    "hr_leave_balance",
    "hr_leave_request",
    "hr_leave_type",
    "hr_salary_revision",
    "hr_shift",
    "hr_work_schedule",
    "iam_api_key",
    "iam_role",
    "iam_tenant_membership",
    "inventory_item",
    "inventory_item_category",
    "inventory_low_stock_alert",
    "inventory_price_list",
    "inventory_price_list_item",
    "inventory_stock_adjustment",
    "inventory_stock_adjustment_line",
    "inventory_stock_batch",
    "inventory_stock_level",
    "inventory_stock_movement",
    "inventory_unit_of_measure",
    "inventory_warehouse",
    "payments_gateway_config",
    "payments_payment",
    "payments_payment_application",
    "payments_refund",
    "payments_webhook_event",
    "payroll_bank_file",
    "payroll_component",
    "payroll_employee_component",
    "payroll_employee_profile",
    "payroll_payslip",
    "payroll_payslip_line",
    "payroll_run",
    "payroll_salary_disbursement",
    "payroll_tax_bracket",
    "projects_project",
    "projects_project_member",
    "projects_project_milestone",
    "projects_project_task",
    "projects_timesheet_entry",
    "reporting_account_grouping",
    "reporting_definition",
    "reporting_line_mapping",
    "reporting_schedule",
    "reporting_snapshot",
    "sales_credit_note",
    "sales_credit_note_line",
    "sales_customer",
    "sales_invoice",
    "sales_invoice_line",
    "sales_payment_reminder_log",
    "sales_payment_reminder_rule",
    "sales_recurring_invoice_line_template",
    "sales_recurring_invoice_profile",
    "tenancy_audit_log",
    "tenancy_domain",
    "tenancy_subscription",
]

POLICY_NAME = "tenant_isolation"

#: The predicate, used identically in USING and WITH CHECK.
#:
#: NULLIF(..., '') guards the empty string that ``bind_database_session``
#: writes when there is no tenant: ''::uuid raises invalid_text_representation,
#: which would turn "no tenant bound" into a hard error on every query rather
#: than an empty result set.
TENANT_PREDICATE = (
    "tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid"
    " OR current_setting('app.rls_bypass', true) = 'on'"
)


def _enable_sql(table: str) -> str:
    return f"""
-- ---------------------------------------------------------------------------
-- {table}
-- ---------------------------------------------------------------------------
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
-- FORCE: without this the table OWNER bypasses the policy entirely.
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS {POLICY_NAME} ON {table};
CREATE POLICY {POLICY_NAME} ON {table}
    AS PERMISSIVE
    FOR ALL
    TO PUBLIC
    USING ({TENANT_PREDICATE})
    WITH CHECK ({TENANT_PREDICATE});
"""


def _disable_sql(table: str) -> str:
    return f"""
DROP POLICY IF EXISTS {POLICY_NAME} ON {table};
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
"""


FORWARD_SQL = "\n".join(_enable_sql(t) for t in TENANT_SCOPED_TABLES)
REVERSE_SQL = "\n".join(_disable_sql(t) for t in reversed(TENANT_SCOPED_TABLES))

# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------
# The application role owns nothing and creates nothing; it only gets DML.
# Keeping DDL away from the runtime role means an SQL-injection foothold
# cannot DROP a policy and unlock every tenant.
GRANT_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_setting('app.app_role', true)) THEN
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I',
            current_setting('app.app_role', true)
        );
    END IF;
END
$$;
"""

REVOKE_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_setting('app.app_role', true)) THEN
        EXECUTE format(
            'REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM %I',
            current_setting('app.app_role', true)
        );
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    """Applied once; extended by later migrations as tables are added."""

    # RLS DDL takes an ACCESS EXCLUSIVE lock per table. That is fine here
    # (fast catalog-only change) but it must not be batched with a long data
    # migration in the same transaction, or the whole schema is locked for the
    # duration.
    atomic = True

    dependencies = [
        ("accounting", "0004_ledger_guards"),
        ("banking", "0002_initial"),
        ("expenses", "0003_initial"),
        ("hr", "0002_initial"),
        ("iam", "0002_initial"),
        ("inventory", "0001_initial"),
        ("payments", "0002_initial"),
        ("payroll", "0001_initial"),
        ("projects", "0002_initial"),
        ("reporting", "0001_initial"),
        ("sales", "0001_initial"),
        ("tenancy", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
        migrations.RunSQL(sql=GRANT_SQL, reverse_sql=REVOKE_SQL),
    ]
