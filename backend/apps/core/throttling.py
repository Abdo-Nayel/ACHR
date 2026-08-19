"""
Rate limiting, scoped per tenant.

The scope key includes the tenant id, not just the user id. Two reasons:

* A user who belongs to several tenants (an outsourced accountant) should not
  have work for client A consume the quota for client B.
* An abusive tenant must be containable without throttling every customer
  who happens to share an egress IP with them.

Anonymous throttling still falls back to the client IP, because before login
there is no tenant to scope by — and login is exactly the endpoint that needs
brute-force protection.
"""

from __future__ import annotations

from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle, UserRateThrottle

from apps.core.tenancy_context import get_current_tenant_id


class TenantScopedUserRateThrottle(UserRateThrottle):
    scope = "user"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        tenant_id = get_current_tenant_id() or "none"
        return self.cache_format % {
            "scope": self.scope,
            "ident": f"{tenant_id}:{request.user.pk}",
        }


class TenantScopedAnonRateThrottle(AnonRateThrottle):
    scope = "anon"


class BurstThrottle(ScopedRateThrottle):
    """For expensive endpoints: report generation, exports, bulk imports.

    Set ``throttle_scope = "reports"`` on the view. Without a separate bucket,
    one user running twelve P&L exports consumes the whole tenant's normal
    request quota and everyone else sees 429s from an unrelated action.
    """

    def get_cache_key(self, request, view):
        scope = getattr(view, "throttle_scope", None)
        if scope is None or not request.user or not request.user.is_authenticated:
            return None
        tenant_id = get_current_tenant_id() or "none"
        return self.cache_format % {
            "scope": scope,
            "ident": f"{tenant_id}:{request.user.pk}",
        }
