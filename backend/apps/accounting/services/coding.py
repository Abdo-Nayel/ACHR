"""The 5-level chart-of-accounts coding scheme.

Ported from the reference GL's ``core/accounts/validators.py`` + the allocation
core of ``accounts/services.py``. Every account sits at a level 1–5 and carries
a small ``segment_code`` (its number within its parent); the absolute
``full_code`` packs the ancestor segments into one integer using fixed per-level
digit widths, so an account's place in the tree is a single sortable, uniquely
indexable number and the human ``code`` is just that number formatted.

The rules, in one place:

* level 1 = a financial-statement section (Assets, …) — never postable;
* each child is exactly ``parent.level + 1``; the tree is exactly five deep;
* **only level-5 accounts may be posted to, and every level-5 account must be**;
* codes are allocated **top-down by the server** — a caller names an account, it
  never types a number — from ``max(sibling)+1`` under a row lock so two
  concurrent creates under one parent cannot collide.
"""

from __future__ import annotations

import uuid
from typing import Optional

from django.db import transaction
from django.db.models import Max

from apps.accounting.models import Account
from apps.core.exceptions import DomainError

MAX_LEVEL = 5

#: Digits reserved in ``full_code`` for each level's own number, in level order.
#: The leaf level (5) and the top two are four digits wide — a chart has a
#: handful of sections but many accounts under one subsidiary ledger, and the
#: leaf is where a real chart grows. Widest full code = 4+4+3+2+4 = 17 digits,
#: inside a signed BigInteger.
LEVEL_CODE_WIDTHS = (4, 4, 3, 2, 4)


def _slot_width(level: int) -> int:
    if not 1 <= level <= len(LEVEL_CODE_WIDTHS):
        raise DomainError(f"Account level must be between 1 and {MAX_LEVEL}.")
    return LEVEL_CODE_WIDTHS[level - 1]


def compute_full_code(parent_full_code: Optional[int], code: int, level: int) -> int:
    """Pack this account's segment onto its parent's full code."""
    if level == 1:
        return code
    if parent_full_code is None:
        raise DomainError("A parent account is required for any level above 1.")
    return parent_full_code * 10 ** _slot_width(level) + code


def max_sibling_code(level: int) -> int:
    """The largest segment number that still fits this level's slot.

    A number wider than the slot carries into the parent's digits and silently
    produces a full code that reads as if it belonged to the *next* parent, so
    the coder refuses it rather than allocate it.
    """
    return 10 ** _slot_width(level) - 1


def format_full_code(full_code: int, level: int) -> str:
    """Human code: the segments dotted apart, e.g. ``1.1.1.1.1``.

    Decoded straight from the packed ``full_code`` so the display can never
    drift from the stored number.
    """
    segments: list[int] = []
    remaining = full_code
    for width in reversed(LEVEL_CODE_WIDTHS[:level]):
        segments.append(remaining % (10 ** width))
        remaining //= 10 ** width
    return ".".join(str(seg) for seg in reversed(segments))


def validate_account_hierarchy(
    level: int, parent_level: Optional[int], is_postable: bool
) -> None:
    """Raise :class:`DomainError` if the level/parent/postable combination is
    illegal. Silent (returns ``None``) when it is fine."""
    if not 1 <= level <= MAX_LEVEL:
        raise DomainError(f"Account level must be between 1 and {MAX_LEVEL}.")
    if parent_level is None:
        if level != 1:
            raise DomainError("A root account must be at level 1.")
        if is_postable:
            raise DomainError("A level-1 account cannot be postable.")
        return
    expected = parent_level + 1
    if level != expected:
        raise DomainError(f"Account level must be {expected} under this parent.")
    if is_postable and level != MAX_LEVEL:
        raise DomainError(f"Only level-{MAX_LEVEL} accounts may be postable.")
    if level == MAX_LEVEL and not is_postable:
        raise DomainError(f"A level-{MAX_LEVEL} account must be postable.")


def next_sibling_code(tenant_id: uuid.UUID, parent: Optional[Account]) -> int:
    """The next free segment number under ``parent`` (or among the roots).

    ``max(sibling)+1`` starting at 1 — inactive siblings still count, so a
    deactivated number is never reissued (which would make a code point at two
    accounts over time).
    """
    siblings = Account.all_tenants.filter(tenant_id=tenant_id, parent=parent)
    highest = siblings.aggregate(m=Max("segment_code"))["m"]
    return (highest or 0) + 1


@transaction.atomic
def allocate_account(
    tenant_id: uuid.UUID,
    *,
    parent: Optional[Account],
    name: str,
    normal_balance: str = "",
    requires_party: bool = False,
    income_category: str = "none",
    currency: Optional[str] = None,
    is_reconcilable: bool = False,
    user_id: Optional[uuid.UUID] = None,
) -> Account:
    """Create an account, allocating its segment/full code server-side.

    The level is derived (root → 1, else parent.level + 1); postability is
    implied (a level-5 account is postable, anything above is a summary). The
    section ``type`` and ``income_category`` are inherited from the parent
    unless overridden. Serialised per parent by a row lock so concurrent creates
    under one parent get distinct segment numbers.
    """
    name = (name or "").strip()
    if not name:
        raise DomainError("An account name is required.")

    if parent is not None:
        # Lock the parent row so two concurrent children can't take the same
        # segment. Root allocation is serialised by the parent-is-null branch's
        # unique constraint plus the surrounding transaction.
        parent = (
            Account.all_tenants.select_for_update()
            .filter(pk=parent.pk, tenant_id=tenant_id)
            .first()
        )
        if parent is None:
            raise DomainError("The parent account does not exist for this tenant.")

    parent_level = parent.level if parent is not None else None
    level = 1 if parent is None else (parent_level or 0) + 1
    is_postable = level == MAX_LEVEL
    validate_account_hierarchy(level, parent_level, is_postable)

    segment = next_sibling_code(tenant_id, parent)
    if segment > max_sibling_code(level):
        raise DomainError("No account numbers remain under this parent.")

    parent_full = parent.full_code if parent is not None else None
    full_code = compute_full_code(parent_full, segment, level)

    section = parent.type if parent is not None else _DEFAULT_TYPE_FOR_ROOT
    account = Account(
        tenant_id=tenant_id,
        parent=parent,
        level=level,
        segment_code=segment,
        full_code=full_code,
        code=format_full_code(full_code, level),
        name=name,
        type=section,
        is_postable=is_postable,
        normal_balance_override=normal_balance or "",
        income_category=income_category if is_postable else "none",
        requires_party=requires_party and is_postable,
        currency=currency,
        is_reconcilable=is_reconcilable and is_postable,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    account.save()
    return account


#: A brand-new root created through the coder with no section chosen defaults to
#: an asset; the seeded chart sets the five real sections explicitly.
_DEFAULT_TYPE_FOR_ROOT = "asset"
