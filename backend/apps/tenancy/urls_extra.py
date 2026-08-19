"""
Non-viewset tenancy routes — mounted at ``/api/v1/tenancy/``.

    GET   /tenancy/current/           the active organisation, its settings and
                                      feature flags, in one call
    PATCH /tenancy/current/           update the organisation's own settings
    POST  /tenancy/audit-logs/export/ request an audit-trail export (stub)

``current/`` is deliberately *not* ``/tenants/{id}/``: the client should never
have to know its own tenant id to bootstrap. The id lives in a signed JWT
claim, the middleware has already resolved and membership-checked it, and a
client that constructs the URL from a value it stores locally is a client that
can be pointed at the wrong organisation by a stale cache.
"""

from __future__ import annotations

from django.urls import path
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import DomainError
from apps.core.throttling import BurstThrottle
from apps.iam.permissions import HasPermission, TenantResolutionError
from apps.tenancy.models import Tenant
from apps.tenancy.serializers import (
    TenantSettingsSerializer,
    TenantSettingsUpdateSerializer,
)


class CurrentTenantView(APIView):
    """``GET /tenancy/current/`` — everything needed to render the first screen.

    Membership is re-read here rather than trusted from the token: this is
    frequently the first call after a token refresh, and it is the cheapest
    place to notice that the caller was removed from the organisation while
    their tab was in the background.
    """

    permission_classes = [IsAuthenticated, HasPermission]
    #: Read and write are separately permissioned. ``settings.organisation.update``
    #: is flagged ``is_sensitive`` in ``config/permissions.json``, so
    #: ``HasPermission`` also demands a fresh ``X-Reauth-Token`` for the PATCH —
    #: obtained from ``POST /api/v1/auth/reauth/``. That is deliberate: these
    #: fields decide what the books mean.
    required_permissions = {
        "GET": ["settings.organisation.read"],
        "HEAD": ["settings.organisation.read"],
        "OPTIONS": ["settings.organisation.read"],
        "PATCH": ["settings.organisation.update"],
    }

    def _resolve_tenant(self, request):
        """The bound organisation, re-checked against a live membership.

        Returns ``None`` rather than raising so both verbs render the same
        404 — a 403 would confirm that an organisation exists, which lets an
        attacker enumerate customer ids.
        """
        tenant_id = getattr(request, "tenant_id", None)
        if tenant_id is None:
            raise TenantResolutionError(
                "No organisation is bound to this request. Send X-Tenant-ID or "
                "sign in again to choose a workspace."
            )
        return (
            Tenant.objects.filter(
                id=tenant_id,
                memberships__user_id=request.user.id,
                memberships__is_active=True,
            )
            .prefetch_related("domains")
            .distinct()
            .first()
        )

    @staticmethod
    def _not_found():
        return Response(
            {"error": {
                "code": "tenant_not_found",
                "detail": "No active membership for this organisation.",
                "status": status.HTTP_404_NOT_FOUND,
            }},
            status=status.HTTP_404_NOT_FOUND,
        )

    def get(self, request):
        tenant = self._resolve_tenant(request)
        if tenant is None:
            return self._not_found()
        return Response(
            TenantSettingsSerializer(tenant, context={"request": request}).data
        )

    @extend_schema(
        request=TenantSettingsUpdateSerializer,
        responses={200: None, 409: None},
    )
    def patch(self, request):
        """Update the organisation's own settings.

        Owner or ``settings.organisation.update`` — enforced twice, in
        ``HasPermission`` above and again in
        ``TenantSerializer._assert_may_write``, because the second one is what
        keeps an Owner who revoked their own role able to fix it.

        ``base_currency`` is accepted but refused with a 409 once any journal
        entry has been posted; see
        :mod:`apps.tenancy.services.settings`. The response is the same
        payload ``GET`` returns, so the client re-renders from one shape.
        """
        tenant = self._resolve_tenant(request)
        if tenant is None:
            return self._not_found()

        serializer = TenantSettingsUpdateSerializer(
            tenant, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        tenant = serializer.save()
        tenant.refresh_from_db()
        return Response(
            TenantSettingsSerializer(tenant, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )


class ExportNotImplemented(DomainError):
    """501 rather than a silent 202 that never produces a file."""

    status_code = status.HTTP_501_NOT_IMPLEMENTED
    default_code = "export_not_implemented"
    default_detail = (
        "Audit-log export is not enabled on this deployment yet. The export "
        "runs as a background job and delivers a signed, expiring download "
        "link; until that job exists this endpoint refuses rather than "
        "returning an empty file."
    )


class AuditLogExportView(APIView):
    """``POST /tenancy/audit-logs/export/`` — stub.

    Kept mounted, permission-guarded and throttled from day one so that the
    contract (and the ``settings.audit_log.export`` permission, which is
    ``is_sensitive`` and therefore demands re-authentication) is exercised by
    the client and by the schema before the job behind it is written.

    Exports are asynchronous by design: the audit trail of a busy tenant is
    millions of rows, and a synchronous export holds a database connection and
    a worker for minutes while the client times out at 30 seconds.
    """

    permission_classes = [IsAuthenticated, HasPermission]
    required_permissions = {"*": ["settings.audit_log.export"]}
    throttle_classes = [BurstThrottle]
    throttle_scope = "export"

    @extend_schema(request=None, responses={501: None})
    def post(self, request):
        raise ExportNotImplemented()


urlpatterns = [
    path("current/", CurrentTenantView.as_view(), name="current"),
    path("audit-logs/export/", AuditLogExportView.as_view(), name="audit-log-export"),
]
