"""
The guard layer: tenant resolution, RBAC, ABAC and re-authentication.

Every request passes three independent gates before it can touch a row.

1. :class:`TenantResolutionMiddleware` establishes *which tenant* — binding
   the ``ContextVar`` that ``TenantManager`` reads and the PostgreSQL session
   variable that Row-Level Security reads.
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
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from django.core.cache import caches
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework import permissions as drf_permissions
from rest_framework.exceptions import APIException, NotFound, PermissionDenied

from apps.core.tenancy_context import (
    _current_tenant_id,
    _current_user_id,
    bind_database_session,
    get_current_tenant_id,
    get_current_user_id,
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
# Tenant resolution middleware
# ---------------------------------------------------------------------------

class TenantResolutionMiddleware:
    """Resolve the tenant, validate membership, bind context, always unbind.

    Resolution order — **the JWT claim wins, then the header, then the host**:

    1. ``request.auth["tid"]`` — the tenant the access token was minted for.
    2. ``X-Tenant-ID`` — must *match* the claim if a claim exists; it may only
       select a tenant when the token is tenant-agnostic (API keys, the
       tenant-switch endpoint).
    3. The ``Host`` header, matched against ``Tenant.slug`` or a verified
       ``TenantDomain``.

    Reversing 1 and 2 is the classic multi-tenant break: an attacker with a
    valid token for tenant A sets ``X-Tenant-ID: <B>`` and, if the header is
    trusted first, the whole request runs bound to B. The claim is signed; the
    header is not. The header exists only because a browser cannot set a
    sub-domain on an XHR to an apex API host, and because sub-domain routing
    breaks under corporate proxies that rewrite ``Host``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = None
        membership = None
        try:
            tenant = self.resolve_tenant(request)
            if tenant is not None:
                membership = self.resolve_membership(request, tenant)
                self.assert_tenant_usable(request, tenant)

                request.tenant = tenant
                request.membership = membership

                _current_tenant_id.set(tenant.id)
                _current_user_id.set(getattr(request.user, "id", None))

                # bind_database_session issues SET LOCAL, which is scoped to
                # the *transaction*. Outside one it is a no-op that silently
                # leaves RLS unbound, so the atomic block is load-bearing.
                with transaction.atomic():
                    bind_database_session(tenant.id)
                    return self.get_response(request)

            return self.get_response(request)
        finally:
            # NOT optional, and not merely tidy.
            #
            # gunicorn/uvicorn workers are pooled and long-lived. A ContextVar
            # set on request N and not reset is still set when request N+1
            # arrives on the same worker. If N+1 is unauthenticated, or is a
            # health check, or fails tenant resolution and returns early, its
            # ORM queries inherit tenant N and TenantManager happily filters
            # to *someone else's* tenant. The failure is invisible in tests
            # (one request per process) and catastrophic in production.
            #
            # It must be `finally`, not a line after get_response: an
            # exception, a DisallowedHost, or a middleware short-circuit above
            # us all skip the happy path while leaving the context set.
            _current_tenant_id.set(None)
            _current_user_id.set(None)

    # -- resolution steps ---------------------------------------------------

    def resolve_tenant(self, request):
        from apps.tenancy.models import Tenant, TenantDomain

        claim_tid = self._claim_tenant_id(request)
        header_tid = self._parse_uuid(request.META.get(TENANT_HEADER))

        if claim_tid is not None:
            if header_tid is not None and header_tid != claim_tid:
                # Do not silently prefer one. A mismatch is either a bug in
                # the client or an attempt; both deserve a 400 and a log line.
                logger.warning(
                    "tenant header/claim mismatch: header=%s claim=%s user=%s",
                    header_tid, claim_tid, getattr(request.user, "id", None),
                )
                raise TenantResolutionError("X-Tenant-ID does not match the access token.")
            return Tenant.objects.filter(pk=claim_tid).first()

        if header_tid is not None:
            return Tenant.objects.filter(pk=header_tid).first()

        host = (request.get_host() or "").split(":")[0].lower()
        domain = (
            TenantDomain.objects.select_related("tenant")
            .filter(domain=host, verified_at__isnull=False)
            .first()
        )
        if domain is not None:
            return domain.tenant

        label = host.split(".")[0]
        if label and label not in {"www", "api", "app", "localhost"}:
            return Tenant.objects.filter(slug=label).first()
        return None

    def resolve_membership(self, request, tenant):
        """A signed claim is not authorisation. Membership is re-read per request.

        ``TenantMembership.is_active`` can flip to False the moment someone is
        fired; a 15-minute access token minted before that must stop working
        now, not in 15 minutes.
        """
        from apps.iam.models import TenantMembership

        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return None

        membership = (
            TenantMembership.objects.select_related("employee")
            .filter(tenant_id=tenant.id, user_id=user.id, is_active=True)
            .first()
        )
        if membership is None:
            if getattr(user, "is_platform_admin", False):
                # Platform admins are *not* implicitly members. They must
                # enter platform_admin_context() explicitly, which audit-logs.
                return None
            # 404, not 403: confirming that a tenant exists to a non-member is
            # itself a leak (it discloses your competitor is a customer).
            raise NotFound("No such workspace.")
        return membership

    def assert_tenant_usable(self, request, tenant) -> None:
        """SUSPENDED/CLOSED tenants are readable but not writable.

        ``Tenant.is_operational`` deliberately keeps PAST_DUE writable-adjacent
        so a customer in arrears can always export their own books — locking
        someone out of their accounting records over an invoice dispute is
        both a support incident and, in several jurisdictions, unlawful.
        """
        if request.method in drf_permissions.SAFE_METHODS:
            return
        if not tenant.is_operational:
            raise TenantSuspended()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _claim_tenant_id(request):
        auth = getattr(request, "auth", None)
        if isinstance(auth, dict):
            return TenantResolutionMiddleware._parse_uuid(auth.get("tid"))
        return TenantResolutionMiddleware._parse_uuid(getattr(auth, "get", lambda *_: None)("tid"))

    @staticmethod
    def _parse_uuid(value) -> Optional[uuid.UUID]:
        if not value:
            return None
        try:
            return uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Effective permissions
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ActorScope:
    """Everything ``build_scope_q`` needs, resolved once per request."""

    user_id: Optional[uuid.UUID] = None
    employee_id: Optional[uuid.UUID] = None
    department_id: Optional[uuid.UUID] = None
    #: Materialised org-chart path of the actor's own department, e.g.
    #: ``/8f1c…/2b90…/``. Prefix-matched for ``department_subtree``.
    department_path: str = ""
    project_ids: tuple[uuid.UUID, ...] = ()
    #: Paths of the departments named on the actor's RoleAssignments.
    #: Drives ``scoped_department`` — note the plural: one person can hold the
    #: same role twice for two branches.
    assigned_department_paths: tuple[str, ...] = ()
    #: resource -> {"strategy": str, "parameters": dict}
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Most authoritative rank the actor holds (lowest number wins). 0 is the
    #: tenant Owner. Read by ``build_scope_q`` to decide what a *missing*
    #: scope rule means.
    rank: Optional[int] = None


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
    tenant_id = getattr(getattr(request, "tenant", None), "id", None) or get_current_tenant_id()
    user_id = getattr(getattr(request, "user", None), "id", None)
    return frozenset(effective_permissions(tenant_id, user_id)["permissions"])


