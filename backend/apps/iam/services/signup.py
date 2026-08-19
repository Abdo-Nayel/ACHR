"""
Self-service organisation provisioning.

``POST /api/v1/auth/signup/`` lands here. One call has to produce a tenant a
person can *work* in, which is more than four rows: a tenant with no chart of
accounts cannot post anything, and ``payroll.services.engine`` and
``inventory.services.stock`` raise rather than guess when a ``system_key`` is
missing. So provisioning runs the real
``accounting.management.commands.seed_chart_of_accounts`` — the same command
operations runs by hand — instead of a second, abbreviated chart that would
drift from it.

Everything is one ``transaction.atomic``. A half-provisioned tenant is worse
than a failed signup: the user has an account, can log in, and discovers at
their first invoice that the ledger has no receivable account. Rolling back
means the email is still free and they can simply try again.

Ordering, and why it is the only order that works
-------------------------------------------------
``iam_tenant_membership``, ``iam_role_assignment``'s join target and
``tenancy_audit_log`` are all RLS-protected, and ``/api/v1/auth/`` is a
tenant-exempt path — nothing has bound ``app.current_tenant``. Writing them
without a bound tenant does not error; the ``WITH CHECK`` clause matches no
row and the INSERT is rejected (or an UPDATE silently affects zero rows, which
Django reports as a baffling "did not affect any rows").

The fix is not ``cross_tenant_lookup()`` for everything. The ``Tenant`` row is
*not* RLS-protected (it is the scope), so it is created first with no context;
from that moment the tenant is known, and every remaining write happens inside
``tenant_context`` + ``bind_database_session`` as an ordinary in-tenant write
subject to the same policy as every other row in the system. The bypass is
used for exactly one thing — the pre-tenant read that checks whether the email
is already registered — and that read is filtered to a single address.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta
from typing import Any, Optional

from django.core.management import call_command

from apps.core.management.commands.seed_tenant_defaults import seed_tenant_defaults
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status

from apps.core.exceptions import DomainError
from apps.core.tenancy_context import bind_database_session, tenant_context
from apps.iam.models import Role, RoleAssignment, TenantMembership, User
from apps.tenancy.models import Tenant, TenantAuditLog

logger = logging.getLogger("erp.security")

#: How long a self-service organisation may run before billing is required.
TRIAL_DAYS = 14

#: ``Tenant.slug`` is a SlugField(max_length=63) validated against
#: ``^[a-z0-9][a-z0-9-]*[a-z0-9]$`` with a 3-character minimum. Leave room for
#: the ``-2``/``-3`` disambiguator so a collision cannot push the slug over the
#: column length.
SLUG_MAX_LENGTH = 55
SLUG_MIN_LENGTH = 3

#: The system role a founder gets. rank 0 — see
#: ``apps.iam.services.permissions.assert_can_grant_role``.
OWNER_ROLE_CODE = "owner"


class EmailAlreadyRegistered(DomainError):
    """409 on signup when the address already has a login.

    Signup is a public endpoint, so this *is* an enumeration oracle and there
    is no way around that: the alternative — accepting the signup and silently
    doing nothing — creates a support ticket for every user who mistyped which
    of their two addresses they used, and tells an attacker the same thing one
    password-reset request later.

    What the message deliberately does not say is anything about *where* the
    account is used. "An account with this email already exists" leaks that the
    address is registered. "You are already a member of Acme Trading" would
    leak a customer's staff list to anyone who can guess an email, which is a
    different and much worse disclosure.
    """

    status_code = status.HTTP_409_CONFLICT
    default_code = "email_already_registered"
    default_detail = (
        "An account with this email already exists. Sign in instead, or use a "
        "different address."
    )


class OwnerRoleMissing(DomainError):
    """The deployment was never seeded. Fail loudly rather than half-provision.

    A tenant whose founder holds no role can log in and then cannot do
    anything at all, which reads as a permissions bug in the product rather
    than as a missing ``manage.py seed_permissions`` on this environment.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "system_roles_missing"
    default_detail = (
        "The system roles are not installed on this deployment. Run "
        "'manage.py seed_permissions' before enabling self-service signup."
    )


