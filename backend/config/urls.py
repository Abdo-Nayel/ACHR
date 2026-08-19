"""
Root URL configuration.

Versioning
----------
The version lives in the *path* (``/api/v1/...``), not in a header. Header
versioning is invisible in logs, in CDN cache keys and in a customer's curl
command; path versioning means "which version broke?" is answerable from an
access log. v2 will mount a second router beside v1 and both will be served
until every mobile build older than the cutoff has aged out — an app on a
user's phone cannot be forced to upgrade.

Health endpoints
----------------
``/healthz``  liveness  — process is up. NEVER touches the database: if the
              DB is down, restarting the pod does not help, and a liveness
              probe that fails on DB loss turns a database blip into a
              cluster-wide crash loop.
``/readyz``   readiness — DB, Redis and the broker are reachable *and* the
              RLS/trigger guards are installed. Failing readiness pulls the
              pod out of the load balancer without killing it.
"""

from __future__ import annotations

from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.routers import DefaultRouter

from apps.accounting import urls as accounting_urls
from apps.banking import urls as banking_urls
from apps.core.views import (
    ApiRootView,
    HealthView,
    ReadinessView,
    VersionView,
    frontend_app,
)
from apps.expenses import urls as expenses_urls
from apps.hr import urls as hr_urls
from apps.iam import urls as iam_urls
from apps.inventory import urls as inventory_urls
from apps.payments import urls as payments_urls
from apps.payroll import urls as payroll_urls
from apps.projects import urls as projects_urls
from apps.reporting import urls as reporting_urls
from apps.sales import urls as sales_urls
from apps.tenancy import urls as tenancy_urls

# ---------------------------------------------------------------------------
# v1 router
# ---------------------------------------------------------------------------
# One DefaultRouter, extended by each app's `register(router)` hook. Each app
# owns its own prefixes so adding a module never edits this file, but the
# registration order stays deterministic (important: drf-spectacular emits
# operation ids in registration order and the generated TypeScript client
# diffs noisily otherwise).
router = DefaultRouter(trailing_slash=True)
router.include_format_suffixes = False

for module in (
    tenancy_urls,
    iam_urls,
    accounting_urls,
    sales_urls,
    payments_urls,
    expenses_urls,
    inventory_urls,
    banking_urls,
    projects_urls,
    hr_urls,
    payroll_urls,
    reporting_urls,
):
    module.register(router)


# Non-viewset routes (auth, actions, webhooks, exports) that each app exposes
# as a plain urlpatterns list.
v1_patterns = [
    path("", include(router.urls)),
    path("auth/", include(("apps.iam.urls_auth", "auth"), namespace="auth")),
    path("tenancy/", include(("apps.tenancy.urls_extra", "tenancy"), namespace="tenancy")),
    path("accounting/", include(("apps.accounting.urls_extra", "accounting"),
                                namespace="accounting")),
    path("sales/", include(("apps.sales.urls_extra", "sales"), namespace="sales")),
    # Webhooks are deliberately outside the JWT-authenticated router: they are
    # authenticated by gateway signature, and CSRF-exempt. Keeping them on a
    # distinct prefix makes that exception auditable in one place.
    path("payments/", include(("apps.payments.urls_extra", "payments"),
                              namespace="payments")),
    path("payroll/", include(("apps.payroll.urls_extra", "payroll"), namespace="payroll")),
    path("hr/", include(("apps.hr.urls_extra", "hr"), namespace="hr")),
    path("reporting/", include(("apps.reporting.urls_extra", "reporting"),
                               namespace="reporting")),
]

urlpatterns = [
    # ---- API ----
    path("api/v1/", include((v1_patterns, "v1"), namespace="v1")),

    # ---- OpenAPI schema ----
    # The schema is a build artefact: packages/api-client and packages/domain
    # in the frontend monorepo are generated from it, and CI fails if the
    # committed schema differs from the generated one. That is what stops the
    # API and the TypeScript types drifting apart silently.
    path("api/schema/", SpectacularAPIView.as_view(api_version="v1"), name="schema"),
    path("api/schema/docs/",
         SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/schema/redoc/",
         SpectacularRedocView.as_view(url_name="schema"), name="redoc"),

    # ---- Operational ----
    path("healthz", HealthView.as_view(), name="healthz"),
    path("readyz", ReadinessView.as_view(), name="readyz"),
    path("version", VersionView.as_view(), name="version"),

    # The web UI, same-origin with the API so no CORS is involved.
    path("app/", frontend_app, name="frontend"),
    path("app/<str:asset>", frontend_app, name="frontend-asset"),
    # Invitation links land here; the client reads ?token= and shows the
    # accept form. Served by the same view so the deep link needs no routing.
    path("app/accept-invite", frontend_app, name="frontend-accept"),

    # Index. Last in the list so it can never shadow a real route.
    path("", ApiRootView.as_view(), name="api-root"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Uncaught errors return the same RFC7807-ish JSON body as DRF errors, so a
# client never has to parse an HTML error page.
handler400 = "apps.core.views.bad_request"
handler403 = "apps.core.views.permission_denied"
handler404 = "apps.core.views.not_found"
handler500 = "apps.core.views.server_error"
