"""The 5-level chart coding scheme: encoding, hierarchy rules, allocation.

Ports the reference GL's guarantees to ACHR — every account is level 1–5, the
full code packs the ancestor segments by fixed digit widths, only level-5
accounts are postable, and the server allocates the next segment under a parent.
"""

from __future__ import annotations

import pytest

from apps.accounting.services.coding import (
    LEVEL_CODE_WIDTHS,
    allocate_account,
    compute_full_code,
    format_full_code,
    max_sibling_code,
    validate_account_hierarchy,
)
from apps.core.exceptions import DomainError

pytestmark = pytest.mark.django_db


# -- pure encoding (no database) --------------------------------------------

def test_full_code_packs_segments_by_level_width():
    assert LEVEL_CODE_WIDTHS == (4, 4, 3, 2, 4)
    l1 = compute_full_code(None, 1, 1)                 # 1
    l2 = compute_full_code(l1, 1, 2)                   # 1 <<4 + 1
    l3 = compute_full_code(l2, 1, 3)                   # <<3
    l4 = compute_full_code(l3, 1, 4)                   # <<2
    l5 = compute_full_code(l4, 1, 5)                   # <<4
    assert l1 == 1
    assert l2 == 1 * 10**4 + 1
    assert l5 == ((((1 * 10**4 + 1) * 10**3 + 1) * 10**2 + 1) * 10**4 + 1)
    assert format_full_code(l5, 5) == "1.1.1.1.1"


def test_max_sibling_code_is_the_slot_width_ceiling():
    assert max_sibling_code(5) == 9999   # leaf slot is 4 digits
    assert max_sibling_code(4) == 99      # subsidiary-ledger-2 slot is 2 digits


@pytest.mark.parametrize("level,parent_level,postable", [
    (1, None, False),   # a root section
    (2, 1, False),      # a summary under a section
    (5, 4, True),       # a postable leaf
])
def test_legal_hierarchies_pass(level, parent_level, postable):
    validate_account_hierarchy(level, parent_level, postable)  # must not raise


@pytest.mark.parametrize("level,parent_level,postable", [
    (1, None, True),    # a root cannot be postable
    (2, None, False),   # a root must be level 1
    (3, 1, False),      # child must be parent.level + 1
    (4, 3, True),       # only level 5 may be postable
    (5, 4, False),      # a level-5 account must be postable
])
def test_illegal_hierarchies_are_refused(level, parent_level, postable):
    with pytest.raises(DomainError):
        validate_account_hierarchy(level, parent_level, postable)


# -- server-side allocation (database) --------------------------------------

def _chain(tenant):
    """Allocate a full 1→5 chain and return the five accounts."""
    root = allocate_account(tenant.id, parent=None, name="Assets")
    l2 = allocate_account(tenant.id, parent=root, name="Current assets")
    l3 = allocate_account(tenant.id, parent=l2, name="Cash & equivalents")
    l4 = allocate_account(tenant.id, parent=l3, name="Cash")
    l5 = allocate_account(tenant.id, parent=l4, name="Main cash box")
    return root, l2, l3, l4, l5


def test_allocation_builds_a_coded_five_level_chain(tenant):
    root, l2, l3, l4, l5 = _chain(tenant)
    assert [a.level for a in (root, l2, l3, l4, l5)] == [1, 2, 3, 4, 5]
    assert [a.segment_code for a in (root, l2, l3, l4, l5)] == [1, 1, 1, 1, 1]
    assert l5.code == "1.1.1.1.1"
    # Only the leaf is postable; the four summaries are not.
    assert [a.is_postable for a in (root, l2, l3, l4, l5)] == [False, False, False, False, True]


def test_siblings_get_consecutive_segment_numbers(tenant):
    root = allocate_account(tenant.id, parent=None, name="Assets")
    a = allocate_account(tenant.id, parent=root, name="Current assets")
    b = allocate_account(tenant.id, parent=root, name="Fixed assets")
    assert (a.segment_code, b.segment_code) == (1, 2)
    assert a.full_code != b.full_code


def test_leaf_inherits_its_section_from_the_root(tenant):
    _, _, _, _, l5 = _chain(tenant)
    assert l5.type == "asset"   # inherited down from the root section
