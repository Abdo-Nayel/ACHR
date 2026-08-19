"""
Invitation and team-management viewsets.

Mounted on the shared v1 router by :func:`apps.iam.urls.register`:

    /api/v1/invitations/                       list, create
    /api/v1/invitations/{id}/resend/
    /api/v1/invitations/{id}/revoke/
    /api/v1/team/members/                      list, retrieve
    /api/v1/team/members/{id}/roles/           POST   grant
    /api/v1/team/members/{id}/roles/{role_id}/ DELETE revoke
    /api/v1/team/members/{id}/deactivate/
    /api/v1/team/members/{id}/activate/

On the *router*, not under ``/api/v1/auth/``, and that is a security decision
rather than a stylistic one: ``/api/v1/auth/`` is listed in
``TENANT_EXEMPT_PREFIXES``, so ``TenantMiddleware`` never runs there — no
tenant is resolved, no membership is re-checked, and ``app.current_tenant`` is
never bound, which means RLS would refuse every read anyway. These endpoints
are the opposite of tenant-exempt: every one of them acts *inside* one
organisation and must be membership-checked on every request.

Like the rest of :mod:`apps.iam.viewsets`, none of these inherit
``apps.core.viewsets.TenantModelViewSet``. That base mixes in
``ScopedQuerysetMixin``, which fails closed for any resource missing from
``apps.iam.permissions.SCOPE_FIELDS`` — and "invitation" and "membership" are
not in it, so every list would come back empty. The tenant narrowing is
therefore written out explicitly, once per queryset.
"""

from __future__ import annotations

import logging

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.pagination import SmallPagePagination
from apps.core.tenancy_context import get_current_tenant_id
from apps.core.viewsets import HardDeleteDisabled
from apps.iam.models import Invitation, Role, TenantMembership
from apps.iam.permissions import HasPermission, TenantResolutionError
from apps.iam.serializers_onboarding import (
    GrantRoleSerializer,
    InvitationCreateSerializer,
    InvitationSerializer,
    TeamMemberSerializer,
)
from apps.iam.services import invitations as invitation_service
from apps.iam.services import team as team_service

logger = logging.getLogger("erp.security")


class _TenantBoundMixin:
    """Deny-by-default RBAC plus a tenant filter that is never implicit."""

    permission_classes = [IsAuthenticated, HasPermission]
    pagination_class = SmallPagePagination

    def current_tenant_id(self):
        tenant_id = getattr(self.request, "tenant_id", None) or get_current_tenant_id()
        if tenant_id is None:
            # Never fall back to "all tenants": every queryset below becomes a
            # cross-tenant read if this returns None.
            raise TenantResolutionError(
                "No organisation is bound to this request; team data cannot be "
                "listed."
            )
        return tenant_id

    def destroy(self, request, *args, **kwargs):
        raise HardDeleteDisabled(
            "Access-control records are revoked or deactivated, never deleted: "
            "the history of who could do what is the audit trail."
        )


