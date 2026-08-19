"""
Operational endpoints and the global error handlers.

The liveness/readiness split is not ceremony — getting it backwards causes
outages:

* ``/healthz`` (**liveness**) must never touch the database. If it did, a
  database blip would fail liveness on every pod at once, the orchestrator
  would restart them all, and a 30-second DB hiccup would become a
  cluster-wide crash loop that outlives the original fault.
* ``/readyz`` (**readiness**) checks the dependencies a request actually
  needs. Failing it removes the pod from the load balancer without killing
  it, so it can recover and rejoin.

``/readyz`` additionally asserts that the RLS policies and ledger triggers are
installed. A pod serving traffic against a database where someone dropped a
policy is worse than a pod that is down.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

APP_VERSION = os.environ.get("APP_VERSION", "0.1.0-phase2")
GIT_SHA = os.environ.get("GIT_SHA", "dev")


class HealthView(APIView):
    """Liveness. Deliberately does nothing but return."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request) -> Response:
        return Response({"status": "ok", "service": "erp-api"})


class ReadinessView(APIView):
    """Readiness: database, cache, and the integrity guards."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request) -> Response:
        checks: dict[str, Any] = {}
        ok = True

        # --- database ------------------------------------------------------
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            checks["database"] = "ok"
        except Exception as exc:  # pragma: no cover - depends on environment
            checks["database"] = f"error: {exc.__class__.__name__}"
            ok = False

        # --- cache ---------------------------------------------------------
        try:
            from django.core.cache import cache

            cache.set("healthcheck:probe", "1", 10)
            checks["cache"] = "ok" if cache.get("healthcheck:probe") == "1" else "degraded"
        except Exception as exc:  # pragma: no cover
            checks["cache"] = f"error: {exc.__class__.__name__}"
            # A cache outage is survivable — we serve slower, not wrong.
            # It must not remove the pod from the load balancer.

        # --- isolation and ledger guards -----------------------------------
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_class "
                    "WHERE relrowsecurity AND relforcerowsecurity"
                )
                rls_tables = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname LIKE 'trg_%'"
                )
                triggers = cursor.fetchone()[0]

            checks["rls_tables"] = rls_tables
            checks["ledger_triggers"] = triggers
            if rls_tables == 0 or triggers < 5:
                checks["guards"] = (
                    "MISSING — refusing traffic: tenant isolation or ledger "
                    "immutability is not enforced on this database."
                )
                ok = False
            else:
                checks["guards"] = "ok"
        except Exception as exc:  # pragma: no cover
            checks["guards"] = f"error: {exc.__class__.__name__}"
            ok = False

        return Response(
            {"status": "ready" if ok else "not_ready", "checks": checks},
            status=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class ApiRootView(APIView):
    """Index at ``/``.

    A bare base URL returning Django's 404 page is a small thing that costs
    real time: it is the first URL anyone types when handed a new API, and a
    debug traceback tells them nothing about where to go next. This lists the
    entry points instead.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request) -> Response:
        base = request.build_absolute_uri("/").rstrip("/")
        return Response(
            {
                "service": "Accounting & HR ERP API",
                "version": APP_VERSION,
                "api": f"{base}/api/v1/",
                "documentation": {
                    "swagger": f"{base}/api/schema/docs/",
                    "redoc": f"{base}/api/schema/redoc/",
                    "openapi": f"{base}/api/schema/",
                },
                "authentication": {
                    "login": f"{base}/api/v1/auth/login/",
                    "refresh": f"{base}/api/v1/auth/refresh/",
                    "me": f"{base}/api/v1/auth/me/",
                    "switch_tenant": f"{base}/api/v1/auth/switch-tenant/",
                    "scheme": "Authorization: Bearer <access token>",
                },
                "operations": {
                    "liveness": f"{base}/healthz",
                    "readiness": f"{base}/readyz",
                    "version": f"{base}/version",
                },
                "notes": [
                    "Every endpoint under /api/v1/ requires authentication.",
                    "Monetary values are JSON strings, not numbers — parsing "
                    "them as floats will corrupt them.",
                    "State changes are POST sub-resources "
                    "(POST /invoices/{id}/issue), never a writable status field.",
                ],
            }
        )


def frontend_app(request, asset: str = "index.html"):
    """Serve the single-file web UI at ``/app/``.

    Served by Django rather than opened as a ``file://`` page on purpose: a
    local file has origin ``null``, so every API call from it is a
    cross-origin request that needs CORS configuration and still breaks on credentials.
    Serving it from the same origin as the API removes that whole class of
    problem — no CORS, no preflight, no "works on my machine".

    Read from disk on each request (not cached) so editing the HTML shows up
    on refresh without restarting the server.
    """
    from django.conf import settings
    from django.http import FileResponse, HttpResponse

    name = (asset or "index.html").split("/")[-1]
    if name not in {"index.html", "app.js"}:
        # Whitelist, not a path join. Serving an arbitrary name from a
        # user-controlled segment is a directory-traversal hole, and this view
        # runs before authentication.
        return HttpResponse("Not found", status=404)
    path = Path(settings.REPO_ROOT) / "frontend" / name
    if not path.exists():
        return HttpResponse(
            "<h1>frontend/index.html not found</h1>"
            f"<p>Looked in: {path}</p>",
            status=404, content_type="text/html; charset=utf-8",
        )
    ctype = ("application/javascript; charset=utf-8" if name.endswith(".js")
             else "text/html; charset=utf-8")
    resp = FileResponse(open(path, "rb"), content_type=ctype)
    # No caching in dev: the whole point of reading from disk per request is
    # that editing the file and refreshing shows the change.
    resp["Cache-Control"] = "no-store"
    return resp


class VersionView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request) -> Response:
        return Response(
            {
                "version": APP_VERSION,
                "commit": GIT_SHA,
                "api_versions": ["v1"],
                "debug": settings.DEBUG,
            }
        )


# ---------------------------------------------------------------------------
# Global error handlers
# ---------------------------------------------------------------------------
# Django's defaults render HTML. A mobile client parsing an HTML 500 page gets
# a JSON decode error, which is then reported as a client bug. These keep the
# envelope identical to every DRF error.

def _error(code: str, detail: str, status_code: int) -> JsonResponse:
    from apps.core.middleware import get_request_id

    body = {"code": code, "detail": detail, "status": status_code}
    request_id = get_request_id()
    if request_id:
        body["request_id"] = request_id
    return JsonResponse({"error": body}, status=status_code)


def bad_request(request, exception=None):
    return _error("bad_request", "The request could not be understood.", 400)


def permission_denied(request, exception=None):
    return _error("permission_denied", "You do not have access to this resource.", 403)


def not_found(request, exception=None):
    return _error("not_found", "No such resource.", 404)


def server_error(request):
    return _error(
        "internal_error",
        "An unexpected error occurred. The incident has been logged.",
        500,
    )
