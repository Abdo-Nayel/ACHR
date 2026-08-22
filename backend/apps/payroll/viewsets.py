"""
Payroll endpoints.

Why none of these transitions uses ``transition_action``
-------------------------------------------------------
``apps.core.viewsets.transition_action`` calls ``service(object_id, *,
tenant_id=..., user_id=...)``. The payroll engine's functions take the
**instance** — ``calculate_run(run, *, user_id=None)``, ``approve_run(run,
user)``, ``post_run_to_ledger(run, *, user_id=None)``, ``mark_run_paid(run,
*, bank_account_system_key=..., user_id=..., payment_date=...)`` — because
they lock and re-read the row themselves. Writing the handlers by hand and
routing them through :class:`~apps.accounting.viewsets.IdempotentActionMixin`
keeps the header, the cache namespace and the ``Idempotency-Replayed``
behaviour identical to a generated transition without pretending the service
has a signature it does not have.

Segregation of duties is *not* enforced here
--------------------------------------------
``approve_run`` raises ``PermissionDenied`` when the approver is the person
who calculated the run. That exception is allowed to propagate: DRF renders it
as 403, and duplicating the rule in the view would create a second place it can
be wrong — and the same rule then has to hold for a run approved from a
management command, where there is no view at all. The ``Accountant`` role is
deliberately not granted ``payroll.payroll_run.approve`` for the same reason.

Payslip confidentiality
-----------------------
``PayslipViewSet`` declares ``resource = "payslip"``, which is in
``SCOPE_FIELDS``. That makes it fail closed: an actor with no ``ScopeRule`` for
``payslip`` sees nothing, and the ``Employee`` role's ``own_record`` rule
narrows the queryset to their own slips before any handler runs. Two
independent layers (RBAC on ``payroll.payslip.read``, ABAC on the rows) would
both have to fail before one employee saw another's pay.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounting.serializers import JournalEntrySerializer
from apps.accounting.viewsets import IdempotentActionMixin, NotImplementedYet
from apps.core.exceptions import DomainError
from apps.core.pagination import SmallPagePagination
from apps.core.serializers import TransitionSerializer
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet
from apps.payroll.models import (
    EmployeePayrollProfile,
    PayrollComponent,
    PayrollRun,
    Payslip,
    SalaryStructure,
    SalaryStructureAssignment,
    SalaryStructureLine,
    TaxBracket,
)
from apps.payroll.serializers import (
    EmployeePayrollProfileSerializer,
    MarkPaidSerializer,
    PayrollComponentSerializer,
    PayrollRunSerializer,
    PayslipSerializer,
    SalaryStructureAssignmentSerializer,
    SalaryStructureLineSerializer,
    SalaryStructureSerializer,
    TaxBracketSerializer,
)

logger = logging.getLogger(__name__)


class RbacOnlyQuerysetMixin:
    """Opt a configuration resource out of ABAC narrowing.

    ``component``, ``tax_bracket`` and the per-employee payroll profile are
    not in ``SCOPE_FIELDS``. ``build_scope_q`` fails closed for anything it
    cannot narrow, so without this the salary-component catalogue would return
    an empty list — with a 200 — to every user who is not the tenant Owner.
    Tenancy and row-level security still apply; only the actor-scope ``Q`` is
    skipped.
    """

    def get_queryset(self):
        model = self.queryset.model
        queryset = model._default_manager.all()
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.prefetch_related:
            queryset = queryset.prefetch_related(*self.prefetch_related)
        ordering = getattr(self, "ordering", None)
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset


class PostRunSerializer(TransitionSerializer):
    """Body for ``POST /payroll-runs/{id}/post``. Nothing beyond the standard
    transition envelope; declared so the schema names a type."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class PayrollComponentViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Earnings, deductions and employer contributions.

    ``sequence`` is the evaluation order and it is load-bearing: a
    percentage-of-gross component evaluated before the earnings it is a
    percentage *of* silently computes a percentage of zero, and the payslip it
    produces looks perfectly reasonable.
    """

    permission_domain = "payroll"
    resource = "component"
    queryset = PayrollComponent.objects.all()
    serializer_class = PayrollComponentSerializer
    select_related = ("expense_account", "liability_account")
    pagination_class = SmallPagePagination
    filterset_fields = ("component_type", "calculation_type", "is_active",
                        "is_taxable", "currency")
    search_fields = ("code", "name")
    ordering_fields = ("sequence", "code", "name")
    ordering = ("sequence", "code")
    extra_permissions = {
        "POST": ["payroll.component.manage"],
        "PUT": ["payroll.component.manage"],
        "PATCH": ["payroll.component.manage"],
    }


class TaxBracketViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """One band of a progressive income-tax scale, dated.

    Rows rather than settings because a payslip recalculated next year must use
    the scale in force on *its own* pay date. A settings blob has one value,
    which silently rewrites history the first time a rate changes.
    """

    permission_domain = "payroll"
    resource = "tax_bracket"
    queryset = TaxBracket.objects.all()
    serializer_class = TaxBracketSerializer
    pagination_class = SmallPagePagination
    filterset_fields = ("country", "currency", "is_annual_basis", "effective_from")
    ordering_fields = ("country", "effective_from", "sequence")
    ordering = ("country", "-effective_from", "sequence")
    extra_permissions = {
        "POST": ["payroll.tax_bracket.manage"],
        "PUT": ["payroll.tax_bracket.manage"],
        "PATCH": ["payroll.tax_bracket.manage"],
    }


class EmployeePayrollProfileViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Per-employee payroll settings: exemptions, insurable wage, dependants.

    Guarded by the ``payroll.component`` permissions: ``permissions.json`` has
    no ``payroll_profile`` resource, and a codename invented here would be one
    nobody can ever hold, because ``HasPermission`` denies anything absent from
    the catalogue. Configuring an employee's tax exemption is in practice the
    same authority as configuring the components themselves.
    """

    permission_domain = "payroll"
    resource = "component"
    scope_resource = "component"
    queryset = EmployeePayrollProfile.objects.all()
    serializer_class = EmployeePayrollProfileSerializer
    select_related = ("employee",)
    pagination_class = SmallPagePagination
    filterset_fields = ("employee", "pay_frequency", "is_active",
                        "is_exempt_from_tax", "payment_method")
    search_fields = ("employee__employee_code", "employee__first_name",
                     "employee__last_name")
    ordering_fields = ("created_at",)
    ordering = ("-created_at",)
    extra_permissions = {
        "POST": ["payroll.component.manage"],
        "PUT": ["payroll.component.manage"],
        "PATCH": ["payroll.component.manage"],
    }


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

