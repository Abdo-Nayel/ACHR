"""The deploy-time checks that assert tenant isolation is switched on.

These back ``manage.py check_rls`` (the Makefile's ``rls-verify``). Against a
correctly-migrated database connected as the non-superuser role, they must be
silent; the negative case (a superuser connection) is exercised operationally by
running the command as ``postgres``.
"""

from __future__ import annotations

import pytest

from apps.accounting.checks import check_ledger_triggers_installed
from apps.tenancy.checks import (
    check_app_role_is_not_privileged,
    check_rls_forced_on_tenant_tables,
    check_rls_policies_present,
    tenant_scoped_tables,
)

pytestmark = pytest.mark.django_db


def test_tenant_table_set_is_derived_from_the_model_registry():
    tables = tenant_scoped_tables()
    # A TenantScopedModel table…
    assert "sales_invoice" in tables
    # …and the iam/tenancy tables that carry tenant_id without subclassing it —
    # exactly the ones a "TenantScopedModel subclasses" derivation would miss.
    for table in ("iam_role", "iam_api_key", "iam_tenant_membership", "tenancy_audit_log"):
        assert table in tables, table


@pytest.mark.rls
def test_every_tenant_table_forces_rls():
    assert check_rls_forced_on_tenant_tables(None) == []


@pytest.mark.rls
def test_every_tenant_table_has_a_policy():
    assert check_rls_policies_present(None) == []


@pytest.mark.rls
def test_ledger_guard_triggers_are_installed():
    assert check_ledger_triggers_installed(None) == []


@pytest.mark.rls
def test_test_database_runs_as_a_non_privileged_role():
    """The suite must run as the non-superuser role, or it proves nothing about
    RLS. This is the check that says so."""
    assert check_app_role_is_not_privileged(None) == []