class ChartProvisioningFailed(DomainError):
    """The ledger could not be made postable, so nothing was created."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "chart_provisioning_failed"
    default_detail = (
        "The organisation's chart of accounts could not be provisioned. "
        "Nothing was created; please try again."
    )


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------

def _base_slug(company_name: str) -> str:
    """A routing identifier derived from the trading name.

    The slug ends up in sub-domains, bookmarked URLs and stored OAuth redirect
    URIs, so it is generated once and never regenerated when the company is
    renamed — see ``TenantSerializer``, where it is read-only.
    """
    candidate = slugify(company_name or "")[:SLUG_MAX_LENGTH].strip("-")
    # slugify() strips non-ASCII entirely, so a purely Arabic or Chinese name
    # yields "". Falling back to a random token keeps signup working for those
    # customers instead of refusing their company name.
    if len(candidate) < SLUG_MIN_LENGTH:
        candidate = f"org-{uuid.uuid4().hex[:8]}"
    if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", candidate):
        candidate = f"org-{uuid.uuid4().hex[:8]}"
    return candidate


def _unique_slug(base: str) -> str:
    """First free ``base``, ``base-2``, ``base-3`` … .

    ``Tenant.slug`` is globally unique, so this is a genuine cross-customer
    read — and a safe one: it discloses only that some slug is taken, which the
    sub-domain namespace discloses anyway. It is advisory, not authoritative:
    the unique index is what actually prevents a duplicate, and
    :func:`_create_tenant` retries on the ``IntegrityError`` two concurrent
    signups would produce.
    """
    if not Tenant.objects.filter(slug=base).exists():
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if not Tenant.objects.filter(slug=candidate).exists():
            return candidate
    return f"{base[:SLUG_MAX_LENGTH - 9]}-{uuid.uuid4().hex[:8]}"


def _create_tenant(
    *,
    company_name: str,
    country: str,
    base_currency: str,
    timezone_name: str,
    legal_name: str = "",
) -> Tenant:
    """Create the scope row, retrying past a slug race.

    Each attempt is its own savepoint. Without one, the ``IntegrityError``
    poisons the outer atomic block and every subsequent statement fails with
    "current transaction is aborted", which surfaces as a 500 on the retry
    rather than as a working signup.
    """
    base = _base_slug(company_name)
    last_error: Optional[Exception] = None
    for attempt in range(5):
        slug = _unique_slug(base) if attempt == 0 else f"{base[:46]}-{uuid.uuid4().hex[:8]}"
        try:
            with transaction.atomic():
                return Tenant.objects.create(
                    name=company_name.strip(),
                    legal_name=(legal_name or "").strip(),
                    slug=slug,
                    status=Tenant.Status.TRIAL,
                    country=country.upper(),
                    timezone=timezone_name,
                    base_currency=base_currency.upper(),
                    trial_ends_at=timezone.now() + timedelta(days=TRIAL_DAYS),
                    settings={},
                )
        except IntegrityError as exc:  # pragma: no cover - concurrent signup
            last_error = exc
            logger.info("signup slug collision on %r, retrying", slug)
    raise DomainError(
        "Could not allocate a unique identifier for that company name. Try a "
        "slightly different name."
    ) from last_error


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

def email_is_taken(email: str) -> bool:
    """Pre-tenant existence check for one address.

    ``iam_user`` is a global table with no ``tenant_id`` and therefore no RLS
    policy, so this needs no bypass. It is a function rather than an inline
    filter so that the "exactly one address, never a listing" rule has one
    place to be reviewed.
    """
    return User.objects.filter(email=email).exists()


def _owner_role() -> Role:
    role = Role.objects.filter(
        tenant__isnull=True, is_system=True, code=OWNER_ROLE_CODE
    ).first()
    if role is None:
        raise OwnerRoleMissing()
    return role


def provision_organisation(
    *,
    company_name: str,
    country: str,
    base_currency: str,
    timezone_name: str,
    full_name: str,
    email: str,
    password: str,
    legal_name: str = "",
    ip_address: Optional[str] = None,
    user_agent: str = "",
) -> dict[str, Any]:
    """Create a working organisation and return its founder's session payload.

    Returns the same shape as ``POST /auth/login/`` (``access``, ``refresh``,
    ``tenant``, ``tenants``, ``user``) so the client goes straight into the
    app: a signup flow that ends on the login screen loses users who assume it
    failed, and re-posting the password they just typed is a second chance to
    typo it.
    """
    email = User.objects.normalize_email(email).lower().strip()

    with transaction.atomic():
        if email_is_taken(email):
            raise EmailAlreadyRegistered()

        role = _owner_role()
        tenant = _create_tenant(
            company_name=company_name,
            country=country,
            base_currency=base_currency,
            timezone_name=timezone_name,
            legal_name=legal_name,
        )

        # From here the tenant is known, so nothing below needs the RLS
        # bypass: bind it and write as the tenant, exactly like any request.
        with tenant_context(tenant.id):
            bind_database_session(tenant.id)

            user = User.objects.create_user(
                email=email,
                password=password,
                full_name=full_name.strip(),
                timezone=timezone_name,
                is_active=True,
            )
            membership = TenantMembership.objects.create(
                tenant=tenant,
                user=user,
                is_active=True,
                is_owner=True,
                invitation_accepted_at=timezone.now(),
            )
            RoleAssignment.objects.create(
                membership=membership,
                role=role,
                granted_by=user,  # self-granted at provisioning; recorded as such
            )

            # The chart, the journals and the fiscal calendar. Imported and
            # called, never re-implemented: `_assert_system_keys` inside the
            # command refuses to finish with a role missing, and that refusal
            # is the check that stops an unpostable tenant being committed.
            try:
                call_command(
                    "seed_chart_of_accounts",
                    tenant=str(tenant.id),
                    verbosity=0,
                )
            except CommandError as exc:
                # The command refuses to commit a chart that cannot post.
                # Surface that as a 503 rather than a 500: the caller did
                # nothing wrong and the whole signup has just rolled back, so
                # retrying after the deployment is fixed is the right advice.
                logger.error("signup chart seed failed: %s", exc, exc_info=True)
                raise ChartProvisioningFailed(
                    f"The organisation could not be provisioned with a usable "
                    f"chart of accounts ({exc}). Nothing was created; please "
                    f"try again."
                ) from exc

            # HR master data: default leave types, shifts and overtime types.
            #
            # Provisioning, not decoration. Without them a new tenant reaches
            # the Leave Request form, finds an empty Leave Type dropdown and
            # cannot submit — the screen is not broken, there is simply
            # nothing to choose and nothing saying so.
            #
            # Failure here is *not* fatal, unlike the chart. A tenant with no
            # leave types can still invoice, post and pay; the fix is one
            # `seed_tenant_defaults --tenant <slug>` away, and rolling back a
            # signup that otherwise succeeded would be the worse trade.
            try:
                seed_tenant_defaults(tenant.id)
            except Exception as exc:  # noqa: BLE001 - never block signup
                logger.warning(
                    "signup HR defaults seed failed for %s: %s",
                    tenant.slug, exc, exc_info=True,
                )

            TenantAuditLog.objects.create(
                tenant=tenant,
                actor_id=user.id,
                actor_email=user.email,
                action=TenantAuditLog.Action.SETTING_CHANGED,
                object_type="tenancy.Tenant",
                object_id=tenant.id,
                payload={
                    "event": "organisation_provisioned",
                    "source": "self_service_signup",
                    "slug": tenant.slug,
                    "country": tenant.country,
                    "base_currency": tenant.base_currency,
                    "timezone": tenant.timezone,
                    "owner_role": role.code,
                },
                ip_address=ip_address,
                user_agent=(user_agent or "")[:512],
            )

            # The founder's permissions were computed as "none" by anything
            # that asked before the assignment existed (the readiness probe,
            # a concurrent request). Drop the entry rather than serve an
            # empty permission set for the next five minutes.
            from apps.iam.permissions import invalidate_permission_cache

            invalidate_permission_cache(tenant.id, user.id)

            payload = _session_payload(user, membership, tenant)

    logger.info(
        "organisation provisioned tenant=%s slug=%s owner=%s",
        tenant.id, tenant.slug, user.id,
    )
    return payload


def _session_payload(user: User, membership: TenantMembership, tenant: Tenant) -> dict:
    """Exactly ``TenantTokenObtainPairSerializer``'s response shape.

    Built from the same helpers rather than hand-assembled, so a claim added
    to login is added here too and the client never has to branch on which
    endpoint minted its session.
    """
    from apps.iam.serializers import tenant_brief, token_pair_for

    data = token_pair_for(user, membership)
    data["tenant"] = tenant_brief(tenant)
    # A brand-new founder belongs to exactly one organisation; the list is
    # still sent because the client's workspace switcher reads it
    # unconditionally.
    data["tenants"] = [tenant_brief(tenant)]
    data["user"] = {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "locale": user.locale,
        "timezone": user.timezone,
    }
    return data