class PayrollRunViewSet(IdempotentActionMixin, TenantModelViewSet):
    """A pay period, and the five moves it makes.

    ``draft -> calculating -> calculated -> pending_approval -> approved ->
    posted -> paid``. Each arrow is a POST sub-resource with its own permission
    codename, which is what makes "the person who calculates payroll may not
    approve it" expressible at all — as a permission the Accountant role simply
    does not hold, rather than as a comment in a runbook.
    """

    permission_domain = "payroll"
    resource = "payroll_run"
    queryset = PayrollRun.objects.all()
    serializer_class = PayrollRunSerializer
    select_related = ("department", "journal_entry", "approved_by", "calculated_by")
    pagination_class = SmallPagePagination
    filterset_fields = ("status", "department", "frequency", "currency", "pay_date")
    search_fields = ("name", "notes")
    ordering_fields = ("pay_date", "period_start", "period_end", "created_at")
    ordering = ("-pay_date", "-created_at")
    extra_permissions = {
        "calculate": ["payroll.payroll_run.calculate"],
        "submit_for_approval": ["payroll.payroll_run.calculate"],
        "approve": ["payroll.payroll_run.approve"],
        "post_to_ledger": ["payroll.payroll_run.post"],
        "mark_paid": ["payroll.payroll_run.pay"],
        "cancel": ["payroll.payroll_run.void"],
        "payslips": ["payroll.payslip.read"],
        "journal_entry": ["payroll.payroll_run.read",
                          "accounting.journal_entry.read"],
        "bank_file": ["payroll.payroll_run.pay"],
    }

    # -- helpers ------------------------------------------------------------

    def _set_status(self, run: PayrollRun, new_status: str, **extra) -> PayrollRun:
        """Validate against ``PayrollRun.ALLOWED_TRANSITIONS`` and write.

        The model owns the transition map (CONVENTIONS §4), so this never
        decides what is legal — it only asks. The engine services do the same
        via ``assert_can_transition``, which is why a run cannot be moved by a
        different route to a state the engine would have refused.
        """
        run.assert_can_transition(new_status)
        run.status = new_status
        for name, value in extra.items():
            setattr(run, name, value)
        run.updated_by_id = self._actor_id()
        run.save(update_fields=["status", *extra.keys(), "updated_by", "updated_at"])
        return run

    # -- transitions --------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="calculate")
    def calculate(self, request, pk=None):
        """``POST /payroll-runs/{id}/calculate`` — compute every payslip.

        Delegates wholesale to ``apps.payroll.services.engine.calculate_run``,
        which is atomic: a half-calculated run is worse than an uncalculated
        one because its control totals look plausible. Recalculation is legal
        until the run is approved and discards the previous payslips first, so
        a headcount change cannot leave a stale slip behind.

        The engine asserts ``net == gross - deductions`` per payslip *and*
        across the run before it returns; a violation rolls the whole thing
        back rather than persisting numbers that do not add up.
        """
        run = self.get_object()
        from apps.payroll.services.engine import calculate_run

        def run_it(_key: Optional[str]) -> PayrollRun:
            return calculate_run(run, user_id=self._actor_id())

        return self.run_idempotent(request, transition="calculate", run=run_it)

    @action(detail=True, methods=["post"], url_path="submit-for-approval")
    def submit_for_approval(self, request, pk=None):
        """``POST /payroll-runs/{id}/submit-for-approval``.

        A pure state move — nothing is computed and nothing is posted — so it
        has no engine function and is applied through the model's own
        transition map. It exists as a separate step because "I have finished
        checking this" and "I authorise this money to leave" are two decisions
        by two people, and collapsing them removes the second pair of eyes.
        """
        run = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        if run.employee_count == 0:
            raise DomainError(
                f"Run {run.name} has no payslips. Calculate it before asking "
                f"anyone to approve it."
            )

        def run_it(_key: Optional[str]) -> PayrollRun:
            return self._set_status(run, PayrollRun.Status.PENDING_APPROVAL)

        return self.run_idempotent(request, transition="submit_for_approval", run=run_it)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """``POST /payroll-runs/{id}/approve`` — sign the run off.

        ``approve_run`` raises ``PermissionDenied`` when the approver is the
        person who calculated the run, and that exception is **left to
        propagate** to DRF's handler as a 403. Catching and re-wording it here
        would put the segregation-of-duties rule in two places; leaving it in
        the service means the same rule holds for a run approved from a Celery
        task or a management command.
        """
        run = self.get_object()
        from apps.payroll.services.engine import approve_run

        def run_it(_key: Optional[str]) -> PayrollRun:
            return approve_run(run, request.user)

        return self.run_idempotent(request, transition="approve", run=run_it)

    @action(detail=True, methods=["post"], url_path="post")
    def post_to_ledger(self, request, pk=None):
        """``POST /payroll-runs/{id}/post`` — accrue the payroll in the ledger.

        ``post_run_to_ledger`` returns the :class:`JournalEntry`, not the run,
        so the response is the entry: that is the thing the caller could not
        otherwise get hold of, and it carries the proof the posting balanced.
        """
        run = self.get_object()
        body = PostRunSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        from apps.payroll.services.engine import post_run_to_ledger

        def run_it(_key: Optional[str]):
            return post_run_to_ledger(run, user_id=self._actor_id())

        return self.run_idempotent(
            request,
            transition="post",
            run=run_it,
            serializer_for=lambda obj: JournalEntrySerializer(
                obj, context=self.get_serializer_context()
            ),
        )

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        """``POST /payroll-runs/{id}/mark-paid`` — record the disbursement.

        A *separate* journal entry from the accrual, on purpose: the liability
        was created when the run was posted and is discharged when the bank
        confirms the transfer. Different date, different approver, different
        reconciliation — and between them the balance sheet correctly shows
        money owed to employees.
        """
        run = self.get_object()
        body = MarkPaidSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        from apps.payroll.services.engine import mark_run_paid

        call_kwargs: dict[str, Any] = {"user_id": self._actor_id()}
        if body.validated_data.get("payment_date"):
            call_kwargs["payment_date"] = body.validated_data["payment_date"]
        key = (body.validated_data.get("bank_account_system_key") or "").strip()
        if key:
            call_kwargs["bank_account_system_key"] = key

        def run_it(_key: Optional[str]):
            return mark_run_paid(run, **call_kwargs)

        return self.run_idempotent(
            request,
            transition="mark_paid",
            run=run_it,
            serializer_for=lambda obj: JournalEntrySerializer(
                obj, context=self.get_serializer_context()
            ),
        )

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """``POST /payroll-runs/{id}/cancel`` — abandon a run before approval.

        Only reachable from the pre-approval states, because
        ``ALLOWED_TRANSITIONS`` says so. Once a run is approved the money has
        been authorised and the correction is a reversing entry, not a
        cancellation that quietly makes the authorisation disappear.
        """
        run = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        def run_it(_key: Optional[str]) -> PayrollRun:
            return self._set_status(run, PayrollRun.Status.CANCELLED)

        return self.run_idempotent(request, transition="cancel", run=run_it)

    # -- reads --------------------------------------------------------------

    @action(detail=True, methods=["get"], url_path="payslips")
    def payslips(self, request, pk=None):
        """``GET /payroll-runs/{id}/payslips`` — this run's slips.

        Narrowed by the *payslip* scope as well as the run's, so a department
        manager listing a company-wide run still sees only the slips their
        ``ScopeRule`` allows. Serving the run's slips unfiltered because the
        caller could see the run would be a confidentiality hole one nesting
        level deep.
        """
        run = self.get_object()

        from apps.iam.permissions import build_scope_q

        scope = build_scope_q(request.user, "payslip", request=request)
        queryset = (
            Payslip.objects.filter(run=run)
            .filter(scope)
            .select_related("employee", "employee__department", "run")
            .prefetch_related("lines", "lines__component")
            .order_by("employee__employee_code")
        )
        page = self.paginate_queryset(queryset)
        context = self.get_serializer_context()
        if page is not None:
            return self.get_paginated_response(
                PayslipSerializer(page, many=True, context=context).data
            )
        return Response(PayslipSerializer(queryset, many=True, context=context).data)

    @action(detail=True, methods=["get"], url_path="journal-entry")
    def journal_entry(self, request, pk=None):
        """The accrual entry this run posted, or 404 with the reason why."""
        from rest_framework.exceptions import NotFound

        run = self.get_object()
        if run.journal_entry_id is None:
            raise NotFound(
                f"Run {run.name} has not been posted, so it has no journal "
                f"entry. Nothing reaches the ledger until POST "
                f"/payroll-runs/{{id}}/post succeeds."
            )
        return Response(
            JournalEntrySerializer(
                run.journal_entry, context=self.get_serializer_context()
            ).data
        )

    @action(detail=True, methods=["post"], url_path="bank-file")
    def bank_file(self, request, pk=None):
        """``POST /payroll-runs/{id}/bank-file`` — **not implemented**.

        Mounted so the route and its permission exist in the schema. A real
        implementation renders the bank's own transfer format from the
        approved payslips and stores the file plus its checksum, because the
        file the bank executed is the evidence of what was paid — regenerating
        it later from live data would produce a different file if anything
        changed in between.
        """
        self.get_object()
        raise NotImplementedYet(
            "Payroll bank-file generation is not implemented yet. It must "
            "render the bank's transfer format from the approved payslips and "
            "store the bytes with a checksum, so the file that was executed "
            "stays recoverable."
        )


