"""The fiscal-period state machine — open, soft-close, close, reopen.

This lived inside ``FiscalPeriodViewSet._transition`` in the API layer, whose own
docstring admitted "there is no ``close_period`` service … so the transition is
implemented here". It is business logic — locking, a state machine, and two
refusals that protect filed figures — so it belongs in a service the view (and a
future close-the-books task or management command) can both call. The view is now
a thin adapter over :func:`transition_period`.

The two properties that must not be lost:

* ``SELECT ... FOR UPDATE`` on the period row. ``post_entry`` takes ``FOR SHARE``
  on the same row, so a close and a concurrent posting are mutually exclusive.
  Without it, a close and a post that both read ``status='open'`` race, and the
  entry lands in a period that is closed by the time it commits.
* A close is refused while unposted drafts remain in the period — a draft in a
  closed period can never be posted and never corrected in place, so it becomes
  a permanently stranded document.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone

from apps.accounting.models import FiscalPeriod, FiscalYear, JournalEntry
from apps.core.exceptions import (
    DomainError,
    IllegalTransitionError,
    PeriodClosedError,
)

logger = logging.getLogger(__name__)

#: The period state machine. ``FiscalPeriod`` carries no ``transition()`` of its
#: own, so the map is here — in one place, consulted by every move.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    FiscalPeriod.Status.OPEN: {
        FiscalPeriod.Status.SOFT_CLOSED,
        FiscalPeriod.Status.CLOSED,
    },
    FiscalPeriod.Status.SOFT_CLOSED: {
        FiscalPeriod.Status.CLOSED,
        FiscalPeriod.Status.OPEN,
    },
    FiscalPeriod.Status.CLOSED: {FiscalPeriod.Status.OPEN},
}


@transaction.atomic
def transition_period(
    period_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    target: str,
    user_id: Optional[uuid.UUID] = None,
    reason: str = "",
) -> FiscalPeriod:
    """Move a fiscal period to ``target``, atomically and under a row lock.

    Idempotent: moving a period to the state it is already in is a no-op that
    returns it unchanged.
    """
    locked = (
        FiscalPeriod.all_tenants.select_for_update()
        .filter(pk=period_id, tenant_id=tenant_id)
        .first()
    )
    if locked is None:  # pragma: no cover - defensive
        raise DomainError("The fiscal period disappeared while it was being locked.")

    if locked.status == target:
        return locked

    allowed = ALLOWED_TRANSITIONS.get(locked.status, set())
    if target not in allowed:
        raise IllegalTransitionError(
            f"Fiscal period '{locked.name}' is "
            f"{locked.get_status_display().lower()} and cannot become {target}."
        )

    if target == FiscalPeriod.Status.CLOSED:
        drafts = JournalEntry.all_tenants.filter(
            tenant_id=locked.tenant_id,
            period_id=locked.pk,
            status=JournalEntry.Status.DRAFT,
        ).count()
        if drafts:
            raise PeriodClosedError(
                f"Period '{locked.name}' still has {drafts} unposted draft "
                f"entr{'y' if drafts == 1 else 'ies'}. Closing now strands them: "
                f"they can never be posted into a closed period and they can "
                f"never be corrected in place. Post or void them first."
            )

    if target == FiscalPeriod.Status.OPEN and locked.status == FiscalPeriod.Status.CLOSED:
        if locked.fiscal_year.status == FiscalYear.Status.CLOSED:
            raise PeriodClosedError(
                f"Fiscal year '{locked.fiscal_year.name}' is closed; its net "
                f"income has already been rolled into equity. Reopening a period "
                f"inside it would change a figure that has been filed. Reopen the "
                f"year first."
            )

    now = timezone.now()
    fields: dict[str, Any] = {
        "status": target,
        "updated_by_id": user_id,
        "updated_at": now,
    }
    if target == FiscalPeriod.Status.CLOSED:
        fields["closed_at"] = now
        fields["closed_by_id"] = user_id
    elif target == FiscalPeriod.Status.OPEN:
        fields["closed_at"] = None
        fields["closed_by_id"] = None

    FiscalPeriod.all_tenants.filter(pk=locked.pk).update(**fields)
    locked.refresh_from_db()
    logger.info(
        "fiscal period %s -> %s by user=%s reason=%r",
        locked.name, target, user_id, reason,
    )
    return locked
