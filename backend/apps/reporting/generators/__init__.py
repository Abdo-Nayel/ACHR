"""
Report generator package.

Importing this package imports every concrete generator module, which is what
populates the ``@register_report`` registry. The import lives here rather than
being left to each caller for a reason worth stating: a registry that is only
populated when someone happens to have imported the right module produces a
"no report generator registered for 'balance_sheet'" error at 03:00 in a
Celery task, and the fix ("import the module") is invisible in the traceback.
Making the package import the modules means the registry is complete as soon
as anything in ``apps.reporting`` is touched.

``apps.reporting.apps.ReportingConfig.ready()`` imports this package for the
same reason, so the registry is also complete after a bare ``django.setup()``.
"""

from __future__ import annotations

from apps.reporting.generators import financial, operational  # noqa: F401
from apps.reporting.generators.base import (  # noqa: F401
    ReportContext,
    ReportError,
    ReportGenerator,
    ReportImbalance,
    ReportLine,
    ReportResult,
    ReportSection,
    get_generator,
    ledger_query,
    register_report,
    registered_reports,
)

__all__ = [
    "ReportContext",
    "ReportError",
    "ReportGenerator",
    "ReportImbalance",
    "ReportLine",
    "ReportResult",
    "ReportSection",
    "get_generator",
    "ledger_query",
    "register_report",
    "registered_reports",
    "financial",
    "operational",
]
