"""
Imperative permission checks for service-layer code.

:mod:`apps.iam.permissions` holds the DRF plumbing — permission classes,
queryset mixins, the effective-permission cache. That layer only runs when a
request passes through a view. Service functions are also called from Celery
tasks, management commands and other services, where no view has run, so they
need a direct way to ask the same question.

This module is that entry point. It is deliberately thin and delegates every
decision to :mod:`apps.iam.permissions` so there is exactly one implementation
of "does this user hold this permission" — two implementations means two
answers, and the one that drifts is always the one guarding the money.

Import it lazily from service modules (``from apps.iam.services.permissions
import assert_permission`` inside the function body). ``apps.iam.permissions``
imports domain models to build its resource registry, so a module-level import
from a domain service closes a cycle.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable, Optional

from django.core.exceptions import PermissionDenied

from apps.core.tenancy_context import get_current_tenant_id


def _resolve_tenant_id(user: Any, tenant_id: Optional[uuid.UUID]) -> uuid.UUID:
    """Never guess the tenant.

    Falling back to "the user's only tenant" would silently authorise an
    action against the wrong company for the outsourced accountant who
    belongs to five. If neither the caller nor the ambient context supplies
    a tenant, that is a bug in the caller and must surface as one.
    """
    resolved = tenant_id or get_current_tenant_id()
    if resolved is None:
        raise PermissionDenied(
            "Permission check attempted without a tenant context. The caller "
            "must pass tenant_id explicitly or run inside tenant_context()."
        )
    return resolved


def has_permission(
    user: Any, codename: str, *, tenant_id: Optional[uuid.UUID] = None
) -> bool:
    """Return whether ``user`` holds ``codename`` in the given tenant.

    Platform admins short-circuit to True. Every such use is expected to be
    written to :class:`apps.tenancy.models.TenantAuditLog` by the caller —
    the check itself does not log, because it is called in hot paths.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_active", False):
        return False
    if getattr(user, "is_platform_admin", False):
        return True

    from apps.iam.permissions import effective_permissions  # local: avoids a cycle

    resolved = _resolve_tenant_id(user, tenant_id)
    payload = effective_permissions(resolved, user.id)
    return codename in set(payload.get("permissions", []))


def assert_permission(
    user: Any, codename: str, *, tenant_id: Optional[uuid.UUID] = None
) -> None:
    """Raise :class:`PermissionDenied` unless ``user`` holds ``codename``.

    The message names the missing permission on purpose. Vague authorisation
    errors ("forbidden") generate support tickets; naming the codename lets an
    administrator fix the role grant without a developer in the loop. It
    leaks nothing an authenticated tenant member should not already know —
    the permission catalogue is public product surface, documented in
    ``docs/05-permission-matrix.md``.
    """
    if not has_permission(user, codename, tenant_id=tenant_id):
        raise PermissionDenied(
            f"You do not have the '{codename}' permission in this organisation."
        )


def assert_any_permission(
    user: Any, codenames: Iterable[str], *, tenant_id: Optional[uuid.UUID] = None
) -> None:
    """Require at least one of ``codenames``. Used where two roles may act."""
    wanted = list(codenames)
    if any(has_permission(user, code, tenant_id=tenant_id) for code in wanted):
        return
    raise PermissionDenied(
        "This action requires one of: " + ", ".join(sorted(wanted)) + "."
    )


def assert_all_permissions(
    user: Any, codenames: Iterable[str], *, tenant_id: Optional[uuid.UUID] = None
) -> None:
    """Require every one of ``codenames``.

    Used for compound actions such as "issue an invoice that also ships
    stock", which touches two domains and should not be possible for someone
    authorised in only one of them.
    """
    missing = [
        code
        for code in codenames
        if not has_permission(user, code, tenant_id=tenant_id)
    ]
    if missing:
        raise PermissionDenied(
            "Missing required permissions: " + ", ".join(sorted(missing)) + "."
        )


def effective_rank(user: Any, *, tenant_id: Optional[uuid.UUID] = None) -> Optional[int]:
    """The user's most authoritative rank (lowest number wins).

    ``min`` rather than ``max``: a user holding both Admin (10) and Employee
    (50) acts with Admin's authority. Taking the max would let anyone
    de-escalate themselves into a role they could then grant upward from.
    """
    if getattr(user, "is_platform_admin", False):
        return 0

    from apps.iam.permissions import effective_permissions  # local: avoids a cycle

    resolved = _resolve_tenant_id(user, tenant_id)
    return effective_permissions(resolved, user.id).get("rank")


def assert_can_grant_role(
    user: Any, role: Any, *, tenant_id: Optional[uuid.UUID] = None
) -> None:
    """Privilege-escalation guard for role assignment.

    A user may only grant roles of *strictly* greater rank than their own.
    Without "strictly", two peers at the same rank can grant each other's
    roles — an HR Manager grants themselves Accountant, and the payroll
    segregation-of-duties control evaporates in a single API call.
    """
    actor_rank = effective_rank(user, tenant_id=tenant_id)
    if actor_rank is None:
        raise PermissionDenied("You hold no role in this organisation.")
    if actor_rank == 0:
        return
    if role.rank <= actor_rank:
        raise PermissionDenied(
            f"You cannot grant the '{role.name}' role: its authority "
            f"(rank {role.rank}) is not strictly below your own "
            f"(rank {actor_rank})."
        )
