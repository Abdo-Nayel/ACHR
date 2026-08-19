"""
IAM serializers: identity, membership, RBAC/ABAC administration, and auth.

Two things in this module are load-bearing beyond ordinary CRUD:

``MeSerializer``
    Returns the user, their memberships, the active tenant and the *flat list
    of effective permission codenames plus scope rules*. The clients render
    their navigation and their per-row action buttons from exactly this
    payload. If it were computed a second way on the client, the menu would
    drift from what the API actually allows and users would be shown buttons
    that 403 — so there is one source of truth and it is the same function the
    permission class itself calls.

``TenantTokenObtainPairSerializer``
    Mints tokens that carry the tenant. The claim is a *hint*: the tenant
    middleware still re-reads ``TenantMembership`` on every request, because a
    membership deactivated one minute ago must not survive in a token for
    another four.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import APIException
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer,
    TokenObtainSerializer,
)
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.serializers import ReadOnlyModelSerializer
from apps.iam.models import (
    ApiKey,
    Permission,
    Role,
    RoleAssignment,
    ScopeRule,
    TenantMembership,
    User,
)

logger = logging.getLogger("erp.security")

# ---------------------------------------------------------------------------
# JWT claim names — the contract with the middleware
# ---------------------------------------------------------------------------
#: ``apps.tenancy.middleware.TenantMiddleware`` (the one wired into
#: ``settings.MIDDLEWARE``) reads ``tenant``; the alternative resolver in
#: ``apps.iam.permissions.TenantResolutionMiddleware`` reads ``tid``. Both are
#: emitted with the same value so swapping middlewares cannot silently produce
#: tenant-less tokens — a token whose tenant claim is not read falls through to
#: the ``X-Tenant-ID`` header, which is exactly the unsigned input the claim
#: exists to override.
TENANT_CLAIM = "tenant"
TENANT_CLAIM_ALIAS = "tid"
TENANT_SLUG_CLAIM = "tenant_slug"
MEMBERSHIP_CLAIM = "membership_id"
EMAIL_CLAIM = "email"


class TenantSelectionRequired(APIException):
    """The user belongs to several tenants and did not say which one.

    Carries the candidate list so the client can prompt instead of guessing.
    Guessing on the server ("use the first one") is wrong for the outsourced
    accountant who serves five companies: they would silently start booking
    into the wrong client's ledger.
    """

    status_code = status.HTTP_409_CONFLICT
    default_code = "tenant_selection_required"
    default_detail = "Choose the organisation to sign in to."

    def __init__(self, tenants: list[dict], detail: Optional[str] = None) -> None:
        super().__init__(detail=detail)
        self.tenants = tenants


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tenant_brief(tenant) -> Optional[dict[str, Any]]:
    """Minimal tenant projection used by auth responses and ``/me``.

    Deliberately hand-rolled rather than importing ``apps.tenancy.serializers``:
    this module is imported by ``settings.SIMPLE_JWT`` at startup, and a
    startup-time import chain through another app's serializers is how import
    cycles get introduced later by someone adding one innocent field.
    """
    if tenant is None:
        return None
    return {
        "id": str(tenant.id),
        "name": tenant.name,
        "slug": tenant.slug,
        "status": tenant.status,
        "base_currency": tenant.base_currency,
        "timezone": tenant.timezone,
        "country": tenant.country,
        "is_operational": tenant.is_operational,
    }


def active_memberships(user) -> list[TenantMembership]:
    """Every tenant this user may act in.

    Runs under ``cross_tenant_lookup``: membership rows are RLS-protected, and
    at login time no tenant is bound yet. Without the bypass this returns an
    empty list for every user and login fails with "not a member of any
    organisation" — the query is filtered to one ``user``, so the widened
    visibility never extends past the caller's own rows.
    """
    from apps.core.tenancy_context import cross_tenant_lookup

    with cross_tenant_lookup():
        return list(
            TenantMembership.objects.select_related("tenant")
            .filter(user=user, is_active=True)
            .order_by("tenant__name")
        )


def apply_tenant_claims(token, *, user, membership) -> None:
    """Stamp the tenant/identity claims onto a token.

    Applied to the **refresh** token: SimpleJWT copies every non-reserved
    claim from the refresh token onto each access token it mints during
    rotation, so doing it here is what keeps the tenant claim alive for the
    whole session instead of only on the first access token.
    """
    tenant = membership.tenant
    token[TENANT_CLAIM] = str(tenant.id)
    token[TENANT_CLAIM_ALIAS] = str(tenant.id)
    token[TENANT_SLUG_CLAIM] = tenant.slug
    token[MEMBERSHIP_CLAIM] = str(membership.id)
    token[EMAIL_CLAIM] = user.email


def token_pair_for(user, membership) -> dict[str, str]:
    refresh = RefreshToken.for_user(user)
    apply_tenant_claims(refresh, user=user, membership=membership)
    access = refresh.access_token
    # The access token is derived from the refresh token *before* our claims
    # existed in some SimpleJWT versions; setting them again is cheap and
    # removes the version dependency.
    apply_tenant_claims(access, user=user, membership=membership)
    return {"refresh": str(refresh), "access": str(access)}


# ---------------------------------------------------------------------------
# Users and memberships
# ---------------------------------------------------------------------------

class UserSerializer(serializers.ModelSerializer):
    """A login identity. Global, not tenant-scoped.

    ``email`` is immutable through this endpoint: it is the username, the
    audit-log denormalisation key and the password-reset channel all at once,
    so changing it is a verified flow of its own, not a PATCH.
    """

    password = serializers.CharField(write_only=True, required=False, allow_blank=False)

    class Meta:
        model = User
        fields = (
            "id", "email", "full_name", "phone", "locale", "timezone",
            "is_active", "mfa_enabled", "last_login", "date_joined_at",
            "password",
        )
        read_only_fields = (
            "id", "is_active", "mfa_enabled", "last_login", "date_joined_at",
        )
        extra_kwargs = {"email": {"required": True}}

    # ``User`` has ``created_at`` from TimeStampedModel; expose it under the
    # name every client already uses for "when did this person join".
    date_joined_at = serializers.DateTimeField(source="created_at", read_only=True)

    def validate_email(self, value: str) -> str:
        value = value.strip().lower()
        if self.instance is not None and value != self.instance.email:
            raise serializers.ValidationError(
                "An email address cannot be changed here: it is the login "
                "identity and the audit-trail key. Use the verified "
                "change-email flow."
            )
        return value

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def create(self, validated_data: dict) -> User:
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        # ``set_password`` even when None: it stores an unusable hash, so an
        # invited-but-not-yet-activated account cannot be logged into.
        user.set_password(password)
        user.save()
        return user

    def update(self, instance: User, validated_data: dict) -> User:
        password = validated_data.pop("password", None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.password_changed_at = timezone.now()
            user.save(update_fields=["password", "password_changed_at", "updated_at"])
        return user


class UserBriefSerializer(ReadOnlyModelSerializer):
    """Embedded user reference. No PII beyond name and email."""

    class Meta:
        model = User
        fields = ("id", "email", "full_name")


class TenantMembershipSerializer(serializers.ModelSerializer):
    """A user's link to one organisation.

    ``tenant`` is read-only: a membership is always created inside the tenant
    the request is bound to. Accepting it from the body would let an admin of
    tenant A add themselves to tenant B.
    """

    user_detail = UserBriefSerializer(source="user", read_only=True)
    tenant_detail = serializers.SerializerMethodField()
    role_assignments = serializers.SerializerMethodField()

    class Meta:
        model = TenantMembership
        fields = (
            "id", "tenant", "tenant_detail", "user", "user_detail", "employee",
            "is_active", "is_owner", "invited_by", "invitation_accepted_at",
            "last_active_at", "role_assignments", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "tenant", "invited_by", "invitation_accepted_at",
            "last_active_at", "created_at", "updated_at",
        )

    def get_tenant_detail(self, obj: TenantMembership) -> Optional[dict]:
        return tenant_brief(obj.tenant)

    def get_role_assignments(self, obj: TenantMembership) -> list[dict]:
        return [
            {
                "id": str(assignment.id),
                "role": assignment.role.code,
                "role_name": assignment.role.name,
                "rank": assignment.role.rank,
                "department": str(assignment.department_id) if assignment.department_id else None,
                "project": str(assignment.project_id) if assignment.project_id else None,
                "valid_until": assignment.valid_until,
            }
            for assignment in obj.role_assignments.all()
        ]

    def validate(self, attrs: dict) -> dict:
        request = self.context.get("request")
        tenant_id = getattr(request, "tenant_id", None)
        if self.instance is None and tenant_id is None:
            raise serializers.ValidationError(
                "No organisation is bound to this request."
            )
        if self.instance is not None and self.instance.is_owner and attrs.get("is_active") is False:
            # The billing owner is the one account that must always be able to
            # get back in; deactivating it locks the customer out of their own
            # data with no self-service recovery.
            raise serializers.ValidationError(
                {"is_active": "The billing owner's membership cannot be deactivated. "
                              "Transfer ownership first."}
            )
        return attrs

    def create(self, validated_data: dict) -> TenantMembership:
        request = self.context.get("request")
        validated_data["tenant_id"] = getattr(request, "tenant_id", None)
        actor = getattr(request, "user", None)
        if actor is not None and getattr(actor, "is_authenticated", False):
            validated_data.setdefault("invited_by", actor)
        return super().create(validated_data)


# ---------------------------------------------------------------------------
# RBAC / ABAC
# ---------------------------------------------------------------------------

class PermissionSerializer(ReadOnlyModelSerializer):
    """The permission catalogue. Read-only, everywhere, always.

    It is seeded from ``config/permissions.json`` by a deploy: the set of
    things the software can do is a property of the software, not of a
    customer, so there is no write path here at all.
    """

    class Meta:
        model = Permission
        fields = ("codename", "domain", "resource", "action", "description", "is_sensitive")


class ScopeRuleSerializer(serializers.ModelSerializer):
    """The ABAC predicate attached to a (role, resource) pair."""

    class Meta:
        model = ScopeRule
        fields = ("id", "role", "resource", "strategy", "parameters")
        read_only_fields = ("id",)


class RoleSerializer(serializers.ModelSerializer):
    """A named bundle of permissions, plus its scope rules.

    ``permission_codenames`` is the write surface: the client sends the list it
    wants the role to hold and this serializer reconciles the through-rows.
    Exposing ``RolePermission`` ids to the client instead would make the
    "save this role" screen a three-request dance that can half-fail.
    """

    permission_codenames = serializers.ListField(
        child=serializers.CharField(max_length=100), required=False, write_only=True
    )
    permissions = serializers.SerializerMethodField()
    scope_rules = ScopeRuleSerializer(many=True, read_only=True)
    is_editable = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = (
            "id", "tenant", "code", "name", "description", "is_system", "rank",
            "permissions", "permission_codenames", "scope_rules", "is_editable",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "tenant", "is_system", "created_at", "updated_at")

    def get_permissions(self, obj: Role) -> list[str]:
        return sorted(permission.codename for permission in obj.permissions.all())

    def get_is_editable(self, obj: Role) -> bool:
        return not obj.is_system

    def validate(self, attrs: dict) -> dict:
        if self.instance is not None and self.instance.is_system:
            # A product update that adds a permission to a system role must not
            # collide with a customer's edit of that same role. Cloning is the
            # supported path; editing the original is not.
            raise serializers.ValidationError(
                "System roles ship with the product and cannot be edited. "
                "Clone this role to create a customisable copy."
            )
        return attrs

    def validate_rank(self, value: int) -> int:
        """A role may not be created with more authority than its author.

        Otherwise "create a role at rank 0, then grant it to myself" is a
        two-request privilege escalation that bypasses ``assert_can_grant_role``
        entirely.
        """
        from apps.iam.services.permissions import effective_rank

        request = self.context.get("request")
        actor = getattr(request, "user", None)
        actor_rank = effective_rank(actor) if actor is not None else None
        if actor_rank is None:
            raise serializers.ValidationError("You hold no role in this organisation.")
        if actor_rank > 0 and value <= actor_rank:
            raise serializers.ValidationError(
                f"You cannot create a role at rank {value}: it is not strictly "
                f"below your own authority (rank {actor_rank})."
            )
        return value

    def _sync_permissions(self, role: Role, codenames: list[str]) -> None:
        from apps.iam.models import RolePermission

        wanted = set(codenames)
        known = set(
            Permission.objects.filter(codename__in=wanted).values_list("codename", flat=True)
        )
        unknown = sorted(wanted - known)
        if unknown:
            # Fail loud, not closed: a role that references a codename which
            # does not exist looks correct in the admin screen and denies in
            # production — the most confusing authorisation bug there is.
            raise serializers.ValidationError(
                {"permission_codenames": f"Unknown permission codenames: {unknown}."}
            )
        RolePermission.objects.filter(role=role).exclude(permission__in=known).delete()
        existing = set(
            RolePermission.objects.filter(role=role).values_list("permission_id", flat=True)
        )
        RolePermission.objects.bulk_create(
            [RolePermission(role=role, permission_id=code) for code in known - existing]
        )

    def create(self, validated_data: dict) -> Role:
        codenames = validated_data.pop("permission_codenames", None)
        request = self.context.get("request")
        validated_data["tenant_id"] = getattr(request, "tenant_id", None)
        validated_data["is_system"] = False  # ck_role_system_has_no_tenant
        role = super().create(validated_data)
        if codenames is not None:
            self._sync_permissions(role, codenames)
        return role

    def update(self, instance: Role, validated_data: dict) -> Role:
        codenames = validated_data.pop("permission_codenames", None)
        role = super().update(instance, validated_data)
        if codenames is not None:
            self._sync_permissions(role, codenames)
        return role


class RoleAssignmentSerializer(serializers.ModelSerializer):
    """Grants a role to a membership, optionally narrowed to a department/project.

    Creating or changing one always runs ``assert_can_grant_role``: a user may
    only grant roles of strictly greater rank than their own. Without that
    check, any user who can reach this endpoint can hand themselves Owner.
    """

    role_detail = serializers.SerializerMethodField()
    member = serializers.SerializerMethodField()

    class Meta:
        model = RoleAssignment
        fields = (
            "id", "membership", "member", "role", "role_detail", "department",
            "project", "valid_from", "valid_until", "granted_by",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "granted_by", "created_at", "updated_at")

    def get_role_detail(self, obj: RoleAssignment) -> dict:
        return {"code": obj.role.code, "name": obj.role.name, "rank": obj.role.rank}

    def get_member(self, obj: RoleAssignment) -> dict:
        user = obj.membership.user
        return {"user_id": str(user.id), "email": user.email, "full_name": user.full_name}

    def validate(self, attrs: dict) -> dict:
        request = self.context.get("request")
        tenant_id = getattr(request, "tenant_id", None)

        membership = attrs.get("membership") or getattr(self.instance, "membership", None)
        role = attrs.get("role") or getattr(self.instance, "role", None)

        if membership is None or role is None:
            raise serializers.ValidationError("Both membership and role are required.")

        if tenant_id is not None and membership.tenant_id != tenant_id:
            # 'Not found' rather than a descriptive error: confirming that a
            # membership id exists in another tenant is an enumeration oracle.
            raise serializers.ValidationError({"membership": "No such membership."})

        if role.tenant_id is not None and tenant_id is not None and role.tenant_id != tenant_id:
            raise serializers.ValidationError({"role": "No such role."})

        valid_from = attrs.get("valid_from") or getattr(self.instance, "valid_from", None)
        valid_until = attrs.get("valid_until")
        if valid_from and valid_until and valid_until <= valid_from:
            raise serializers.ValidationError(
                {"valid_until": "The end of the grant must be after its start."}
            )

        # THE privilege-escalation guard. Imported lazily: the services package
        # imports back into apps.iam.permissions.
        from apps.iam.services.permissions import assert_can_grant_role

        assert_can_grant_role(getattr(request, "user", None), role, tenant_id=tenant_id)
        return attrs

    def create(self, validated_data: dict) -> RoleAssignment:
        request = self.context.get("request")
        actor = getattr(request, "user", None)
        if actor is not None and getattr(actor, "is_authenticated", False):
            validated_data["granted_by"] = actor
        assignment = super().create(validated_data)
        self._audit(assignment, granted=True)
        return assignment

    def update(self, instance: RoleAssignment, validated_data: dict) -> RoleAssignment:
        assignment = super().update(instance, validated_data)
        self._audit(assignment, granted=True)
        return assignment

    def _audit(self, assignment: RoleAssignment, *, granted: bool) -> None:
        from apps.tenancy.models import TenantAuditLog

        request = self.context.get("request")
        actor = getattr(request, "user", None)
        TenantAuditLog.objects.create(
            tenant_id=assignment.membership.tenant_id,
            actor_id=getattr(actor, "id", None),
            actor_email=getattr(actor, "email", "") or "",
            action=TenantAuditLog.Action.ROLE_GRANTED if granted
            else TenantAuditLog.Action.ROLE_REVOKED,
            object_type="iam.RoleAssignment",
            object_id=assignment.id,
            payload={
                "role": assignment.role.code,
                "rank": assignment.role.rank,
                "membership": str(assignment.membership_id),
                "department": str(assignment.department_id or ""),
                "project": str(assignment.project_id or ""),
                "valid_until": assignment.valid_until.isoformat()
                if assignment.valid_until else None,
            },
        )


class ApiKeySerializer(serializers.ModelSerializer):
    """Machine credential. The plaintext is returned **once**, on creation.

    Only a hash is stored, so a leaked database backup yields no working keys —
    and, unavoidably, so a customer who loses the key must issue a new one.
    That trade is the right way round: the alternative is a table of live
    credentials for every integration our customers run.
    """

    #: Populated only on the response to the creating request.
    key = serializers.SerializerMethodField()
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = ApiKey
        fields = (
            "id", "name", "prefix", "role", "expires_at", "revoked_at",
            "last_used_at", "created_by", "created_at", "key", "is_active",
        )
        read_only_fields = (
            "id", "prefix", "revoked_at", "last_used_at", "created_by", "created_at",
        )

    def get_key(self, obj: ApiKey) -> Optional[str]:
        # ``_plaintext_key`` exists only on the in-memory instance returned by
        # create(); a re-read from the database can never repopulate it.
        return getattr(obj, "_plaintext_key", None)

    def get_is_active(self, obj: ApiKey) -> bool:
        now = timezone.now()
        return obj.revoked_at is None and (obj.expires_at is None or obj.expires_at > now)

    def validate_role(self, role: Role) -> Role:
        request = self.context.get("request")
        tenant_id = getattr(request, "tenant_id", None)
        if role.tenant_id is not None and tenant_id is not None and role.tenant_id != tenant_id:
            raise serializers.ValidationError("No such role.")
        # An API key is a credential that never sees a second factor and never
        # expires on its own, so it may not carry more authority than its
        # creator could grant to a human.
        from apps.iam.services.permissions import assert_can_grant_role

        assert_can_grant_role(getattr(request, "user", None), role, tenant_id=tenant_id)
        return role

    def create(self, validated_data: dict) -> ApiKey:
        from apps.iam.authentication import generate_api_key

        request = self.context.get("request")
        prefix, secret, key_hash = generate_api_key()
        api_key = ApiKey.objects.create(
            tenant_id=getattr(request, "tenant_id", None),
            name=validated_data["name"],
            role=validated_data["role"],
            expires_at=validated_data.get("expires_at"),
            prefix=prefix,
            key_hash=key_hash,
            created_by=request.user,
        )
        api_key._plaintext_key = f"{prefix}.{secret}"
        logger.info(
            "api key issued tenant=%s prefix=%s by=%s",
            api_key.tenant_id, prefix, request.user.id,
        )
        return api_key

    def update(self, instance: ApiKey, validated_data: dict) -> ApiKey:
        # A key's secret is never rotated in place: rotation means issuing a
        # new key and revoking the old one, so that the window in which both
        # work is explicit and auditable.
        validated_data.pop("role", None)
        return super().update(instance, validated_data)


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------

class MeSerializer(serializers.Serializer):
    """Everything the client needs to render itself for this user, in one call.

    Includes the flat list of effective permission codenames and the compiled
    scope rules. The client uses them to decide which menu items and row
    actions to render; the server uses the same list, from the same function,
    to allow or deny. One computation, two consumers — a second implementation
    on the client is how "the button is there but it 403s" happens.
    """

    def to_representation(self, user) -> dict[str, Any]:
        from apps.iam.permissions import effective_permissions

        request = self.context.get("request")
        tenant_id = getattr(request, "tenant_id", None)
        memberships = active_memberships(user)
        current = next((m for m in memberships if m.tenant_id == tenant_id), None)

        payload = effective_permissions(tenant_id, user.id) if tenant_id else {
            "permissions": [], "rules": {}, "rank": None,
        }

        return {
            "user": {
                "id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
                "phone": user.phone,
                "locale": user.locale,
                "timezone": user.timezone,
                "is_platform_admin": user.is_platform_admin,
                "mfa_enabled": user.mfa_enabled,
                "last_login": user.last_login,
            },
            "tenant": tenant_brief(getattr(current, "tenant", None)),
            "membership": {
                "id": str(current.id),
                "is_owner": current.is_owner,
                "employee_id": str(current.employee_id) if current.employee_id else None,
            } if current is not None else None,
            "memberships": [
                {
                    "id": str(m.id),
                    "is_owner": m.is_owner,
                    "is_current": m.tenant_id == tenant_id,
                    "tenant": tenant_brief(m.tenant),
                }
                for m in memberships
            ],
            # Flat codename list: the client checks membership of a Set, which
            # is O(1) and needs no knowledge of the role model.
            "permissions": payload.get("permissions", []),
            # resource -> {"strategy": ..., "parameters": {...}}: lets the
            # client grey out "approve" on rows it knows the server will refuse.
            "scopes": payload.get("rules", {}),
            "rank": payload.get("rank"),
        }


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class LoginSerializer(TokenObtainSerializer):
    """Documentation/validation shape for ``POST /auth/login/``.

    The real work happens in :class:`TenantTokenObtainPairSerializer`; this
    exists so drf-spectacular emits a request body with the optional
    ``tenant_id`` in it.
    """

    tenant_id = serializers.UUIDField(required=False, allow_null=True)


class TenantTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Email + password -> a token pair bound to exactly one tenant.

    Tenant selection:

    * exactly one active membership  -> use it, no prompt;
    * several and ``tenant_id`` sent -> validate membership, use it;
    * several and nothing sent       -> 409 with the candidate list, so the
      client can prompt. The server never picks for the user: an outsourced
      accountant who serves five companies would otherwise start posting into
      whichever ledger happened to sort first.

    The response carries ``tenants`` in every case so the client can build its
    workspace switcher without a second round trip.
    """

    tenant_id = serializers.UUIDField(required=False, allow_null=True, write_only=True)

    def validate(self, attrs: dict) -> dict[str, Any]:
        requested_tenant = attrs.pop("tenant_id", None)

        # Grandparent, not parent: TokenObtainPairSerializer.validate mints a
        # tenant-less pair we would immediately throw away, and with the
        # blacklist app installed that leaves an orphan OutstandingToken row
        # per login.
        TokenObtainSerializer.validate(self, attrs)
        user = self.user

        memberships = active_memberships(user)
        if not memberships:
            if user.is_platform_admin:
                # Platform admins are not implicitly members of anything; they
                # use the impersonation flow, which is audit-logged.
                raise serializers.ValidationError(
                    "This account has no organisation membership. Platform "
                    "administrators must use the impersonation flow."
                )
            raise serializers.ValidationError(
                "This account is not an active member of any organisation."
            )

        if requested_tenant is not None:
            membership = next(
                (m for m in memberships if str(m.tenant_id) == str(requested_tenant)), None
            )
            if membership is None:
                # Same text whether the tenant does not exist or the user is
                # simply not in it: the difference is not the caller's business.
                raise serializers.ValidationError(
                    {"tenant_id": "You are not an active member of that organisation."}
                )
        elif len(memberships) == 1:
            membership = memberships[0]
        else:
            raise TenantSelectionRequired(
                tenants=[tenant_brief(m.tenant) for m in memberships]
            )

        data = token_pair_for(user, membership)
        data["tenant"] = tenant_brief(membership.tenant)
        data["tenants"] = [tenant_brief(m.tenant) for m in memberships]
        data["user"] = {
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "locale": user.locale,
            "timezone": user.timezone,
        }

        from django.contrib.auth.models import update_last_login
        from rest_framework_simplejwt.settings import api_settings

        if api_settings.UPDATE_LAST_LOGIN:
            update_last_login(None, user)

        # Stamping activity is still a pre-tenant write: nothing has bound
        # `app.current_tenant` yet, so the RLS policy's WITH CHECK clause
        # matches no row and the UPDATE silently affects 0 rows — which Django
        # surfaces as "Save with update_fields did not affect any rows."
        # Scoped to the single membership row we just authorised.
        from apps.core.tenancy_context import cross_tenant_lookup

        membership.last_active_at = timezone.now()
        with cross_tenant_lookup():
            membership.save(update_fields=["last_active_at", "updated_at"])
        return data


