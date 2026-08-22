# Phase 4 — Split the IAM god module

*`apps/iam/permissions.py` was 1,170 lines holding five unrelated concerns. Two
of them were genuine defects, not just clutter.*

## What changed

### F12 — permission-cache invalidation was never wired *(live security bug)*
`register_cache_invalidation()` connects `post_save`/`post_delete` on
RoleAssignment, Role, RolePermission, ScopeRule and TenantMembership to drop the
cached permission set. Its docstring said "call from `IamConfig.ready()`" — but
`IamConfig` had no `ready()`, so **revoking a role kept working for up to the
300-second cache TTL**: an offboarding hole. Fixed:
- `IamConfig.ready()` now calls `register_cache_invalidation()`.
- Added a dedicated **`permissions` cache alias** (base + dev) *without*
  `tenant_key_func` — the permission keys already embed the tenant id, and the
  default cache's ambient-tenant prefixing would have made a key written under
  one bound tenant invisible to invalidation running under another. Previously
  `_cache()` silently fell through to `default`, so even a wired invalidation
  could have missed.
- New test `tests/test_permission_cache_invalidation.py` proves a revoked role
  clears the cache on the next lookup, not 300s later.

### F10/F11 — a dead second tenant middleware, with a leaking contract
`TenantResolutionMiddleware` (151 lines) was **not in `settings.MIDDLEWARE`** —
dead — yet still exported, and four live readers (`user_permission_set`,
`actor_rank`, `resolve_actor_scope`, `assert_reauth`) read its `request.tenant`
attribute first (which the *active* middleware never sets) before falling back to
`get_current_tenant_id()`. Deleted the class; the readers now read
`request.tenant_id` (what the active `apps.tenancy.middleware.TenantMiddleware`
actually sets) then fall back. Removed its now-orphaned imports and its stale
docstring/serializer references.

### F9 — ABAC engine extracted to the module the code already documented
`apps/iam/models.py` documents `apps.iam.services.abac.build_scope_q()` — a
module that did not exist. It does now: `ScopeFields`, `SCOPE_FIELDS`, `DENY_ALL`,
`ActorScope`, `resolve_actor_scope`, `build_scope_q` and the strategy compilers
(~360 lines) moved into `apps/iam/services/abac.py`. `permissions.py` re-exports
them, so every existing `from apps.iam.permissions import build_scope_q` still
works. The dependency runs one way — `permissions` imports `abac` at module load;
`abac` imports the two cache helpers it needs *inside* the two functions that use
them — so there is no import cycle.

**`apps/iam/permissions.py`: 1,170 → 654 lines.**

## Verification (PostgreSQL 18, as `erp_app`)

| Check | Result |
|---|---|
| `pytest` | **298 passed** (296 + 2 new) |
| `manage.py check` | 0 issues (no import cycle) |
| OpenAPI schema vs baseline | paths + components **byte-identical** (D2) |
| ruff F-category on both files | clean |

## Deferred
The remaining concerns in `permissions.py` (the effective-permissions/cache layer,
re-auth + segregation-of-duties, and the view mixins) are cohesive and could each
become their own module for symmetry with `services/abac.py`. Left as optional
follow-up: `permissions.py` is now a reasonable size and the highest-value split
(the promised `abac` module) plus both real bugs are done. `HasTenantPermission`
= `HasPermission` and the IAM/tenancy viewset-mixin unification (F13, from Phase 2)
also remain a nice-to-have.
