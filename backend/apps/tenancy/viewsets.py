"""
Tenancy viewsets.

WHY NONE OF THIS USES ``apps.core.viewsets.TenantModelViewSet``
---------------------------------------------------------------
``Tenant`` is **not** a tenant-scoped row — it *is* the scope. There is no
``tenant_id`` column on ``tenancy_tenant`` to filter by, ``TenantManager`` does
not apply to it, and the Row-Level Security policies that protect every
business table do not cover it either (a policy comparing ``tenant_id`` to the
session variable cannot exist on the table that defines what a tenant is).

Everything downstream of that follows:

* The visibility rule has to be written out explicitly, and it is *membership*:
  a caller sees the organisations they hold an active ``TenantMembership`` in.
  There is no fallback and no "if no tenant is bound, show everything" branch —
  an unbound request gets an empty queryset.
* ``ScopedQuerysetMixin`` would be worse than useless here: "tenant" is not in
  ``apps.iam.permissions.SCOPE_FIELDS``, so ``build_scope_q`` fails closed and
  every list would return nothing, which reads as a data bug rather than as a
  policy decision.
* ``TenantDomain``, ``Subscription`` and ``TenantAuditLog`` *do* carry a
  ``tenant`` FK but are plain models (no ``TenantManager``), so they are
  filtered explicitly against the request's bound tenant for the same reason.
"""

from __future__ import annotations

import logging

from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated

from apps.core.pagination import SmallPagePagination, TenantCursorPagination
from apps.core.tenancy_context import get_current_tenant_id
from apps.core.viewsets import HardDeleteDisabled
from apps.iam.permissions import HasPermission, TenantResolutionError
from apps.tenancy.models import Subscription, Tenant, TenantAuditLog, TenantDomain
from apps.tenancy.serializers import (
    SubscriptionSerializer,
    TenantAuditLogSerializer,
    TenantDomainSerializer,
    TenantSerializer,
)

logger = logging.getLogger("erp.security")


class AuditLogCursorPagination(TenantCursorPagination):
    """``TenantAuditLog`` records ``occurred_at``, not ``created_at``.

    The default tenant cursor orders by ``(-created_at, -id)``; this table has
    no such column (it is a bare ``UUIDModel``), and a cursor paginator whose
    ordering field does not exist fails at request time, not at import time.
    """

    ordering = ("-occurred_at", "-id")


class TenancyViewSetMixin:
    """Deny-by-default RBAC plus an explicit, never-implicit tenant filter."""

    permission_classes = [IsAuthenticated, HasPermission]
    pagination_class = TenantCursorPagination

    def current_tenant_id(self):
        tenant_id = getattr(self.request, "tenant_id", None) or get_current_tenant_id()
        if tenant_id is None:
            raise TenantResolutionError(
                "No organisation is bound to this request."
            )
        return tenant_id

    def destroy(self, request, *args, **kwargs):
        raise HardDeleteDisabled(
            "Organisation records are closed or archived, never deleted: the "
            "books they own must outlive the subscription that paid for them."
        )