class SwitchTenantSerializer(serializers.Serializer):
    """Mint a pair for another tenant the caller is already a member of.

    A new pair rather than a mutated one: the tenant lives in a *signed* claim,
    so switching workspace has to be a new signature. It is also the moment to
    re-check the membership, which is why the old token is not simply extended.
    """

    tenant_id = serializers.UUIDField()
    #: Optional: the refresh token being replaced. Blacklisted on success so a
    #: session cannot be left holding two live refresh tokens for two tenants.
    refresh = serializers.CharField(required=False, allow_blank=True)

    def validate_tenant_id(self, value):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        # Same chicken-and-egg as login: the caller is asking to switch INTO
        # a tenant, so the current session is bound to a different one (or to
        # none). Scoped to this user and this tenant id.
        from apps.core.tenancy_context import cross_tenant_lookup

        with cross_tenant_lookup():
            membership = (
                TenantMembership.objects.select_related("tenant")
                .filter(user=user, tenant_id=value, is_active=True)
                .first()
            )
        if membership is None:
            raise serializers.ValidationError(
                "You are not an active member of that organisation."
            )
        self.context["membership"] = membership
        return value

    def create(self, validated_data: dict) -> dict[str, Any]:
        request = self.context.get("request")
        membership = self.context["membership"]

        raw_refresh = (validated_data.get("refresh") or "").strip()
        if raw_refresh:
            try:
                RefreshToken(raw_refresh).blacklist()
            except Exception:  # noqa: BLE001 - an unusable old token is not an error
                logger.info("tenant switch: previous refresh token could not be blacklisted")

        data = token_pair_for(request.user, membership)
        data["tenant"] = tenant_brief(membership.tenant)
        logger.info(
            "tenant switch user=%s -> tenant=%s", request.user.id, membership.tenant_id
        )
        return data