# ---------------------------------------------------------------------------
# Payslips
# ---------------------------------------------------------------------------

class PayslipViewSet(ReadOnlyTenantViewSet):
    """An employee's pay for one period. Read-only, and ABAC-scoped.

    Read-only because a payslip is an ``ImmutableFinancialModel``: it is the
    statement given to the employee and, in most jurisdictions, filed with the
    tax authority. Correcting one means recalculating the run (legal until
    approval) or running an off-cycle adjustment — never editing the row,
    because the number the employee was shown must stay recoverable.

    ``resource = "payslip"`` is in ``SCOPE_FIELDS``, so a caller with no
    ``ScopeRule`` for it gets nothing rather than everything. The ``Employee``
    role carries ``own_record``, which resolves to
    ``Q(employee_id=<their employee>)``.
    """

    permission_domain = "payroll"
    resource = "payslip"
    queryset = Payslip.objects.all()
    serializer_class = PayslipSerializer
    select_related = ("employee", "employee__department", "run")
    prefetch_related = ("lines", "lines__component")
    pagination_class = SmallPagePagination
    filterset_fields = ("run", "employee", "payment_status", "currency")
    search_fields = ("employee__employee_code", "employee__first_name",
                     "employee__last_name", "run__name")
    ordering_fields = ("created_at", "net_amount", "gross_amount")
    ordering = ("-created_at",)
    extra_permissions = {
        "pdf": ["payroll.payslip.read"],
        "publish": ["payroll.payslip.publish"],
    }

    @action(detail=True, methods=["get"], url_path="pdf")
    def pdf(self, request, pk=None):
        """``GET /payslips/{id}/pdf`` — **not implemented**.

        Mounted so the contract exists. It must render from the stored
        ``employee_snapshot`` and the payslip's own lines, not from live
        employee data: an employee who has since changed department, name or
        salary must still be able to download the document they were given.
        """
        self.get_object()
        raise NotImplementedYet(
            "Payslip PDF rendering is not implemented yet. It must render from "
            "the stored employee snapshot and the payslip lines so a historical "
            "payslip cannot change after the fact."
        )