class TenantViewSet(
    TenancyViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """The organisations the caller belongs to.

    No ``create``: provisioning a tenant is a signup/billing flow with a
    chart-of-accounts seed and a subscription attached, not a POST to a CRUD
    endpoint. No ``destroy``: closing an organisation is a status change that
    keeps the books readable.

    The queryset filter is the security boundary for this endpoint. It joins
    through ``TenantMembership`` rather than comparing to the bound tenant so
    that the workspace switcher (which lists every organisation you can reach)
    and the detail view (which must refuse the ones you cannot) share one rule.
    """

    serializer_class = TenantSerializer
    queryset = Tenant.objects.none()  # never used; get_queryset is authoritative
    pagination_class = SmallPagePagination
    search_fields = ("name", "slug")

    required_permissions = {
        "GET": ["settings.organisation.read"],
        "HEAD": ["settings.organisation.read"],
        "OPTIONS": ["settings.organisation.read"],
        "PUT": ["settings.organisation.update"],
        "PATCH": ["settings.organisation.update"],
    }

    def get_queryset(self):
        user = self.request.user
        if not getattr(user, "is_authenticated", False):
            return Tenant.objects.none()
        return (
            Tenant.objects.filter(
                memberships__user_id=user.id, memberships__is_active=True
            )
            .distinct()
            .order_by("name")
        )

    def perform_update(self, serializer) -> None:
        before = dict(serializer.instance.settings or {})
        updated = serializer.save()
        after = dict(updated.settings or {})
        changed_keys = sorted(
            key for key in set(before) | set(after) if before.get(key) != after.get(key)
        )

        TenantAuditLog.objects.create(
            tenant_id=updated.id,
            actor_id=self.request.user.id,
            actor_email=self.request.user.email,
            action=TenantAuditLog.Action.SETTING_CHANGED,
            object_type="tenancy.Tenant",
            object_id=updated.id,
            # Keys only, not values: settings carry integration hints and
            # contact details, and an audit row is read by more people than the
            # setting itself is.
            payload={
                "changed_setting_keys": changed_keys,
                "changed_fields": sorted(serializer.validated_data.keys()),
            },
        )


class TenantDomainViewSet(TenancyViewSetMixin, viewsets.ModelViewSet):
    """Custom hostnames mapped to the active organisation.

    Verification is a separate, deliberate step (a DNS TXT challenge run by
    ``apps.tenancy.services``): the host-header resolver only ever matches a
    domain with ``verified_at`` set, so an unverified row is inert. Without
    that, adding a row here would be enough to hijack another customer's
    hostname routing.
    """

    serializer_class = TenantDomainSerializer
    queryset = TenantDomain.objects.none()
    pagination_class = SmallPagePagination
    filterset_fields = ("is_primary",)

    required_permissions = {
        "GET": ["settings.organisation.read"],
        "HEAD": ["settings.organisation.read"],
        "OPTIONS": ["settings.organisation.read"],
        "POST": ["settings.organisation.update"],
        "PUT": ["settings.organisation.update"],
        "PATCH": ["settings.organisation.update"],
        "DELETE": ["settings.organisation.update"],
    }

    def get_queryset(self):
        return TenantDomain.objects.filter(
            tenant_id=self.current_tenant_id()
        ).order_by("-is_primary", "domain")

    def destroy(self, request, *args, **kwargs):
        # A domain mapping is a routing row, not a business record: removing it
        # loses no history and is the only way to undo a typo'd hostname.
        return mixins.DestroyModelMixin.destroy(self, request, *args, **kwargs)


class SubscriptionViewSet(
    TenancyViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Plan history for the active organisation. Read-only through the API.

    A customer cannot upgrade themselves by POSTing a row: the plan is written
    by the billing integration once payment succeeds. Exposing it read-only is
    still worth doing — the client shows seats used against seats paid for, and
    the "past due" banner is rendered from it.
    """

    serializer_class = SubscriptionSerializer
    queryset = Subscription.objects.none()
    pagination_class = SmallPagePagination

    required_permissions = {"*": ["settings.organisation.read"]}

    def get_queryset(self):
        return Subscription.objects.filter(
            tenant_id=self.current_tenant_id()
        ).order_by("-started_on")


class TenantAuditLogViewSet(
    TenancyViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """The security trail: logins, role grants, period closes, exports.

    Read-only, append-only, and filtered to the bound tenant. Cursor-paginated
    on ``occurred_at`` because this is the one table that only ever grows and is
    always read newest-first.
    """

    serializer_class = TenantAuditLogSerializer
    queryset = TenantAuditLog.objects.none()
    pagination_class = AuditLogCursorPagination
    filterset_fields = ("action", "actor_id", "object_type", "object_id")
    search_fields = ("actor_email", "object_type")

    required_permissions = {"*": ["settings.audit_log.read"]}

    def get_queryset(self):
        return TenantAuditLog.objects.filter(
            tenant_id=self.current_tenant_id()
        ).order_by("-occurred_at", "-id")
