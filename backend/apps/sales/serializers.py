"""
Serializers for accounts receivable.

The one rule this module exists to enforce
------------------------------------------
**Invoice totals are computed by the server and are read-only on the wire.**

A client-supplied ``total_amount`` is a discount-fraud vector, and a plain
one. The line items say 10 units at 100.00; the header says the total is
50.00. Both are stored, both look legitimate in isolation, and the invoice
that reaches the customer — and the journal entry that reaches the ledger —
uses the header. Nobody reviewing the line items sees anything wrong. The same
hole in the other direction lets an insider inflate a total against a related
party.

There is no request in which a caller needs to name a total. Every figure on
an invoice is derivable from its lines plus a header discount, so the server
derives it, once, and the totals travel outward only. ``amount_paid`` and
``amount_due`` are likewise derived — from ``PaymentApplication`` rows, by
``apps.sales.services.invoice_workflow.apply_payment`` — and a writable
``amount_paid`` would let a caller mark an invoice settled without any money
having arrived.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from rest_framework import serializers

from apps.core.fields import ZERO, quantize_currency
from apps.core.serializers import (
    MoneyField,
    RateField,
    TenantScopedSerializer,
)
from apps.sales.models import (
    CreditNote,
    CreditNoteLine,
    Customer,
    Invoice,
    InvoiceAttachment,
    InvoiceLine,
    PaymentReminderRule,
    RecurringInvoiceProfile,
)

ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

class CustomerSerializer(TenantScopedSerializer):
    """A party that buys from the tenant and may owe them money.

    ``outstanding_balance`` is the sum of ``amount_due`` over the customer's
    *open* invoices — the figure a credit controller acts on. It is computed,
    never stored: a stored copy is one more thing that can drift from the
    invoices it summarises, and drift in an AR balance is invisible until a
    customer disputes a statement.

    The viewset annotates it on list queries so a page of 100 customers is one
    query rather than 101. The fallback below covers the single-object case
    (retrieve, and the response to a create).
    """

    outstanding_balance = serializers.SerializerMethodField()
    receivable_account_code = serializers.CharField(
        source="receivable_account.code", read_only=True, default=None
    )
    credit_limit = MoneyField(min_value=Decimal("0"), required=False)

    class Meta:
        model = Customer
        fields = (
            "id",
            "code",
            "name",
            "display_name",
            "email",
            "phone",
            "tax_number",
            "billing_address",
            "shipping_address",
            "payment_terms_days",
            "credit_limit",
            "has_credit_limit",
            "currency",
            "receivable_account",
            "receivable_account_code",
            "is_active",
            "notes",
            "outstanding_balance",
            "created_at",
            "updated_at",
        )

    def get_outstanding_balance(self, obj: Customer) -> str:
        annotated = getattr(obj, "outstanding_balance", None)
        if annotated is not None:
            return f"{annotated:f}"
        from django.db.models import Sum

        total = obj.invoices.filter(
            status__in=[
                Invoice.Status.SENT,
                Invoice.Status.PARTIALLY_PAID,
                Invoice.Status.OVERDUE,
            ]
        ).aggregate(total=Sum("amount_due"))["total"]
        return f"{(total or ZERO):f}"

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        has_limit = attrs.get(
            "has_credit_limit", getattr(instance, "has_credit_limit", False)
        )
        limit = attrs.get("credit_limit", getattr(instance, "credit_limit", ZERO))
        if has_limit and (limit or ZERO) <= ZERO:
            raise serializers.ValidationError(
                {
                    "credit_limit": (
                        "A customer flagged as having a credit limit needs a "
                        "positive one. A limit of zero reads as 'no credit' to a "
                        "human and as 'unlimited' to code that checks the flag."
                    )
                }
            )
        return attrs


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

class InvoiceLineSerializer(TenantScopedSerializer):
    """One billable item.

    ``line_subtotal``, ``line_tax`` and ``line_total`` are materialised by the
    parent serializer and read-only here. They are computed once, at write
    time, and stored — recomputing them at read time reproduces the rounding
    differently in Python, in SQL and in the PDF renderer, and the three
    disagree by a cent on roughly one invoice in three hundred.
    """

    server_owned_fields = ("line_subtotal", "line_tax", "line_total")

    unit_price = MoneyField(min_value=Decimal("0"))
    quantity = MoneyField(min_value=Decimal("0"), required=False, default=ONE)
    discount_rate = RateField(
        min_value=Decimal("0"), max_value=ONE, required=False, default=ZERO
    )
    line_subtotal = MoneyField(read_only=True)
    line_tax = MoneyField(read_only=True)
    line_total = MoneyField(read_only=True)

    class Meta:
        model = InvoiceLine
        fields = (
            "id",
            "line_number",
            "item",
            "description",
            "quantity",
            "unit_price",
            "discount_rate",
            "tax_rate",
            "line_subtotal",
            "line_tax",
            "line_total",
            "income_account",
            "project",
        )
        # Assigned from list position by the parent, so re-ordering lines in
        # the UI cannot collide with ``uq_invoice_line_number``.
        read_only_fields = ("line_number",)


def _replace_draft_lines(model, *, tenant_id, **filters) -> None:
    """Drop a draft document's lines so they can be rewritten.

    Two guard rails stand in the way, and this is where sales steps around
    them — deliberately, and only for a draft:

    * ``TenantQuerySet.delete()`` raises: bulk delete is disabled on
      tenant-scoped models so that a stray ``.delete()`` cannot wipe a
      customer's ledger.

    Both callers have already refused anything past DRAFT in ``validate()``,
    so the rows being dropped are lines of a document that has no number, no
    journal entry and has never been sent to anybody. They are intermediate
    input to a document still being typed, not financial records.

    Without this the whole update path was dead: ``PATCH /invoices/{id}/``
    with any ``lines`` payload answered *403 "Bulk delete is disabled on
    tenant-scoped models"* — a message about an internal guard, on a request
    that was entirely legitimate. Nothing caught it because no test and no
    screen had ever edited a draft.

    The plain (non-tenant) queryset is filtered explicitly by ``tenant_id`` so
    the bypass cannot accidentally widen its own scope — the same shape, and
    the same reasoning, as ``payroll.services.engine._discard_payslips``.
    """
    from django.db.models.query import QuerySet  # local: see docstring

    QuerySet(model=model).filter(tenant_id=tenant_id, **filters).delete()


class InvoiceSerializer(TenantScopedSerializer):
    """A customer invoice with its lines, writable as one document while DRAFT.

    Totals are server-side and read-only — this is a fraud control
    -------------------------------------------------------------
    ``subtotal_amount``, ``tax_amount``, ``total_amount``, ``amount_paid`` and
    ``amount_due`` are all computed here from the lines and the header
    discount. None of them is accepted from the request body.

    If a client could send ``total_amount``, it could send line items worth
    1,000.00 and a total of 50.00. Both are stored; both look correct in
    isolation; the customer is billed 50.00 and the journal entry credits
    revenue 50.00 while the line detail says otherwise. Nobody reviewing the
    lines sees a problem, and the discrepancy only surfaces as an unexplained
    margin gap months later. The same hole run the other way — inflating a
    total above the line items — is how an insider bills a related party.

    Since every figure is derivable from the lines, there is no legitimate
    request that needs to name one. ``discount_amount`` *is* accepted: a
    header-level discount is a real commercial decision and cannot be derived
    from anything.

    ``status`` is read-only. Issuing is ``POST /invoices/{id}/issue``, which
    allocates the gapless number, posts the journal entry and releases stock
    in one transaction. A PATCH that only wrote the column would do none of
    those things and would return 200.

    Lines are editable only while the invoice is DRAFT. After issue the
    document has been sent to a customer and posted to the ledger; the
    corrections are a credit note or a write-off, never an edit.
    """

    server_owned_fields = (
        "status",
        "number",
        "subtotal_amount",
        "tax_amount",
        "total_amount",
        "amount_paid",
        "amount_due",
        "journal_entry",
        "sent_at",
        "viewed_at",
        "paid_at",
        "written_off_at",
        "void_reason",
        "recurring_profile",
    )

    lines = InvoiceLineSerializer(many=True)
    discount_amount = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)
    exchange_rate = RateField(min_value=Decimal("0.000001"), required=False)
    subtotal_amount = MoneyField(read_only=True)
    tax_amount = MoneyField(read_only=True)
    total_amount = MoneyField(read_only=True)
    amount_paid = MoneyField(read_only=True)
    amount_due = MoneyField(read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    salesperson_name = serializers.CharField(
        source="salesperson.full_name", read_only=True, default=""
    )
    is_settled = serializers.BooleanField(read_only=True)
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = Invoice
        fields = (
            "id",
            "number",
            "order_number",
            "subject",
            "salesperson",
            "salesperson_name",
            "customer",
            "customer_name",
            "issue_date",
            "due_date",
            "currency",
            "exchange_rate",
            "subtotal_amount",
            "discount_amount",
            "tax_amount",
            "total_amount",
            "amount_paid",
            "amount_due",
            "status",
            "is_settled",
            "is_open",
            "journal_entry",
            "notes",
            "terms",
            "project",
            "recurring_profile",
            "sent_at",
            "viewed_at",
            "paid_at",
            "void_reason",
            "written_off_at",
            "lines",
            "created_at",
            "created_by",
            "updated_at",
        )

    # -- field shaping ------------------------------------------------------

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        # ``getattr(instance, "status", ...)``, not ``instance.status``: under
        # ``many=True`` DRF builds the child serializer with the *list* as its
        # instance, and a list has no ``status``. Reading it there raised
        # AttributeError -> 500 on every GET /invoices/, i.e. the list endpoint
        # was down while every single-object path worked.
        instance = getattr(self, "instance", None)
        instance_status = getattr(instance, "status", None)
        if instance_status is not None and instance_status != Invoice.Status.DRAFT:
            for name in ("lines", "customer", "issue_date", "currency",
                         "exchange_rate", "discount_amount", "project"):
                field = fields.get(name)
                if field is not None:
                    field.read_only = True
                    field.required = False
        return fields

    # -- validation ---------------------------------------------------------

    def validate_lines(self, lines: list[dict]) -> list[dict]:
        if not lines:
            raise serializers.ValidationError(
                "An invoice needs at least one line. Issuing a zero-value "
                "invoice is refused by the workflow, so a line-less invoice is "
                "a document that can never leave draft."
            )
        return lines

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        if instance is not None and instance.status != Invoice.Status.DRAFT:
            raise serializers.ValidationError(
                f"Invoice {instance.number or instance.id} is "
                f"{instance.get_status_display().lower()} and has been sent to a "
                f"customer and posted to the ledger. Issue a credit note or a "
                f"write-off — it cannot be edited."
            )

        issue_date = attrs.get("issue_date", getattr(instance, "issue_date", None))
        due_date = attrs.get("due_date", getattr(instance, "due_date", None))
        if issue_date and due_date and due_date < issue_date:
            raise serializers.ValidationError(
                {"due_date": "An invoice cannot fall due before it is issued."}
            )

        customer = attrs.get("customer", getattr(instance, "customer", None))
        currency = attrs.get("currency", getattr(instance, "currency", None))
        if customer is not None and currency and customer.currency != currency:
            # Not fatal, but worth refusing: an invoice raised in a currency the
            # customer's receivable account is not denominated in produces an FX
            # exposure nobody chose.
            raise serializers.ValidationError(
                {
                    "currency": (
                        f"Customer {customer.code} trades in "
                        f"{customer.currency}; raising this invoice in {currency} "
                        f"creates an unintended FX exposure. Change the "
                        f"customer's currency deliberately if that is what you "
                        f"mean."
                    )
                }
            )
        return attrs

    # -- write paths --------------------------------------------------------

    def create(self, validated_data: dict) -> Invoice:
        from django.db import transaction

        lines = validated_data.pop("lines", [])
        # The header discount is held back from the INSERT and applied by
        # `_recalculate_totals` together with the figures it depends on.
        #
        # `ck_invoice_total_identity` asserts
        # `total = subtotal - discount + tax` and is an IMMEDIATE check, so it
        # runs on the INSERT itself. `subtotal_amount`, `tax_amount` and
        # `total_amount` are read-only on this serializer — they are derived
        # from the lines, which do not exist yet — so the insert would write
        # 0, 0, 0 alongside a discount of 5.00 and the database would evaluate
        # `0 = 0 - 5 + 0` and refuse the row.
        #
        # That is why *any* non-zero header discount answered "The request
        # conflicts with existing data" while a zero one worked: `0 = 0 - 0 + 0`
        # holds. The field had never functioned; nothing exercised it until the
        # invoice form exposed it.
        discount = validated_data.pop("discount_amount", None)
        with transaction.atomic():
            invoice = super().create(validated_data)
            self._write_lines(invoice, lines)
            self._recalculate_totals(invoice, discount=discount)
        invoice.refresh_from_db()
        return invoice

    def update(self, instance: Invoice, validated_data: dict) -> Invoice:
        from django.db import transaction

        lines = validated_data.pop("lines", None)
        # Held back for the same reason as in create(): writing a new discount
        # while subtotal/tax/total still hold their old values breaks the
        # identity on the UPDATE statement, before the recalculation runs.
        discount = validated_data.pop("discount_amount", None)
        with transaction.atomic():
            invoice = super().update(instance, validated_data)
            if lines is not None:
                # Wholesale replacement, not a diff: line identity carries no
                # business meaning on a draft, and a partial diff is how a
                # "removed" line survives and quietly inflates the total.
                _replace_draft_lines(
                    InvoiceLine, tenant_id=invoice.tenant_id, invoice=invoice
                )
                self._write_lines(invoice, lines)
            self._recalculate_totals(invoice, discount=discount)
        invoice.refresh_from_db()
        return invoice

    # -- totals -------------------------------------------------------------

    def _write_lines(self, invoice: Invoice, lines: list[dict]) -> None:
        rows = []
        for index, line in enumerate(lines, start=1):
            subtotal, tax, total = self._line_amounts(invoice.currency, line)
            rows.append(
                InvoiceLine(
                    tenant_id=invoice.tenant_id,
                    invoice=invoice,
                    line_number=index,
                    item=line.get("item"),
                    description=line.get("description", "")[:500],
                    quantity=line.get("quantity") or ONE,
                    unit_price=line.get("unit_price") or ZERO,
                    discount_rate=line.get("discount_rate") or ZERO,
                    tax_rate=line.get("tax_rate"),
                    line_subtotal=subtotal,
                    line_tax=tax,
                    line_total=total,
                    income_account=line["income_account"],
                    project=line.get("project"),
                    created_by_id=self.get_actor_id(),
                )
            )
        InvoiceLine.objects.bulk_create(rows)

    @staticmethod
    def _line_amounts(currency: str, line: dict) -> tuple[Decimal, Decimal, Decimal]:
        """``quantity * unit_price * (1 - discount_rate)``, then tax.

        Rounded to the currency's minor unit exactly once per line, at this
        boundary. Rounding the total instead of the lines makes the printed
        line amounts fail to add up to the printed total, which is the single
        most common invoice complaint; rounding at both levels double-rounds
        and drifts.
        """
        quantity = line.get("quantity") or ONE
        unit_price = line.get("unit_price") or ZERO
        discount = line.get("discount_rate") or ZERO
        tax_rate = line.get("tax_rate")

        subtotal = quantize_currency(quantity * unit_price * (ONE - discount), currency)
        rate = getattr(tax_rate, "rate", None) or ZERO
        tax = quantize_currency(subtotal * rate, currency)
        return subtotal, tax, subtotal + tax

    @staticmethod
    def _recalculate_totals(invoice: Invoice, discount=None) -> None:
        """Derive every header figure from the rows that actually exist.

        Recomputed from the persisted lines rather than the submitted payload,
        because those are the only two things that can disagree — and the rows
        are what ``build_invoice_entry`` will read when the invoice is posted.

        ``amount_due`` is written explicitly to satisfy
        ``ck_invoice_due_identity``; ``amount_paid`` is left alone because it
        belongs to ``apply_payment``, which derives it from the
        ``PaymentApplication`` rows.
        """
        from django.db.models import Sum

        totals = invoice.lines.aggregate(
            subtotal=Sum("line_subtotal"), tax=Sum("line_tax")
        )
        subtotal = totals["subtotal"] or ZERO
        tax = totals["tax"] or ZERO
        # `discount` is the value the caller asked for, withheld from the
        # INSERT/UPDATE by create()/update(); None means "leave what is on the
        # row". Either way it is written *here*, in the same statement as the
        # figures the identity constraint checks it against.
        discount = (invoice.discount_amount or ZERO) if discount is None else discount
        if discount > subtotal:
            raise serializers.ValidationError(
                {
                    "discount_amount": (
                        f"A header discount of {discount} exceeds the line "
                        f"subtotal of {subtotal}. A negative invoice is a credit "
                        f"note, and credit notes are their own document."
                    )
                }
            )
        total = quantize_currency(subtotal - discount + tax, invoice.currency)
        paid = invoice.amount_paid or ZERO

        Invoice.all_tenants.filter(pk=invoice.pk).update(
            subtotal_amount=subtotal,
            discount_amount=discount,
            tax_amount=tax,
            total_amount=total,
            amount_due=total - paid,
        )


# ---------------------------------------------------------------------------
# Credit notes
# ---------------------------------------------------------------------------

class CreditNoteLineSerializer(TenantScopedSerializer):
    """One line of a credit note. Amounts are server-computed, as on invoices."""

    server_owned_fields = ("line_subtotal", "line_tax", "line_total")

    unit_price = MoneyField(min_value=Decimal("0"))
    quantity = MoneyField(min_value=Decimal("0"), required=False, default=ONE)
    discount_rate = RateField(
        min_value=Decimal("0"), max_value=ONE, required=False, default=ZERO
    )
    line_subtotal = MoneyField(read_only=True)
    line_tax = MoneyField(read_only=True)
    line_total = MoneyField(read_only=True)

    class Meta:
        model = CreditNoteLine
        fields = (
            "id",
            "line_number",
            "invoice_line",
            "item",
            "description",
            "quantity",
            "unit_price",
            "discount_rate",
            "tax_rate",
            "line_subtotal",
            "line_tax",
            "line_total",
            "income_account",
            "project",
            "restocks_inventory",
        )
        read_only_fields = ("line_number",)


class CreditNoteSerializer(TenantScopedSerializer):
    """A reduction of what a customer owes.

    A credit note is a document in its own right, not a negative invoice, for
    the same reason a void is not a delete: it reconciles. Totals are
    server-computed and read-only for exactly the reason given on
    :class:`InvoiceSerializer` — a client-supplied total on a credit note is
    the same fraud vector pointed the other way.

    ``amount_applied`` / ``amount_remaining`` are derived by the application
    path, not written here.
    """

    server_owned_fields = (
        "status",
        "number",
        "subtotal_amount",
        "tax_amount",
        "total_amount",
        "amount_applied",
        "amount_remaining",
        "journal_entry",
        "void_reason",
    )

    lines = CreditNoteLineSerializer(many=True, required=False)
    exchange_rate = RateField(min_value=Decimal("0.000001"), required=False)
    subtotal_amount = MoneyField(read_only=True)
    tax_amount = MoneyField(read_only=True)
    total_amount = MoneyField(read_only=True)
    amount_applied = MoneyField(read_only=True)
    amount_remaining = MoneyField(read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    salesperson_name = serializers.CharField(
        source="salesperson.full_name", read_only=True, default=""
    )

    class Meta:
        model = CreditNote
        fields = (
            "id",
            "number",
            "order_number",
            "subject",
            "salesperson",
            "salesperson_name",
            "customer",
            "customer_name",
            "invoice",
            "issue_date",
            "currency",
            "exchange_rate",
            "subtotal_amount",
            "tax_amount",
            "total_amount",
            "amount_applied",
            "amount_remaining",
            "status",
            "reason",
            "journal_entry",
            "notes",
            "void_reason",
            "lines",
            "created_at",
            "updated_at",
        )

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        # See InvoiceSerializer.get_fields: under ``many=True`` the child's
        # ``instance`` is the queryset/list, which has no ``status``.
        instance = getattr(self, "instance", None)
        instance_status = getattr(instance, "status", None)
        if instance_status is not None and instance_status != CreditNote.Status.DRAFT:
            for name in ("lines", "customer", "invoice", "issue_date", "currency",
                         "exchange_rate"):
                field = fields.get(name)
                if field is not None:
                    field.read_only = True
                    field.required = False
        return fields

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        if instance is not None and instance.status != CreditNote.Status.DRAFT:
            raise serializers.ValidationError(
                f"Credit note {instance.number or instance.id} is "
                f"{instance.get_status_display().lower()} and cannot be edited."
            )
        customer = attrs.get("customer", getattr(instance, "customer", None))
        invoice = attrs.get("invoice", getattr(instance, "invoice", None))
        if invoice is not None and customer is not None and invoice.customer_id != customer.id:
            raise serializers.ValidationError(
                {
                    "invoice": (
                        "The credit note's customer and the invoice it credits "
                        "must be the same party; crediting one customer against "
                        "another's invoice makes both AR balances wrong."
                    )
                }
            )
        return attrs

    def create(self, validated_data: dict) -> CreditNote:
        from django.db import transaction

        lines = validated_data.pop("lines", [])
        with transaction.atomic():
            note = super().create(validated_data)
            self._write_lines(note, lines)
            self._recalculate_totals(note)
        note.refresh_from_db()
        return note

    def update(self, instance: CreditNote, validated_data: dict) -> CreditNote:
        from django.db import transaction

        lines = validated_data.pop("lines", None)
        with transaction.atomic():
            note = super().update(instance, validated_data)
            if lines is not None:
                # Same bug, same fix: this path 403'd on the internal
                # bulk-delete guard for every credit-note amendment.
                _replace_draft_lines(
                    CreditNoteLine, tenant_id=note.tenant_id, credit_note=note
                )
                self._write_lines(note, lines)
            self._recalculate_totals(note)
        note.refresh_from_db()
        return note

    def _write_lines(self, note: CreditNote, lines: list[dict]) -> None:
        rows = []
        for index, line in enumerate(lines, start=1):
            subtotal, tax, total = InvoiceSerializer._line_amounts(note.currency, line)
            rows.append(
                CreditNoteLine(
                    tenant_id=note.tenant_id,
                    credit_note=note,
                    line_number=index,
                    invoice_line=line.get("invoice_line"),
                    item=line.get("item"),
                    description=line.get("description", "")[:500],
                    quantity=line.get("quantity") or ONE,
                    unit_price=line.get("unit_price") or ZERO,
                    discount_rate=line.get("discount_rate") or ZERO,
                    tax_rate=line.get("tax_rate"),
                    line_subtotal=subtotal,
                    line_tax=tax,
                    line_total=total,
                    income_account=line["income_account"],
                    project=line.get("project"),
                    restocks_inventory=line.get("restocks_inventory", False),
                    created_by_id=self.get_actor_id(),
                )
            )
        CreditNoteLine.objects.bulk_create(rows)

    @staticmethod
    def _recalculate_totals(note: CreditNote) -> None:
        from django.db.models import Sum

        totals = note.lines.aggregate(
            subtotal=Sum("line_subtotal"), tax=Sum("line_tax")
        )
        subtotal = totals["subtotal"] or ZERO
        tax = totals["tax"] or ZERO
        total = quantize_currency(subtotal + tax, note.currency)
        applied = note.amount_applied or ZERO
        CreditNote.all_tenants.filter(pk=note.pk).update(
            subtotal_amount=subtotal,
            tax_amount=tax,
            total_amount=total,
            amount_remaining=total - applied,
        )


# ---------------------------------------------------------------------------
# Automation
# ---------------------------------------------------------------------------

class RecurringInvoiceProfileSerializer(TenantScopedSerializer):
    """A schedule that generates invoices.

    ``occurrences_generated``, ``last_run_at`` and ``last_error`` are owned by
    the generator task. A writable ``occurrences_generated`` would let a caller
    reset the counter and re-bill a customer for a period already invoiced.

    ``next_run_date`` is writable: rescheduling a run is a legitimate
    administrative act, and it is the only lever a user has when a schedule was
    created with the wrong start date.
    """

    server_owned_fields = ("occurrences_generated", "last_run_at", "last_error")

    class Meta:
        model = RecurringInvoiceProfile
        fields = (
            "id",
            "name",
            "customer",
            "currency",
            "frequency",
            "interval",
            "start_date",
            "next_run_date",
            "end_date",
            "max_occurrences",
            "occurrences_generated",
            "auto_send",
            "payment_terms_days",
            "lead_days",
            "project",
            "notes",
            "terms",
            "is_active",
            "last_run_at",
            "last_error",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        start = attrs.get("start_date", getattr(instance, "start_date", None))
        end = attrs.get("end_date", getattr(instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError(
                {"end_date": "A schedule cannot end before it starts."}
            )
        interval = attrs.get("interval", getattr(instance, "interval", 1))
        if interval is not None and interval < 1:
            raise serializers.ValidationError(
                {
                    "interval": (
                        "An interval below 1 would make the schedule fire "
                        "continuously; every tick would be due."
                    )
                }
            )
        return attrs


class PaymentReminderRuleSerializer(TenantScopedSerializer):
    """When and how to chase an unpaid invoice.

    ``offset_days`` is signed and relative to the due date: negative is a
    courtesy reminder before it falls due, positive is dunning after. That sign
    convention is the whole configuration surface, so it is worth being
    explicit about — a rule written as ``7`` meaning "a week before" would send
    every reminder a fortnight late.

    ``minimum_amount_due`` suppresses chasing trivial balances, where the cost
    of the email exceeds the debt and the nuisance costs goodwill.
    """

    minimum_amount_due = MoneyField(min_value=Decimal("0"), required=False)

    class Meta:
        model = PaymentReminderRule
        fields = (
            "id",
            "name",
            "offset_days",
            "channel",
            "template_subject",
            "template_body",
            "minimum_amount_due",
            "is_active",
            "notify_internal_only",
            "created_at",
            "updated_at",
        )


__all__ = [
    "CustomerSerializer",
    "InvoiceLineSerializer",
    "InvoiceSerializer",
    "CreditNoteLineSerializer",
    "CreditNoteSerializer",
    "RecurringInvoiceProfileSerializer",
    "PaymentReminderRuleSerializer",
]


class InvoiceAttachmentSerializer(TenantScopedSerializer):
    """Read view of an attachment. Uploading goes through the multipart action.

    ``file`` is exposed as a URL, not as the storage key: the key is an
    implementation detail of whichever backend is configured, and handing it to
    a client invites them to construct their own paths against the bucket.
    """

    server_owned_fields = (
        "file", "original_filename", "content_type", "size_bytes", "sha256",
        "uploaded_by",
    )

    file_url = serializers.SerializerMethodField()
    uploaded_by_name = serializers.CharField(
        source="uploaded_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = InvoiceAttachment
        fields = (
            "id", "invoice", "file_url", "original_filename", "content_type",
            "size_bytes", "sha256", "description", "uploaded_by",
            "uploaded_by_name", "created_at",
        )
        read_only_fields = (
            "invoice", "original_filename", "content_type", "size_bytes",
            "sha256", "uploaded_by",
        )

    def get_file_url(self, obj) -> str:
        try:
            return obj.file.url
        except Exception:  # noqa: BLE001 - storage may not expose a URL
            return ""


class InvoiceAttachmentUploadSerializer(serializers.Serializer):
    """Validate one uploaded file before anything touches storage.

    Every check here answers "what happens if this is wrong", not "what does
    the spec say":

    ``size``
        Refused *before* the file is read into a hash or written to disk. A
        DRF ``FileField`` has already spooled the body to a temp file by this
        point, but refusing here still stops the write and the database row.

    ``content type``
        An allowlist. The browser-supplied type is not trusted on its own —
        it is trivially forged — so the extension is checked to agree with it.
        A ``.html`` or ``.svg`` uploaded as ``image/png`` and later served
        from this origin is stored XSS against every user of the tenant, which
        is why those two extensions are refused outright regardless of the
        declared type.

    ``emptiness``
        A zero-byte file is almost always a failed drag-and-drop, and storing
        it produces an attachment that looks present and opens to nothing.
    """

    file = serializers.FileField()
    description = serializers.CharField(
        required=False, allow_blank=True, max_length=255, default=""
    )

    #: Extensions that execute when opened from the app's own origin. Refused
    #: whatever content type the browser claims.
    DANGEROUS_SUFFIXES = frozenset({
        ".html", ".htm", ".svg", ".xhtml", ".xml", ".js", ".mjs",
        ".exe", ".dll", ".bat", ".cmd", ".sh", ".ps1", ".jar", ".msi",
    })

    def validate_file(self, upload):
        from pathlib import PurePosixPath  # noqa: PLC0415

        if upload.size == 0:
            raise serializers.ValidationError(
                "That file is empty — nothing was uploaded. It is usually a "
                "drag-and-drop that did not complete."
            )
        if upload.size > InvoiceAttachment.MAX_BYTES:
            mb = InvoiceAttachment.MAX_BYTES // (1024 * 1024)
            raise serializers.ValidationError(
                f"{upload.name} is {upload.size // 1024} KB; the limit is "
                f"{mb} MB. Attach a link or split the document."
            )

        suffix = PurePosixPath(upload.name or "").suffix.lower()
        if suffix in self.DANGEROUS_SUFFIXES:
            raise serializers.ValidationError(
                f"{suffix} files are not accepted as attachments: served from "
                f"this application's own origin they can run script against "
                f"everyone in your organisation."
            )

        content_type = (upload.content_type or "").split(";")[0].strip().lower()
        if content_type not in InvoiceAttachment.ALLOWED_CONTENT_TYPES:
            raise serializers.ValidationError(
                f"{content_type or 'that file type'} is not an accepted "
                f"attachment. PDFs, images, Office documents, CSV and ZIP are."
            )
        return upload
