"""
Tenancy router registrations.

Registered first by ``config/urls.py`` — before IAM and before every business
app — so that the OpenAPI document (and therefore the generated TypeScript
client) starts with the resources everything else is scoped by.
"""

from __future__ import annotations

from rest_framework.routers import BaseRouter

from apps.tenancy.viewsets import (
    SubscriptionViewSet,
    TenantAuditLogViewSet,
    TenantDomainViewSet,
    TenantViewSet,
)


def register(router: BaseRouter) -> None:
    """Add the tenancy prefixes to the shared v1 router."""
    router.register(r"tenants", TenantViewSet, basename="tenant")
    router.register(r"domains", TenantDomainViewSet, basename="domain")
    router.register(r"subscriptions", SubscriptionViewSet, basename="subscription")
    router.register(r"audit-logs", TenantAuditLogViewSet, basename="audit-log")
