"""
Authentication routes — mounted at ``/api/v1/auth/``.

This prefix is listed in ``TENANT_EXEMPT_PREFIXES``: you cannot know the tenant
before you know the user, so these views run with no tenant bound. Everything
here therefore resolves membership explicitly rather than relying on
``request.tenant_id``.

    POST /auth/login/            email + password (+ optional tenant_id) -> pair
    POST /auth/refresh/          refresh -> new access (claims are carried over)
    POST /auth/logout/           blacklist a refresh token
    GET  /auth/me/               identity + memberships + effective permissions
    POST /auth/switch-tenant/    new pair bound to another of your tenants
    POST /auth/password/change/  change own password, revoke other sessions
    POST /auth/reauth/           re-present the password for a sensitive action
    GET  /auth/reference/        countries/currencies/timezones/roles (AllowAny)
    POST /auth/signup/           provision a new organisation      (AllowAny)
    POST /auth/accept-invite/    join an organisation by token     (AllowAny)

Why signup and accept-invite belong *here* and not on the router
----------------------------------------------------------------
Both are the chicken-and-egg case this prefix exists for. Signup has no
tenant because it is creating one; accept-invite has no session at all — the
caller holds a token and nothing else. Everything else in the onboarding
surface (invitations, team management) is the opposite: it acts inside one
organisation, so it is mounted on the tenant-resolved router in
``apps.iam.urls`` where ``TenantMiddleware`` re-checks membership on every
request.
"""

from __future__ import annotations

import logging

from django.urls import path
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenRefreshView

from apps.core.throttling import BurstThrottle
from apps.iam.serializers import (
    LoginSerializer,
    LogoutSerializer,
    MeSerializer,
    PasswordChangeSerializer,
    SwitchTenantSerializer,
    TenantSelectionRequired,
    TenantTokenObtainPairSerializer,
)
from apps.iam.serializers_onboarding import (
    AcceptInviteSerializer,
    ReauthSerializer,
    SignupResponseSerializer,
    SignupSerializer,
)

logger = logging.getLogger("erp.security")


class LoginThrottle(AnonRateThrottle):
    """Credential stuffing is the attack the ``login`` rate (10/min) exists for.

    An *anon* throttle, keyed on the client IP: the caller is by definition
    unauthenticated at this point, so there is no user or tenant to key on. The
    tenant-scoped throttles in ``apps.core.throttling`` return ``None`` for an
    anonymous request and would let the login endpoint through unlimited.
    """

    scope = "login"


class TenantTokenObtainPairView(APIView):
    """``POST /auth/login/``.

    Not SimpleJWT's ``TokenObtainPairView`` because of one response shape: when
    the user belongs to several organisations we must answer 409 with the list
    of candidates so the client can prompt. A serializer cannot express that —
    a ``ValidationError`` would flatten the list into a field-errors dict — so
    the view catches :class:`TenantSelectionRequired` and renders it.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [LoginThrottle]
    serializer_class = TenantTokenObtainPairSerializer

    @extend_schema(request=LoginSerializer, responses={200: None, 409: None})
    def post(self, request):
        serializer = self.serializer_class(data=request.data, context={"request": request})
        try:
            serializer.is_valid(raise_exception=True)
        except TenantSelectionRequired as exc:
            # 409, with the candidates. The client renders a workspace picker
            # and re-posts with tenant_id; the server never guesses.
            return Response(
                {"error": {
                    "code": exc.default_code,
                    "detail": str(exc.detail),
                    "status": exc.status_code,
                    "tenants": exc.tenants,
                }},
                status=exc.status_code,
            )

        data = serializer.validated_data
        self._audit_login(request, data)
        return Response(data, status=status.HTTP_200_OK)

    @staticmethod
    def _audit_login(request, data: dict) -> None:
        from apps.core.middleware import get_client_ip, get_user_agent
        from apps.tenancy.models import TenantAuditLog

        # The login audit row carries the tenant the user just authenticated
        # into, but login is a tenant-exempt path — nothing has bound
        # `app.current_tenant`, so the RLS WITH CHECK clause rejects the INSERT
        # ("new row violates row-level security policy"). Writing it under the
        # scoped bypass is correct: this row is the *record* of establishing
        # the tenant, so it necessarily precedes the binding.
        from apps.core.tenancy_context import cross_tenant_lookup

        tenant = data.get("tenant") or {}
        try:
            with cross_tenant_lookup():
                    TenantAuditLog.objects.create(
                    tenant_id=tenant.get("id"),
                    actor_id=(data.get("user") or {}).get("id"),
                    actor_email=(data.get("user") or {}).get("email", ""),
                    action=TenantAuditLog.Action.LOGIN,
                    ip_address=get_client_ip(),
                    user_agent=(get_user_agent() or "")[:512],
                )
        except Exception:  # noqa: BLE001 - never fail a login on audit trouble
            logger.warning("login audit write failed", exc_info=True)


class TenantTokenRefreshView(TokenRefreshView):
    """``POST /auth/refresh/``.

    Plain SimpleJWT: rotation is on and blacklist-after-rotation is on, and the
    tenant claims ride along because they were set on the *refresh* token, and
    SimpleJWT copies every non-reserved claim onto the access token it mints.
    """

    throttle_classes = [LoginThrottle]


class LogoutView(APIView):
    """``POST /auth/logout/`` — blacklist the presented refresh token."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=LogoutSerializer, responses={205: None})
    def post(self, request):
        serializer = LogoutSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # 205 Reset Content: the client should drop its stored credentials.
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(APIView):
    """``GET /auth/me/`` — the payload the client renders its navigation from.

    Exempt from tenant resolution like the rest of this prefix, so it also
    answers for a user who has not selected a workspace yet: ``tenant`` is then
    null and ``permissions`` is empty, which is exactly what the client needs
    to know it must show the workspace picker.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(MeSerializer(request.user, context={"request": request}).data)


class SwitchTenantView(APIView):
    """``POST /auth/switch-tenant/`` — a new pair for another of your tenants.

    Mints rather than mutates: the tenant is a signed claim, so a different
    tenant means a different signature. It also re-reads the membership, which
    is the check that stops a token minted before an offboarding from being
    switched into a tenant the user has since been removed from.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(request=SwitchTenantSerializer, responses={200: None})
    def post(self, request):
        serializer = SwitchTenantSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save(), status=status.HTTP_200_OK)


