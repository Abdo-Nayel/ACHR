"""
Purchasing URL registration.

Note the domain/app mismatch, which is deliberate: the Django app is
``expenses`` but ``config/permissions.json`` calls the domain ``purchasing``.
The permission catalogue is the contract shared with the frontend and the
audit matrix; renaming the app to match it would touch every migration.

``expense-categories``, ``expense-receipts`` and ``bill-payments`` are new
prefixes for viewsets that had no route.
"""

from __future__ import annotations

from apps.expenses.viewsets import (
    BillPaymentViewSet,
    BillViewSet,
    ExpenseCategoryViewSet,
    ExpenseReceiptViewSet,
    ExpenseViewSet,
    RecurringBillProfileViewSet,
    RecurringExpenseProfileViewSet,
    VendorCreditViewSet,
    VendorViewSet,
)


def register(router) -> None:
    """Mount this app's routes on the v1 router."""
    router.register(r"vendors", VendorViewSet, basename="vendors")
    router.register(r"expenses", ExpenseViewSet, basename="expenses")
    router.register(r"bills", BillViewSet, basename="bills")
    router.register(
        r"expense-categories", ExpenseCategoryViewSet, basename="expense-categories"
    )
    router.register(
        r"expense-receipts", ExpenseReceiptViewSet, basename="expense-receipts"
    )
    router.register(r"bill-payments", BillPaymentViewSet, basename="bill-payments")
    router.register(r"vendor-credits", VendorCreditViewSet, basename="vendor-credits")
    router.register(
        r"recurring-bills", RecurringBillProfileViewSet, basename="recurring-bills"
    )
    router.register(
        r"recurring-expenses", RecurringExpenseProfileViewSet,
        basename="recurring-expenses",
    )
