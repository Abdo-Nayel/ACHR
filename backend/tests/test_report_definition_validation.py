"""A report definition may only name a report the engine can actually produce.

``ReportType`` still lists ``expense_by_category`` for the roadmap with no
generator registered for it. Saving a definition for such a type used to
succeed and then fail silently at run time (an uninformative ``last_error`` on
the schedule); the serializer now refuses it up front. The general-ledger,
journal-register, financial-ratios and party-statement types *are* now
implemented, so a definition may name them.
"""

from __future__ import annotations

import pytest

from apps.reporting.generators.base import registered_reports
from apps.reporting.models import ReportType
from apps.reporting.viewsets import ReportDefinitionSerializer

pytestmark = pytest.mark.django_db


def _payload(report_type: str) -> dict:
    return {
        "code": "R1",
        "name": "Test report",
        "report_type": report_type,
        "default_parameters": {},
    }


@pytest.mark.parametrize(
    "report_type",
    [
        ReportType.TRIAL_BALANCE,
        ReportType.GENERAL_LEDGER,
        ReportType.JOURNAL_REGISTER,
        ReportType.FINANCIAL_RATIOS,
        ReportType.PARTY_STATEMENT,
    ],
)
def test_a_registered_report_type_is_accepted(tenant, report_type):
    assert report_type in registered_reports()  # premise of the test
    serializer = ReportDefinitionSerializer(
        data=_payload(report_type), context={"tenant_id": tenant.id}
    )
    assert serializer.is_valid(), serializer.errors


@pytest.mark.parametrize("report_type", [ReportType.EXPENSE_BY_CATEGORY])
def test_an_unimplemented_report_type_is_refused(tenant, report_type):
    assert report_type not in registered_reports()  # premise of the test
    serializer = ReportDefinitionSerializer(
        data=_payload(report_type), context={"tenant_id": tenant.id}
    )
    assert not serializer.is_valid()
    assert "report_type" in serializer.errors
