"""Helpers for Celery tasks that must run once per tenant.

A scheduled task like leave accrual or stock-drift detection is really "do this
for every active tenant". Each iteration must run inside ``tenant_context`` so
the ORM *and* Row-Level Security are bound to that tenant — and one tenant's
failure must not abort the sweep for the rest. This centralises both, so a task
module is a one-liner per job instead of re-deriving the fan-out (and the
RLS-binding it depends on) each time.
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable

from apps.core.tenancy_context import tenant_context

logger = logging.getLogger(__name__)


def active_tenant_ids() -> list[uuid.UUID]:
    """Tenants whose scheduled work is worth running.

    ``PAST_DUE`` is included — read-only access survives non-payment, so their
    books still have to stay sound. Only ``SUSPENDED`` and ``CLOSED`` are skipped.
    """
    from apps.tenancy.models import Tenant

    return list(
        Tenant.objects.filter(
            status__in=[
                Tenant.Status.ACTIVE,
                Tenant.Status.TRIAL,
                Tenant.Status.PAST_DUE,
            ]
        ).values_list("id", flat=True)
    )


def for_each_tenant(job: Callable[[uuid.UUID], object]) -> dict[str, int]:
    """Run ``job(tenant_id)`` for every active tenant, each under ``tenant_context``.

    Returns ``{"tenants": n, "failures": m}``. A single tenant's exception is
    logged and skipped rather than failing the whole sweep — one tenant's bad
    data must not stop every other tenant's accrual.
    """
    tenant_ids = active_tenant_ids()
    failures = 0
    for tenant_id in tenant_ids:
        try:
            with tenant_context(tenant_id):
                job(tenant_id)
        except Exception:  # noqa: BLE001 - isolate one tenant's failure
            failures += 1
            logger.exception("scheduled job failed for tenant %s", tenant_id)
    return {"tenants": len(tenant_ids), "failures": failures}
