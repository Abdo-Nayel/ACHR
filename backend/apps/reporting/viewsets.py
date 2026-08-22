"""
Reporting endpoints: saved definitions, frozen snapshots, and running a report.

Permission naming
-----------------
``config/permissions.json`` gives the ``reporting`` domain one resource per
*statement* (``profit_loss``, ``balance_sheet``, ``trial_balance``,
``cash_flow``, ``aging``, ``tax_summary``, ``payroll_register``) plus
``report.export``. There is no ``report_definition`` resource and there must
not be one invented here: ``HasPermission`` denies any codename absent from
the catalogue, so a made-up ``reporting.report_definition.create`` is a
permission nobody can ever hold and an endpoint nobody can ever call.

So the CRUD viewsets here reuse two codenames that do exist:
``reporting.trial_balance.read`` to *read* the catalogue of saved reports, and
``reporting.report.export`` to write one. The split matters because
``report.export`` is ``is_sensitive`` and therefore triggers the re-auth
challenge — appropriate for creating a shared, scheduled report, and actively
harmful on a list endpoint, where prompting for re-authentication on every page
load teaches people to click through the prompt that guards payroll approval.

The per-statement ``*.read`` codenames guard the *running* of each report, in
``apps.reporting.urls_extra``, where they belong: the sensitive thing is the
figures, not the saved configuration that asks for them.

Snapshots are read-only over the API
------------------------------------
A :class:`~apps.reporting.models.ReportSnapshot` is evidence: the exact
figures given to a bank or a tax authority, their parameters, when they were
produced and by whom, plus a checksum. It is created by
``apps.reporting.services.snapshot.generate_and_snapshot`` inside the same
transaction that computed it, so the figures and the evidence cannot diverge.
A writable endpoint would let a caller file a statement the ledger never
produced — which is the single thing the whole snapshot design exists to make
impossible. Correcting a report means taking a *new* snapshot and explaining
the difference, exactly as correcting a posted entry means a reversing entry.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from django.utils.dateparse import parse_date
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import ParseError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pagination import SmallPagePagination
from apps.core.serializers import ReadOnlyModelSerializer, TenantScopedSerializer
from apps.core.tenancy_context import get_current_tenant_id
from apps.core.viewsets import (
    RbacOnlyQuerysetMixin,
    ReadOnlyTenantViewSet,
    TenantModelViewSet,
)
from apps.iam.permissions import HasPermission
from apps.reporting.generators.base import ReportContext, ReportError, get_generator
from apps.reporting.models import ReportDefinition, ReportSnapshot

logger = logging.getLogger(__name__)

#: Writing a saved report configuration. ``reporting.report.export`` is the
#: only non-read authority the domain has, and it is ``is_sensitive``, so these
#: calls also demand a fresh ``X-Reauth-Token``. That is the right weight for
#: "create a shared, scheduled report" and the wrong weight for reading a list.
REPORT_WRITE = ["reporting.report.export"]

#: Reading the catalogue. Deliberately *not* ``report.export``: that codename
#: is sensitive, and requiring re-authentication merely to list saved reports
#: would train users to click through the re-auth prompt, which is the fastest
#: way to make the control worthless where it matters. ``trial_balance.read``
#: is the base statement every accounting reader holds, so it is the honest
#: floor for "may see what reports exist".
REPORT_READ = ["reporting.trial_balance.read"]


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

class ReportDefinitionSerializer(TenantScopedSerializer):
    """A saved report configuration.

    ``default_parameters`` may hold relative dates ("previous_month") because a
    *definition* is a recipe. A :class:`ReportSnapshot`'s parameters may not —
    they are resolved absolutely, which is what makes a filed statement
    reproducible.
    """

    owner_email = serializers.CharField(source="owner.email", read_only=True,
                                        default=None)

    class Meta:
        model = ReportDefinition
        fields = (
            "id", "code", "name", "description", "report_type",
            "default_parameters", "grouping", "is_shared", "owner",
            "owner_email", "is_active", "created_at", "created_by",
            "updated_at",
        )


class ReportSnapshotSerializer(ReadOnlyModelSerializer):
    """A frozen rendering of a report, with the checksum that proves it intact."""

    is_intact = serializers.SerializerMethodField()

    class Meta:
        model = ReportSnapshot
        fields = (
            "id", "definition", "report_type", "parameters", "period_start",
            "period_end", "as_of_date", "generated_at", "generated_by",
            "payload", "row_count", "checksum", "is_intact", "file_key",
            "file_format", "warnings", "currency", "created_at",
        )

    def get_is_intact(self, obj: ReportSnapshot) -> bool:
        """Recompute the checksum on read.

        Cheap, and the only way to notice a payload edited by a direct UPDATE —
        which the ORM's immutability guard does not prevent, because it only
        blocks ``delete()``.
        """
        from apps.reporting.services.snapshot import verify_snapshot

        return verify_snapshot(obj)


# ---------------------------------------------------------------------------
# Context building — shared by every report endpoint
# ---------------------------------------------------------------------------

def _date(params, key: str):
    raw = params.get(key)
    if not raw:
        return None
    parsed = parse_date(str(raw))
    if parsed is None:
        raise ParseError(
            f"'{key}' must be an ISO date (YYYY-MM-DD); got {raw!r}. Guessing a "
            f"format is how a report silently covers the wrong period."
        )
    return parsed


def _uuid(params, key: str) -> Optional[uuid.UUID]:
    raw = params.get(key)
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ParseError(f"'{key}' must be a UUID; got {raw!r}.") from exc


def _bool(params, key: str, default: bool = False) -> bool:
    raw = params.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def build_report_context(request, **overrides: Any) -> ReportContext:
    """Turn query parameters (or a JSON body) into a :class:`ReportContext`.

    The tenant is taken from the request, never from the payload. A report is
    the one place where a caller-supplied tenant id would be both plausible
    ("run this for subsidiary X") and catastrophic, and ``ReportContext``
    refuses a ``None`` tenant outright for the same reason.

    Dates are parsed strictly. A silently-dropped ``date_from`` turns a period
    report into "everything since the company was founded", which is a
    plausible-looking number that answers a different question.
    """
    params = request.query_params if request.method == "GET" else request.data
    if not hasattr(params, "get"):  # pragma: no cover - defensive
        raise ParseError("Report parameters must be an object.")

    tenant_id = getattr(request, "tenant_id", None) or get_current_tenant_id()
    if tenant_id is None:
        raise ParseError(
            "No organisation is bound to this request, so there is nothing to "
            "report on."
        )

    comparison: Optional[tuple] = None
    compare_from = _date(params, "compare_from")
    compare_to = _date(params, "compare_to")
    if compare_from and compare_to:
        comparison = (compare_from, compare_to)

    options = params.get("options") or {}
    if not isinstance(options, dict):
        raise ParseError("'options' must be an object.")

    grouping_code = params.get("grouping_code")
    if grouping_code:
        options = {**options, "grouping_code": grouping_code}

    kwargs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "date_from": _date(params, "date_from"),
        "date_to": _date(params, "date_to"),
        "as_of": _date(params, "as_of"),
        "currency": (params.get("currency") or "").strip().upper(),
        "comparison_period": comparison,
        "department_id": _uuid(params, "department"),
        "project_id": _uuid(params, "project"),
        "include_unposted": _bool(params, "include_unposted"),
        "options": options,
    }
    kwargs.update(overrides)
    try:
        return ReportContext(**kwargs)
    except ReportError as exc:
        raise ParseError(str(exc)) from exc


class ReportRunView(APIView):
    """Base for every ``/reporting/<report>/`` endpoint.

    Subclasses set :attr:`report_type` and :attr:`required_permissions`; the
    machinery — context building, generator resolution, optional snapshotting
    and the ``ReportError -> 400`` translation — lives here once. A per-report
    copy of it is how one report ends up including drafts while another does
    not, which is the drift ``apps.reporting.generators.base`` exists to stop.

    ``GET`` runs the report and returns ``ReportResult.to_dict()``: every
    amount a **string**, because JSON's single numeric type is an IEEE-754
    double and ``numeric(19, 6)`` does not survive it.

    ``POST`` runs it and *files* it — a :class:`ReportSnapshot` with the exact
    figures, the resolved parameters and a checksum. That is the difference
    between looking at a statement and issuing one, so it is a different HTTP
    verb rather than a query flag.
    """

    permission_classes = [IsAuthenticated, HasPermission]
    #: ``ReportType`` value, e.g. ``"profit_loss"``.
    report_type: str = ""
    #: Extra generator constructor kwargs, resolved per request.
    def generator_kwargs(self, request) -> dict[str, Any]:
        return {}

    def _run(self, request):
        context = build_report_context(request)
        try:
            generator = get_generator(self.report_type)
            extra = self.generator_kwargs(request)
            if extra:
                # ``get_generator`` builds with no arguments; rebuild through
                # the class when a generator needs constructor state (the
                # payroll register's scope filter is the only such case).
                generator = type(generator)(**extra)
            return generator.run(context), context
        except ReportError as exc:
            # A wrong report that is *delivered* costs far more than one that
            # fails loudly, so every consistency failure surfaces as a 400
            # naming what went wrong rather than a partial payload.
            raise ParseError(str(exc)) from exc

    def get(self, request, *args, **kwargs):
        result, _context = self._run(request)
        return Response(result.to_dict())

    def post(self, request, *args, **kwargs):
        """Run and file the report as an immutable snapshot."""
        from apps.reporting.services.snapshot import generate_and_snapshot

        context = build_report_context(request)
        extra = self.generator_kwargs(request)
        try:
            generator = get_generator(self.report_type)
            if extra:
                generator = type(generator)(**extra)
            snapshot = generate_and_snapshot(
                self.report_type,
                context,
                user_id=getattr(request.user, "id", None),
                generator=generator,
            )
        except ReportError as exc:
            raise ParseError(str(exc)) from exc

        return Response(
            ReportSnapshotSerializer(snapshot, context={"request": request}).data,
            status=201,
        )


# ---------------------------------------------------------------------------
# Viewsets
# ---------------------------------------------------------------------------

class ReportDefinitionViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Saved report configurations."""

    permission_domain = "reporting"
    #: ``trial_balance`` rather than ``report``: the read codename must be a
    #: real, non-sensitive entry in permissions.json, and ``reporting.report``
    #: only has ``export``. See REPORT_READ.
    resource = "trial_balance"
    queryset = ReportDefinition.objects.all()
    serializer_class = ReportDefinitionSerializer
    select_related = ("grouping", "owner")
    pagination_class = SmallPagePagination
    filterset_fields = ("report_type", "is_shared", "is_active", "owner")
    search_fields = ("code", "name", "description")
    ordering_fields = ("name", "code", "report_type", "created_at")
    ordering = ("name",)
    extra_permissions = {
        "GET": REPORT_READ,
        "HEAD": REPORT_READ,
        "OPTIONS": REPORT_READ,
        "POST": REPORT_WRITE,
        "PUT": REPORT_WRITE,
        "PATCH": REPORT_WRITE,
        "DELETE": REPORT_WRITE,
        "run": REPORT_WRITE,
    }

    @action(detail=True, methods=["post"], url_path="run")
    def run(self, request, pk=None):
        """``POST /report-definitions/{id}/run`` — run this definition and file it.

        The definition's ``default_parameters`` are the base and the request
        body overrides them, so "the standard monthly P&L, but for March" is
        one call. The *resolved* parameters are what lands in the snapshot;
        a snapshot that recorded "previous_month" would not be reproducible,
        which defeats the purpose of taking one.
        """
        from apps.reporting.services.snapshot import generate_and_snapshot

        definition = self.get_object()
        merged = dict(definition.default_parameters or {})
        if isinstance(request.data, dict):
            merged.update(request.data)

        class _Params:
            method = "POST"
            data = merged
            query_params = merged
            tenant_id = getattr(request, "tenant_id", None)

        context = build_report_context(_Params())
        try:
            snapshot = generate_and_snapshot(
                definition.report_type,
                context,
                user_id=getattr(request.user, "id", None),
                definition=definition,
            )
        except ReportError as exc:
            raise ParseError(str(exc)) from exc
        return Response(
            ReportSnapshotSerializer(snapshot, context=self.get_serializer_context()).data,
            status=201,
        )


