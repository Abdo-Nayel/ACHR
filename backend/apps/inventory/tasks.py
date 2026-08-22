"""Scheduled inventory tasks.

Thin Celery wrapper over ``apps.inventory.services.stock.recompute_stock_levels``,
one run per active tenant. The service existed but nothing wired it, so the
``recompute-stock-levels`` beat entry fired ``NotRegistered`` nightly.
"""

from __future__ import annotations

import logging

from celery import shared_task

from apps.core.tenancy_tasks import for_each_tenant

logger = logging.getLogger(__name__)


@shared_task(name="apps.inventory.tasks.recompute_stock_levels", acks_late=True)
def recompute_stock_levels() -> dict:
    """Re-derive every StockLevel from the movement ledger and log any drift.

    Reports rather than silently repairs (``repair=False``): a silent fix
    destroys the evidence of the bug that caused the drift.
    """
    from apps.inventory.services.stock import recompute_stock_levels as _recompute

    def _job(tenant_id):
        drift = _recompute(tenant_id, repair=False)
        if drift:
            logger.warning("stock drift for tenant %s: %d item(s)", tenant_id, len(drift))

    return for_each_tenant(_job)
