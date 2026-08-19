"""
Banking URL registration.

``bank-accounts`` and ``bank-transactions`` keep their prefixes. The
reconciliation surface — statements, sessions and matches — is new: those
viewsets existed with no route, which left importing a statement and
reconciling it impossible over the API even though the services behind them
were complete.
"""

from __future__ import annotations

from apps.banking.viewsets import (
    BankAccountViewSet,
    BankStatementViewSet,
    BankTransactionViewSet,
    ReconciliationMatchViewSet,
    ReconciliationSessionViewSet,
)


def register(router) -> None:
    """Mount this app's routes on the v1 router."""
    router.register(r"bank-accounts", BankAccountViewSet, basename="bank-accounts")
    router.register(
        r"bank-transactions", BankTransactionViewSet, basename="bank-transactions"
    )
    router.register(r"bank-statements", BankStatementViewSet, basename="bank-statements")
    router.register(
        r"reconciliation-sessions", ReconciliationSessionViewSet,
        basename="reconciliation-sessions",
    )
    router.register(
        r"reconciliation-matches", ReconciliationMatchViewSet,
        basename="reconciliation-matches",
    )