def actor_rank(request) -> Optional[int]:
    tenant_id = getattr(getattr(request, "tenant", None), "id", None) or get_current_tenant_id()
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


# ---------------------------------------------------------------------------
# ABAC
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScopeFields:
    """How a resource's model spells the columns the strategies need."""

    owner: str = "created_by_id"
    employee: Optional[str] = None
    department: Optional[str] = None
    project: Optional[str] = None
    manager: Optional[str] = None


#: Resource name (matching ``ScopeRule.resource``) -> field spelling.
#: A resource missing from this map cannot be scoped, and
#: :func:`build_scope_q` therefore denies it. Fail closed: a new model whose
#: author forgot to register it returns nothing rather than everything.
SCOPE_FIELDS: dict[str, ScopeFields] = {
    "employee": ScopeFields(
        owner="id", employee="id", department="department", manager="manager_id"
    ),
    "department": ScopeFields(owner="id", department="id"),
    "payslip": ScopeFields(
        employee="employee_id", department="employee__department",
        manager="employee__manager_id",
    ),
    "payroll_run": ScopeFields(owner="created_by_id"),
    "leave_request": ScopeFields(
        employee="employee_id", department="employee__department",
        manager="employee__manager_id",
    ),
    "leave_balance": ScopeFields(
        employee="employee_id", department="employee__department",
        manager="employee__manager_id",
    ),
    "attendance": ScopeFields(
        employee="employee_id", department="employee__department",
        manager="employee__manager_id",
    ),
    "document": ScopeFields(
        employee="employee_id", department="employee__department",
        manager="employee__manager_id",
    ),
    "expense": ScopeFields(
        owner="created_by_id", employee="employee_id",
        department="department", project="project_id", manager="employee__manager_id",
    ),
    "timesheet_entry": ScopeFields(
        owner="created_by_id", employee="employee_id",
        department="employee__department", project="project_id",
        manager="employee__manager_id",
    ),
    "project": ScopeFields(owner="created_by_id", project="id", department="department"),
    "task": ScopeFields(owner="created_by_id", project="project_id"),
    "invoice": ScopeFields(owner="created_by_id", project="project_id"),
    "credit_note": ScopeFields(owner="created_by_id", project="project_id"),
    "customer": ScopeFields(owner="created_by_id"),
    "payment": ScopeFields(owner="created_by_id"),
    "refund": ScopeFields(owner="created_by_id"),
    "bill": ScopeFields(owner="created_by_id"),
    "vendor": ScopeFields(owner="created_by_id"),
    "journal_entry": ScopeFields(owner="created_by_id", project="lines__project_id",
                                 department="lines__department"),
    "stock_movement": ScopeFields(owner="created_by_id"),
    "adjustment": ScopeFields(owner="created_by_id"),
    "item": ScopeFields(owner="created_by_id"),
}

