"""
IAM router registrations.

``register(router)`` is the contract every app module in this project exposes
(see ``config/urls.py``): the root URLconf owns *one* router and each app adds
its own prefixes, so mounting a new module never edits the root file while the
registration order stays deterministic — drf-spectacular emits operation ids in
registration order and the generated TypeScript client would otherwise diff
noisily on every unrelated change.

Auth routes are not here: they live in ``urls_auth.py`` and are mounted under
``/api/v1/auth/``, which is tenant-exempt.
"""

from __future__ import annotations

from rest_framework.routers import BaseRouter

from apps.iam.viewsets import (
    ApiKeyViewSet,
    PermissionViewSet,
    RoleAssignmentViewSet,
    RoleViewSet,
    TenantMembershipViewSet,
    UserViewSet,
)
from apps.iam.viewsets_team import InvitationViewSet, TeamMemberViewSet


def register(router: BaseRouter) -> None:
    """Add the IAM prefixes to the shared v1 router.

    ``basename`` is explicit on every registration. DRF would otherwise derive
    it from ``queryset.model``, and every viewset here overrides
    ``get_queryset()`` to apply the tenant filter — a viewset whose ``queryset``
    attribute is later removed in favour of the method would then fail at
    import time with a basename error rather than at review time.
    """
    router.register(r"users", UserViewSet, basename="user")
    router.register(r"memberships", TenantMembershipViewSet, basename="membership")
    router.register(r"roles", RoleViewSet, basename="role")
    router.register(r"permissions", PermissionViewSet, basename="permission")
    router.register(r"role-assignments", RoleAssignmentViewSet, basename="role-assignment")
    router.register(r"api-keys", ApiKeyViewSet, basename="api-key")
    # Onboarding surface. Registered last so the operation ids of everything
    # above are unchanged and the generated TypeScript client diffs by
    # addition only.
    #
    # These live on the router rather than under ``/api/v1/auth/`` on purpose:
    # that prefix is tenant-exempt, so ``TenantMiddleware`` never binds a
    # tenant there and every RLS-protected read would come back empty. Only
    # the three genuinely pre-tenant routes (signup, accept-invite,
    # reference) belong in ``urls_auth``.
    router.register(r"invitations", InvitationViewSet, basename="invitation")
    router.register(r"team/members", TeamMemberViewSet, basename="team-member")