class LogoutSerializer(serializers.Serializer):
    """Blacklist a refresh token.

    Access tokens are deliberately *not* revoked: they are bearer credentials
    valid for five minutes and checking a blacklist on every request would put
    Redis in the authentication path. Killing the refresh token bounds the
    session to that five-minute window.
    """

    refresh = serializers.CharField()

    def validate_refresh(self, value: str) -> str:
        try:
            self.context["token"] = RefreshToken(value)
        except Exception as exc:  # noqa: BLE001
            raise serializers.ValidationError("That refresh token is not valid.") from exc
        return value

    def save(self, **kwargs) -> None:
        self.context["token"].blacklist()


class PasswordChangeSerializer(serializers.Serializer):
    """Change your own password.

    Requires the current password even though the caller is authenticated: the
    threat is a lifted bearer token, and without this check that token converts
    into permanent account takeover.
    """

    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value: str) -> str:
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("That is not your current password.")
        return value

    def validate_new_password(self, value: str) -> str:
        validate_password(value, user=self.context["request"].user)
        return value

    def validate(self, attrs: dict) -> dict:
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "The new password must differ from the current one."}
            )
        return attrs

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.password_changed_at = timezone.now()
        user.failed_login_count = 0
        user.save(
            update_fields=["password", "password_changed_at", "failed_login_count", "updated_at"]
        )
        self._revoke_sessions(user)
        return user

    @staticmethod
    def _revoke_sessions(user) -> None:
        """Blacklist every outstanding refresh token for this user.

        A password change is the action a user takes when they believe they
        have been compromised. Leaving previously issued refresh tokens alive
        for seven days means the attacker keeps the account for seven days.
        """
        try:
            from rest_framework_simplejwt.token_blacklist.models import (
                BlacklistedToken,
                OutstandingToken,
            )
        except ImportError:  # pragma: no cover - blacklist app not installed
            return

        for token in OutstandingToken.objects.filter(user=user).exclude(
            Q(id__in=BlacklistedToken.objects.values_list("token_id", flat=True))
        ):
            BlacklistedToken.objects.get_or_create(token=token)