DENY_ALL = Q(pk__in=[])


def resolve_actor_scope(request) -> ActorScope:
    """Resolve the actor's identity facts once, then cache them per request.

    Cached on the request object *and* in Redis: the department path and
    project membership are two extra queries that would otherwise run on
    every list endpoint of every page load.
    """
    cached = getattr(request, "_actor_scope", None)
    if cached is not None:
        return cached

    tenant_id = getattr(getattr(request, "tenant", None), "id", None) or get_current_tenant_id()
    user_id = getattr(getattr(request, "user", None), "id", None) or get_current_user_id()
    payload = effective_permissions(tenant_id, user_id)

    cache = _cache()
    key = scope_cache_key(tenant_id, user_id)
    facts = cache.get(key)
    if facts is None:
        facts = _load_actor_facts(tenant_id, user_id)
        cache.set(key, facts, PERMISSION_CACHE_TTL)

    scope = ActorScope(
        user_id=user_id,
        employee_id=facts.get("employee_id"),
        department_id=facts.get("department_id"),
        department_path=facts.get("department_path") or "",
        project_ids=tuple(facts.get("project_ids") or ()),
        assigned_department_paths=tuple(payload.get("assigned_department_paths") or ()),
        rules=payload.get("rules") or {},
        rank=payload.get("rank"),
    )
    request._actor_scope = scope
    return scope


def _load_actor_facts(tenant_id, user_id) -> dict[str, Any]:
    from apps.iam.models import TenantMembership

    membership = (
        TenantMembership.objects.select_related("employee", "employee__department")
        .filter(tenant_id=tenant_id, user_id=user_id, is_active=True)
        .first()
    )
    if membership is None or membership.employee_id is None:
        # A user with no linked Employee (an external auditor) has no
        # own_record / department identity at all. Every employee-shaped
        # strategy will deny for them, which is correct.
        return {"employee_id": None, "department_id": None,
                "department_path": "", "project_ids": []}

    employee = membership.employee
    department = getattr(employee, "department", None)

    from apps.projects.models import ProjectMember

    project_ids = list(
        ProjectMember.objects.filter(
            tenant_id=tenant_id, employee_id=employee.id, is_active=True
        ).values_list("project_id", flat=True)
    )
    return {
        "employee_id": employee.id,
        "department_id": getattr(department, "id", None),
        "department_path": getattr(department, "path", "") or "",
        "project_ids": project_ids,
    }


