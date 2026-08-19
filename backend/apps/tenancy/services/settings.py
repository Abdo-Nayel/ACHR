"""
Organisation settings: the rules behind ``PATCH /api/v1/tenancy/current/``.

:class:`apps.tenancy.models.Tenant` documents its own invariant —

    "The currency the general ledger is kept in. Immutable after the first
    posted journal entry ... Enforced in
    ``apps.tenancy.services.settings.change_base_currency``."

— and this module is that enforcement. It lives in a service rather than in
the serializer because the same rule has to hold for a management command, a
migration that fixes up a mis-provisioned tenant, and a support tool. A rule
that only exists in a DRF ``validate_*`` method is a rule that applies to
exactly one caller.

Why a posted entry is the line, and not "any data"
--------------------------------------------------
``base_currency`` is not a display preference. Every ``JournalLine`` stores
``base_debit`` / ``base_credit`` — the amount *converted into the tenant's base
currency at the rate on the day it was posted*. Those numbers are already
written. Changing the base currency does not re-convert them and could not: the
historical rates that produced them are the whole point, and re-deriving them
would silently restate filed accounts.

So a tenant with a posted entry is a tenant whose trial balance, balance sheet
and every comparative report are denominated in the old currency. The only
correct answer is to refuse, and to say *why* — a caller who is told "cannot
change" opens a support ticket; a caller who is told "you have 412 posted
entries, this would restate them" understands and stops.

Before the first posting there is nothing to invalidate, so the change is
allowed — that is the realistic case this exists to serve, the founder who
picked the wrong currency in the signup form ten minutes ago.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from django.db import transaction
from rest_framework import status

from apps.core.exceptions import DomainError
from apps.tenancy.models import Tenant, TenantAuditLog

logger = logging.getLogger("erp.security")


class BaseCurrencyLocked(DomainError):
    """409 — the ledger has history denominated in the current base currency."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "base_currency_locked"
    default_detail = (
        "The base currency cannot be changed once a journal entry has been "
        "posted."
    )


def posted_entry_count(tenant_id: uuid.UUID) -> int:
    """How many entries are already denominated in the current base currency.

    ``all_tenants`` with an explicit ``tenant_id``: this is called from the
    settings path where the tenant *is* bound, but pinning it makes the query
    correct when it is called from a management command too, and it is filtered
    to the one tenant either way. Draft entries do not count — they are
    editable and invisible to reports, so nothing has been stated in the old
    currency yet.
    """
    from apps.accounting.models import JournalEntry

    return (
        JournalEntry.all_tenants.filter(tenant_id=tenant_id)
        .exclude(status=JournalEntry.Status.DRAFT)
        .count()
    )


def assert_base_currency_changeable(tenant: Tenant) -> None:
    posted = posted_entry_count(tenant.id)
    if posted:
        raise BaseCurrencyLocked(
            f"The base currency cannot be changed from {tenant.base_currency}: "
            f"{posted} journal entr{'y has' if posted == 1 else 'ies have'} "
            f"already been posted, and every stored base-currency amount "
            f"(JournalLine.base_debit / base_credit) was converted at the rate "
            f"on its posting date. Changing it now would restate every "
            f"historical trial balance, balance sheet and comparative report "
            f"without re-converting a single figure. Open a new organisation, "
            f"or ask support about a supervised re-denomination."
        )


@transaction.atomic
def change_base_currency(
    tenant: Tenant,
    new_currency: str,
    *,
    actor=None,
    ip_address: Optional[str] = None,
    user_agent: str = "",
) -> Tenant:
    """Move a tenant's ledger currency, or refuse with the reason.

    Named exactly as ``Tenant``'s docstring promises, so the model and the
    enforcement cannot drift apart under a grep.
    """
    new_currency = (new_currency or "").strip().upper()
    if not new_currency or new_currency == tenant.base_currency:
        return tenant

    from apps.core.models import Currency

    if new_currency not in set(Currency.values):
        raise DomainError(
            f"The ledger cannot be kept in {new_currency} on this deployment. "
            f"Supported base currencies: {', '.join(sorted(Currency.values))}."
        )

    assert_base_currency_changeable(tenant)

    previous = tenant.base_currency
    tenant.base_currency = new_currency
    tenant.save(update_fields=["base_currency", "updated_at"])

    record_setting_change(
        tenant,
        actor=actor,
        changes={"base_currency": {"from": previous, "to": new_currency}},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    logger.info(
        "base currency changed tenant=%s %s -> %s by=%s",
        tenant.id, previous, new_currency, getattr(actor, "id", None),
    )
    return tenant


def record_setting_change(
    tenant: Tenant,
    *,
    actor=None,
    changes: dict[str, Any],
    ip_address: Optional[str] = None,
    user_agent: str = "",
) -> None:
    """Append a before/after snapshot to the audit trail.

    Organisation settings decide what the books mean (fiscal year, tax
    registration, currency). "Who changed the fiscal year start, and when" is
    the first question after a period lands in the wrong quarter, and it is
    unanswerable from the row itself, which only holds the current value.
    """
    if not changes:
        return
    try:
        TenantAuditLog.objects.create(
            tenant=tenant,
            actor_id=getattr(actor, "id", None),
            actor_email=getattr(actor, "email", "") or "",
            action=TenantAuditLog.Action.SETTING_CHANGED,
            object_type="tenancy.Tenant",
            object_id=tenant.id,
            payload={"changes": changes},
            ip_address=ip_address,
            user_agent=(user_agent or "")[:512],
        )
    except Exception:  # noqa: BLE001 - never fail a settings write on audit trouble
        logger.warning("tenant setting audit write failed", exc_info=True)
