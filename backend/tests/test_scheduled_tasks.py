"""The scheduled tasks wired in phase 6 must actually run.

Each was an implementation that existed but had no ``tasks.py``, so its beat
entry fired ``NotRegistered``. These call the tasks synchronously (Celery runs
eagerly in tests) against a seeded tenant and assert they complete and report
the per-tenant sweep result — proving the wiring, the shared ``for_each_tenant``
fan-out, and the underlying services agree.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


def _run(task):
    # for_each_tenant returns {"tenants": n, "failures": m}; no tenant may fail.
    result = task()
    assert isinstance(result, dict)
    assert result["failures"] == 0, result
    return result


def test_accrue_leave_runs_for_active_tenants(tenant, chart_of_accounts):
    from apps.hr.tasks import accrue_leave

    result = _run(accrue_leave)
    assert result["tenants"] >= 1


def test_recompute_stock_levels_runs_for_active_tenants(tenant, chart_of_accounts):
    from apps.inventory.tasks import recompute_stock_levels

    _run(recompute_stock_levels)


def test_refresh_overdue_invoices_runs_for_active_tenants(tenant, chart_of_accounts):
    from apps.sales.tasks import refresh_overdue_invoices

    _run(refresh_overdue_invoices)
