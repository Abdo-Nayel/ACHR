"""
Tenant resolution and database session binding.

This is the single most security-critical module in the codebase. Everything
downstream — the ORM's default manager, every PostgreSQL RLS policy, every
cache key — reads the tenant this middleware establishes. If it resolves the
wrong tenant, or leaks one request's tenant into the next, the result is a
cross-tenant data breach with no error and no log line.

Resolution order
----------------
1. **JWT claim** (``tenant`` in the access token). Authoritative when present.
2. **``X-Tenant-ID`` header** — used by the mobile client, which cannot rely
   on sub-domains, and by API keys.
3. **Sub-domain / custom domain** — ``acme.app.example.com`` or a verified
   ``TenantDomain``.

The JWT claim wins over the header. If a token minted for tenant A arrives
with ``X-Tenant-ID: B``, that is either a bug or an attack; we do not "prefer
the more specific value", we reject the request. Silently honouring the header
would let anyone with a valid login read any tenant they can name.

Whatever is resolved is then checked against an **active TenantMembership**.
The claim alone is never sufficient: a user removed from a tenant five minutes
ago still holds a valid, unexpired access token.
"""

from __future__ import annotations

import uuid
from typing import Callable, Optional

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.deprecation import MiddlewareMixin

from apps.core.tenancy_context import (
    _current_tenant_id,
    _current_user_id,
    bind_database_session,
)

TENANT_HEADER = "HTTP_X_TENANT_ID"

#: Paths that must work with no tenant at all: login (you cannot know the
#: tenant before you know the user), health probes, the schema, and gateway
#: webhooks (authenticated by signature, tenant derived from the payload).
TENANT_EXEMPT_PREFIXES = (
    "/healthz",
    "/readyz",
    "/version",
    "/api/schema",
    "/api/v1/auth/",
    "/api/v1/payments/webhooks/",
    "/admin/",
    "/static/",
    "/media/",
)


def _json_error(status: int, code: str, detail: str) -> JsonResponse:
    return JsonResponse(
        {"error": {"code": code, "detail": detail, "status": status}}, status=status
    )


