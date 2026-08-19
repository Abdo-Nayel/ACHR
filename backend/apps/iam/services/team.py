"""
Team management: who is in this organisation, with what authority.

Three rules live here rather than in the viewset, because all three are
reachable from a management command and a Celery offboarding task as well as
from HTTP:

**Rank.** Every grant and every revoke goes through
:func:`~apps.iam.services.permissions.assert_can_grant_role`. A user may only
touch roles of strictly greater rank than their own — without "strictly", two
peers grant each other's roles and every segregation-of-duties control in the
product evaporates in one API call.

**The last owner.** An organisation with no active owner is unrecoverable
through the product: the owner is the account that can always fix a
misconfiguration, including one that revoked its own role
(``TenantSerializer._assert_may_write`` relies on exactly that). So the last
active owner cannot be deactivated and cannot be demoted, and the check counts
*other* rows rather than trusting ``is_owner`` on the row being changed —
"is there another one" is the question, and it has to be asked of the
database inside the same transaction.

**Self-service.** A member cannot deactivate themselves or revoke their own
role. Not a safety rail for the user: it is what stops "deactivate everyone,
starting with me" from being a one-request denial of service against a
customer's whole organisation.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework import status

from apps.core.exceptions import DomainError
from apps.iam.models import Role, RoleAssignment, TenantMembership, User
from apps.tenancy.models import TenantAuditLog

logger = logging.getLogger("erp.security")

OWNER_ROLE_CODE = "owner"


class LastOwnerProtected(DomainError):
    """409 — the organisation would be left with no active owner."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "last_owner_protected"
    default_detail = (
        "This is the organisation's last active owner. Grant the owner role to "
        "another member first — an organisation with no owner cannot be "
        "administered or recovered."
    )


