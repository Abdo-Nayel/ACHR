"""
Log correlation.

Every log line carries ``request_id``, ``tenant_id`` and ``user_id`` so that
"customer X reports an error at 14:02" becomes a single grep rather than an
archaeology session across API and worker logs.

``tenant_id`` in particular is what makes an incident scopeable: when
something breaks, the first question is always "is this one customer or all of
them?", and a log format that cannot answer it turns a five-minute triage into
an hour.
"""

from __future__ import annotations

import logging

from apps.core.middleware import get_client_ip, get_request_id
from apps.core.tenancy_context import get_current_tenant_id, get_current_user_id


class CorrelationFilter(logging.Filter):
    """Injects correlation fields into every record.

    A ``Filter`` rather than a custom ``Formatter`` because it composes: the
    fields become available to *any* formatter, including the JSON formatter
    used in production and the human-readable one used locally.

    Defaults are the literal string ``"-"`` rather than ``None`` so that a
    printf-style format string never renders "None" and never raises on a
    record emitted outside a request (startup, migrations, a bare worker).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        record.tenant_id = str(get_current_tenant_id() or "-")
        record.user_id = str(get_current_user_id() or "-")
        record.client_ip = get_client_ip() or "-"
        return True


class SuppressNoisyPathsFilter(logging.Filter):
    """Drop access-log lines for health probes.

    Kubernetes probes /healthz every few seconds. Left alone they are ~90% of
    the access log volume, which costs money in log storage and, worse, buries
    the real traffic you are trying to read during an incident.
    """

    NOISY = ("/healthz", "/readyz", "/static/", "/favicon.ico")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(path in message for path in self.NOISY)
