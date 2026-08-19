"""
Serializers for purchasing: vendors, staff expenses and supplier bills.

The invariant this module carries
---------------------------------
``total_amount == amount + tax_amount`` is a database check constraint
(``ck_expense_total_identity``), and the same identity for a bill is
``amount_due == total_amount - amount_paid`` (``ck_bill_due_identity``). None
of the derived halves is accepted from the request body: the server computes
them from the parts, so a client cannot post a claim whose gross and net
disagree. Letting a caller name all three means the one an approver reads and
the one that posts to the ledger can be different numbers, and the difference
only surfaces as an unexplained variance in a cost centre months later.

``status`` is read-only on every serializer here. Submitting, approving,
rejecting and reimbursing are POST sub-resources, each with its own permission
codename — which is what makes "an employee may submit but not approve their
own claim" expressible.
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from apps.core.fields import ZERO, quantize_currency
from apps.core.serializers import (
    MoneyField,
    QuantityField,
    RateField,
    ReadOnlyModelSerializer,
    ReasonRequiredTransitionSerializer,
    TenantScopedSerializer,
    TransitionSerializer,
)
from apps.expenses.models import (
    Bill,
    BillLine,
    BillPayment,
    Expense,
    ExpenseCategory,
    ExpenseReceipt,
    RecurringBillLineTemplate,
    RecurringBillProfile,
    RecurringExpenseProfile,
    Vendor,
    VendorCredit,
    VendorCreditLine,
)

ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

class ExpenseCategorySerializer(TenantScopedSerializer):
    """A spend classification and the account it posts to.

    ``deductible_rate`` is a rate rather than a boolean because partial
    deductibility is the normal case in most tax regimes (entertainment at
    50%, fuel at 80%). A boolean forces the difference to be handled by hand
    at return time, which is where it gets forgotten.
    """

    approval_threshold_amount = MoneyField(min_value=Decimal("0"), required=False)
    deductible_rate = RateField(min_value=Decimal("0"), max_value=ONE, required=False)

    class Meta:
        model = ExpenseCategory
        fields = (
            "id", "code", "name", "parent", "expense_account",
            "default_tax_rate", "is_tax_deductible", "deductible_rate",
            "approval_threshold_amount", "requires_receipt", "is_active",
            "created_at", "updated_at",
        )


class VendorSerializer(TenantScopedSerializer):
    """A party the tenant buys from and may owe money to."""

    withholding_rate = RateField(min_value=Decimal("0"), max_value=ONE, required=False)

    class Meta:
        model = Vendor
        fields = (
            "id", "code", "name", "display_name", "email", "phone",
            "tax_number", "address", "currency", "payment_terms_days",
            "payable_account", "default_expense_account",
            "is_withholding_applicable", "withholding_rate", "bank_details",
            "is_active", "notes", "created_at", "updated_at",
        )

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        applies = attrs.get(
            "is_withholding_applicable",
            getattr(instance, "is_withholding_applicable", False),
        )
        rate = attrs.get("withholding_rate", getattr(instance, "withholding_rate", ZERO))
        if applies and (rate or ZERO) <= ZERO:
            raise serializers.ValidationError(
                {"withholding_rate": (
                    "A vendor flagged for withholding needs a positive rate. "
                    "Zero reads as 'no withholding' to a human and as "
                    "'withholding applies' to code that checks the flag."
                )}
            )
        return attrs


class ExpenseReceiptSerializer(ReadOnlyModelSerializer):
    """The evidence file for an expense, plus what OCR read from it.

    Read-only: the file is uploaded through the storage endpoint, and the OCR
    fields are written by the extraction job. A client that could edit
    ``ocr_extracted`` could make the receipt appear to say whatever the claim
    says, which is the one thing the receipt exists to contradict.
    """

    class Meta:
        model = ExpenseReceipt
        fields = (
            "id", "expense", "file_key", "original_filename", "content_type",
            "size_bytes", "ocr_text", "ocr_confidence", "ocr_extracted",
            "ocr_processed_at", "uploaded_by", "created_at",
        )


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------

class ExpenseSerializer(TenantScopedSerializer):
    """A single item of spend, from claim to reimbursement.

    ``total_amount`` is derived (``amount + tax_amount``) and read-only, so the
    figure an approver sees is always the figure that posts. ``status`` moves
    only through the sub-resources; in particular ``approved_at`` and
    ``approved_by`` are server-owned, because a claim that records its own
    approver is not an approval.
    """

    server_owned_fields = (
        "status", "number", "total_amount", "journal_entry",
        "reimbursement_entry", "submitted_at", "approved_at", "approved_by",
        "rejected_reason", "reimbursed_at", "invoiced_line",
    )

    amount = MoneyField(min_value=Decimal("0"))
    tax_amount = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)
    total_amount = MoneyField(read_only=True)
    exchange_rate = RateField(min_value=Decimal("0.000001"), required=False)
    markup_rate = RateField(min_value=Decimal("0"), required=False, default=ZERO)
    category_name = serializers.CharField(source="category.name", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True,
                                        default=None)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True,
                                          default=None)
    receipts = ExpenseReceiptSerializer(many=True, read_only=True)

    class Meta:
        model = Expense
        fields = (
            "id", "number", "vendor", "vendor_name", "category",
            "category_name", "description", "expense_date", "currency",
            "exchange_rate", "amount", "tax_amount", "total_amount",
            "tax_rate", "payment_method", "paid_from_account", "status",
            "is_billable", "is_reimbursable", "markup_rate", "invoiced_line",
            "customer", "project", "employee", "employee_name",
            "journal_entry", "reimbursement_entry", "submitted_at",
            "approved_at", "approved_by", "rejected_reason", "reimbursed_at",
            "notes", "receipts", "created_at", "created_by", "updated_at",
        )

    def get_fields(self) -> dict:
        fields = super().get_fields()
        # ``getattr(instance, "status", None)``, not ``instance.status``: under
        # ``many=True`` DRF hands the child serializer the *list* as its
        # instance, and a list has no ``status``. Reading it directly turns
        # every list request into a 500 while single-object reads keep working,
        # which is a maddening bug to find from the symptom.
        instance = getattr(self, "instance", None)
        instance_status = getattr(instance, "status", None)
        # An expense stops being editable the moment somebody else is asked to
        # look at it. Editing a submitted claim while it sits in an approver's
        # queue means the thing approved is not the thing reviewed.
        if instance_status is not None and instance_status != Expense.Status.DRAFT:
            for field in fields.values():
                field.read_only = True
                field.required = False
        return fields

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        if instance is not None and instance.status != Expense.Status.DRAFT:
            raise serializers.ValidationError(
                f"Expense {instance.number or instance.id} is "
                f"{instance.get_status_display().lower()} and can no longer be "
                f"edited. Reject it back to draft if it needs correcting — the "
                f"reviewer must see the version they are approving."
            )

        billable = attrs.get("is_billable", getattr(instance, "is_billable", False))
        customer = attrs.get("customer", getattr(instance, "customer", None))
        if billable and customer is None:
            # Mirrors ck_expense_billable_has_customer.
            raise serializers.ValidationError(
                {"customer": (
                    "Billable spend with nobody to bill is invisible lost "
                    "margin: it never reaches an invoice and never reaches an "
                    "exception report either. Name the customer."
                )}
            )

        reimbursable = attrs.get(
            "is_reimbursable", getattr(instance, "is_reimbursable", False)
        )
        employee = attrs.get("employee", getattr(instance, "employee", None))
        if reimbursable and employee is None:
            raise serializers.ValidationError(
                {"employee": (
                    "A reimbursable expense needs a claimant; somebody has to "
                    "be paid back."
                )}
            )

        amount = attrs.get("amount", getattr(instance, "amount", ZERO)) or ZERO
        tax = attrs.get("tax_amount", getattr(instance, "tax_amount", ZERO)) or ZERO
        if amount + tax <= ZERO:
            raise serializers.ValidationError(
                {"amount": "A zero-value expense records nothing. "
                           "ck_expense_amounts_valid refuses it."}
            )
        return attrs

    # -- write paths --------------------------------------------------------

    def create(self, validated_data: dict) -> Expense:
        self._derive_total(validated_data, None)
        return super().create(validated_data)

    def update(self, instance: Expense, validated_data: dict) -> Expense:
        self._derive_total(validated_data, instance)
        return super().update(instance, validated_data)

    @staticmethod
    def _derive_total(validated_data: dict, instance) -> None:
        """``total = amount + tax``, quantized once, at this boundary.

        Computed rather than accepted so the database check constraint can
        never be the thing that first notices a disagreement — by then the
        error is an opaque IntegrityError with no field attached.
        """
        currency = validated_data.get(
            "currency", getattr(instance, "currency", None)
        ) or "EGP"
        amount = validated_data.get("amount", getattr(instance, "amount", ZERO)) or ZERO
        tax = validated_data.get(
            "tax_amount", getattr(instance, "tax_amount", ZERO)
        ) or ZERO
        validated_data["amount"] = quantize_currency(amount, currency)
        validated_data["tax_amount"] = quantize_currency(tax, currency)
        validated_data["total_amount"] = (
            validated_data["amount"] + validated_data["tax_amount"]
        )


class ExpenseRejectSerializer(ReasonRequiredTransitionSerializer):
    """Body for ``POST /expenses/{id}/reject``.

    The reason is mandatory (and ``ck_expense_rejected_has_reason`` agrees):
    a claim bounced without one costs two more messages to resolve, and the
    claimant has no way to produce a version that would pass.
    """


class ExpenseApproveSerializer(TransitionSerializer):
    """Body for ``POST /expenses/{id}/approve``."""


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------

class BillLineSerializer(TenantScopedSerializer):
    """One line of a supplier invoice. Amounts are materialised by the parent."""

    server_owned_fields = ("line_subtotal", "line_tax", "line_total")

    quantity = QuantityField(min_value=Decimal("0"), required=False, default=ONE)
    unit_price = MoneyField(min_value=Decimal("0"))
    line_subtotal = MoneyField(read_only=True)
    line_tax = MoneyField(read_only=True)
    line_total = MoneyField(read_only=True)

    class Meta:
        model = BillLine
        fields = (
            "id", "line_number", "item", "description", "quantity",
            "unit_price", "tax_rate", "line_subtotal", "line_tax",
            "line_total", "expense_account", "category", "project",
            "is_billable", "customer",
        )
        read_only_fields = ("line_number",)


class BillSerializer(TenantScopedSerializer):
    """A vendor's invoice to us: an obligation to pay, recorded when incurred.

    Every header total is derived from the lines, for the same reason invoice
    totals are: a header that disagrees with its lines is how a company pays a
    different number from the one it agreed, and neither document looks wrong
    on its own.
    """

    server_owned_fields = (
        "status", "number", "subtotal_amount", "tax_amount", "total_amount",
        "amount_paid", "amount_due", "journal_entry", "approved_at",
        "approved_by", "paid_at", "void_reason",
    )

    lines = BillLineSerializer(many=True)
    withholding_amount = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)
    exchange_rate = RateField(min_value=Decimal("0.000001"), required=False)
    subtotal_amount = MoneyField(read_only=True)
    tax_amount = MoneyField(read_only=True)
    total_amount = MoneyField(read_only=True)
    amount_paid = MoneyField(read_only=True)
    amount_due = MoneyField(read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)

    class Meta:
        model = Bill
        fields = (
            "id", "number", "vendor", "vendor_name", "vendor_reference",
            "bill_date", "due_date", "received_at", "currency",
            "exchange_rate", "subtotal_amount", "tax_amount",
            "withholding_amount", "total_amount", "amount_paid", "amount_due",
            "status", "journal_entry", "project", "approved_at", "approved_by",
            "paid_at", "void_reason", "notes", "lines", "created_at",
            "created_by", "updated_at",
        )

    def validate_lines(self, lines: list[dict]) -> list[dict]:
        if not lines:
            raise serializers.ValidationError(
                "A bill needs at least one line: a payable with no detail "
                "cannot be coded to an account and cannot be checked against "
                "what was ordered."
            )
        return lines

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        if instance is not None and instance.status not in (
            Bill.Status.DRAFT,
            Bill.Status.AWAITING_APPROVAL,
        ):
            raise serializers.ValidationError(
                f"Bill {instance.number or instance.id} is "
                f"{instance.get_status_display().lower()} and has posted a "
                f"liability. Void it and re-enter, or raise a debit note — it "
                f"cannot be edited."
            )
        bill_date = attrs.get("bill_date", getattr(instance, "bill_date", None))
        due_date = attrs.get("due_date", getattr(instance, "due_date", None))
        if bill_date and due_date and due_date < bill_date:
            raise serializers.ValidationError(
                {"due_date": "A bill cannot fall due before it is dated."}
            )
        return attrs

    # -- write paths --------------------------------------------------------

    def create(self, validated_data: dict) -> Bill:
        from django.db import transaction

        lines = validated_data.pop("lines", [])
        validated_data.setdefault("subtotal_amount", ZERO)
        validated_data.setdefault("tax_amount", ZERO)
        validated_data.setdefault("total_amount", ZERO)
        validated_data.setdefault("amount_paid", ZERO)
        validated_data.setdefault("amount_due", ZERO)
        with transaction.atomic():
            bill = super().create(validated_data)
            self._write_lines(bill, lines)
            self._recalculate_totals(bill)
        bill.refresh_from_db()
        return bill

    def update(self, instance: Bill, validated_data: dict) -> Bill:
        from django.db import transaction

        lines = validated_data.pop("lines", None)
        with transaction.atomic():
            bill = super().update(instance, validated_data)
            if lines is not None:
                # Wholesale replacement, not a diff: a partial diff is how a
                # "removed" line survives and quietly inflates the payable.
                bill.lines.all().delete()
                self._write_lines(bill, lines)
            self._recalculate_totals(bill)
        bill.refresh_from_db()
        return bill

    def _write_lines(self, bill: Bill, lines: list[dict]) -> None:
        rows = []
        for index, line in enumerate(lines, start=1):
            quantity = line.get("quantity") or ONE
            unit_price = line.get("unit_price") or ZERO
            tax_rate = line.get("tax_rate")
            subtotal = quantize_currency(quantity * unit_price, bill.currency)
            rate = getattr(tax_rate, "rate", None) or ZERO
            tax = quantize_currency(subtotal * rate, bill.currency)
            rows.append(
                BillLine(
                    tenant_id=bill.tenant_id,
                    bill=bill,
                    line_number=index,
                    item=line.get("item"),
                    description=(line.get("description") or "")[:500],
                    quantity=quantity,
                    unit_price=unit_price,
                    tax_rate=tax_rate,
                    line_subtotal=subtotal,
                    line_tax=tax,
                    line_total=subtotal + tax,
                    expense_account=line["expense_account"],
                    category=line.get("category"),
                    project=line.get("project"),
                    is_billable=line.get("is_billable", False),
                    customer=line.get("customer"),
                    created_by_id=self.get_actor_id(),
                )
            )
        BillLine.objects.bulk_create(rows)

    @staticmethod
    def _recalculate_totals(bill: Bill) -> None:
        """Derive the header from the rows that actually exist in the database.

        Recomputed from the persisted lines rather than the submitted payload:
        those are the only two things that can disagree, and the rows are what
        the ledger and the approver will read.
        """
        from django.db.models import Sum

        totals = bill.lines.aggregate(
            subtotal=Sum("line_subtotal"), tax=Sum("line_tax")
        )
        subtotal = quantize_currency(totals["subtotal"] or ZERO, bill.currency)
        tax = quantize_currency(totals["tax"] or ZERO, bill.currency)
        total = subtotal + tax - quantize_currency(
            bill.withholding_amount or ZERO, bill.currency
        )
        bill.subtotal_amount = subtotal
        bill.tax_amount = tax
        bill.total_amount = total
        bill.amount_due = total - (bill.amount_paid or ZERO)
        bill.save(
            update_fields=[
                "subtotal_amount", "tax_amount", "total_amount", "amount_due",
                "updated_at",
            ]
        )


class BillPaymentSerializer(ReadOnlyModelSerializer):
    """A disbursement against a bill. Read-only over the API for now.

    Creating one has to lock the parent bill, recompute ``amount_paid`` from
    the payment rows and post the cash entry in one transaction; until that
    service exists, exposing a writable endpoint would let a caller record a
    payment the ledger never sees.
    """

    bill_number = serializers.CharField(source="bill.number", read_only=True, default="")
    vendor_name = serializers.CharField(source="vendor.name", read_only=True, default="")

    class Meta:
        model = BillPayment
        fields = (
            "id", "number", "bill", "vendor", "payment_date", "currency",
            "bill_number", "vendor_name",
            "exchange_rate", "amount", "withholding_amount", "cash_amount",
            "payment_method", "paid_from_account", "reference",
            "payment_batch_reference", "status", "journal_entry",
            "failure_reason", "notes", "created_at",
        )


class BillPaymentInputSerializer(serializers.Serializer):
    """Request body for ``POST /bills/{id}/pay``.

    Deliberately *not* a ModelSerializer over ``BillPayment``: what a caller
    supplies (how much, out of which account, when) is a much smaller set than
    what the row ends up holding (the journal entry, the recomputed status,
    the running paid figure). A ModelSerializer would advertise those as
    writable and invite a client to assert a status the service is the only
    thing entitled to decide.

    ``amount`` is a ``MoneyField`` — Decimal from a string, never a float.
    """

    amount = MoneyField(min_value=Decimal("0.01"))
    paid_from_account = serializers.UUIDField()
    payment_date = serializers.DateField(required=False, allow_null=True)
    reference = serializers.CharField(
        required=False, allow_blank=True, max_length=255, default=""
    )
    payment_method = serializers.CharField(
        required=False, allow_blank=True, max_length=32, default=""
    )


__all__ = [
    "ExpenseCategorySerializer",
    "VendorSerializer",
    "ExpenseReceiptSerializer",
    "ExpenseSerializer",
    "ExpenseRejectSerializer",
    "ExpenseApproveSerializer",
    "BillLineSerializer",
    "BillSerializer",
    "BillPaymentSerializer",
    "BillPaymentInputSerializer",
]


class VendorCreditLineSerializer(TenantScopedSerializer):
    """One line of a supplier credit."""

    server_owned_fields = ("line_subtotal", "line_tax", "line_total")

    unit_price = MoneyField(min_value=Decimal("0"))
    quantity = MoneyField(min_value=Decimal("0"), required=False, default=Decimal("1"))

    class Meta:
        model = VendorCreditLine
        fields = (
            "id", "line_number", "description", "quantity", "unit_price",
            "tax_rate", "line_subtotal", "line_tax", "line_total",
            "expense_account",
        )
        read_only_fields = ("line_number",)


class VendorCreditSerializer(TenantScopedSerializer):
    """A supplier credit note. Totals are derived from the lines."""

    server_owned_fields = (
        "number", "subtotal_amount", "tax_amount", "total_amount",
        "amount_applied", "amount_remaining", "status", "journal_entry",
        "void_reason",
    )

    lines = VendorCreditLineSerializer(many=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)

    class Meta:
        model = VendorCredit
        fields = (
            "id", "number", "vendor", "vendor_name", "bill", "credit_date",
            "currency", "exchange_rate", "subtotal_amount", "tax_amount",
            "total_amount", "amount_applied", "amount_remaining", "status",
            "journal_entry", "reason", "void_reason", "notes", "lines",
            "created_at", "updated_at",
        )

    def create(self, validated_data: dict) -> VendorCredit:
        from django.db import transaction

        lines = validated_data.pop("lines", [])
        with transaction.atomic():
            credit = super().create(validated_data)
            self._write_lines(credit, lines)
            self._recalculate(credit)
        credit.refresh_from_db()
        return credit

    def update(self, instance, validated_data: dict) -> VendorCredit:
        from django.db import transaction

        if instance.status != VendorCredit.Status.DRAFT:
            raise serializers.ValidationError(
                f"Vendor credit {instance.number or instance.id} is "
                f"{instance.get_status_display().lower()} and cannot be edited."
            )
        lines = validated_data.pop("lines", None)
        with transaction.atomic():
            credit = super().update(instance, validated_data)
            if lines is not None:
                # Same wholesale replacement, and the same reason it must go
                # through the plain queryset: bulk delete is disabled on
                # tenant-scoped models. See _replace_draft_lines in sales.
                from django.db.models.query import QuerySet

                QuerySet(model=VendorCreditLine).filter(
                    tenant_id=credit.tenant_id, credit_note=credit
                ).delete()
                self._write_lines(credit, lines)
            self._recalculate(credit)
        credit.refresh_from_db()
        return credit

    def _write_lines(self, credit: VendorCredit, lines: list) -> None:
        rows = []
        for index, line in enumerate(lines, start=1):
            quantity = line.get("quantity") or Decimal("1")
            unit_price = line.get("unit_price") or ZERO
            subtotal = quantize_currency(quantity * unit_price, credit.currency)
            rate = line.get("tax_rate")
            tax = quantize_currency(
                subtotal * (rate.rate if rate else ZERO), credit.currency
            )
            rows.append(VendorCreditLine(
                tenant_id=credit.tenant_id, credit_note=credit, line_number=index,
                description=(line.get("description") or "")[:500],
                quantity=quantity, unit_price=unit_price, tax_rate=rate,
                line_subtotal=subtotal, line_tax=tax, line_total=subtotal + tax,
                expense_account=line["expense_account"],
                created_by_id=self.get_actor_id(),
            ))
        VendorCreditLine.objects.bulk_create(rows)

    @staticmethod
    def _recalculate(credit: VendorCredit) -> None:
        """Totals from the rows, and `amount_remaining` with them.

        Written in one statement for the same reason the invoice discount is:
        `ck_vendor_credit_remaining_identity` is an IMMEDIATE check, so a
        partial write fails before the rest catches up.
        """
        from django.db.models import Sum

        totals = credit.lines.aggregate(
            subtotal=Sum("line_subtotal"), tax=Sum("line_tax")
        )
        subtotal = totals["subtotal"] or ZERO
        tax = totals["tax"] or ZERO
        total = quantize_currency(subtotal + tax, credit.currency)
        VendorCredit.all_tenants.filter(pk=credit.pk).update(
            subtotal_amount=subtotal, tax_amount=tax, total_amount=total,
            amount_remaining=total - (credit.amount_applied or ZERO),
        )


class RecurringBillLineTemplateSerializer(TenantScopedSerializer):
    class Meta:
        model = RecurringBillLineTemplate
        fields = (
            "id", "line_number", "description", "quantity", "unit_price",
            "tax_rate", "expense_account",
        )


class RecurringBillProfileSerializer(TenantScopedSerializer):
    """A standing vendor bill schedule."""

    server_owned_fields = ("occurrences_generated", "last_run_at", "last_error")

    lines = RecurringBillLineTemplateSerializer(many=True, read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    is_exhausted = serializers.BooleanField(read_only=True)

    class Meta:
        model = RecurringBillProfile
        fields = (
            "id", "name", "vendor", "vendor_name", "currency", "frequency",
            "interval", "start_date", "next_run_date", "end_date",
            "max_occurrences", "occurrences_generated", "is_exhausted",
            "payment_terms_days", "auto_submit", "notes", "is_active",
            "last_run_at", "last_error", "lines", "created_at", "updated_at",
        )

    def validate(self, attrs):
        instance = getattr(self, "instance", None)
        start = attrs.get("start_date", getattr(instance, "start_date", None))
        end = attrs.get("end_date", getattr(instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError(
                {"end_date": "A schedule cannot end before it starts."}
            )
        # A profile with no next run is a schedule that will never fire; default
        # it to the start date rather than leaving the generator nothing to find.
        if not attrs.get("next_run_date") and start and instance is None:
            attrs["next_run_date"] = start
        return attrs


class RecurringExpenseProfileSerializer(TenantScopedSerializer):
    """A standing expense — a subscription on the company card."""

    server_owned_fields = ("occurrences_generated", "last_run_at", "last_error")

    amount = MoneyField(min_value=Decimal("0.01"))
    tax_amount = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    is_exhausted = serializers.BooleanField(read_only=True)

    class Meta:
        model = RecurringExpenseProfile
        fields = (
            "id", "name", "vendor", "vendor_name", "category", "category_name",
            "currency", "frequency", "interval", "start_date", "next_run_date",
            "end_date", "max_occurrences", "occurrences_generated",
            "is_exhausted", "paid_from_account", "payment_method", "amount",
            "tax_amount", "description", "notes", "is_active", "last_run_at",
            "last_error", "created_at", "updated_at",
        )

    def validate(self, attrs):
        instance = getattr(self, "instance", None)
        start = attrs.get("start_date", getattr(instance, "start_date", None))
        end = attrs.get("end_date", getattr(instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError(
                {"end_date": "A schedule cannot end before it starts."}
            )
        if not attrs.get("next_run_date") and start and instance is None:
            attrs["next_run_date"] = start
        return attrs