class SelfActionRefused(DomainError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "self_action_refused"
    default_detail = (
        "You cannot change your own membership or roles. Ask another "
        "administrator of this organisation."
    )


class RoleNotHeld(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = "role_not_held"
    default_detail = "That member does not hold that role."


# ---------------------------------------------------------------------------
# Owner accounting
# ---------------------------------------------------------------------------

def _owner_role_id(tenant_id: uuid.UUID) -> Optional[uuid.UUID]:
    """The system owner role's id. Tenants cannot define their own."""
    return (
        Role.objects.filter(tenant__isnull=True, is_system=True, code=OWNER_ROLE_CODE)
        .values_list("id", flat=True)
        .first()
    )


def is_owner_membership(membership: TenantMembership) -> bool:
    """Owner by the billing flag *or* by holding the owner role.

    Both are checked because they can legitimately disagree: ``is_owner`` is
    the billing contact, the role is the authority. Losing either one is
    losing an owner, so the guard treats them as one question.
    """
    if membership.is_owner:
        return True
    role_id = _owner_role_id(membership.tenant_id)
    if role_id is None:
        return False
    return RoleAssignment.objects.filter(
        membership=membership, role_id=role_id
    ).exists()


def other_active_owners(membership: TenantMembership) -> int:
    """How many *other* active owners this organisation has.

    Counted, not cached, and inside the caller's transaction: two concurrent
    "demote the other owner" requests would both see one other owner and both
    succeed if this were read outside the lock.
    """
    role_id = _owner_role_id(membership.tenant_id)
    query = TenantMembership.objects.filter(
        tenant_id=membership.tenant_id, is_active=True
    ).exclude(pk=membership.pk)

    flagged = query.filter(is_owner=True)
    if role_id is None:
        return flagged.count()
    by_role = query.filter(role_assignments__role_id=role_id)
    return (flagged | by_role).distinct().count()


def assert_not_last_owner(membership: TenantMembership) -> None:
    if is_owner_membership(membership) and other_active_owners(membership) == 0:
        raise LastOwnerProtected()


# ---------------------------------------------------------------------------
# Role grants
# ---------------------------------------------------------------------------

@transaction.atomic
def grant_role(
    *,
    membership: TenantMembership,
    role: Role,
    actor: User,
    department=None,
    valid_until=None,
    request=None,
) -> RoleAssignment:
    """Give ``membership`` ``role``, subject to the rank rule."""
    from apps.iam.services.permissions import assert_can_grant_role

    tenant_id = membership.tenant_id
    assert_can_grant_role(actor, role, tenant_id=tenant_id)

    if role.tenant_id is not None and role.tenant_id != tenant_id:
        # Same wording as "no such role": whether another customer's role
        # exists is not this caller's business.
        raise RoleNotHeld("No such role.")

    assignment, created = RoleAssignment.objects.get_or_create(
        membership=membership,
        role=role,
        department=department,
        project=None,
        defaults={"granted_by": actor, "valid_until": valid_until},
    )
    if not created and valid_until != assignment.valid_until:
        assignment.valid_until = valid_until
        assignment.save(update_fields=["valid_until", "updated_at"])

    # Granting the owner role also makes them a billing owner, so that the
    # "last owner" guard and the billing contact never disagree after a
    # deliberate ownership transfer.
    if role.code == OWNER_ROLE_CODE and not membership.is_owner:
        membership.is_owner = True
        membership.save(update_fields=["is_owner", "updated_at"])

    _invalidate(membership)
    _audit(
        membership=membership,
        actor=actor,
        action=TenantAuditLog.Action.ROLE_GRANTED,
        payload={"role": role.code, "rank": role.rank, "created": created},
        request=request,
    )
    logger.info(
        "role granted tenant=%s member=%s role=%s by=%s",
        tenant_id, membership.id, role.code, actor.id,
    )
    return assignment


@transaction.atomic
def revoke_role(
    *,
    membership: TenantMembership,
    role: Role,
    actor: User,
    request=None,
) -> None:
    """Take ``role`` away, subject to the rank rule and the last-owner guard.

    The rank check is the *same* one as for granting, on purpose: being able
    to revoke a role you could not grant is an escalation in the other
    direction — strip the Accountant role from everyone who could review your
    postings and the control is gone just as thoroughly as if you had granted
    yourself their access.
    """
    from apps.iam.services.permissions import assert_can_grant_role

    if membership.user_id == getattr(actor, "id", None):
        raise SelfActionRefused()

    assert_can_grant_role(actor, role, tenant_id=membership.tenant_id)

    assignments = RoleAssignment.objects.filter(membership=membership, role=role)
    if not assignments.exists():
        raise RoleNotHeld()

    if role.code == OWNER_ROLE_CODE:
        # Demotion is the other way to lose the last owner.
        if other_active_owners(membership) == 0:
            raise LastOwnerProtected()
        if membership.is_owner:
            membership.is_owner = False
            membership.save(update_fields=["is_owner", "updated_at"])

    assignments.delete()

    _invalidate(membership)
    _audit(
        membership=membership,
        actor=actor,
        action=TenantAuditLog.Action.ROLE_REVOKED,
        payload={"role": role.code, "rank": role.rank},
        request=request,
    )
    logger.info(
        "role revoked tenant=%s member=%s role=%s by=%s",
        membership.tenant_id, membership.id, role.code, actor.id,
    )


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

@transaction.atomic
def deactivate_member(
    *, membership: TenantMembership, actor: User, request=None
) -> TenantMembership:
    """Offboard. The row survives; access stops on the next request.

    Not a delete: the membership row is what proves this person had access
    between these dates, which is the first thing an auditor asks for after an
    incident.
    """
    if membership.user_id == getattr(actor, "id", None):
        raise SelfActionRefused()

    assert_not_last_owner(membership)

    if membership.is_active:
        membership.is_active = False
        membership.save(update_fields=["is_active", "updated_at"])

    _invalidate(membership)
    _audit(
        membership=membership,
        actor=actor,
        action=TenantAuditLog.Action.ROLE_REVOKED,
        payload={"event": "membership_deactivated"},
        request=request,
    )
    logger.info(
        "membership deactivated tenant=%s member=%s by=%s",
        membership.tenant_id, membership.id, actor.id,
    )
    return membership


@transaction.atomic
def activate_member(
    *, membership: TenantMembership, actor: User, request=None
) -> TenantMembership:
    """Reinstate a previously offboarded member.

    Their old role assignments are still on the row and come back with them —
    which is why deactivation, not deletion, is the offboarding path, and why
    reactivation is a permissioned act rather than an undo button.
    """
    if not membership.is_active:
        membership.is_active = True
        membership.save(update_fields=["is_active", "updated_at"])

    _invalidate(membership)
    _audit(
        membership=membership,
        actor=actor,
        action=TenantAuditLog.Action.ROLE_GRANTED,
        payload={"event": "membership_activated"},
        request=request,
    )
    return membership


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def member_payload(membership: TenantMembership) -> dict[str, Any]:
    """One row of ``GET /team/members/``.

    ``status`` is derived rather than stored: "invited" is not a column, it is
    the combination of an inactive membership that has never been accepted.
    Deriving it here means the list and the invitation endpoints cannot
    disagree about who is pending.
    """
    now = timezone.now()
    if membership.is_active:
        member_status = "active"
    elif membership.invitation_accepted_at is None:
        member_status = "invited"
    else:
        member_status = "deactivated"

    roles = []
    for assignment in membership.role_assignments.all():
        roles.append(
            {
                "assignment_id": str(assignment.id),
                "role_id": str(assignment.role_id),
                "code": assignment.role.code,
                "name": assignment.role.name,
                "rank": assignment.role.rank,
                "department_id": (
                    str(assignment.department_id) if assignment.department_id else None
                ),
                "valid_from": assignment.valid_from,
                "valid_until": assignment.valid_until,
                "is_currently_valid": (
                    assignment.valid_from <= now
                    and (assignment.valid_until is None or assignment.valid_until > now)
                ),
            }
        )
    roles.sort(key=lambda r: (r["rank"], r["name"]))

    return {
        "membership_id": str(membership.id),
        "user_id": str(membership.user_id),
        "email": membership.user.email,
        "full_name": membership.user.full_name,
        "is_active": membership.is_active,
        "is_owner": membership.is_owner,
        "status": member_status,
        "roles": roles,
        "employee_id": str(membership.employee_id) if membership.employee_id else None,
        "invited_by_id": (
            str(membership.invited_by_id) if membership.invited_by_id else None
        ),
        "invitation_accepted_at": membership.invitation_accepted_at,
        "last_active_at": membership.last_active_at,
        "created_at": membership.created_at,
    }


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _invalidate(membership: TenantMembership) -> None:
    """Drop the cached permission set so the change lands on the next request.

    Without this the member keeps their old authority for up to
    ``PERMISSION_CACHE_TTL`` (five minutes) — which for a revoke is five
    minutes of access someone has just decided to remove.
    """
    from apps.iam.permissions import invalidate_permission_cache

    invalidate_permission_cache(membership.tenant_id, membership.user_id)


def _audit(
    *,
    membership: TenantMembership,
    actor: Optional[User],
    action: str,
    payload: dict[str, Any],
    request=None,
) -> None:
    ip_address = None
    user_agent = ""
    if request is not None:
        from apps.core.middleware import get_client_ip, get_user_agent

        ip_address = get_client_ip()
        user_agent = get_user_agent() or ""
    try:
        TenantAuditLog.objects.create(
            tenant_id=membership.tenant_id,
            actor_id=getattr(actor, "id", None),
            actor_email=getattr(actor, "email", "") or "",
            action=action,
            object_type="iam.TenantMembership",
            object_id=membership.id,
            payload={
                "member_email": membership.user.email,
                "member_user_id": str(membership.user_id),
                **payload,
            },
            ip_address=ip_address,
            user_agent=(user_agent or "")[:512],
        )
    except Exception:  # noqa: BLE001 - never fail the action on audit trouble
        logger.warning("team audit write failed", exc_info=True)


__all__ = [
    "LastOwnerProtected",
    "RoleNotHeld",
    "SelfActionRefused",
    "activate_member",
    "assert_not_last_owner",
    "deactivate_member",
    "grant_role",
    "is_owner_membership",
    "member_payload",
    "other_active_owners",
    "revoke_role",
]
