"""Revoking access must take effect on the next request, not 300 seconds later.

``apps.iam.permissions.register_cache_invalidation`` wires ``post_save`` /
``post_delete`` on RoleAssignment (and Role, RolePermission, ScopeRule,
TenantMembership) to drop the cached permission set. Its docstring said "call
from IamConfig.ready()" — but nothing ever did, so a revoked role kept working
until the cache TTL expired: a real offboarding hole. ``IamConfig.ready`` now
makes that call; these tests prove the invalidation fires.
"""

from __future__ import annotations

import pytest

from apps.iam.models import RoleAssignment, TenantMembership
from apps.iam.permissions import (
    _cache,
    effective_permissions,
    permission_cache_key,
)

pytestmark = pytest.mark.django_db


def test_revoking_a_role_assignment_clears_the_cache_immediately(tenant, accountant_user):
    # Warm the cache for this (tenant, user).
    effective_permissions(tenant.id, accountant_user.id)
    key = permission_cache_key(tenant.id, accountant_user.id)
    assert _cache().get(key) is not None, "permissions should be cached after a lookup"

    # Revoke the role. The post_delete signal must invalidate the cache.
    membership = TenantMembership.objects.get(tenant=tenant, user=accountant_user)
    for assignment in RoleAssignment.objects.filter(membership=membership):
        assignment.delete()

    assert _cache().get(key) is None, (
        "revoking a role must clear the cached permission set, not leave it "
        "live until the TTL expires"
    )


def test_permissions_cache_alias_is_configured(settings):
    """The permission cache must be its own alias, not the silent fall-through
    to ``default`` (whose tenant_key_func would break cross-tenant invalidation)."""
    assert "permissions" in settings.CACHES
