"""
Serializers for the self-service onboarding surface.

A separate module from :mod:`apps.iam.serializers` on purpose: that one is
imported at startup while Django resolves ``settings.SIMPLE_JWT``, so anything
added to it joins the import graph of the whole process. Nothing here is
needed to mint a token, and signup pulls in the tenancy models and the
accounting seed command — a chain that has no business running before the app
registry is ready.

Validation philosophy: these are the only endpoints in the product that an
unauthenticated stranger can POST to, so every field is validated against an
explicit allow-list (country codes, IANA zones, ledger currencies) rather than
being passed through to the model. A ``Tenant`` row with ``country='<script>'``
is not a security hole by itself, but it is a row nobody can fix through the
UI afterwards.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.iam import reference_data
from apps.iam.models import Invitation, Role


class SignupSerializer(serializers.Serializer):
    """``POST /api/v1/auth/signup/``.

    Not a ``ModelSerializer``: the request body spans three tables (tenant,
    user, membership) and none of them should be writable field-for-field from
    an anonymous request. ``status``, ``slug`` and ``trial_ends_at`` are
    server-owned and are not accepted here at any price — a signup form that
    can set its own subscription status is a free Enterprise plan.
    """

    company_name = serializers.CharField(max_length=200, trim_whitespace=True)
    legal_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, trim_whitespace=True
    )
    country = serializers.CharField(max_length=2)
    base_currency = serializers.CharField(max_length=3)
    timezone = serializers.CharField(max_length=64)
    full_name = serializers.CharField(max_length=200, trim_whitespace=True)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_company_name(self, value: str) -> str:
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Enter the organisation's name.")
        return value.strip()

    def validate_country(self, value: str) -> str:
        code = (value or "").strip().upper()
        if code not in reference_data.country_map():
            raise serializers.ValidationError(
                "Unknown country code. Use one of the codes from "
                "GET /api/v1/auth/reference/."
            )
        return code

    def validate_base_currency(self, value: str) -> str:
        """Restricted to the currencies a ledger may actually be kept in.

        Every monetary column in the schema declares
        :class:`apps.core.models.Currency` as its choices, so a tenant based in
        a currency outside that set could not create an invoice: the
        serializer's ``ChoiceField`` would reject it, days after signup, with
        an error that points nowhere near the cause. Refusing it here, naming
        the supported set, is the honest failure.
        """
        code = (value or "").strip().upper()
        supported = reference_data.ledger_currencies()
        if code not in supported:
            raise serializers.ValidationError(
                f"The ledger cannot be kept in {code} on this deployment. "
                f"Supported base currencies: {', '.join(supported)}. "
                f"(Invoices and payments may still be issued in any currency "
                f"once an exchange rate is configured.)"
            )
        return code

    def validate_timezone(self, value: str) -> str:
        zone = (value or "").strip()
        if zone not in set(reference_data.timezones()):
            raise serializers.ValidationError(
                "Unknown timezone. Use one of the IANA zones from "
                "GET /api/v1/auth/reference/."
            )
        return zone

    def validate_full_name(self, value: str) -> str:
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value.strip()

    def validate_email(self, value: str) -> str:
        return (value or "").strip().lower()

    def validate_password(self, value: str) -> str:
        # Django's configured validators, so signup, invite-accept and
        # password-change all enforce the same policy. A signup form with a
        # weaker rule than the change form is a permanent weak-password
        # population that nobody ever notices.
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value

    def validate(self, attrs: dict) -> dict:
        """Warn nobody, but keep country and currency at least plausible.

        A mismatch is legitimate (a Gulf holding company keeping USD books), so
        it is not rejected — it is recorded, because "why is our VAT report
        empty" three months later traces back to exactly this choice.
        """
        country = reference_data.country_map().get(attrs["country"])
        if country is not None and country.default_currency != attrs["base_currency"]:
            attrs["_currency_differs_from_country"] = True
        return attrs


class SignupResponseSerializer(serializers.Serializer):
    """Documentation-only shape; the view returns the service's dict verbatim."""

    access = serializers.CharField()
    refresh = serializers.CharField()
    tenant = serializers.DictField()
    tenants = serializers.ListField(child=serializers.DictField())
    user = serializers.DictField()