def _scope_from_user(user) -> ActorScope:
    """Build an :class:`ActorScope` outside an HTTP request.

    ``build_scope_q`` is also called from Celery tasks, management commands
    and the reporting generators, where there is no ``request`` to hang a
    cached scope off. Referencing this helper without defining it made every
    such call raise ``NameError`` at runtime — invisible until a scheduled
    report actually ran.

    Falls back to a deny-everything scope when no tenant is bound, rather
    than raising: a task that forgot ``tenant_context`` should return nothing,
    not crash and retry forever.
    """
    from apps.core.tenancy_context import get_current_tenant_id

    tenant_id = get_current_tenant_id()
    user_id = getattr(user, "id", None)
    if tenant_id is None or user_id is None:
        return ActorScope()

    payload = effective_permissions(tenant_id, user_id)
    facts = _load_actor_facts(tenant_id, user_id)
    return ActorScope(
        user_id=user_id,
        employee_id=facts.get("employee_id"),
        department_id=facts.get("department_id"),
        department_path=facts.get("department_path") or "",
        project_ids=tuple(facts.get("project_ids") or ()),
        assigned_department_paths=tuple(payload.get("assigned_department_paths") or ()),
        rules=payload.get("rules") or {},
        rank=payload.get("rank"),
    )


def build_scope_q(user, resource: str, *, request=None) -> Q:
    """Compile the actor's ``ScopeRule`` for ``resource`` into a ``Q``.

    Implements every value of ``ScopeRule.Strategy``. Anything not recognised
    denies — a strategy string that reaches here without a branch is either a
    database row written by hand or an enum member added without updating this
    function, and both should stop traffic rather than open it.
    """
    scope = resolve_actor_scope(request) if request is not None else _scope_from_user(user)
    rule = scope.rules.get(resource)
    if rule is None:
        # No explicit rule. What that means depends on whether the resource is
        # narrowable at all, because RBAC and ABAC answer different questions:
        # RBAC already decided *whether* the actor may touch this resource;
        # ABAC only decides *which rows*. Treating a missing rule as a blanket
        # denial conflates the two and silently voids the permission
        # catalogue — an Accountant granted `accounting.account.read` would
        # still get an empty list, with a 200 and no error to explain it.
        #
        # So:
        #   * The tenant Owner (rank 0) is the tenant's ultimate authority and
        #     holds every permission by definition — never row-restricted.
        #   * A resource with no SCOPE_FIELDS entry has no dimension to narrow
        #     by (there is no "your own" chart of accounts). RBAC is the only
        #     meaningful gate, so the scope is the whole tenant. RLS and the
        #     tenant manager still bound that to the bound tenant.
        #   * A resource that IS in SCOPE_FIELDS is narrowable and therefore
        #     sensitive — payslips, employee records, documents, leave. There a
        #     missing rule stays fail-closed, because "we could not determine
        #     which rows" must never degrade to "all of them".
        if scope.rank == 0:
            return Q()
        if resource not in SCOPE_FIELDS:
            logger.debug("resource=%r is not narrowable; scope=all-in-tenant", resource)
            return Q()
        logger.debug("no scope rule for narrowable resource=%r; denying", resource)
        return DENY_ALL

    strategy = rule["strategy"]
    params = rule.get("parameters") or {}
    fields = SCOPE_FIELDS.get(resource)
    if fields is None and strategy not in ("all", "none"):
        logger.error("resource %r has no SCOPE_FIELDS entry; denying", resource)
        return DENY_ALL

    q = _strategy_q(strategy, fields, scope, resource)
    return _apply_parameters(q, params, scope, fields)


