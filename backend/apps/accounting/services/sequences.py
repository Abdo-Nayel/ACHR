"""Gapless document numbering — one implementation, one place.

Every document that a tax authority may inspect (invoice, credit note, payment,
refund, journal entry) carries a per-tenant, per-year, gapless number. The rule
is always the same and it was written out three times — in the sales invoice
workflow, in a *viewset* under payments, and inside the ledger's posting engine —
so a fix or a format change had to be made in three places and, predictably,
drifted (the posting engine hard-coded six-digit padding and ignored the
sequence's own ``padding`` column).

This is the single implementation. It takes the next value off a counter row
locked ``FOR UPDATE`` inside the caller's transaction — never ``MAX(number)+1``
(which hands the same number to two concurrent writers under READ COMMITTED) and
never a PostgreSQL ``SEQUENCE`` (which burns a number on rollback, leaving the
gap an auditor reads as a deleted document). Because the counter moves inside the
caller's transaction, a rollback returns the number, which is exactly what
"gapless" means.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional


def allocate_document_number(
    tenant_id: uuid.UUID,
    *,
    scope: str,
    prefix: str,
    on_date: date,
    collision_model: Optional[type] = None,
) -> str:
    """Return the next gapless number for ``(tenant, scope, year)``.

    ``scope`` names the counter (``"invoice"``, ``"payment"``, ``"journal:SAL"``);
    ``prefix`` seeds a freshly-created counter's format. Formatting always goes
    through :meth:`DocumentSequence.format`, so the tenant's configured ``padding``
    is honoured — the bug the old per-caller copies had.

    ``collision_model`` is the bootstrap escape hatch: a counter created *after*
    documents already carry numbers restarts at 1 and its first allocation
    collides with the document's unique constraint. Passing the document model
    makes the allocator skip numbers already taken, under the counter's row lock,
    so two concurrent writers still cannot land on the same value. Needed by any
    tenant whose data predates its counter (e.g. the seeded demo tenant).
    """
    from apps.accounting.models_sequence import DocumentSequence

    sequence, _ = DocumentSequence.all_tenants.select_for_update().get_or_create(
        tenant_id=tenant_id,
        scope=scope,
        year=on_date.year,
        defaults={"next_value": 1, "prefix": prefix},
    )
    value = sequence.next_value
    candidate = sequence.format(value)
    if collision_model is not None:
        taken = collision_model.all_tenants.filter(tenant_id=tenant_id)
        while taken.filter(number=candidate).exists():
            value += 1
            candidate = sequence.format(value)
    sequence.next_value = value + 1
    sequence.save(update_fields=["next_value", "updated_at"])
    return candidate
