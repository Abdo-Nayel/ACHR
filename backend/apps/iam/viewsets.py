"""
IAM viewsets: users, memberships, roles, the permission catalogue, grants and
API keys.

Why none of these use ``apps.core.viewsets.TenantModelViewSet``
---------------------------------------------------------------
That base mixes in ``ScopedQuerysetMixin``, which compiles the actor's
``ScopeRule`` for a resource into a ``Q``. The ABAC layer only knows how to
scope the resources listed in ``apps.iam.permissions.SCOPE_FIELDS`` — employees,
payslips, invoices and so on — and it **fails closed** for anything else. None
of the IAM resources are in that map (there is no meaningful "own record"
narrowing for a role definition), so inheriting the tenant base would make
every one of these endpoints return an empty list.

So the tenant narrowing here is written out explicitly, once per viewset, and
it is the membership join that does it: you see the users, roles and keys of
the organisation your request is bound to, and nothing else. ``User``,
``Role`` (system roles) and ``Permission`` are not tenant-scoped tables, which
is precisely why the filter cannot be left implicit.
"""

from __future__ import annotations

import logging

from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.pagination import SmallPagePagination, TenantCursorPagination
from apps.core.tenancy_context import get_current_tenant_id
from apps.core.viewsets import HardDeleteDisabled
from apps.iam.models import (
    ApiKey,
    Permission,
    Role,
    RoleAssignment,
    TenantMembership,
    User,
)
from apps.iam.permissions import HasPermission, TenantResolutionError
from apps.iam.serializers import (
    ApiKeySerializer,
    PermissionSerializer,
    RoleAssignmentSerializer,
    RoleSerializer,
    TenantMembershipSerializer,
    UserSerializer,
)

logger = logging.getLogger("erp.security")


class IamViewSetMixin:
    """Shared plumbing: deny-by-default RBAC, cursor pagination, tenant filter."""

    permission_classes = [IsAuthenticated, HasPermission]
    pagination_class = TenantCursorPagination

    def current_tenant_id(self):
        tenant_id = getattr(self.request, "tenant_id", None) or get_current_tenant_id()
        if tenant_id is None:
            # Never fall back to "all tenants". Every queryset below is a
            # cross-tenant read if this returns None, so it raises instead.
            raise TenantResolutionError(
                "No organisation is bound to this request; access-control data "
                "cannot be listed."
            )
        return tenant_id

    def destroy(self, request, *args, **kwargs):  # pragma: no cover - overridden where legal
        raise HardDeleteDisabled(
            "Access-control records are deactivated or revoked, never deleted: "
            "the history of who could do what is the audit trail."
        )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UserViewSet(IamViewSetMixin, viewsets.ModelViewSet):
    """People with a login in this organisation.

    ``User`` rows are global — one identity can serve five customers — so this
    endpoint deliberately exposes only the users who hold a membership in the
    active tenant, and only the profile fields. Everything tenant-specific
    (is_owner, roles, employee link) lives on ``/memberships/``.

    There is no hard delete: a user is removed from an organisation by
    deactivating their membership. Deleting the ``User`` row would orphan every
    ``created_by`` reference in the ledger, and ``on_delete=PROTECT`` would
    refuse anyway.
    """

    serializer_class = UserSerializer
    queryset = User.objects.all()
    search_fields = ("email", "full_name")
    ordering_fields = ("email", "full_name", "created_at")
    filterset_fields = ("is_active",)

    required_permissions = {
        "GET": ["iam.user.read"],
        "HEAD": ["iam.user.read"],
        "OPTIONS": ["iam.user.read"],
        "POST": ["iam.user.invite"],
        "PUT": ["iam.user.update"],
        "PATCH": ["iam.user.update"],
        "DELETE": ["iam.user.deactivate"],
    }

    def get_queryset(self):
        return (
            User.objects.filter(memberships__tenant_id=self.current_tenant_id())
            .distinct()
            .order_by("-created_at", "-id")
        )

    def perform_create(self, serializer) -> None:
        """Inviting a user creates the identity *and* the membership.

        Two rows, one transaction (ATOMIC_REQUESTS): a ``User`` with no
        membership can authenticate and then belongs nowhere, which the login
        path rejects — an invited person who cannot log in reads as a broken
        product, not as a safety feature.
        """
        user = serializer.save()
        TenantMembership.objects.get_or_create(
            tenant_id=self.current_tenant_id(),
            user=user,
            defaults={"invited_by": self.request.user, "is_active": True},
        )


