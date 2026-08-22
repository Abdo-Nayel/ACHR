"""Creating an account through the API allocates its code server-side.

The chart is a positional tree, so a client names an account under a parent and
picks its side — it never types a number. These tests pin that contract through
the serializer (the viewset is a thin wrapper over it).
"""

from __future__ import annotations

import pytest
from rest_framework.exceptions import ValidationError

from apps.accounting.models import Account
from apps.accounting.serializers import AccountSerializer

pytestmark = pytest.mark.django_db


def _ctx(tenant):
    return {"tenant_id": tenant.id}


def _create(tenant, **data):
    serializer = AccountSerializer(data=data, context=_ctx(tenant))
    serializer.is_valid(raise_exception=True)
    return serializer.save()


def _root(tenant, name="Assets"):
    return _create(tenant, parent=None, name=name)


def test_create_allocates_a_segmented_code_under_a_parent(tenant):
    root = _root(tenant)
    l2 = _create(tenant, parent=str(root.id), name="Current assets")
    assert l2.level == 2
    assert l2.segment_code == 1
    assert l2.code == "1.1"
    assert l2.full_code == root.full_code * 10**4 + 1


def test_siblings_get_the_next_free_number(tenant):
    root = _root(tenant)
    a = _create(tenant, parent=str(root.id), name="Current assets")
    b = _create(tenant, parent=str(root.id), name="Fixed assets")
    assert (a.segment_code, b.segment_code) == (1, 2)


def test_a_leaf_account_cannot_be_given_children(tenant):
    # Build a full 1..5 chain; the level-5 account is a postable leaf.
    node = _root(tenant)
    for name in ("Current assets", "Cash", "Cash on hand", "Main cash box"):
        node = _create(tenant, parent=str(node.id), name=name)
    assert node.level == 5 and node.is_postable
    with pytest.raises(ValidationError):
        _create(tenant, parent=str(node.id), name="Too deep")


def test_the_chosen_side_is_stored(tenant):
    root = _root(tenant)
    node = root
    for name in ("Liabilities L2", "L3", "L4"):
        node = _create(tenant, parent=str(node.id), name=name)
    leaf = _create(tenant, parent=str(node.id), name="A payable",
                   normal_balance_override="credit")
    assert leaf.normal_balance == "credit"


def test_code_is_read_only_on_create(tenant):
    root = _root(tenant)
    # A client-supplied code is ignored; the server allocates it.
    l2 = _create(tenant, parent=str(root.id), name="Current assets", code="999")
    assert l2.code == "1.1"


def test_update_cannot_reparent_a_coded_account(tenant):
    root = _root(tenant)
    a = _create(tenant, parent=str(root.id), name="Group A")
    b = _create(tenant, parent=str(root.id), name="Group B")
    child = _create(tenant, parent=str(a.id), name="Child")
    serializer = AccountSerializer(
        instance=child, data={"parent": str(b.id), "name": "Renamed"},
        partial=True, context=_ctx(tenant),
    )
    serializer.is_valid(raise_exception=True)
    updated = serializer.save()
    assert updated.name == "Renamed"
    assert updated.parent_id == a.id          # re-parent ignored
    updated.refresh_from_db()
    assert Account.objects.get(pk=child.pk).parent_id == a.id
