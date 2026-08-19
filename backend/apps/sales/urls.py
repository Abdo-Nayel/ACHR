"""
Sales URL registration.

Mounts the real accounts-receivable viewsets, including the invoice
transitions (``issue``, ``void``, ``write-off``, ``send-reminder``) that the
previous read-only scaffold omitted.

``recurring-profiles`` and ``reminder-rules`` are additions rather than
renames: both viewsets already existed in :mod:`apps.sales.viewsets` with no
route, which meant the automation half of the module was unreachable.
"""

from __future__ import annotations

from apps.sales.viewsets import (
    CreditNoteViewSet,
    CustomerViewSet,
    InvoiceAttachmentViewSet,
    InvoiceViewSet,
    PaymentReminderRuleViewSet,
    RecurringInvoiceProfileViewSet,
)


def register(router) -> None:
    """Mount this app's routes on the v1 router."""
    router.register(r"customers", CustomerViewSet, basename="customers")
    router.register(r"invoices", InvoiceViewSet, basename="invoices")
    router.register(r"credit-notes", CreditNoteViewSet, basename="credit-notes")
    router.register(
        r"recurring-profiles", RecurringInvoiceProfileViewSet,
        basename="recurring-profiles",
    )
    router.register(
        r"reminder-rules", PaymentReminderRuleViewSet, basename="reminder-rules"
    )
    router.register(
        r"invoice-attachments", InvoiceAttachmentViewSet,
        basename="invoice-attachments",
    )