# ---------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------

class TenantMembershipViewSet(IamViewSetMixin, viewsets.ModelViewSet):
    """Who belongs to this organisation, and in what capacity."""

    serializer_class = TenantMembershipSerializer
    queryset = TenantMembership.objects.all()
    filterset_fields = ("is_active", "is_owner", "user")
    search_fields = ("user__email", "user__full_name")

    required_permissions = {
        "GET": ["iam.membership.read"],
        "HEAD": ["iam.membership.read"],
        "OPTIONS": ["iam.membership.read"],
        "POST": ["iam.user.invite"],
        "PUT": ["iam.user.update"],
        "PATCH": ["iam.user.update"],
        "DELETE": ["iam.user.deactivate"],
        "deactivate": ["iam.user.deactivate"],
    }

    def get_queryset(self):
        return (
            TenantMembership.objects.filter(tenant_id=self.current_tenant_id())
            .select_related("user", "tenant", "employee")
            .prefetch_related("role_assignments__role")
            .order_by("-created_at", "-id")
        )

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None):
        """Revoke access immediately — the offboarding path.

        Not a DELETE: the row is what proves this person had access between
        these dates, which is the first thing an auditor asks for after an
        incident. Flipping ``is_active`` also invalidates the permission cache,
        so the change takes effect on the next request rather than at the end
        of the cache TTL.
        """
        membership = self.get_object()
        if membership.is_owner:
            return Response(
                {"error": {
                    "code": "owner_membership_protected",
                    "detail": "The billing owner cannot be deactivated. Transfer "
                              "ownership first.",
                    "status": status.HTTP_409_CONFLICT,
                }},
                status=status.HTTP_409_CONFLICT,
            )
        membership.is_active = False
        membership.save(update_fields=["is_active", "updated_at"])

        from apps.iam.permissions import invalidate_permission_cache

        invalidate_permission_cache(membership.tenant_id, membership.user_id)
        logger.info(
            "membership deactivated tenant=%s user=%s by=%s",
            membership.tenant_id, membership.user_id, request.user.id,
        )
        return Response(self.get_serializer(membership).data)


# ---------------------------------------------------------------------------
# Roles and the permission catalogue
# ---------------------------------------------------------------------------

class RoleViewSet(IamViewSetMixin, viewsets.ModelViewSet):
    """System roles (read-only, shipped with the product) plus this tenant's own.

    Both are listed together because that is how the role picker needs them;
    ``is_editable`` on the payload tells the client which ones it may open for
    editing, and the serializer refuses a write to a system role regardless of
    what the client believes.
    """

    serializer_class = RoleSerializer
    queryset = Role.objects.all()
    pagination_class = SmallPagePagination  # bounded set; a page count is useful
    search_fields = ("code", "name")
    ordering_fields = ("rank", "name")

    required_permissions = {
        "GET": ["iam.role.read"],
        "HEAD": ["iam.role.read"],
        "OPTIONS": ["iam.role.read"],
        "POST": ["iam.role.create"],
        "PUT": ["iam.role.update"],
        "PATCH": ["iam.role.update"],
        "DELETE": ["iam.role.delete"],
    }

    def get_queryset(self):
        from django.db.models import Q

        return (
            Role.objects.filter(
                Q(tenant_id=self.current_tenant_id()) | Q(tenant__isnull=True)
            )
            .prefetch_related("permissions", "scope_rules")
            .order_by("rank", "name")
        )

    def destroy(self, request, *args, **kwargs):
        """Custom roles may be deleted; system roles and roles in use may not.

        ``RoleAssignment.role`` is ``PROTECT``, so a role that is still granted
        to somebody raises at the database. Checking here first turns that into
        a comprehensible 409 instead of a constraint-violation envelope.
        """
        role = self.get_object()
        if role.is_system:
            raise HardDeleteDisabled(
                "System roles ship with the product and cannot be deleted."
            )
        if role.assignments.exists():
            return Response(
                {"error": {
                    "code": "role_in_use",
                    "detail": "This role is still granted to at least one member. "
                              "Revoke those grants first.",
                    "status": status.HTTP_409_CONFLICT,
                }},
                status=status.HTTP_409_CONFLICT,
            )
        return mixins.DestroyModelMixin.destroy(self, request, *args, **kwargs)