class ReportSnapshotViewSet(RbacOnlyQuerysetMixin, ReadOnlyTenantViewSet):
    """Filed reports. Read-only — see the module docstring."""

    permission_domain = "reporting"
    resource = "trial_balance"
    queryset = ReportSnapshot.objects.all()
    serializer_class = ReportSnapshotSerializer
    select_related = ("definition", "generated_by")
    filterset_fields = ("report_type", "definition", "currency", "period_start",
                        "period_end", "as_of_date")
    ordering_fields = ("generated_at", "created_at", "report_type")
    ordering = ("-generated_at",)
    extra_permissions = {
        "GET": REPORT_READ,
        "HEAD": REPORT_READ,
        "OPTIONS": REPORT_READ,
        "verify": REPORT_READ,
        "compare": REPORT_READ,
    }

    @action(detail=True, methods=["get"], url_path="verify")
    def verify(self, request, pk=None):
        """Recompute the checksum and say whether the payload is untampered.

        The answer a counterparty needs when they ask "is this the statement
        you gave us?". A mismatch means the row was edited by something other
        than this application — the ORM's immutability guard blocks
        ``delete()``, not a direct UPDATE.
        """
        from apps.reporting.services.snapshot import compute_checksum

        snapshot = self.get_object()
        recomputed = compute_checksum(snapshot.payload)
        return Response(
            {
                "snapshot": str(snapshot.id),
                "report_type": snapshot.report_type,
                "generated_at": snapshot.generated_at.isoformat(),
                "stored_checksum": snapshot.checksum,
                "recomputed_checksum": recomputed,
                "is_intact": recomputed == snapshot.checksum,
            }
        )

    @action(detail=True, methods=["get"], url_path="compare")
    def compare(self, request, pk=None):
        """Diff this snapshot against another (``?against=<snapshot id>``).

        The March P&L rendered in April and the same report rendered in May are
        both correct and they differ, because a prior-period correction was
        posted in between. Being able to answer "by how much, and on which
        lines?" is what turns that from an argument into a reconciliation.
        """
        from apps.reporting.services.snapshot import compare_snapshots

        snapshot = self.get_object()
        other_id = request.query_params.get("against")
        if not other_id:
            raise ParseError(
                "Pass ?against=<snapshot id> — a comparison needs two snapshots."
            )
        other = self.get_queryset().filter(pk=other_id).first()
        if other is None:
            raise ParseError(f"Snapshot {other_id} not found in this organisation.")
        try:
            # ``other`` is the baseline, ``snapshot`` the later figure, so every
            # delta reads as "movement since the version we filed".
            return Response(dict(compare_snapshots(other, snapshot)))
        except ReportError as exc:
            # Refusing to diff two incomparable snapshots is a 400, not a 500:
            # the caller picked the pair.
            raise ParseError(str(exc)) from exc


__all__ = [
    "ReportDefinitionSerializer",
    "ReportSnapshotSerializer",
    "ReportDefinitionViewSet",
    "ReportSnapshotViewSet",
    "ReportRunView",
    "build_report_context",
    "RbacOnlyQuerysetMixin",
]
