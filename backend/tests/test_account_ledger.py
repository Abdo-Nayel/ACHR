"""The account ledger endpoint — the drill-down every report links into.

``GET /accounts/{id}/ledger/`` answered 500 for every account. The cause was
not in the action: ``@action(pagination_class=LedgerCursorPagination)`` was
correct, and ``LedgerCursorPagination.ordering`` was correct. But DRF's
``CursorPagination.get_ordering`` prefers an ``OrderingFilter`` on the *view*
when one is present, and ``AccountViewSet`` declares ``ordering = ("code",)``
for its own list. So the paginator ordered ``JournalLine`` rows by ``code`` —
a field they do not have — and raised ``FieldError``.

Nothing caught it because no test called the endpoint and no report linked to
it. It surfaced the moment the reports gained drill-down links.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounting.viewsets import AccountViewSet
from apps.core.pagination import LedgerCursorPagination
from tests.conftest import make_draft

pytestmark = pytest.mark.django_db


def _ledger(tenant, owner_user, account, **params):
    """Call the real action through DRF, without an HTTP client fixture.

    The suite is service-level and has no authenticated APIClient. Driving the
    viewset directly still exercises the thing that was broken — the paginator
    the action selects — while skipping the middleware that would otherwise
    have to be stood up to bind a tenant.
    """
    factory = APIRequestFactory()
    request = factory.get(f"/api/v1/accounts/{account.id}/ledger/", params)
    force_authenticate(request, user=owner_user)
    view = AccountViewSet.as_view(
        {"get": "ledger"}, pagination_class=LedgerCursorPagination
    )
    response = view(request, pk=str(account.id))
    response.render()
    return response


def _post(tenant, owner_user, debit, credit, amount, when=None):
    from apps.accounting.services.posting import post_entry  # noqa: PLC0415

    draft = make_draft(debit_account=debit, credit_account=credit, amount=amount)
    if when is not None:
        draft.entry_date = when
    return post_entry(draft, tenant_id=tenant.id, user_id=owner_user.id)


def test_ledger_paginator_ignores_the_views_ordering():
    """The unit form of the bug, so the failure names the cause.

    Asserted against the paginator directly: if a future refactor drops the
    override, this fails with "ordering is ('code',)" rather than as a 500 in
    an unrelated feature.
    """
    paginator = LedgerCursorPagination()

    class _ViewWithItsOwnOrdering:
        ordering = ("code",)
        filter_backends = []

    assert paginator.get_ordering(None, None, _ViewWithItsOwnOrdering()) == (
        "-entry_date", "-created_at", "-id",
    )


def test_ledger_endpoint_returns_the_accounts_movement(
    tenant, chart_of_accounts, open_period, owner_user, permission_catalogue
):
    bank = chart_of_accounts["bank_main"]
    _post(tenant, owner_user, bank, chart_of_accounts["sales_revenue"],
          Decimal("500.00"))

    response = _ledger(tenant, owner_user, bank)

    assert response.status_code == 200, response.content
    rows = response.data["results"]
    assert len(rows) == 1
    assert rows[0]["entry_number"]
    assert Decimal(rows[0]["debit"]) == Decimal("500.00")


def test_ledger_running_balance_accumulates_in_date_order(
    tenant, chart_of_accounts, open_period, owner_user, permission_catalogue
):
    """The running balance is why the ordering override is a correctness fix
    and not just a crash fix: the figures only mean anything in this order."""
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    start = open_period.start_date
    _post(tenant, owner_user, bank, revenue, Decimal("100.00"), when=start)
    _post(tenant, owner_user, bank, revenue, Decimal("250.00"),
          when=start + timedelta(days=1))

    rows = _ledger(tenant, owner_user, bank).data["results"]

    # Newest first, so the first row carries the cumulative total.
    assert Decimal(rows[0]["running_balance"]) == Decimal("350.00")
    assert Decimal(rows[-1]["running_balance"]) == Decimal("100.00")


def test_ledger_excludes_draft_entries(
    tenant, chart_of_accounts, open_period, owner_user, permission_catalogue
):
    """A draft has no number and is not required to balance. Including it
    would give a closing balance that cannot be tied to the trial balance."""
    from apps.accounting.models import JournalEntry  # noqa: PLC0415

    bank = chart_of_accounts["bank_main"]
    entry = _post(tenant, owner_user, bank, chart_of_accounts["sales_revenue"],
                  Decimal("90.00"))
    JournalEntry.all_tenants.filter(pk=entry.pk).update(
        status=JournalEntry.Status.DRAFT
    )

    response = _ledger(tenant, owner_user, bank)

    assert response.data["results"] == []
