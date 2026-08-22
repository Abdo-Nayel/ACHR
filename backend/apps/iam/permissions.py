"""
The guard layer: tenant resolution, RBAC, ABAC and re-authentication.

Every request passes three independent gates before it can touch a row.

1. ``apps.tenancy.middleware.TenantMiddleware`` establishes *which tenant* —
   binding the ``ContextVar`` that ``TenantManager`` reads and the PostgreSQL
   session variable that Row-Level Security reads.
2. :class:`HasPermission` answers *may this actor do this at all* from the
   role's ``Permission.codename`` set (RBAC).
3. :class:`ScopedQuerysetMixin` / :class:`ObjectPermissionMixin` answer
   *on which rows* by compiling ``ScopeRule.strategy`` into a ``Q`` (ABAC).

All three fail closed. An unknown permission codename denies; an absent scope
rule yields no rows; an unresolved tenant yields no rows because
``TenantManager.get_queryset`` returns ``.none()``. There is no code path in
this module where "we could not determine the answer" degrades to "allow".

See ``docs/05-permission-matrix.md`` for the catalogue this file enforces.
"""

from __future__ import annotations

import functools
import logging
import uuid
from typing import Any, Optional, Sequence

from django.core.cache import caches
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework import permissions as drf_permissions
from rest_framework.exceptions import APIException, NotFound, PermissionDenied

from apps.core.tenancy_context import (
    get_current_tenant_id,
)
from apps.iam.services.abac import (  # noqa: F401 (re-exported for callers)
    DENY_ALL,
    SCOPE_FIELDS,
    ActorScope,
    ScopeFields,
    build_scope_q,
    resolve_actor_scope,
)

logger = logging.getLogger(__name__)

#: Redis alias. A dedicated cache so that flushing the page cache during a
#: deploy cannot silently widen anybody's permissions by repopulating from a
#: stale source — this cache is only ever written by :func:`effective_permissions`.
PERMISSION_CACHE = "permissions"

#: Bumped whenever the *shape* of the cached payload changes. Without it, a
#: deploy that adds a field to the payload reads old dicts and KeyErrors on
#: every request until the TTL expires.
CACHE_SCHEMA_VERSION = "v1"

#: Short by design. The cache is invalidated explicitly on every write that
#: could change a permission set (see :func:`invalidate_permission_cache`);
#: the TTL is the backstop for the invalidation we forgot, not the mechanism.
PERMISSION_CACHE_TTL = 300

#: Re-authentication window for ``Permission.is_sensitive`` actions.
REAUTH_TTL_SECONDS = 300

TENANT_HEADER = "HTTP_X_TENANT_ID"
REAUTH_HEADER = "HTTP_X_REAUTH_TOKEN"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TenantSuspended(APIException):
    status_code = 403
    default_code = "tenant_suspended"
    default_detail = "This workspace is suspended. Read-only access only."


class ReauthRequired(APIException):
    status_code = 403
    default_code = "reauth_required"
    default_detail = "This action requires re-authentication."


class TenantResolutionError(APIException):
    status_code = 400
    default_code = "tenant_unresolved"
    default_detail = "Could not determine the tenant for this request."


# ---------------------------------------------------------------------------
# Cache keys
# ---------------------------------------------------------------------------

def permission_cache_key(tenant_id, user_id) -> str:
    """``perms:v1:{tenant_id}:{user_id}``.

    **The tenant_id is not optional and never will be.** The same ``User`` row
    is an Accountant in tenant A and a Read-Only Auditor in tenant B — that is
    the entire point of ``TenantMembership``. A key of ``perms:v1:{user_id}``
    caches whichever tenant the user happened to open first and then serves it
    to every other tenant they belong to, which is simultaneously a privilege
    escalation (auditor gains ``journal_entry.post``) and a cross-tenant
    information leak (the permission set discloses which modules tenant A has
    licensed). It fails silently: the user sees buttons that work.
    """
    return f"perms:{CACHE_SCHEMA_VERSION}:{tenant_id}:{user_id}"


def scope_cache_key(tenant_id, user_id) -> str:
    """``scopes:v1:{tenant_id}:{user_id}`` — same rule, same reason."""
    return f"scopes:{CACHE_SCHEMA_VERSION}:{tenant_id}:{user_id}"


def reauth_cache_key(tenant_id, user_id, jti) -> str:
    return f"reauth:{tenant_id}:{user_id}:{jti}"


def _cache():
    try:
        return caches[PERMISSION_CACHE]
    except Exception:  # pragma: no cover - misconfigured alias in dev
        return caches["default"]