def _strategy_q(strategy: str, fields: Optional[ScopeFields], scope: ActorScope,
                resource: str) -> Q:
    if strategy == "none":
        return DENY_ALL

    if strategy == "all":
        # Q() is *not* "everything in the database": TenantManager and RLS have
        # already narrowed to the bound tenant. "all" means all of this tenant.
        return Q()

    if strategy == "own_record":
        if fields.employee and scope.employee_id:
            return Q(**{fields.employee: scope.employee_id})
        if scope.user_id:
            return Q(**{fields.owner: scope.user_id})
        return DENY_ALL

    if strategy == "own_department":
        if not (fields.department and scope.department_id):
            return DENY_ALL
        lookup = fields.department
        # ``department`` may be spelled as a relation ("employee__department")
        # or as the row's own PK ("id"); both compare by id.
        suffix = "" if lookup.endswith("_id") or lookup == "id" else "_id"
        return Q(**{f"{lookup}{suffix}": scope.department_id})

    if strategy == "department_subtree":
        if not (fields.department and scope.department_path):
            return DENY_ALL
        return _subtree_q(fields.department, [scope.department_path])

    if strategy == "scoped_department":
        # The department named on RoleAssignment.department, NOT the actor's
        # own. This is what makes "HR Manager, Alexandria branch" one scoped
        # assignment instead of a second role.
        if not (fields.department and scope.assigned_department_paths):
            return DENY_ALL
        return _subtree_q(fields.department, list(scope.assigned_department_paths))

    if strategy == "assigned_projects":
        if not fields.project or not scope.project_ids:
            return DENY_ALL
        lookup = fields.project
        suffix = "" if lookup.endswith("_id") or lookup == "id" else "_id"
        return Q(**{f"{lookup}{suffix}__in": list(scope.project_ids)})

    if strategy == "managed_employees":
        # Direct reports only, via Employee.manager — deliberately narrower
        # than department_subtree, which follows the org chart.
        if not (fields.manager and scope.employee_id):
            return DENY_ALL
        own = Q(**{fields.employee: scope.employee_id}) if fields.employee else Q()
        return Q(**{fields.manager: scope.employee_id}) | own

    logger.error("unknown ScopeRule.strategy %r for resource %r; denying", strategy, resource)
    return DENY_ALL


def _subtree_q(department_lookup: str, paths: Iterable[str]) -> Q:
    """Materialised-path prefix match.

    ``Department.path`` is ``/root_uuid/child_uuid/…/`` and is indexed with
    ``varchar_pattern_ops``, so ``LIKE '<prefix>%'`` is an index range scan.

    The alternative — a recursive CTE, or walking children in Python — costs
    one query per level and cannot use an index, so a manager over a
    4 000-person division sequential-scans the employee table on every list
    request. Re-parenting a department rewrites the affected ``path`` values
    in one UPDATE; that cost is paid on reorganisation (monthly) rather than
    on read (continuously).

    ``startswith``, not ``contains``: a contains-match would let the subtree of
    an unrelated department whose UUID happens to appear later in another path
    leak in.
    """
    base = department_lookup[:-3] if department_lookup.endswith("_id") else department_lookup
    lookup = "path__startswith" if base == "id" else f"{base}__path__startswith"
    q = Q()
    for path in paths:
        if path:
            q |= Q(**{lookup: path})
    return q if q else DENY_ALL


def _apply_parameters(q: Q, params: dict[str, Any], scope: ActorScope,
                      fields: Optional[ScopeFields]) -> Q:
    """Apply ``ScopeRule.parameters`` that are expressible as row filters.

    Only row-shaped parameters belong here. ``max_amount`` is *not* one of
    them: it is a transition guard checked by the service against the specific
    document being approved, because filtering an approver's list to
    "documents under 5 000" would hide the ones they need to escalate rather
    than refusing the approval.

    ``exclude_self_prepared`` is not one of them either, for exactly the same
    reason, and it used to be applied here as ``q &= ~Q(created_by_id=...)``.
    That is a visibility rule standing in for an authorisation rule, and the
    two are not interchangeable:

    * It hid rows rather than refusing an action. An Owner who created a
      payroll run could not list it, open it, edit it, submit it or read its
      payslips -- ``GET /payroll-runs/`` returned ``200 {"count": 0}`` while
      the rows sat in the table. Nothing in the response said why.
    * It filtered on ``created_by_id``, which is whoever inserted the row, not
      whoever *prepared* the document. For a payroll run the SoD-relevant
      actor is ``calculated_by`` -- the field ``approve_run`` actually checks,
      and the field ``PayrollRun`` stores the value for.
    * It could not express ``break_glass``, so the one-person tenant that
      docs/05-permission-matrix.md carves out (Owner approves their own run,
      audited) was unreachable: the run was invisible before any approval was
      attempted.
    * docs/06-api-contract.md specifies the behaviour as ``403
      segregation_of_duties`` at the transition. A silently shorter list is
      not that.

    The control now lives in :func:`assert_not_self_prepared`, called at the
    approve transition beside :func:`assert_within_limit`.
    """
    return q


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
    """

    scope_resource: Optional[str] = None

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

    tenant_id = getattr(getattr(request, "tenant", None), "id", None) or get_current_tenant_id()
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
    "TenantResolutionMiddleware",
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