def _parse_uuid(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


class TenantMiddleware:
    """Resolve the tenant, verify membership, bind it to the DB session.

    Placed **after** ``AuthenticationMiddleware`` (it needs ``request.user``)
    and **before** any view or DRF permission class (they all assume the
    tenant is already bound).
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    # -- resolution helpers -------------------------------------------------

    def _authenticate(self, request: HttpRequest):
        """Resolve the caller, decoding the bearer token if necessary.

        This is the subtle part. With session auth, Django's
        ``AuthenticationMiddleware`` has already populated ``request.user`` by
        the time we run. With **JWT** it has not: DRF authenticates inside the
        view, which is *after* every middleware. So a middleware that only
        reads ``request.user`` sees ``AnonymousUser`` on every token-carrying
        request, skips tenant binding entirely, and every endpoint then fails
        with a misleading ``permission_denied`` — the permissions were fine,
        the tenant was simply never bound.

        We therefore run SimpleJWT's authenticator here ourselves. The result
        is cached on the request so DRF's own pass is a no-op rather than a
        second signature verification.
        """
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return user, getattr(request, "auth", None)

        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.lower().startswith("bearer "):
            return None, None

        from rest_framework_simplejwt.authentication import JWTAuthentication
        from rest_framework_simplejwt.exceptions import (
            AuthenticationFailed,
            InvalidToken,
            TokenError,
        )

        try:
            result = JWTAuthentication().authenticate(request)
        except (AuthenticationFailed, InvalidToken, TokenError):
            # An invalid token is DRF's 401 to report, with its own envelope
            # and WWW-Authenticate header. Swallow it here and carry on
            # unauthenticated so there is exactly one place that renders it.
            return None, None

        if result is None:
            return None, None

        user, validated_token = result
        # Cache for DRF: `request._authenticate()` short-circuits on these.
        request.user = user
        request._cached_user = user
        request.auth = validated_token
        return user, validated_token

    def _from_jwt(self, request: HttpRequest, token=None) -> Optional[uuid.UUID]:
        auth = token if token is not None else getattr(request, "auth", None)
        if auth is None:
            return None
        try:
            claim = auth.get("tenant")
        except (AttributeError, TypeError):
            return None
        return _parse_uuid(claim) if claim else None

    def _from_header(self, request: HttpRequest) -> Optional[uuid.UUID]:
        raw = request.META.get(TENANT_HEADER)
        return _parse_uuid(raw) if raw else None

    def _from_host(self, request: HttpRequest) -> Optional[uuid.UUID]:
        from apps.tenancy.models import Tenant, TenantDomain

        host = request.get_host().split(":")[0].lower()
        base = getattr(settings, "TENANT_BASE_DOMAIN", "")

        if base and host.endswith("." + base):
            slug = host[: -(len(base) + 1)].split(".")[-1]
            if slug and slug not in {"www", "api", "app"}:
                tenant_id = (
                    Tenant.objects.filter(slug=slug).values_list("id", flat=True).first()
                )
                if tenant_id:
                    return tenant_id

        return (
            TenantDomain.objects.filter(domain=host, verified_at__isnull=False)
            .values_list("tenant_id", flat=True)
            .first()
        )

    # -- main ---------------------------------------------------------------

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path
        if any(path.startswith(p) for p in TENANT_EXEMPT_PREFIXES):
            request.tenant_id = None
            return self.get_response(request)

        user, validated_token = self._authenticate(request)
        if user is None:
            # Let DRF produce the 401 with its normal envelope; there is no
            # tenant to bind for an anonymous caller.
            request.tenant_id = None
            return self.get_response(request)

        from_jwt = self._from_jwt(request, validated_token)
        from_header = self._from_header(request)

        if from_jwt and from_header and from_jwt != from_header:
            return _json_error(
                403, "tenant_mismatch",
                "The X-Tenant-ID header does not match the tenant in your access "
                "token. Re-authenticate against the tenant you intend to use.",
            )

        tenant_id = from_jwt or from_header or self._from_host(request)
        if tenant_id is None:
            return _json_error(
                400, "tenant_required",
                "No organisation could be determined for this request. Send an "
                "X-Tenant-ID header or use your organisation's sub-domain.",
            )

        membership = self._active_membership(user, tenant_id)
        if membership is None:
            # 404 rather than 403 on purpose: a 403 confirms that the tenant
            # exists, which lets an attacker enumerate customer ids.
            return _json_error(
                404, "tenant_not_found",
                "No active membership for this organisation.",
            )

        request.tenant_id = tenant_id
        request.membership = membership

        tenant_token = _current_tenant_id.set(tenant_id)
        user_token = _current_user_id.set(user.id)
        try:
            # ATOMIC_REQUESTS wraps the view in a transaction, so `SET LOCAL`
            # here is scoped to it and cannot survive on a pooled connection.
            with transaction.atomic():
                bind_database_session(tenant_id, bypass=False)
                return self.get_response(request)
        finally:
            # Not optional. Gunicorn/uvicorn reuse worker threads and Django
            # reuses connections; a ContextVar left set is the next request's
            # tenant. Resetting in `finally` is what makes an exception mid-view
            # safe instead of a silent cross-tenant leak.
            _current_tenant_id.reset(tenant_token)
            _current_user_id.reset(user_token)

    @staticmethod
    def _active_membership(user, tenant_id):
        """Verify the claimed tenant against a live membership row.

        This runs *before* the tenant is bound to the session — that is the
        whole point, it is the check that decides whether binding is allowed —
        so it needs ``cross_tenant_lookup`` to see RLS-protected membership
        rows at all. The filter pins both ``user_id`` and ``tenant_id``, so
        the widened visibility covers exactly the row being authorised.

        Re-reading the row on every request (rather than trusting the JWT
        claim) is deliberate: a user removed from a tenant five minutes ago
        still holds a valid, unexpired access token.
        """
        from apps.core.tenancy_context import cross_tenant_lookup
        from apps.iam.models import TenantMembership

        with cross_tenant_lookup():
            return (
                TenantMembership.objects.select_related("tenant")
                .filter(user_id=user.id, tenant_id=tenant_id, is_active=True)
                .first()
            )


class TenantSubscriptionGateMiddleware(MiddlewareMixin):
    """Block writes for suspended or closed tenants — but never block reads.

    A customer whose card failed must always be able to log in, read their
    books and export them. Locking them out of their own accounting records
    over a billing problem is both a terrible experience and, in several
    jurisdictions, a records-access obligation we would be breaching.
    """

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

    def process_request(self, request: HttpRequest):
        membership = getattr(request, "membership", None)
        if membership is None or request.method in self.SAFE_METHODS:
            return None

        tenant = membership.tenant
        if tenant.is_operational:
            return None

        return _json_error(
            402, "tenant_not_operational",
            f"This organisation is {tenant.get_status_display().lower()}. "
            f"Read access and data export remain available; writes are "
            f"suspended until billing is resolved.",
        )