# ---------------------------------------------------------------------------
# Effective permissions
# ---------------------------------------------------------------------------

def _load_effective_permissions(tenant_id, user_id) -> dict[str, Any]:
    """Read the permission set and scope rules straight from the database."""
    from apps.iam.models import RoleAssignment

    now = timezone.now()
    assignments = (
        RoleAssignment.objects.select_related("role", "department", "membership")
        .prefetch_related("role__permissions", "role__scope_rules")
        .filter(
            membership__tenant_id=tenant_id,
            membership__user_id=user_id,
            membership__is_active=True,
            valid_from__lte=now,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
    )

    codenames: set[str] = set()
    rules: dict[str, dict[str, Any]] = {}
    ranks: list[int] = []
    assigned_paths: list[str] = []

    for assignment in assignments:
        role = assignment.role
        ranks.append(role.rank)
        for perm in role.permissions.all():
            codenames.add(perm.codename)
        if assignment.department_id and getattr(assignment.department, "path", ""):
            assigned_paths.append(assignment.department.path)
        for rule in role.scope_rules.all():
            existing = rules.get(rule.resource)
            # A user holding two roles gets the *widest* scope of the two.
            # Taking the narrowest would make adding a role remove access,
            # which nobody expects and everybody reports as a bug.
            if existing is None or _strategy_breadth(rule.strategy) > _strategy_breadth(
                existing["strategy"]
            ):
                rules[rule.resource] = {
                    "strategy": rule.strategy,
                    "parameters": dict(rule.parameters or {}),
                }

    return {
        "permissions": sorted(codenames),
        "rules": rules,
        # min(): a user holding admin(10) and employee(50) acts at 10.
        # max() would let anyone de-escalate into a grant they may not make.
        "rank": min(ranks) if ranks else None,
        "assigned_department_paths": assigned_paths,
    }


#: Ordering used only to resolve a two-role conflict. Not an authority order.
_BREADTH = {
    "none": 0,
    "own_record": 1,
    "own_department": 2,
    "managed_employees": 2,
    "assigned_projects": 3,
    "scoped_department": 4,
    "department_subtree": 5,
    "all": 6,
}


def _strategy_breadth(strategy: str) -> int:
    return _BREADTH.get(strategy, 0)


def effective_permissions(tenant_id, user_id) -> dict[str, Any]:
    """Cached ``{"permissions": [...], "rules": {...}, "rank": int}``."""
    if tenant_id is None or user_id is None:
        return {"permissions": [], "rules": {}, "rank": None,
                "assigned_department_paths": []}

    key = permission_cache_key(tenant_id, user_id)
    cache = _cache()
    payload = cache.get(key)
    if payload is None:
        payload = _load_effective_permissions(tenant_id, user_id)
        cache.set(key, payload, PERMISSION_CACHE_TTL)
    return payload


def invalidate_permission_cache(tenant_id, user_id=None) -> None:
    """Drop cached permissions for one user, or for the whole tenant.

    Called from ``post_save``/``post_delete`` on ``RoleAssignment``,
    ``RolePermission``, ``ScopeRule``, ``Role`` and ``TenantMembership``.

    Revocation is the case that matters. A stale *grant* is an inconvenience;
    a stale *revocation* means the person you just removed keeps working for
    up to ``PERMISSION_CACHE_TTL``. That is the window an offboarding process
    is supposed to close, so invalidation is explicit and the TTL is only the
    safety net.
    """
    cache = _cache()
    if user_id is not None:
        cache.delete_many(
            [permission_cache_key(tenant_id, user_id), scope_cache_key(tenant_id, user_id)]
        )
        return
    # Tenant-wide (a Role's permissions changed): every member is affected.
    from apps.iam.models import TenantMembership

    user_ids = TenantMembership.objects.filter(tenant_id=tenant_id).values_list(
        "user_id", flat=True
    )
    keys: list[str] = []
    for uid in user_ids:
        keys.append(permission_cache_key(tenant_id, uid))
        keys.append(scope_cache_key(tenant_id, uid))
    if keys:
        cache.delete_many(keys)


def register_cache_invalidation() -> None:
    """Wire the signals. Call from ``IamConfig.ready()``.

    Deliberately not a module-level side effect: importing this module must
    not mutate global signal state, or a management command that imports it
    for one helper starts receiving signals it never asked for.
    """
    from django.db.models.signals import post_delete, post_save

    from apps.iam.models import (
        Role,
        RoleAssignment,
        RolePermission,
        ScopeRule,
        TenantMembership,
    )

    def _on_assignment(sender, instance, **kwargs):
        membership = instance.membership
        invalidate_permission_cache(membership.tenant_id, membership.user_id)

    def _on_membership(sender, instance, **kwargs):
        invalidate_permission_cache(instance.tenant_id, instance.user_id)

    def _on_role_change(sender, instance, **kwargs):
        role = instance if isinstance(instance, Role) else instance.role
        if role.tenant_id is not None:
            invalidate_permission_cache(role.tenant_id)
            return
        # A system role changed (a deploy ran sync_permissions). Every tenant
        # is affected; clearing per tenant would be O(tenants) round trips, so
        # the seeder flushes the whole permission cache once instead.
        _cache().clear()

    for signal in (post_save, post_delete):
        signal.connect(_on_assignment, sender=RoleAssignment, weak=False,
                       dispatch_uid=f"iam_perm_cache_assignment_{signal}")
        signal.connect(_on_membership, sender=TenantMembership, weak=False,
                       dispatch_uid=f"iam_perm_cache_membership_{signal}")
        signal.connect(_on_role_change, sender=RolePermission, weak=False,
                       dispatch_uid=f"iam_perm_cache_roleperm_{signal}")
        signal.connect(_on_role_change, sender=ScopeRule, weak=False,
                       dispatch_uid=f"iam_perm_cache_scoperule_{signal}")
        signal.connect(_on_role_change, sender=Role, weak=False,
                       dispatch_uid=f"iam_perm_cache_role_{signal}")


def user_permission_set(request) -> frozenset[str]:
    tenant_id = getattr(request, "tenant_id", None) or get_current_tenant_id()
    user_id = getattr(getattr(request, "user", None), "id", None)
    return frozenset(effective_permissions(tenant_id, user_id)["permissions"])


def actor_rank(request) -> Optional[int]:
    tenant_id = getattr(request, "tenant_id", None) or get_current_tenant_id()
    user_id = getattr(getattr(request, "user", None), "id", None)
    return effective_permissions(tenant_id, user_id)["rank"]


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

class HasPermission(drf_permissions.BasePermission):
    """Enforce ``view.required_permissions``.

    ::

        class InvoiceViewSet(ScopedQuerysetMixin, ModelViewSet):
            permission_classes = [IsAuthenticated, HasPermission]
            scope_resource = "invoice"
            required_permissions = {
                "GET":     ["sales.invoice.read"],
                "POST":    ["sales.invoice.create"],
                "PATCH":   ["sales.invoice.update"],
                "issue":   ["sales.invoice.issue"],     # DRF @action name
                "void":    ["sales.invoice.void"],
            }

    Lookup order is **action name first, then HTTP method**, so a custom
    ``@action`` can demand something stricter than the verb it rides on:
    ``POST /invoices/{id}/void`` is a POST, but it must require
    ``sales.invoice.void``, not ``sales.invoice.create``.

    A view with no ``required_permissions`` at all is **denied**, not allowed.
    Defaulting to open means every new viewset a developer adds is public
    until someone remembers; defaulting to closed means they find out in the
    first test run.
    """

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view) -> bool:
        required = self.required_for(request, view)
        if required is None:
            logger.error(
                "%s declares no required_permissions for %s %s — denying.",
                type(view).__name__, request.method, request.path,
            )
            return False
        if not required:
            return True

        held = user_permission_set(request)
        missing = [codename for codename in required if codename not in held]
        if missing:
            self.message = f"Missing permission: {missing[0]}."
            logger.info(
                "permission denied user=%s tenant=%s missing=%s path=%s",
                getattr(request.user, "id", None),
                get_current_tenant_id(), missing, request.path,
            )
            return False

        if any(is_sensitive(codename) for codename in required):
            assert_reauth(request)
        return True

    @staticmethod
    def required_for(request, view) -> Optional[Sequence[str]]:
        table = getattr(view, "required_permissions", None)
        if table is None:
            return None
        action = getattr(view, "action", None)
        if action and action in table:
            return table[action]
        if request.method in table:
            return table[request.method]
        # An explicit "*" fallback lets a read-only viewset declare one entry.
        return table.get("*")


