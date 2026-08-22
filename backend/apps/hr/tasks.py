"""Scheduled HR tasks.

Thin Celery wrappers over the HR services, one job per active tenant. The
service functions (``apps.hr.services.leave.accrue_monthly``) already existed
and were idempotent, but no ``tasks.py`` wired them, so the ``accrue-leave``
beat entry fired ``NotRegistered`` on the first of every month.
"""

from __future__ import annotations

from celery import shared_task

from apps.core.tenancy_tasks import for_each_tenant


@shared_task(name="apps.hr.tasks.accrue_leave", acks_late=True)
def accrue_leave() -> dict:
    """Grant one month's leave entitlement to every eligible employee, per tenant.

    Idempotent per (employee, leave_type, period) via a unique constraint, so a
    retry credits nothing twice.
    """
    from apps.hr.services.leave import accrue_monthly

    return for_each_tenant(lambda tenant_id: accrue_monthly(tenant_id))
