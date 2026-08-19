"""
Reporting URL registration.

Only the two stored resources are routed here. *Running* a report is not a
collection — there is no "list of profit and losses" — so each statement gets
its own endpoint under ``/api/v1/reporting/`` in
:mod:`apps.reporting.urls_extra`, guarded by its own ``reporting.<statement>.read``
codename. Mounting them as a viewset would force one permission across
statements that are deliberately granted separately (the HR manager may read
the payroll register and must not read the balance sheet).
"""

from __future__ import annotations

from apps.reporting.viewsets import ReportDefinitionViewSet, ReportSnapshotViewSet


def register(router) -> None:
    """Mount this app's routes on the v1 router."""
    router.register(
        r"report-definitions", ReportDefinitionViewSet, basename="report-definitions"
    )
    router.register(
        r"report-snapshots", ReportSnapshotViewSet, basename="report-snapshots"
    )