#: Alias required by ``settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]``.
#: DRF resolves that dotted path while importing ``rest_framework.views``, so
#: the name must exist or nothing in the project imports at all. It points at
#: :class:`HasPermission` on purpose: as a *default*, it denies any view that
#: declares no ``required_permissions``, which is the fail-closed behaviour a
#: project-wide default must have.
HasTenantPermission = HasPermission


@functools.lru_cache(maxsize=2048)
def is_sensitive(codename: str) -> bool:
    """``Permission.is_sensitive`` for a codename.

    ``lru_cache`` is safe here and only here: the permission *catalogue* is a
    property of the software, seeded from ``config/permissions.json``, and does
    not change without a deploy. Caching a *user's* permissions this way would
    be a cross-tenant leak — see :func:`permission_cache_key`.
    """
    from apps.iam.models import Permission

    return Permission.objects.filter(codename=codename, is_sensitive=True).exists()


class SegregationOfDuties(PermissionDenied):
    """403 raised when an actor tries to approve what they prepared."""

    default_code = "segregation_of_duties"
    default_detail = (
        "You prepared this document, so you may not approve it. A second "
        "authorised approver is required."
    )


def assert_not_self_prepared(
    obj,
    resource: str,
    request,
    *,
    prepared_by_field: str = "created_by_id",
) -> bool:
    """Enforce the ``exclude_self_prepared`` parameter at the transition.

    Call this from an approve handler, immediately before the state change,
    the way :func:`assert_within_limit` is called. It answers one question --
    "may *this* actor approve *this* document" -- against the specific row,
    which is the question the control is actually about.

    ``prepared_by_field`` names the column that holds the preparer. It
    defaults to ``created_by_id`` because for most documents the person who
    entered it is the person who prepared it, but the caller should override
    it wherever the domain disagrees: a payroll run is prepared by whoever
    ran the calculation (``calculated_by_id``), which may not be whoever
    created the empty run.

    Returns ``True`` when the actor held ``break_glass`` and used it, so the
    caller can write the audit row that makes the override reviewable --
    ``docs/05-permission-matrix.md`` treats break-glass as *permitted and
    recorded*, never as *permitted and quiet*. Returns ``False`` on the
    ordinary path where the actor is not the preparer. Raises
    :class:`SegregationOfDuties` otherwise.
    """
    scope = resolve_actor_scope(request)
    rule = scope.rules.get(resource) or {}
    params = rule.get("parameters") or {}

    if not params.get("exclude_self_prepared"):
        return False

    prepared_by = getattr(obj, prepared_by_field, None)
    if not prepared_by or not scope.user_id or prepared_by != scope.user_id:
        return False

    if params.get("break_glass"):
        # Permitted, but never silently. The caller writes the TenantAuditLog
        # row; returning True rather than logging here keeps the audit record
        # attached to the transition that actually happened, with the
        # document's identity in it.
        logger.warning(
            "break-glass self-approval: user=%s resource=%s pk=%s",
            scope.user_id, resource, getattr(obj, "pk", None),
        )
        return True

    raise SegregationOfDuties()