__all__ = [
    "PayrollComponentViewSet",
    "TaxBracketViewSet",
    "EmployeePayrollProfileViewSet",
    "PayrollRunViewSet",
    "PayslipViewSet",
    "RbacOnlyQuerysetMixin",
]


class SalaryStructureViewSet(TenantModelViewSet):
    """Reusable salary packages.

    Under ``payroll.component`` rather than a resource of its own: a structure
    *is* a bundle of components, and anyone entitled to manage components is
    entitled to bundle them. Splitting the permission would leave a role that
    can change what a component pays but not which package it belongs to,
    which is a distinction without a difference.
    """

    permission_domain = "payroll"
    resource = "component"
    queryset = SalaryStructure.objects.all()
    serializer_class = SalaryStructureSerializer
    prefetch_related = ("lines", "lines__component", "assignments")
    pagination_class = SmallPagePagination
    filterset_fields = ("is_active", "currency")
    search_fields = ("code", "name")
    ordering_fields = ("code", "name", "created_at")
    ordering = ("code",)
    extra_permissions = {
        "POST": ["payroll.component.manage"],
        "PUT": ["payroll.component.manage"],
        "PATCH": ["payroll.component.manage"],
    }


class SalaryStructureLineViewSet(TenantModelViewSet):
    """Lines inside a structure. Written here, not nested in the parent.

    Editing a line changes the pay of everyone assigned to the structure, so
    it is a deliberate call against a deliberate URL rather than a side effect
    of PATCHing the package.
    """

    permission_domain = "payroll"
    resource = "component"
    queryset = SalaryStructureLine.objects.all()
    serializer_class = SalaryStructureLineSerializer
    select_related = ("structure", "component")
    pagination_class = SmallPagePagination
    filterset_fields = ("structure", "component")
    ordering = ("structure", "sequence")
    extra_permissions = {
        "POST": ["payroll.component.manage"],
        "PUT": ["payroll.component.manage"],
        "PATCH": ["payroll.component.manage"],
    }


class SalaryStructureAssignmentViewSet(TenantModelViewSet):
    """Who is on which package, from when, at what base.

    Additive by design: a promotion is a new assignment, not an edit to the
    old one. Payroll for a past period resolves the assignment that was in
    force then, so overwriting history makes prior payslips unexplainable.
    """

    permission_domain = "payroll"
    resource = "component"
    queryset = SalaryStructureAssignment.objects.all()
    serializer_class = SalaryStructureAssignmentSerializer
    select_related = ("employee", "structure")
    pagination_class = SmallPagePagination
    filterset_fields = ("employee", "structure", "from_date")
    search_fields = ("employee__employee_code",)
    ordering_fields = ("from_date", "created_at")
    ordering = ("-from_date",)
    extra_permissions = {
        "POST": ["payroll.component.manage"],
        "PUT": ["payroll.component.manage"],
        "PATCH": ["payroll.component.manage"],
    }