class InvitationViewSet(
    _TenantBoundMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Pending offers of membership in the active organisation.

    ``list`` defaults to *pending* only, because that is the question the team
    screen asks ("who have we invited who has not joined?"). The full history
    is available with ``?status=accepted`` and is worth keeping: an accepted
    invitation is the record of how somebody got in.
    """

    serializer_class = InvitationSerializer
    queryset = Invitation.objects.none()  # never used; get_queryset is authoritative

    required_permissions = {
        "GET": ["iam.user.read"],
        "HEAD": ["iam.user.read"],
        "OPTIONS": ["iam.user.read"],
        "POST": ["iam.user.invite"],
        "resend": ["iam.user.invite"],
        "revoke": ["iam.user.invite"],
    }

    def get_queryset(self):
        queryset = (
            Invitation.objects.filter(tenant_id=self.current_tenant_id())
            .select_related("role", "invited_by", "department")
            .order_by("-created_at", "-id")
        )
        if self.action != "list":
            return queryset
        wanted = self.request.query_params.get("status")
        if wanted:
            return queryset.filter(status=wanted)
        return queryset.filter(status=Invitation.Status.PENDING)

    def create(self, request, *args, **kwargs):
        serializer = InvitationCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        invitation, url = invitation_service.create_invitation(
            tenant_id=self.current_tenant_id(),
            email=data["email"],
            role=data["role"],
            department=data.get("department"),
            actor=request.user,
            request=request,
        )
        body = InvitationSerializer(invitation, context={"request": request}).data
        # Returned once, to the inviter, so they can hand the link over
        # directly when corporate mail eats the message. It is not stored and
        # cannot be re-read: ``resend`` mints a new one.
        body["invite_url"] = url
        return Response(body, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="resend")
    def resend(self, request, pk=None):
        """New token, new deadline, same invitation row.

        The previous token stops working the moment ``token_hash`` is
        overwritten — a resend must not leave two live links, because the one
        that leaked is always the older one.
        """
        invitation = self.get_object()
        invitation, url = invitation_service.resend_invitation(
            invitation, actor=request.user, request=request
        )
        body = InvitationSerializer(invitation, context={"request": request}).data
        body["invite_url"] = url
        return Response(body, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None):
        invitation = self.get_object()
        invitation = invitation_service.revoke_invitation(
            invitation, actor=request.user, request=request
        )
        return Response(
            InvitationSerializer(invitation, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )


class TeamMemberViewSet(
    _TenantBoundMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """The people in this organisation, their roles and their status.

    A read-only list plus four explicit verbs. There is no ``PATCH``: "set
    this member's roles to X" would express a grant and a revoke as one
    opaque write, and the two are separately permissioned
    (``iam.membership.assign_role`` / ``iam.membership.revoke_role``) because
    an administrator who may add an Accountant is not automatically one who
    may strip everybody else's access.
    """

    serializer_class = TeamMemberSerializer
    queryset = TenantMembership.objects.none()

    required_permissions = {
        "GET": ["iam.membership.read"],
        "HEAD": ["iam.membership.read"],
        "OPTIONS": ["iam.membership.read"],
        "grant_role": ["iam.membership.assign_role"],
        "revoke_role": ["iam.membership.revoke_role"],
        "deactivate": ["iam.user.deactivate"],
        "activate": ["iam.user.update"],
    }

    def get_queryset(self):
        return (
            TenantMembership.objects.filter(tenant_id=self.current_tenant_id())
            .select_related("user", "invited_by")
            .prefetch_related("role_assignments__role", "role_assignments__department")
            .order_by("-is_owner", "user__email")
        )

    def _role_from_code(self, code: str) -> Role:
        """Resolve a role code inside this tenant, or 404.

        System roles plus this tenant's own custom roles, never another
        customer's: ``Role`` is not filtered by ``TenantManager`` (it is a
        plain model with a nullable tenant), so the narrowing has to be
        written here.
        """
        tenant_id = self.current_tenant_id()
        role = (
            Role.objects.filter(code=code, tenant_id=tenant_id).first()
            or Role.objects.filter(
                code=code, tenant__isnull=True, is_system=True
            ).first()
        )
        if role is None:
            raise team_service.RoleNotHeld(f"No role with code '{code}'.")
        return role

    @action(detail=True, methods=["post"], url_path="roles")
    def grant_role(self, request, pk=None):
        """``POST /team/members/{id}/roles/`` — body ``{role, department?}``."""
        membership = self.get_object()
        serializer = GrantRoleSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        team_service.grant_role(
            membership=membership,
            role=data["role"],
            department=data.get("department"),
            valid_until=data.get("valid_until"),
            actor=request.user,
            request=request,
        )
        membership.refresh_from_db()
        return Response(
            TeamMemberSerializer(membership).data, status=status.HTTP_200_OK
        )

    @action(
        detail=True,
        methods=["delete"],
        url_path=r"roles/(?P<role_id>[^/.]+)",
    )
    def revoke_role(self, request, pk=None, role_id=None):
        """``DELETE /team/members/{id}/roles/{role_id}/``.

        ``role_id`` is the role's UUID here (not its code) because this is a
        RESTful sub-resource address: the client got it from the ``roles``
        array of the same member payload it is now editing.
        """
        membership = self.get_object()
        role = Role.objects.filter(pk=role_id).first()
        if role is None or (
            role.tenant_id is not None and role.tenant_id != membership.tenant_id
        ):
            raise team_service.RoleNotHeld()

        team_service.revoke_role(
            membership=membership,
            role=role,
            actor=request.user,
            request=request,
        )
        membership.refresh_from_db()
        return Response(
            TeamMemberSerializer(membership).data, status=status.HTTP_200_OK
        )

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None):
        membership = self.get_object()
        membership = team_service.deactivate_member(
            membership=membership, actor=request.user, request=request
        )
        return Response(TeamMemberSerializer(membership).data)

    @action(detail=True, methods=["post"], url_path="activate")
    def activate(self, request, pk=None):
        membership = self.get_object()
        membership = team_service.activate_member(
            membership=membership, actor=request.user, request=request
        )
        return Response(TeamMemberSerializer(membership).data)
