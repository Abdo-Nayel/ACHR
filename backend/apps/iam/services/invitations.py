"""
Invite a person into a tenant, and let them accept.

The token
---------
``<signed>`` = ``TimestampSigner(salt=...).sign("<invitation_id>.<secret>")``.

Two independent protections, because they fail differently:

* **Signing** makes the token tamper-evident and gives it a hard maximum age
  that is enforced without a database read. A forged or edited token is
  rejected before anything is looked up, so the accept endpoint — which is
  anonymous — cannot be used to probe for valid invitation ids.
* **Hashing** means the database never holds a usable credential. Only
  ``sha256(secret)`` is stored, the same trade
  :class:`~apps.iam.models.ApiKey` makes: a leaked backup must not yield a
  working invitation, because accepting one mints a session inside a
  customer's books. sha256 rather than Argon2 for the same reason as ApiKey —
  the secret is 32 bytes from ``secrets``, not a human-chosen password, so a
  slow KDF buys nothing and costs latency.

Comparison is ``hmac.compare_digest``. A plain ``==`` on a hex digest is a
timing oracle; it is cheap to avoid and expensive to explain afterwards.

Why the membership is created at invite time
--------------------------------------------
``TenantMembership(is_active=False)`` plus its ``RoleAssignment`` are written
when the invitation is *sent*, not when it is accepted. The role grant is the
security decision, and it must be visible in ``/team/`` and in the audit log
from the moment an administrator makes it — not from the moment the invitee
gets round to reading their email. An inactive membership grants nothing:
``_load_effective_permissions`` filters on ``membership__is_active=True``, and
``TenantMiddleware`` refuses to bind a tenant without an active row.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Any, Optional

from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework import status

from apps.core.exceptions import DomainError
from apps.core.tenancy_context import (
    cross_tenant_lookup,
    tenant_context,
)
from apps.iam.models import Invitation, Role, RoleAssignment, TenantMembership, User
from apps.tenancy.models import Tenant, TenantAuditLog

logger = logging.getLogger("erp.security")

#: Salt namespaces the signature. Without it a token minted by any other
#: ``TimestampSigner`` in the project (session, password reset) would verify
#: here, because they all derive from the same SECRET_KEY.
SIGNING_SALT = "apps.iam.invitation"

#: Long enough to survive a weekend and a corporate spam quarantine, short
#: enough that a forwarded email is not a permanent back door.
INVITE_TTL_DAYS = 7

SECRET_BYTES = 32


class InvitationError(DomainError):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "invitation_invalid"
    default_detail = "That invitation link is not valid."


class InvitationExpired(DomainError):
    status_code = status.HTTP_410_GONE
    default_code = "invitation_expired"
    default_detail = (
        "That invitation has expired. Ask an administrator of the organisation "
        "to send a new one."
    )


class InvitationNotPending(DomainError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "invitation_not_pending"
    default_detail = "That invitation has already been used or revoked."


class AlreadyAMember(DomainError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "already_a_member"
    default_detail = "That person is already an active member of this organisation."


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mint_token(invitation_id: uuid.UUID) -> tuple[str, str]:
    """Return ``(signed_token, token_hash)``. The secret is never stored."""
    secret = secrets.token_urlsafe(SECRET_BYTES)
    signed = signing.TimestampSigner(salt=SIGNING_SALT).sign(
        f"{invitation_id}.{secret}"
    )
    return signed, _hash_secret(secret)


def _unsign(token: str) -> tuple[uuid.UUID, str]:
    """Verify the signature and age, then split into ``(id, secret)``.

    Every failure mode collapses to the same :class:`InvitationError`. Telling
    an anonymous caller "the signature is wrong" versus "the invitation does
    not exist" hands them an oracle for enumerating ids.
    """
    try:
        raw = signing.TimestampSigner(salt=SIGNING_SALT).unsign(
            (token or "").strip(), max_age=timedelta(days=INVITE_TTL_DAYS)
        )
    except signing.SignatureExpired as exc:
        raise InvitationExpired() from exc
    except signing.BadSignature as exc:
        raise InvitationError() from exc

    invitation_id, separator, secret = raw.partition(".")
    if not separator or not secret:
        raise InvitationError()
    try:
        return uuid.UUID(invitation_id), secret
    except (ValueError, AttributeError) as exc:
        raise InvitationError() from exc


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------

def invite_url(invitation: Invitation, token: str, *, request=None) -> str:
    """Where the invitee should be sent.

    Prefers an explicitly configured front-end origin; falls back to the
    request's own origin so a developer running only the backend still gets a
    link that resolves. Never built from the ``Host`` header alone without
    Django's ``ALLOWED_HOSTS`` having validated it first — an attacker-supplied
    Host would otherwise put their domain in an email we send.
    """
    base = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
    if not base and request is not None:
        base = request.build_absolute_uri("/app").rstrip("/")
    if not base:
        base = "/app"
    return f"{base}/accept-invite?token={token}"


@transaction.atomic
def create_invitation(
    *,
    tenant_id: uuid.UUID,
    email: str,
    role: Role,
    actor: User,
    department=None,
    request=None,
) -> tuple[Invitation, str]:
    """Invite ``email`` into ``tenant_id`` with ``role``.

    Runs the privilege-escalation guard first and unconditionally: an invite
    is a role grant with an email attached, so allowing it to skip
    :func:`~apps.iam.services.permissions.assert_can_grant_role` would make it
    the escalation path around every other grant endpoint.
    """
    from apps.iam.services.permissions import assert_can_grant_role

    assert_can_grant_role(actor, role, tenant_id=tenant_id)

    email = User.objects.normalize_email(email).lower().strip()
    tenant = Tenant.objects.get(pk=tenant_id)

    # The invitee's identity is global; the membership is not. Look the user
    # up on the global table (no RLS), then work inside the tenant.
    user = User.objects.filter(email=email).first()
    created_user = False
    if user is None:
        user = User(email=email, full_name="", is_active=True)
        # No usable password: the account cannot be logged into until the
        # invitation is accepted, so an invite is not a way to create a
        # credential-less shell someone else could take over by resetting.
        user.set_unusable_password()
        user.save()
        created_user = True

    with tenant_context(tenant_id):

        membership = TenantMembership.objects.filter(
            tenant_id=tenant_id, user=user
        ).first()
        if membership is not None and membership.is_active:
            raise AlreadyAMember()
        if membership is None:
            membership = TenantMembership.objects.create(
                tenant=tenant,
                user=user,
                is_active=False,          # inactive until accepted
                is_owner=False,
                invited_by=actor,
            )

        # Idempotent: re-inviting must not leave two live grants for the same
        # (membership, role, scope) — the unique constraint would refuse the
        # second one anyway, and a 500 on "invite again" is not an answer.
        RoleAssignment.objects.get_or_create(
            membership=membership,
            role=role,
            department=department,
            project=None,
            defaults={"granted_by": actor},
        )

        # One open offer per address per organisation (partial unique index).
        # A re-invite supersedes the previous token rather than adding a
        # second one that the UI cannot show or revoke.
        superseded = Invitation.objects.filter(
            tenant_id=tenant_id, email=email, status=Invitation.Status.PENDING
        )
        for stale in superseded:
            stale.transition(Invitation.Status.REVOKED)

        invitation = Invitation.objects.create(
            tenant=tenant,
            email=email,
            role=role,
            department=department,
            invited_by=actor,
            token_hash="",  # replaced below, once the id exists to sign
            status=Invitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=INVITE_TTL_DAYS),
        )
        token, token_hash = mint_token(invitation.id)
        invitation.token_hash = token_hash
        invitation.save(update_fields=["token_hash", "updated_at"])

        _audit(
            tenant=tenant,
            actor=actor,
            action=TenantAuditLog.Action.ROLE_GRANTED,
            invitation=invitation,
            extra={"event": "invitation_sent", "created_user": created_user},
            request=request,
        )

    url = invite_url(invitation, token, request=request)
    send_invitation_email(invitation, url, tenant)
    logger.info(
        "invitation created tenant=%s email=%s role=%s by=%s",
        tenant_id, email, role.code, actor.id,
    )
    return invitation, url


def send_invitation_email(invitation: Invitation, url: str, tenant: Tenant) -> None:
    """Deliver the link. Console backend in dev; never fails the request.

    An invitation that exists in the database but whose email bounced is
    recoverable (resend). An HTTP 500 that rolled back the invitation because
    SMTP was briefly down is not — the administrator sees a failure and has no
    idea whether to try again.
    """
    subject = f"You have been invited to join {tenant.name}"
    body = (
        f"You have been invited to join {tenant.name} on the ERP.\n\n"
        f"Accept the invitation here:\n{url}\n\n"
        f"This link expires on {invitation.expires_at:%Y-%m-%d %H:%M} UTC.\n"
        f"If you were not expecting this, you can ignore this email.\n"
    )
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[invitation.email],
            fail_silently=False,
        )
    except Exception:  # noqa: BLE001 - delivery is retryable; the row is not
        logger.warning(
            "invitation email delivery failed invitation=%s", invitation.id, exc_info=True
        )


@transaction.atomic
def resend_invitation(
    invitation: Invitation, *, actor: User, request=None
) -> tuple[Invitation, str]:
    """Issue a *new* token and extend the deadline.

    The old token is invalidated by construction: ``token_hash`` is
    overwritten, so the previous secret no longer matches. Resending must not
    leave two live links — the first one is the one that leaked into a
    forwarded email thread.
    """
    if invitation.status != Invitation.Status.PENDING:
        raise InvitationNotPending()

    token, token_hash = mint_token(invitation.id)
    invitation.token_hash = token_hash
    invitation.expires_at = timezone.now() + timedelta(days=INVITE_TTL_DAYS)
    invitation.save(update_fields=["token_hash", "expires_at", "updated_at"])

    _audit(
        tenant=invitation.tenant,
        actor=actor,
        action=TenantAuditLog.Action.ROLE_GRANTED,
        invitation=invitation,
        extra={"event": "invitation_resent"},
        request=request,
    )

    url = invite_url(invitation, token, request=request)
    send_invitation_email(invitation, url, invitation.tenant)
    return invitation, url


@transaction.atomic
def revoke_invitation(invitation: Invitation, *, actor: User, request=None) -> Invitation:
    """Withdraw an unaccepted offer, and the grant that came with it.

    The inactive membership and its role assignment are removed too. Leaving
    them behind would mean a revoked invitation still shows the person as a
    pending member holding a role, and a later re-invite would silently
    reuse a grant nobody re-approved.
    """
    if invitation.status != Invitation.Status.PENDING:
        raise InvitationNotPending()

    invitation.transition(Invitation.Status.REVOKED)

    user = User.objects.filter(email=invitation.email).first()
    if user is not None:
        membership = TenantMembership.objects.filter(
            tenant_id=invitation.tenant_id, user=user, is_active=False
        ).first()
        if membership is not None and membership.invitation_accepted_at is None:
            RoleAssignment.objects.filter(membership=membership).delete()
            membership.delete()

    _audit(
        tenant=invitation.tenant,
        actor=actor,
        action=TenantAuditLog.Action.ROLE_REVOKED,
        invitation=invitation,
        extra={"event": "invitation_revoked"},
        request=request,
    )
    logger.info(
        "invitation revoked tenant=%s email=%s by=%s",
        invitation.tenant_id, invitation.email, actor.id,
    )
    return invitation


# ---------------------------------------------------------------------------
# Accepting
# ---------------------------------------------------------------------------

def _load_invitation(invitation_id: uuid.UUID) -> Invitation:
    """Read one invitation with no tenant bound.

    This is the second legitimate pre-tenant read in the system, and it has
    the same shape as the membership read at login: the caller is anonymous,
    holds a token, and the tenant is a *property of the row we are about to
    fetch*. The filter pins a single primary key, so the widened visibility
    covers exactly the row being authorised — and the signature has already
    been verified, so an id cannot be guessed into this call.
    """
    with cross_tenant_lookup():
        invitation = (
            Invitation.objects.select_related("tenant", "role")
            .filter(pk=invitation_id)
            .first()
        )
    if invitation is None:
        raise InvitationError()
    return invitation


@transaction.atomic
def accept_invitation(
    *,
    token: str,
    full_name: str,
    password: str,
    ip_address: Optional[str] = None,
    user_agent: str = "",
) -> dict[str, Any]:
    """Activate the membership and hand back a session.

    Two cases, and the difference is a security boundary rather than a
    convenience:

    *The address had no account.* The invitation token is the only proof of
    identity there is or could be, and the account has no credential to
    protect. Set the password, activate, return tokens.

    *The address already has a usable password.* The invite link must **not**
    set it. An administrator of any organisation can invite any address, so
    "accepting" would otherwise be an unauthenticated password reset for an
    account in someone else's tenant — full account takeover from a form that
    only asks for an email. The membership is activated (that part is the
    inviting tenant's decision to make) and the caller is told to sign in with
    the password they already have.
    """
    invitation_id, secret = _unsign(token)
    invitation = _load_invitation(invitation_id)

    if not hmac.compare_digest(invitation.token_hash or "", _hash_secret(secret)):
        raise InvitationError()
    if invitation.status != Invitation.Status.PENDING:
        raise InvitationNotPending()
    if invitation.expires_at <= timezone.now():
        with tenant_context(invitation.tenant_id):
            invitation.transition(Invitation.Status.EXPIRED)
        raise InvitationExpired()

    user = User.objects.filter(email=invitation.email).first()
    if user is None:  # pragma: no cover - create_invitation always makes one
        raise InvitationError()

    had_password = user.has_usable_password()

    with tenant_context(invitation.tenant_id):

        membership = TenantMembership.objects.filter(
            tenant_id=invitation.tenant_id, user=user
        ).first()
        if membership is None:
            raise InvitationError()

        if not had_password:
            from django.contrib.auth.password_validation import validate_password
            from django.core.exceptions import ValidationError as DjangoValidationError

            try:
                validate_password(password, user=user)
            except DjangoValidationError as exc:
                raise InvitationError(
                    {"password": list(exc.messages)}
                ) from exc
            user.set_password(password)
            user.full_name = (full_name or user.full_name or "").strip()
            user.password_changed_at = timezone.now()
            user.save(
                update_fields=[
                    "password", "full_name", "password_changed_at", "updated_at",
                ]
            )

        membership.is_active = True
        membership.invitation_accepted_at = timezone.now()
        membership.save(
            update_fields=["is_active", "invitation_accepted_at", "updated_at"]
        )
        invitation.transition(Invitation.Status.ACCEPTED)

        _audit(
            tenant=invitation.tenant,
            actor=user,
            action=TenantAuditLog.Action.ROLE_GRANTED,
            invitation=invitation,
            extra={"event": "invitation_accepted", "existing_account": had_password},
            ip_address=ip_address,
            user_agent=user_agent,
        )

        from apps.iam.permissions import invalidate_permission_cache

        invalidate_permission_cache(invitation.tenant_id, user.id)

        if had_password:
            # Deliberately no tokens: see the docstring.
            return {
                "requires_login": True,
                "email": user.email,
                "tenant": _tenant_brief(invitation.tenant),
                "detail": (
                    "Your membership is active. Sign in with your existing "
                    "password to open this organisation."
                ),
            }

        from apps.iam.serializers import tenant_brief, token_pair_for

        data = token_pair_for(user, membership)
        data["requires_login"] = False
        data["tenant"] = tenant_brief(invitation.tenant)
        data["tenants"] = [tenant_brief(invitation.tenant)]
        data["user"] = {
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "locale": user.locale,
            "timezone": user.timezone,
        }
        return data


def _tenant_brief(tenant: Tenant) -> dict[str, Any]:
    from apps.iam.serializers import tenant_brief

    return tenant_brief(tenant)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _audit(
    *,
    tenant: Tenant,
    actor: Optional[User],
    action: str,
    invitation: Invitation,
    extra: dict[str, Any],
    request=None,
    ip_address: Optional[str] = None,
    user_agent: str = "",
) -> None:
    if request is not None:
        from apps.core.middleware import get_client_ip, get_user_agent

        ip_address = ip_address or get_client_ip()
        user_agent = user_agent or (get_user_agent() or "")
    try:
        TenantAuditLog.objects.create(
            tenant=tenant,
            actor_id=getattr(actor, "id", None),
            actor_email=getattr(actor, "email", "") or "",
            action=action,
            object_type="iam.Invitation",
            object_id=invitation.id,
            payload={
                "email": invitation.email,
                "role": invitation.role.code,
                "department_id": (
                    str(invitation.department_id) if invitation.department_id else None
                ),
                **extra,
            },
            ip_address=ip_address,
            user_agent=(user_agent or "")[:512],
        )
    except Exception:  # noqa: BLE001 - never fail the action on audit trouble
        logger.warning("invitation audit write failed", exc_info=True)