class PermissionViewSet(IamViewSetMixin, viewsets.ReadOnlyModelViewSet):
    """The permission catalogue: read-only, global, seeded by a deploy.

    Guarded by ``iam.role.read`` rather than a permission of its own — the
    catalogue is only useful to someone building a role, and adding a codename
    to ``config/permissions.json`` for the endpoint that lists codenames is a
    loop nobody needs.

    Paginated by page number, not cursor: ``Permission`` has no ``created_at``
    (it is not a ``TimeStampedModel``), which the cursor paginator's ordering
    requires, and it is a small bounded set a user genuinely wants a page count
    for.
    """

    serializer_class = PermissionSerializer
    queryset = Permission.objects.all().order_by("domain", "resource", "action")
    pagination_class = SmallPagePagination
    # The primary key *is* the codename, and codenames contain dots
    # ("iam.role.read"). DRF's default lookup regex is ``[^/.]+``, which stops
    # at the first dot and makes every detail URL a 404.
    lookup_value_regex = "[^/]+"
    filterset_fields = ("domain", "resource", "is_sensitive")
    search_fields = ("codename", "description")

    required_permissions = {"*": ["iam.role.read"]}


class RoleAssignmentViewSet(IamViewSetMixin, viewsets.ModelViewSet):
    """Grants of a role to a membership, optionally narrowed by department/project.

    Creation and update run ``assert_can_grant_role`` inside the serializer, so
    the escalation guard cannot be skipped by a future viewset that reuses the
    serializer. Deletion is permitted here — a role assignment is a join row
    whose fact is "this grant exists now"; its history lives in
    ``TenantAuditLog``, which is written on both grant and revoke.
    """

    serializer_class = RoleAssignmentSerializer
    queryset = RoleAssignment.objects.all()
    filterset_fields = ("membership", "role", "department", "project")

    required_permissions = {
        "GET": ["iam.membership.read"],
        "HEAD": ["iam.membership.read"],
        "OPTIONS": ["iam.membership.read"],
        "POST": ["iam.membership.assign_role"],
        "PUT": ["iam.membership.assign_role"],
        "PATCH": ["iam.membership.assign_role"],
        "DELETE": ["iam.membership.revoke_role"],
    }

    def get_queryset(self):
        return (
            RoleAssignment.objects.filter(
                membership__tenant_id=self.current_tenant_id()
            )
            .select_related("role", "membership", "membership__user", "department", "project")
            .order_by("-created_at", "-id")
        )

    def destroy(self, request, *args, **kwargs):
        # Overrides IamViewSetMixin's blanket refusal: revoking a grant is the
        # one delete in this module that is a legitimate business action.
        return mixins.DestroyModelMixin.destroy(self, request, *args, **kwargs)

    def perform_destroy(self, instance: RoleAssignment) -> None:
        from apps.tenancy.models import TenantAuditLog

        TenantAuditLog.objects.create(
            tenant_id=instance.membership.tenant_id,
            actor_id=self.request.user.id,
            actor_email=self.request.user.email,
            action=TenantAuditLog.Action.ROLE_REVOKED,
            object_type="iam.RoleAssignment",
            object_id=instance.id,
            payload={
                "role": instance.role.code,
                "membership": str(instance.membership_id),
            },
        )
        instance.delete()


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

class ApiKeyViewSet(
    IamViewSetMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Server-to-server credentials.

    No update route: a key's secret is never rotated in place. Rotation is
    "issue a new key, revoke the old one", which leaves both live for a window
    the customer controls and leaves an audit trail of exactly when the old one
    stopped working. No delete route either — a revoked key must stay listed,
    or "which integration was using the key that leaked?" has no answer.
    """

    serializer_class = ApiKeySerializer
    queryset = ApiKey.objects.all()
    filterset_fields = ("role",)
    search_fields = ("name", "prefix")

    required_permissions = {
        "GET": ["iam.api_key.read"],
        "HEAD": ["iam.api_key.read"],
        "OPTIONS": ["iam.api_key.read"],
        "POST": ["iam.api_key.create"],
        "revoke": ["iam.api_key.revoke"],
    }

    def get_queryset(self):
        return (
            ApiKey.objects.filter(tenant_id=self.current_tenant_id())
            .select_related("role", "created_by")
            .order_by("-created_at", "-id")
        )

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None):
        api_key = self.get_object()
        if api_key.revoked_at is None:
            api_key.revoked_at = timezone.now()
            api_key.save(update_fields=["revoked_at", "updated_at"])
            logger.info(
                "api key revoked tenant=%s prefix=%s by=%s",
                api_key.tenant_id, api_key.prefix, request.user.id,
            )
        return Response(self.get_serializer(api_key).data)