def assert_within_limit(amount, resource: str, request) -> None:
    """Enforce the ``max_amount`` parameter at the transition boundary."""
    from decimal import Decimal

    scope = resolve_actor_scope(request)
    rule = scope.rules.get(resource) or {}
    raw = (rule.get("parameters") or {}).get("max_amount")
    if raw is None:
        return
    # str -> Decimal, never float: see apps/core/fields.py.
    if Decimal(str(amount)) > Decimal(str(raw)):
        raise PermissionDenied(
            f"Amount exceeds your approval limit of {raw} for {resource}."
        )


# ---------------------------------------------------------------------------
# View mixins
# ---------------------------------------------------------------------------

class ScopedQuerysetMixin:
    """Narrow every list/detail queryset by the actor's ABAC scope.

    ``scope_resource`` must match ``ScopeRule.resource``. A view that forgets
    it raises at request time rather than serving unscoped rows.

    Set ``abac = False`` on a viewset whose resource is *not* actor-scoped —
    a tenant-wide configuration catalogue (salary components, tax brackets,
    units of measure) that every member may list. Tenancy and Row-Level
    Security still apply; only the per-actor scope ``Q`` is skipped, and
    ``scope_resource`` is then not required. This replaced seven byte-identical
    ``RbacOnlyQuerysetMixin`` copies that each re-implemented ``get_queryset``
    to do exactly this.
    """

    scope_resource: Optional[str] = None
    #: Whether to narrow by the actor's ABAC scope. See the class docstring.
    abac: bool = True

    def get_scope_resource(self) -> str:
        resource = self.scope_resource
        if not resource:
            raise ImproperlyScopedView(
                f"{type(self).__name__} must declare scope_resource to use "
                f"ScopedQuerysetMixin."
            )
        return resource

    def get_queryset(self) -> QuerySet:
        queryset = super().get_queryset()
        if not self.abac:
            # RBAC + tenancy + RLS still apply; only the actor-scope narrowing
            # is skipped, for a resource that is tenant-wide by design.
            return queryset
        scope_q = build_scope_q(
            self.request.user, self.get_scope_resource(), request=self.request
        )
        # .filter(), never .exclude(): an exclude() over a nullable join drops
        # rows whose join is NULL and silently narrows more than intended.
        return queryset.filter(scope_q)


