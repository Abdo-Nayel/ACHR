"""Scheduled sales tasks.

Thin Celery wrapper over
``apps.sales.services.invoice_workflow.refresh_overdue_status``, one run per
active tenant. The service existed (and is exposed at ``POST
/sales/invoices/refresh-overdue``) but no task wired it, so the
``refresh-overdue-invoices`` beat entry fired ``NotRegistered`` hourly.
"""

from __future__ import annotations

from celery import shared_task

from apps.core.tenancy_tasks import for_each_tenant


@shared_task(name="apps.sales.tasks.refresh_overdue_invoices", acks_late=True)
def refresh_overdue_invoices() -> dict:
    """Flip past-due unpaid invoices to OVERDUE, per tenant, in the tenant's own
    time zone (the service resolves that)."""
    from apps.sales.services.invoice_workflow import refresh_overdue_status

    return for_each_tenant(lambda tenant_id: refresh_overdue_status(tenant_id=tenant_id))