class AcceptInviteSerializer(serializers.Serializer):
    """``POST /api/v1/auth/accept-invite/``.

    ``password`` is required even for an address that already has an account:
    the client cannot know which case it is in without being told, and asking
    for it unconditionally keeps the form the same for both. It is ignored —
    never applied — when the account already has a usable password. See
    :func:`apps.iam.services.invitations.accept_invitation`.
    """

    token = serializers.CharField(trim_whitespace=True)
    full_name = serializers.CharField(max_length=200, trim_whitespace=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_full_name(self, value: str) -> str:
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value.strip()


class ReauthSerializer(serializers.Serializer):
    """``POST /api/v1/auth/reauth/`` — re-present the password, get a token.

    ``apps.iam.permissions.assert_reauth`` demands ``X-Reauth-Token`` for every
    permission marked ``is_sensitive`` (granting a role, deactivating a user,
    changing organisation settings). :func:`issue_reauth_token` documents this
    endpoint as its caller but it was never mounted, which left every sensitive
    action in the product unreachable rather than merely guarded. This is that
    endpoint; it weakens nothing, because it requires the caller's current
    password and the token it issues is single-use and expires in five minutes.
    """

    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_password(self, value: str) -> str:
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("That is not your current password.")
        return value


class InvitationSerializer(serializers.ModelSerializer):
    """A pending offer, as the team screen shows it.

    ``token_hash`` is not in ``fields`` and must never be: it is the verifier
    for a credential, and an administrator who can read it can accept the
    invitation themselves under someone else's address.
    """

    role_code = serializers.CharField(source="role.code", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)
    role_rank = serializers.IntegerField(source="role.rank", read_only=True)
    invited_by_email = serializers.EmailField(
        source="invited_by.email", read_only=True, default=None
    )
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = Invitation
        fields = (
            "id", "email", "role", "role_code", "role_name", "role_rank",
            "department", "status", "expires_at", "accepted_at", "is_open",
            "invited_by", "invited_by_email", "created_at", "updated_at",
        )
        read_only_fields = fields


class InvitationCreateSerializer(serializers.Serializer):
    """Request body of ``POST /api/v1/invitations/``.

    ``role`` is the role *code* ("accountant"), not a UUID: the codes are
    stable product surface listed by ``GET /auth/reference/``, whereas a role
    id differs per deployment for tenant-defined roles and would force the
    client to resolve it first. Custom tenant roles are addressable by code
    too — the lookup is scoped to system roles plus this tenant's own, so a
    code cannot reach another customer's role.
    """

    email = serializers.EmailField()
    role = serializers.CharField(max_length=50)
    department = serializers.UUIDField(required=False, allow_null=True)

    def validate_email(self, value: str) -> str:
        return (value or "").strip().lower()

    def validate_role(self, value: str) -> Role:
        request = self.context["request"]
        tenant_id = getattr(request, "tenant_id", None)
        code = (value or "").strip()
        role = (
            Role.objects.filter(code=code)
            .filter(tenant_id=tenant_id)
            .first()
            or Role.objects.filter(code=code, tenant__isnull=True, is_system=True).first()
        )
        if role is None:
            raise serializers.ValidationError(
                f"No role with code '{code}' exists in this organisation."
            )
        return role

    def validate_department(self, value):
        if value is None:
            return None
        from apps.hr.models import Department

        # ``Department.objects`` is the tenant-filtered manager, so a
        # department id belonging to another customer resolves to nothing
        # here rather than being silently attached to the grant.
        department = Department.objects.filter(pk=value).first()
        if department is None:
            raise serializers.ValidationError("No such department.")
        return department


class GrantRoleSerializer(serializers.Serializer):
    """Request body of ``POST /team/members/{id}/roles/``."""

    role = serializers.CharField(max_length=50)
    department = serializers.UUIDField(required=False, allow_null=True)
    valid_until = serializers.DateTimeField(required=False, allow_null=True)

    validate_role = InvitationCreateSerializer.validate_role
    validate_department = InvitationCreateSerializer.validate_department


class TeamMemberSerializer(serializers.Serializer):
    """Read-only projection built by
    :func:`apps.iam.services.team.member_payload`."""

    def to_representation(self, instance) -> dict[str, Any]:
        from apps.iam.services.team import member_payload

        return member_payload(instance)