class ImproperlyScopedView(APIException):
    status_code = 500
    default_code = "view_misconfigured"
    default_detail = "This endpoint is not correctly scoped."


class ObjectPermissionMixin:
    """Per-object re-check on detail routes.

    ``get_queryset`` already applied the scope, so ``/invoices/{id}/`` outside
    the scope 404s naturally. This mixin exists for the cases where it does
    not: custom ``@action`` handlers that fetch by pk themselves, nested
    lookups, and anything that calls ``get_object_or_404`` directly. Checking
    twice costs one cheap ``Q`` evaluation and closes the gap between "the
    list is filtered" and "every handler is filtered".
    """

    def check_object_permissions(self, request, obj) -> None:
        super().check_object_permissions(request, obj)
        resource = getattr(self, "scope_resource", None)
        if not resource:
            return
        scope_q = build_scope_q(request.user, resource, request=request)
        model = type(obj)
        manager = getattr(model, "objects", None)
        if manager is None:  # pragma: no cover - defensive
            return
        if not manager.filter(scope_q, pk=obj.pk).exists():
            # 404, not 403. A 403 confirms the row exists, which for
            # sequentially guessable references is an enumeration oracle —
            # "invoice 9f3c… exists in this tenant but is not yours".
            raise NotFound()


# ---------------------------------------------------------------------------
# Re-authentication for sensitive permissions
# ---------------------------------------------------------------------------

def assert_reauth(request) -> None:
    """Require a fresh second factor for ``is_sensitive`` actions.

    Prevents the realistic attack, which is not password guessing: it is a
    bearer token lifted from an unlocked laptop or an XSS payload in a
    third-party widget. That token is enough to *read* the books — that is
    what bearer tokens are. It must not also be enough to approve a payroll
    run, rotate a gateway signing secret, or reopen a closed period.
    """
    token = request.META.get(REAUTH_HEADER, "")
    if not token:
        raise ReauthRequired()

    tenant_id = getattr(request, "tenant_id", None) or get_current_tenant_id()
    user_id = getattr(getattr(request, "user", None), "id", None)
    key = reauth_cache_key(tenant_id, user_id, token)

    cache = _cache()
    if cache.get(key) is None:
        raise ReauthRequired("Re-authentication token is missing, expired or not yours.")

    # Single-use. A replayable re-auth token is only a longer-lived session
    # token, which defeats the point; the client re-prompts per sensitive act.
    cache.delete(key)


def issue_reauth_token(tenant_id, user_id) -> str:
    """Called by ``POST /api/v1/auth/reauth`` after re-presenting password/TOTP."""
    token = uuid.uuid4().hex
    _cache().set(reauth_cache_key(tenant_id, user_id, token), True, REAUTH_TTL_SECONDS)
    return token


def require_reauth(view_func):
    """Decorator for handlers guarding a sensitive permission.

    ``HasPermission`` already calls :func:`assert_reauth` when any required
    codename has ``is_sensitive = True``. This decorator is for the handlers
    that do not go through a ``required_permissions`` table — function-based
    views, webhook admin tools, the tenant-switch endpoint — and for making the
    requirement visible at the call site where a reviewer will look for it.
    """

    @functools.wraps(view_func)
    def _wrapped(*args, **kwargs):
        request = _find_request(args)
        if request is None:  # pragma: no cover - defensive
            raise ReauthRequired("Could not locate the request to re-authenticate.")
        assert_reauth(request)
        return view_func(*args, **kwargs)

    _wrapped.requires_reauth = True
    return _wrapped


def _find_request(args: tuple) -> Any:
    """Support both ``def view(request)`` and ``def method(self, request)``."""
    for candidate in args[:2]:
        if hasattr(candidate, "META") and hasattr(candidate, "method"):
            return candidate
    return None


__all__ = [
    "HasPermission",
    "ScopedQuerysetMixin",
    "ObjectPermissionMixin",
    "ActorScope",
    "build_scope_q",
    "resolve_actor_scope",
    "effective_permissions",
    "invalidate_permission_cache",
    "register_cache_invalidation",
    "permission_cache_key",
    "scope_cache_key",
    "assert_reauth",
    "assert_within_limit",
    "issue_reauth_token",
    "require_reauth",
    "is_sensitive",
    "actor_rank",
    "ReauthRequired",
    "TenantSuspended",
    "TenantResolutionError",
    "SCOPE_FIELDS",
    "PERMISSION_CACHE_TTL",
    "REAUTH_TTL_SECONDS",
]
