"""
Cross-cutting request middleware.

Two concerns live here, both deliberately independent of tenancy so they keep
working on requests that have no tenant yet (login, health checks, webhooks):

* :class:`RequestIDMiddleware` — assigns every request a correlation id.
* :class:`AuditContextMiddleware` — captures the actor and client metadata
  that :class:`apps.tenancy.models.TenantAuditLog` records.

Tenant resolution itself lives in ``apps.tenancy.middleware`` because it needs
the authenticated user, and therefore must run *after* Django's
``AuthenticationMiddleware``.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Callable, Optional

from django.http import HttpRequest, HttpResponse

#: Correlation id for the request currently being served. Read by the logging
#: filter and by Celery task headers, so a log line in a worker can be traced
#: back to the HTTP request that queued it.
_request_id: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
_client_ip: ContextVar[Optional[str]] = ContextVar("client_ip", default=None)
_user_agent: ContextVar[Optional[str]] = ContextVar("user_agent", default=None)

REQUEST_ID_HEADER = "HTTP_X_REQUEST_ID"
REQUEST_ID_RESPONSE_HEADER = "X-Request-ID"


def get_request_id() -> Optional[str]:
    return _request_id.get()


def get_client_ip() -> Optional[str]:
    return _client_ip.get()


def get_user_agent() -> Optional[str]:
    return _user_agent.get()


class RequestIDMiddleware:
    """Attach a correlation id to every request and echo it back.

    An inbound ``X-Request-ID`` is honoured so that a trace started at the CDN
    or the mobile client survives into our logs — but it is *validated* as a
    UUID first. Echoing an arbitrary client-supplied string into log files is
    a log-injection vector: a caller who sends a value containing newlines can
    forge whole log entries.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.META.get(REQUEST_ID_HEADER, "")
        try:
            request_id = str(uuid.UUID(incoming))
        except (ValueError, AttributeError, TypeError):
            request_id = str(uuid.uuid4())

        request.request_id = request_id
        token = _request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)
        response[REQUEST_ID_RESPONSE_HEADER] = request_id
        return response


class AuditContextMiddleware:
    """Record who is calling and from where, for the audit log.

    ``REMOTE_ADDR`` is not trusted directly when the app runs behind a proxy;
    the left-most entry of ``X-Forwarded-For`` is used instead, but only when
    ``USE_X_FORWARDED_FOR`` is enabled in settings. Trusting the header
    unconditionally lets any client spoof its own IP in the audit trail, which
    is worse than having no IP at all.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        from django.conf import settings

        self.use_forwarded = getattr(settings, "USE_X_FORWARDED_FOR", False)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        ip = request.META.get("REMOTE_ADDR")
        if self.use_forwarded:
            forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
            if forwarded:
                ip = forwarded.split(",")[0].strip()

        agent = request.META.get("HTTP_USER_AGENT", "")[:512]
        request.client_ip = ip
        request.client_user_agent = agent

        ip_token = _client_ip.set(ip)
        agent_token = _user_agent.set(agent)
        try:
            return self.get_response(request)
        finally:
            _client_ip.reset(ip_token)
            _user_agent.reset(agent_token)
