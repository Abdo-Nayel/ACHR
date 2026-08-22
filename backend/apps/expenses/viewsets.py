"""
Purchasing endpoints: vendors, staff expense claims and supplier bills.

Why an expense claim has four separate POST sub-resources
--------------------------------------------------------
``submit``, ``approve``, ``reject`` and ``reimburse`` are four different
authorities held by different people. ``purchasing.expense.submit`` is granted
to every ``Employee``; ``purchasing.expense.approve`` is not. Collapsing them
into a writable ``status`` field would mean the single permission "may update
an expense" — which every claimant needs, to fix their own draft — also
approves it. That is not a hypothetical: self-approval of expenses is the most
common small-scale occupational fraud there is, and the control that prevents
it has to be expressible as a permission, not as a UI that hides a button.

How these transitions reach the ledger
--------------------------------------
``approve`` posts the accrual and ``reimburse`` posts the settlement, both via
``apps.expenses.services.posting`` — which builds a draft and hands it to
``apps.accounting.services.posting.post_entry``, per CONVENTIONS §7. No GL
logic lives in this file: these handlers call a service, so a Celery task or a
management command can drive the same posting without going through HTTP.

Each transition wraps its status change and its posting in one
``transaction.atomic`` block. The pairing is the point — an APPROVED expense
with no entry understates cost until somebody reconciles, and an entry with no
approval records a cost nobody accepted.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.viewsets import IdempotentActionMixin
from apps.core.exceptions import DomainError
from apps.core.pagination import SmallPagePagination
from apps.expenses.services.bill_posting import pay_bill, post_bill
from apps.expenses.services.posting import post_expense, post_reimbursement
from apps.iam.permissions import assert_not_self_prepared
from apps.core.serializers import (
    ReasonRequiredTransitionSerializer,
    TransitionSerializer,
)
from apps.core.viewsets import (
    RbacOnlyQuerysetMixin,
    ReadOnlyTenantViewSet,
    TenantModelViewSet,
)
from apps.expenses.models import (
    Bill,
    BillPayment,
    Expense,
    ExpenseCategory,
    ExpenseReceipt,
    RecurringBillProfile,
    RecurringExpenseProfile,
    Vendor,
    VendorCredit,
)
from apps.expenses.serializers import (
    BillPaymentInputSerializer,
    BillPaymentSerializer,
    BillSerializer,
    ExpenseCategorySerializer,
    ExpenseReceiptSerializer,
    ExpenseRejectSerializer,
    ExpenseSerializer,
    RecurringBillProfileSerializer,
    RecurringExpenseProfileSerializer,
    VendorCreditSerializer,
    VendorSerializer,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status views
# ---------------------------------------------------------------------------

class StatusViewFilterMixin:
    """``?view=`` — the named status filters every purchase list carries.

    A thin vocabulary over raw statuses, because the names a finance team uses
    do not map one-to-one onto the state machine. "Open" is not a status: it
    is *approved but not settled*, which spans ``approved``,
    ``partially_paid`` and ``overdue``. "Unpaid" is the same set from the
    other direction. Making the client assemble those from a status list means
    every screen re-derives the definition and they drift.

    ``my_approvals`` is the one that must be computed server-side. It is
    "documents I am entitled to approve and have not", which depends on the
    caller's permissions and ABAC scope — neither of which the browser can be
    trusted to evaluate. A client-side version would be a filtered view of
    rows the user was already sent, so it would either show documents they
    cannot act on or require sending them documents they should not see.
    """

    #: view name -> statuses. `None` means "handled by a method below".
    STATUS_VIEWS: dict[str, tuple[str, ...] | None] = {}

    def _view_param(self) -> str:
        return (self.request.query_params.get("view") or "").strip().lower()

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        view = self._view_param()
        if not view or view == "all":
            return queryset

        if view == "my_approvals":
            return self._my_approvals(queryset)

        statuses = self.STATUS_VIEWS.get(view)
        if statuses is None:
            # An unknown view is a client bug, not a reason to silently return
            # everything — that would look like the filter worked.
            raise DomainError(
                f"Unknown view {view!r}. Available: "
                f"{', '.join(['all', *sorted(self.STATUS_VIEWS), 'my_approvals'])}."
            )
        return queryset.filter(status__in=statuses)

    def _my_approvals(self, queryset):
        """Documents awaiting *this* user's decision.

        Two conditions, and both matter:

        * the document is in a state that wants a decision, and
        * the caller holds the approve permission for it.

        Without the second, the list shows work to people who cannot do it.
        The ABAC scope is already applied by `get_queryset`, so a department
        manager's list is narrowed to their subtree before this runs.

        Segregation of duties is *not* applied here. A document the caller
        prepared still appears, and the approve call refuses it — showing it
        and explaining why beats hiding it and leaving the preparer wondering
        where their submission went.
        """
        from apps.iam.permissions import user_permission_set

        codename = getattr(self, "approval_permission", None)
        pending = getattr(self, "pending_approval_statuses", ())
        if not codename or not pending:
            return queryset.none()
        # Takes the *request* — it resolves both the tenant and the actor from
        # it, and reads the same cached effective-permission set the permission
        # classes use, so this filter cannot disagree with what the approve
        # endpoint will actually allow.
        held = user_permission_set(self.request)
        if codename not in held:
            return queryset.none()
        return queryset.filter(status__in=pending)


#: The vocabulary shared by bills and vendor credits. Expenses use a subset —
#: they have no partial payment.
BILL_STATUS_VIEWS: dict[str, tuple[str, ...] | None] = {
    "draft": ("draft",),
    "pending_approval": ("awaiting_approval",),
    "open": ("approved", "partially_paid", "overdue"),
    "overdue": ("overdue",),
    "unpaid": ("approved", "partially_paid", "overdue"),
    "partially_paid": ("partially_paid",),
    "paid": ("paid",),
    "voided": ("voided",),
}


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

class ExpenseCategoryViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Spend classifications and the accounts they post to."""

    permission_domain = "purchasing"
    resource = "category"
    queryset = ExpenseCategory.objects.all()
    serializer_class = ExpenseCategorySerializer
    select_related = ("parent", "expense_account", "default_tax_rate")
    pagination_class = SmallPagePagination
    filterset_fields = ("is_active", "parent", "is_tax_deductible", "requires_receipt")
    search_fields = ("code", "name")
    ordering_fields = ("code", "name")
    ordering = ("code",)
    extra_permissions = {
        "POST": ["purchasing.category.manage"],
        "PUT": ["purchasing.category.manage"],
        "PATCH": ["purchasing.category.manage"],
    }


class VendorViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Supplier master data."""

    permission_domain = "purchasing"
    resource = "vendor"
    queryset = Vendor.objects.all()
    serializer_class = VendorSerializer
    select_related = ("payable_account", "default_expense_account")
    pagination_class = SmallPagePagination
    filterset_fields = ("is_active", "currency", "is_withholding_applicable")
    search_fields = ("code", "name", "display_name", "email", "tax_number")
    ordering_fields = ("name", "code", "created_at")
    ordering = ("name",)
    extra_permissions = {"DELETE": ["purchasing.vendor.archive"]}


class ExpenseReceiptViewSet(RbacOnlyQuerysetMixin, ReadOnlyTenantViewSet):
    """Receipt evidence attached to a claim.

    Read-only: the bytes arrive through the storage endpoint and the OCR
    fields are written by the extraction job. A writable ``ocr_extracted``
    would let a claimant make the receipt appear to say whatever the claim
    says — the one thing a receipt exists to contradict.
    """

    permission_domain = "purchasing"
    resource = "expense"
    scope_resource = "expense"
    queryset = ExpenseReceipt.objects.all()
    serializer_class = ExpenseReceiptSerializer
    select_related = ("expense", "uploaded_by")
    pagination_class = SmallPagePagination
    filterset_fields = ("expense",)
    ordering_fields = ("created_at",)
    ordering = ("-created_at",)


# ---------------------------------------------------------------------------
# Expense claims
# ---------------------------------------------------------------------------

class ExpenseViewSet(StatusViewFilterMixin, IdempotentActionMixin, TenantModelViewSet):
    """Staff and card spend, from draft claim to reimbursement.

    ABAC-scoped on ``expense``: the ``Employee`` role carries ``own_record``,
    so a claimant's list is their own claims and ``get_object()`` — which every
    transition calls first — cannot reach anybody else's.
    """

    permission_domain = "purchasing"
    resource = "expense"
    queryset = Expense.objects.all()
    serializer_class = ExpenseSerializer
    select_related = ("vendor", "category", "employee", "project", "customer",
                      "journal_entry", "paid_from_account")
    prefetch_related = ("receipts",)
    filterset_fields = ("status", "category", "vendor", "employee", "project",
                        "customer", "currency", "expense_date", "is_billable",
                        "is_reimbursable", "payment_method")
    search_fields = ("number", "description", "notes", "vendor__name")
    ordering_fields = ("expense_date", "total_amount", "created_at")
    ordering = ("-expense_date", "-created_at")
    #: An expense has no partial payment, so "open"/"unpaid" both mean
    #: "approved and not yet reimbursed".
    STATUS_VIEWS = {
        "draft": ("draft",),
        "pending_approval": ("submitted",),
        "open": ("approved",),
        "unpaid": ("approved",),
        "paid": ("reimbursed",),
        "rejected": ("rejected",),
    }
    approval_permission = "purchasing.expense.approve"
    pending_approval_statuses = ("submitted",)
    extra_permissions = {
        "submit": ["purchasing.expense.submit"],
        "approve": ["purchasing.expense.approve"],
        "reject": ["purchasing.expense.approve"],
        "reimburse": ["purchasing.expense.reimburse"],
        "receipts": ["purchasing.expense.read"],
    }

    # -- create -------------------------------------------------------------

    def perform_create(self, serializer) -> None:
        """Default the claimant to the caller's own employee record.

        Not cosmetic. The ``expense`` scope resolves ``own_record`` to
        ``Q(employee_id=<the actor's employee>)`` — ``created_by`` is only the
        fallback when the resource has no employee column, and ``expense``
        has one. So a claim saved with ``employee = NULL`` is a row its own
        author immediately cannot see: the POST returns 201, the list comes
        back empty, and ``POST {id}/submit`` answers 404. The claim is
        stranded, and nothing anywhere reports an error.

        Only defaulted, never forced: a bookkeeper entering somebody else's
        receipt names the claimant explicitly, and that is a different and
        legitimate case.
        """
        from apps.iam.permissions import resolve_actor_scope

        if serializer.validated_data.get("employee") is None:
            employee_id = resolve_actor_scope(self.request).employee_id
            if employee_id is not None:
                serializer.save(employee_id=employee_id)
                return
        serializer.save()

    # -- helpers ------------------------------------------------------------

    def _move(self, expense: Expense, new_status: str, **extra) -> Expense:
        """Ask the model whether the move is legal, then write only what changed.

        ``Expense.ALLOWED_TRANSITIONS`` is the single definition of the
        lifecycle (CONVENTIONS §4). This never decides; it asks, and lets the
        ``ValueError`` the model raises be translated into the API error
        vocabulary by :func:`apps.core.viewsets.raise_as_api_error`.
        """
        try:
            expense.assert_can_transition(new_status)
        except ValueError as exc:
            raise DomainError(str(exc)) from exc
        expense.status = new_status
        for name, value in extra.items():
            setattr(expense, name, value)
        expense.updated_by_id = self._actor_id()
        expense.save(
            update_fields=["status", *extra.keys(), "updated_by", "updated_at"]
        )
        return expense

    # -- transitions --------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        """``POST /expenses/{id}/submit`` — hand the claim to an approver.

        Refuses a claim whose category demands a receipt and has none. The
        check belongs here rather than in the approver's inbox because the
        person who can fix it is the claimant, and telling them at submission
        costs one round trip instead of three.
        """
        expense = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        category = expense.category
        if getattr(category, "requires_receipt", False) and not expense.receipts.exists():
            raise DomainError(
                f"Category '{category.name}' requires a receipt and this claim "
                f"has none. Attach the evidence before submitting — an approver "
                f"cannot verify spend they cannot see."
            )

        def run(_key: Optional[str]) -> Expense:
            return self._move(
                expense, Expense.Status.SUBMITTED, submitted_at=timezone.now()
            )

        return self.run_idempotent(request, transition="submit", run=run)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """``POST /expenses/{id}/approve`` — the company accepts the cost.

        Self-approval is refused explicitly. RBAC already stops most claimants
        (``Employee`` does not hold ``purchasing.expense.approve``), but a
        department manager who *does* hold it would otherwise be able to
        approve their own claims, and "the approver is not the claimant" is the
        control an auditor actually tests.
        """
        expense = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        actor_id = self._actor_id()
        if expense.created_by_id and expense.created_by_id == actor_id:
            raise DomainError(
                "Segregation of duties: the person who raised this claim may "
                "not approve it. A second authorised approver is required."
            )

        def run(_key: Optional[str]) -> Expense:
            # Status and ledger move together, in one transaction. An expense
            # that is APPROVED with no entry understates cost for as long as
            # nobody notices; one with an entry and no approval records a cost
            # nobody accepted. Approval *is* the recognition event, so this is
            # where the accrual belongs.
            with transaction.atomic():
                moved = self._move(
                    expense,
                    Expense.Status.APPROVED,
                    approved_at=timezone.now(),
                    approved_by_id=actor_id,
                    rejected_reason="",
                )
                post_expense(moved, user_id=actor_id)
                return moved

        return self.run_idempotent(request, transition="approve", run=run)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        """``POST /expenses/{id}/reject`` — send it back, with a reason.

        The reason is mandatory, and ``ck_expense_rejected_has_reason`` agrees
        at the database level. A rejection without one gives the claimant no
        way to produce a version that would pass, which turns one rejection
        into a thread.
        """
        expense = self.get_object()
        body = ExpenseRejectSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reason = body.validated_reason()

        def run(_key: Optional[str]) -> Expense:
            return self._move(
                expense, Expense.Status.REJECTED, rejected_reason=reason[:255]
            )

        return self.run_idempotent(request, transition="reject", run=run)

    @action(detail=True, methods=["post"], url_path="reimburse")
    def reimburse(self, request, pk=None):
        """``POST /expenses/{id}/reimburse`` — record paying the claimant back.

        A distinct state from ``APPROVED`` because it answers a different
        question: approval says "the company accepts this cost", reimbursement
        says "we have paid the employee". For company-card spend the two
        collapse, which is why ``is_reimbursable`` gates this step instead of
        every expense walking the full chain.
        """
        expense = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        if not expense.is_reimbursable:
            raise DomainError(
                f"Expense {expense.number or expense.id} is not reimbursable — "
                f"the money never left the claimant's pocket. Marking it "
                f"reimbursed would record a payment that did not happen."
            )

        def run(_key: Optional[str]) -> Expense:
            with transaction.atomic():
                moved = self._move(
                    expense,
                    Expense.Status.REIMBURSED,
                    reimbursed_at=timezone.now(),
                )
                # Clears the payable raised at approval. Same transaction as
                # the status change for the same reason: "reimbursed" and "the
                # liability is gone" are one fact.
                post_reimbursement(moved, user_id=self._actor_id())
                return moved

        return self.run_idempotent(request, transition="reimburse", run=run)

    # -- reads --------------------------------------------------------------

    @action(detail=True, methods=["get"], url_path="receipts")
    def receipts(self, request, pk=None):
        """The evidence attached to this claim."""
        expense = self.get_object()
        rows = expense.receipts.all().order_by("created_at")
        return Response(
            ExpenseReceiptSerializer(
                rows, many=True, context=self.get_serializer_context()
            ).data
        )


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------

class BillViewSet(StatusViewFilterMixin, IdempotentActionMixin, TenantModelViewSet):
    """Supplier invoices: the AP mirror of a sales invoice.

    Approval matters more here than on the sales side, because a bill creates a
    liability from a document produced by somebody outside the company. The
    duplicate-entry guard is the database's:
    ``uq_bill_vendor_reference`` refuses the same supplier document twice,
    which is the most common way a company pays a bill twice.
    """

    permission_domain = "purchasing"
    resource = "bill"
    queryset = Bill.objects.all()
    serializer_class = BillSerializer
    select_related = ("vendor", "journal_entry", "project")
    prefetch_related = ("lines",)
    filterset_fields = ("status", "vendor", "currency", "project", "bill_date",
                        "due_date")
    search_fields = ("number", "vendor_reference", "vendor__name", "notes")
    ordering_fields = ("bill_date", "due_date", "total_amount", "amount_due")
    ordering = ("-bill_date", "-created_at")
    STATUS_VIEWS = BILL_STATUS_VIEWS
    approval_permission = "purchasing.bill.approve"
    pending_approval_statuses = ("awaiting_approval",)
    extra_permissions = {
        "submit_for_approval": ["purchasing.bill.update"],
        "approve": ["purchasing.bill.approve"],
        "pay": ["purchasing.bill.pay"],
        "void": ["purchasing.bill.void"],
        "post_to_ledger": ["purchasing.bill.post"],
    }

    def _move(self, bill: Bill, new_status: str, **extra) -> Bill:
        try:
            bill.assert_can_transition(new_status)
        except ValueError as exc:
            raise DomainError(str(exc)) from exc
        bill.status = new_status
        for name, value in extra.items():
            setattr(bill, name, value)
        bill.updated_by_id = self._actor_id()
        bill.save(update_fields=["status", *extra.keys(), "updated_by", "updated_at"])
        return bill

    @action(detail=True, methods=["post"], url_path="submit-for-approval")
    def submit_for_approval(self, request, pk=None):
        """``POST /bills/{id}/submit-for-approval`` — queue it for sign-off."""
        bill = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        def run(_key: Optional[str]) -> Bill:
            return self._move(bill, Bill.Status.AWAITING_APPROVAL)

        return self.run_idempotent(request, transition="submit_for_approval", run=run)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """``POST /bills/{id}/approve`` — accept the liability."""
        bill = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        # Segregation of duties. Until this call existed the control was
        # applied as a queryset filter in `_apply_parameters`, which hid the
        # bill from its own author rather than refusing the approval — so
        # removing that filter would have left `bill` with no SoD control at
        # all. Approving a supplier invoice you entered yourself is the
        # textbook fake-vendor path, so it gets the explicit guard the
        # permission matrix already documents for it.
        assert_not_self_prepared(bill, "bill", request)

        def run(_key: Optional[str]) -> Bill:
            # Approval is the recognition event for a purchase: it is the
            # moment the company accepts both the cost and the debt. Status
            # and ledger move in one transaction, because a bill that is
            # APPROVED with no entry understates creditors for as long as
            # nobody reconciles.
            with transaction.atomic():
                moved = self._move(
                    bill,
                    Bill.Status.APPROVED,
                    approved_at=timezone.now(),
                    approved_by_id=self._actor_id(),
                )
                post_bill(moved, user_id=self._actor_id())
                return moved

        return self.run_idempotent(request, transition="approve", run=run)

    @action(detail=True, methods=["post"], url_path="void")
    def void(self, request, pk=None):
        """``POST /bills/{id}/void`` — cancel it, with a reason on the record.

        ``ck_bill_void_has_reason`` requires the reason at the database level:
        a voided document nobody can explain is indistinguishable from a
        deleted one when an auditor asks why the payable disappeared.
        """
        bill = self.get_object()
        body = ReasonRequiredTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reason = body.validated_reason()

        if (bill.amount_paid or 0) > 0:
            raise DomainError(
                f"Bill {bill.number or bill.id} has been paid in part; voiding "
                f"it would strand the payment against nothing. Reverse the "
                f"payment first."
            )

        def run(_key: Optional[str]) -> Bill:
            return self._move(bill, Bill.Status.VOIDED, void_reason=reason[:255])

        return self.run_idempotent(request, transition="void", run=run)

    @action(detail=True, methods=["post"], url_path="post")
    def post_to_ledger(self, request, pk=None):
        """``POST /bills/{id}/post`` — record the obligation in the ledger.

        Normally redundant: ``approve`` posts as part of accepting the bill.
        This exists for the bill that was approved before the posting service
        did, and for an approval whose posting was rolled back by a closed
        period — it is idempotent (keyed on the bill), so calling it on an
        already-posted bill returns the existing entry rather than a second.
        """
        bill = self.get_object()

        def run(_key: Optional[str]) -> Bill:
            post_bill(bill, user_id=self._actor_id())
            bill.refresh_from_db()
            return bill

        return self.run_idempotent(request, transition="post", run=run)

    @action(detail=True, methods=["post"], url_path="pay")
    def pay(self, request, pk=None):
        """``POST /bills/{id}/pay`` — settle all or part of a posted bill.

        Body: ``amount``, ``paid_from_account``, optional ``payment_date`` and
        ``reference``. The service re-reads the bill under a row lock and
        recomputes ``amount_paid`` and the status itself, so two concurrent
        payments cannot both see a zero balance and overpay the vendor.
        """
        bill = self.get_object()
        body = BillPaymentInputSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        def run(_key: Optional[str]) -> Bill:
            pay_bill(
                bill,
                amount=data["amount"],
                paid_from_account_id=data["paid_from_account"],
                payment_date=data.get("payment_date"),
                reference=data.get("reference", ""),
                payment_method=data.get("payment_method", ""),
                user_id=self._actor_id(),
            )
            bill.refresh_from_db()
            return bill

        return self.run_idempotent(request, transition="pay", run=run)


class BillPaymentViewSet(RbacOnlyQuerysetMixin, ReadOnlyTenantViewSet):
    """Disbursements against bills. Read-only until the AP payment service exists.

    Recording one has to lock the parent bill, recompute ``amount_paid`` from
    the payment rows and post the cash entry in one transaction. A writable
    endpoint without that service would let a caller mark a bill paid with no
    money and no ledger entry.
    """

    permission_domain = "purchasing"
    resource = "bill"
    scope_resource = "bill"
    queryset = BillPayment.objects.all()
    serializer_class = BillPaymentSerializer
    select_related = ("bill", "vendor", "paid_from_account", "journal_entry")
    filterset_fields = ("status", "bill", "vendor", "currency", "payment_date",
                        "payment_method")
    search_fields = ("number", "reference", "payment_batch_reference")
    ordering_fields = ("payment_date", "amount", "created_at")
    ordering = ("-payment_date", "-created_at")


__all__ = [
    "ExpenseCategoryViewSet",
    "VendorViewSet",
    "ExpenseReceiptViewSet",
    "ExpenseViewSet",
    "BillViewSet",
    "BillPaymentViewSet",
    "RbacOnlyQuerysetMixin",
]


# ---------------------------------------------------------------------------
# Vendor credits
# ---------------------------------------------------------------------------

class VendorCreditViewSet(IdempotentActionMixin, TenantModelViewSet):
    """Supplier credit notes: money the vendor owes back.

    A bill is never edited after it posts, so an overcharge or a return is
    corrected by this second document rather than by rewriting a figure the
    vendor has already invoiced.
    """

    permission_domain = "purchasing"
    resource = "bill"
    scope_resource = "bill"
    queryset = VendorCredit.objects.all()
    serializer_class = VendorCreditSerializer
    select_related = ("vendor", "bill", "journal_entry")
    prefetch_related = ("lines",)
    filterset_fields = ("status", "vendor", "bill", "currency", "credit_date")
    search_fields = ("number", "reason", "vendor__name", "notes")
    ordering_fields = ("credit_date", "total_amount", "created_at")
    ordering = ("-credit_date", "-created_at")


class RecurringBillProfileViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Standing vendor bills: rent, support contracts, leases."""

    permission_domain = "purchasing"
    resource = "bill"
    queryset = RecurringBillProfile.objects.all()
    serializer_class = RecurringBillProfileSerializer
    select_related = ("vendor",)
    prefetch_related = ("lines",)
    pagination_class = SmallPagePagination
    filterset_fields = ("is_active", "vendor", "frequency", "currency")
    search_fields = ("name", "vendor__name")
    ordering_fields = ("name", "next_run_date", "created_at")
    ordering = ("name",)


class RecurringExpenseProfileViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Standing expenses: subscriptions on the company card."""

    permission_domain = "purchasing"
    resource = "expense"
    queryset = RecurringExpenseProfile.objects.all()
    serializer_class = RecurringExpenseProfileSerializer
    select_related = ("vendor", "category", "paid_from_account")
    pagination_class = SmallPagePagination
    filterset_fields = ("is_active", "vendor", "category", "frequency", "currency")
    search_fields = ("name", "description", "vendor__name")
    ordering_fields = ("name", "next_run_date", "amount", "created_at")
    ordering = ("name",)
