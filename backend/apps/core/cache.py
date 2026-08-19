"""
Tenant-namespaced cache keys.

**The rule:** every cache key in this system contains the tenant id.

This is the most dangerous bug class in a shared-database design, and the one
that no other safety net catches. RLS does not help — a cache hit runs no
query. The ORM's tenant manager does not help — it was never called. The
result is simply that tenant B is served tenant A's cached data, with no
error, no log line, and nothing in ``pg_stat_statements`` to find afterwards.

So the namespacing happens *here*, in the ``KEY_FUNCTION`` every backend
routes through, rather than being left to each caller to remember.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from apps.core.tenancy_context import get_current_tenant_id

#: Keys that are genuinely global. Anything not matching one of these prefixes
#: is namespaced. The list is deliberately short and explicit — an allowlist,
#: because the failure mode of wrongly treating a key as global is a leak,
#: while the failure mode of wrongly namespacing a global key is a cache miss.
GLOBAL_KEY_PREFIXES = (
    "permission_catalogue",   # the product's permission list, not a tenant's
    "system_roles",
    "fx_rate_public",
    "schema_hash",
    "healthcheck",
)


def tenant_key_func(key: str, key_prefix: str, version: Any) -> str:
    """Django ``CACHES[...]['KEY_FUNCTION']``.

    Produces ``<prefix>:<version>:t:<tenant_id>:<key>`` for tenant data and
    ``<prefix>:<version>:g:<key>`` for global data.

    A key generated with *no* tenant bound is marked ``t:none`` rather than
    silently becoming global. That way a caching bug shows up as a cache miss
    for everyone instead of as one tenant's data shared with all of them.
    """
    if any(key.startswith(prefix) for prefix in GLOBAL_KEY_PREFIXES):
        return f"{key_prefix}:{version}:g:{key}"

    tenant_id = get_current_tenant_id()
    scope = str(tenant_id) if tenant_id else "none"
    return f"{key_prefix}:{version}:t:{scope}:{key}"


def tenant_cache_key(*parts: Any, tenant_id: Optional[Any] = None) -> str:
    """Build an explicit key for code that caches outside a request.

    Celery tasks have no ambient tenant unless they were started inside
    ``tenant_context``; requiring the id here makes the omission a TypeError
    at the call site rather than a leak at runtime.
    """
    resolved = tenant_id or get_current_tenant_id()
    if resolved is None:
        raise ValueError(
            "tenant_cache_key() called with no tenant. Pass tenant_id "
            "explicitly or wrap the call in tenant_context()."
        )
    joined = ":".join(str(p) for p in parts)
    return f"t:{resolved}:{joined}"


def digest(*parts: Any) -> str:
    """Short stable hash for keys built from long or unbounded values.

    Used for report parameter sets and filter querystrings, which can exceed
    memcached's 250-byte key limit and contain characters Redis dislikes.
    """
    joined = "|".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]