class PasswordChangeView(APIView):
    """``POST /auth/password/change/``."""

    permission_classes = [IsAuthenticated]
    # Authenticated, so the anon throttle above would not fire; the burst
    # throttle keys on (tenant, user) and reuses the same 10/min budget.
    throttle_classes = [BurstThrottle]
    throttle_scope = "login"

    @extend_schema(request=PasswordChangeSerializer, responses={204: None})
    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        logger.info("password changed user=%s", request.user.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Self-service onboarding
# ---------------------------------------------------------------------------

class ReferenceDataView(APIView):
    """``GET /auth/reference/`` — everything the signup form needs to render.

    AllowAny, because it is consumed before an account exists. Nothing here is
    customer data: the country/currency/timezone tables are properties of the
    release, and ``roles`` lists only the **system** roles shipped with the
    product. A tenant's own custom roles are deliberately absent — their names
    ("Cairo branch approver") describe a customer's org chart and this endpoint
    is public.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [LoginThrottle]

    @extend_schema(responses={200: None})
    def get(self, request):
        from apps.iam.reference_data import reference_payload

        return Response(reference_payload(), status=status.HTTP_200_OK)


class SignupView(APIView):
    """``POST /auth/signup/`` — create an organisation and sign its founder in.

    Throttled on the *login* scope rather than a scope of its own. Signup and
    login are the two unauthenticated write endpoints in the product and the
    abuse is the same shape (a script against the IP), so they share a budget;
    a separate scope would have to be added to every settings module and its
    absence surfaces as ``ImproperlyConfigured`` at request time.

    The response is deliberately identical to ``/auth/login/`` — see
    :func:`apps.iam.services.signup.provision_organisation`.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [LoginThrottle]

    @extend_schema(
        request=SignupSerializer,
        responses={201: SignupResponseSerializer, 409: None},
    )
    def post(self, request):
        from apps.core.middleware import get_client_ip, get_user_agent
        from apps.iam.services.signup import provision_organisation

        serializer = SignupSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        payload = provision_organisation(
            company_name=data["company_name"],
            legal_name=data.get("legal_name", ""),
            country=data["country"],
            base_currency=data["base_currency"],
            timezone_name=data["timezone"],
            full_name=data["full_name"],
            email=data["email"],
            password=data["password"],
            ip_address=get_client_ip(),
            user_agent=(get_user_agent() or "")[:512],
        )
        return Response(payload, status=status.HTTP_201_CREATED)


class AcceptInviteView(APIView):
    """``POST /auth/accept-invite/`` — join an organisation with a token.

    AllowAny and unauthenticated by construction: the invitee has no account
    yet in the common case. The token is the entire authorisation, which is
    why it is signed (tamper-evident, hard-expiring) *and* only its hash is
    stored (a leaked backup yields nothing usable).
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [LoginThrottle]

    @extend_schema(request=AcceptInviteSerializer, responses={200: None})
    def post(self, request):
        from apps.core.middleware import get_client_ip, get_user_agent
        from apps.iam.services.invitations import accept_invitation

        serializer = AcceptInviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        payload = accept_invitation(
            token=data["token"],
            full_name=data["full_name"],
            password=data["password"],
            ip_address=get_client_ip(),
            user_agent=(get_user_agent() or "")[:512],
        )
        return Response(payload, status=status.HTTP_200_OK)


class ReauthView(APIView):
    """``POST /auth/reauth/`` — a five-minute, single-use token for a
    sensitive action.

    ``apps.iam.permissions.assert_reauth`` requires ``X-Reauth-Token`` for
    every permission flagged ``is_sensitive`` — granting a role, deactivating
    a member, changing organisation settings — and
    :func:`apps.iam.permissions.issue_reauth_token` names this route as its
    caller. It had never been mounted, which made those actions unreachable
    rather than guarded. Mounting it does not widen anything: the caller must
    already hold a valid session *and* re-present their password, and the
    token it returns is consumed on first use.

    The tenant comes from the JWT claim and is re-checked against a live
    membership. It has to be explicit: this prefix is tenant-exempt, so
    ``get_current_tenant_id()`` is ``None`` here — and the reauth cache key is
    tenant-namespaced twice over (once in ``reauth_cache_key``, once in
    ``apps.core.cache.tenant_key_func``), so a token issued with no tenant
    bound would be stored under a key the checking side never looks at.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [BurstThrottle]
    throttle_scope = "login"

    @extend_schema(request=ReauthSerializer, responses={200: None})
    def post(self, request):
        from apps.core.tenancy_context import cross_tenant_lookup, tenant_context
        from apps.iam.models import TenantMembership
        from apps.iam.permissions import REAUTH_TTL_SECONDS, issue_reauth_token
        from apps.iam.serializers import TENANT_CLAIM

        serializer = ReauthSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        auth = getattr(request, "auth", None)
        tenant_id = None
        try:
            tenant_id = auth.get(TENANT_CLAIM) if auth is not None else None
        except (AttributeError, TypeError):
            tenant_id = None
        if not tenant_id:
            tenant_id = request.META.get("HTTP_X_TENANT_ID")
        if not tenant_id:
            return Response(
                {"error": {
                    "code": "tenant_required",
                    "detail": "Sign in to an organisation before requesting a "
                              "re-authentication token.",
                    "status": status.HTTP_400_BAD_REQUEST,
                }},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The claim is a hint, never an authorisation — same rule as the
        # middleware. Re-read the membership before minting anything.
        with cross_tenant_lookup():
            membership = TenantMembership.objects.filter(
                user_id=request.user.id, tenant_id=tenant_id, is_active=True
            ).first()
        if membership is None:
            return Response(
                {"error": {
                    "code": "tenant_not_found",
                    "detail": "No active membership for this organisation.",
                    "status": status.HTTP_404_NOT_FOUND,
                }},
                status=status.HTTP_404_NOT_FOUND,
            )

        with tenant_context(tenant_id, user_id=request.user.id):
            token = issue_reauth_token(tenant_id, request.user.id)

        logger.info("reauth token issued user=%s tenant=%s", request.user.id, tenant_id)
        return Response(
            {
                "reauth_token": token,
                "expires_in": REAUTH_TTL_SECONDS,
                "header": "X-Reauth-Token",
            },
            status=status.HTTP_200_OK,
        )


urlpatterns = [
    path("login/", TenantTokenObtainPairView.as_view(), name="login"),
    path("refresh/", TenantTokenRefreshView.as_view(), name="refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path("switch-tenant/", SwitchTenantView.as_view(), name="switch-tenant"),
    path("password/change/", PasswordChangeView.as_view(), name="password-change"),
    path("reauth/", ReauthView.as_view(), name="reauth"),
    # ---- self-service onboarding (AllowAny) ----
    path("reference/", ReferenceDataView.as_view(), name="reference"),
    path("signup/", SignupView.as_view(), name="signup"),
    path("accept-invite/", AcceptInviteView.as_view(), name="accept-invite"),
]
