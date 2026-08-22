"""Attribute-based access control: which *rows* an actor may see.

Extracted from the 1,170-line ``apps.iam.permissions`` god module. This is the
module ``apps.iam.models`` already documents as the home of ``build_scope_q``,
so the split also closes a doc/code mismatch.

RBAC (``apps.iam.permissions.HasPermission``) answers *may this actor do this at
all*; ABAC answers *on which rows*, by compiling each ``ScopeRule.strategy`` into
a Django ``Q``. It fails closed: an absent rule for a resource the actor is not
Owner of yields no rows (``DENY_ALL``).

The two cache helpers it needs (``effective_permissions``, ``scope_cache_key``,
``_cache``) live in ``apps.iam.permissions`` and are imported *inside* the two
functions that use them — a deliberate lazy import so this module never imports
``permissions`` at load time, which is what keeps ``permissions`` free to import
*this* module at the top level and re-export these names.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from django.db.models import Q

from apps.core.tenancy_context import get_current_tenant_id, get_current_user_id

logger = logging.getLogger(__name__)


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
    from apps.iam.permissions import (
        PERMISSION_CACHE_TTL, _cache, effective_permissions, scope_cache_key,
    )

    cached = getattr(request, "_actor_scope", None)
    if cached is not None:
        return cached

    tenant_id = getattr(request, "tenant_id", None) or get_current_tenant_id()
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
    from apps.iam.permissions import effective_permissions

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
