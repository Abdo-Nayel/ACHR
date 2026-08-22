"""
Payment endpoints: recording receipts, allocating them, refunding them.

Why ``Idempotency-Key`` is *mandatory* on ``POST /payments/``
------------------------------------------------------------
Everywhere else in this codebase the header is optional — a replayed
``issue`` returns the same invoice, and a caller who omits it merely loses
that protection. Creating a payment is the one operation where omitting it is
not a lost optimisation but a defect: the request has no natural key, so a
client that times out after the server committed has no way to ask "did it
land?" and will retry. The retry creates a second receipt, the second receipt
is applied to the same invoices, and the customer's account shows a credit
they never paid — or, on the gateway path, a second charge on their card.

The header is therefore refused-if-absent rather than defaulted, because a
server-generated key defends against nothing: two retries would generate two
keys. Only the client knows that request N+1 *is* request N.

Allocation and refunds are POST sub-resources for the reason set out in
``apps.core.viewsets``: they are business verbs with their own permission
(``banking.payment.allocate``, ``banking.refund.create``), their own guards
and their own idempotency boundary, none of which a PATCH on a column has.
"""

from __future__ import annotations

import logging

from apps.accounting.services.sequences import allocate_document_number
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from apps.core.exceptions import NotImplementedYet
from apps.core.viewsets import IdempotentActionMixin
from apps.core.exceptions import DomainError
from apps.core.fields import ZERO, quantize_currency
from apps.core.pagination import SmallPagePagination
from apps.core.viewsets import (
    RbacOnlyQuerysetMixin,
    ReadOnlyTenantViewSet,
    TenantModelViewSet,
    read_idempotency_key,
)
from apps.payments.models import (
    Payment,
    PaymentApplication,
    PaymentGatewayConfig,
    Refund,
    WebhookEvent,
)
from apps.payments.serializers import (
    ApplyPaymentSerializer,
    PaymentApplicationSerializer,
    PaymentGatewayConfigSerializer,
    PaymentSerializer,
    RefundRequestSerializer,
    RefundSerializer,
    WebhookEventSerializer,
)
from apps.sales.models import Invoice

logger = logging.getLogger(__name__)

#: Statuses in which a receipt represents money that has actually moved and
#: may therefore settle an invoice. An AUTHORIZED payment is a hold on a card
#: limit; applying it would mark an invoice paid against funds that can still
#: simply expire.
APPLICABLE_STATUSES = (
    Payment.Status.CAPTURED,
    Payment.Status.SETTLED,
    Payment.Status.PARTIALLY_REFUNDED,
    Payment.Status.DISPUTED,
)

#: Refund rows that still count against the parent payment. A FAILED or
#: CANCELLED refund returned nothing and must not reduce the refundable
#: balance, or a failed attempt would permanently strand the customer's money.
LIVE_REFUND_STATUSES = (Refund.Status.PENDING, Refund.Status.SUCCEEDED)


class MissingIdempotencyKey(DomainError):
    """400 for a payment creation with no ``Idempotency-Key`` header."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "idempotency_key_required"
    default_detail = (
        "POST /payments/ requires an Idempotency-Key header. Without one a "
        "retried request records a second receipt for the same money."
    )


class PaymentGatewayConfigViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Configured payment providers. Secrets never leave the server."""

    permission_domain = "banking"
    resource = "gateway_config"
    queryset = PaymentGatewayConfig.objects.all()
    serializer_class = PaymentGatewayConfigSerializer
    select_related = ("clearing_account", "fee_account")
    pagination_class = SmallPagePagination
    filterset_fields = ("provider", "is_active", "is_default", "is_test_mode")
    search_fields = ("display_name", "provider")
    ordering_fields = ("display_name", "provider", "created_at")
    ordering = ("display_name",)
    extra_permissions = {
        "POST": ["banking.gateway_config.manage"],
        "PUT": ["banking.gateway_config.manage"],
        "PATCH": ["banking.gateway_config.manage"],
        "rotate_secret": ["banking.gateway_config.rotate_secret"],
    }

    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request, pk=None):
        """``POST /payment-gateways/{id}/rotate-secret`` — **not implemented**.

        Mounted so the route, its permission (which is ``is_sensitive`` and
        therefore demands re-authentication) and its schema entry exist. A real
        implementation writes the new secret to the secrets manager, updates
        ``webhook_secret_ref``, and keeps the *previous* secret valid for an
        overlap window — a rotation that invalidates the old secret instantly
        drops every webhook already in flight at the provider, and those are
        the events that move money.
        """
        self.get_object()
        raise NotImplementedYet(
            "Gateway secret rotation is not implemented yet. It requires the "
            "secrets-manager integration plus a dual-secret overlap window, "
            "without which rotation silently drops in-flight webhooks."
        )


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

class PaymentViewSet(IdempotentActionMixin, TenantModelViewSet):
    """Customer receipts: record, allocate, refund.

    ``update``/``partial_update`` are inherited but always refused by the
    serializer — a receipt is a record of something that happened at a bank,
    and the way to correct it is a refund or a reversal, not an edit.
    """

    permission_domain = "banking"
    resource = "payment"
    queryset = Payment.objects.all()
    serializer_class = PaymentSerializer
    select_related = ("customer", "gateway", "deposit_account", "journal_entry")
    prefetch_related = ("applications", "applications__invoice")
    filterset_fields = ("status", "customer", "method", "currency", "gateway",
                        "payment_date")
    search_fields = ("number", "reference", "gateway_transaction_id",
                     "customer__name", "customer__code")
    ordering_fields = ("payment_date", "number", "amount", "created_at")
    ordering = ("-payment_date", "-created_at")
    extra_permissions = {
        "apply": ["banking.payment.allocate"],
        "refund": ["banking.refund.create"],
        "applications": ["banking.payment.read"],
        "DELETE": ["banking.payment.void"],
    }

    # -- create -------------------------------------------------------------

    def create(self, request, *args, **kwargs):
        """Record a receipt. ``Idempotency-Key`` is required — see the module
        docstring for why this is the one endpoint where it is not optional.

        A replay returns **the payment the first call created**, with
        ``Idempotency-Replayed: true`` and a 200 rather than a 201. Returning
        201 twice would tell a client two payments exist.
        """
        key = read_idempotency_key(
            request, request.data if isinstance(request.data, dict) else None
        )
        if not key:
            raise MissingIdempotencyKey()

        existing = Payment.objects.filter(idempotency_key=key).first()
        if existing is not None:
            response = Response(self.get_serializer(existing).data,
                                status=status.HTTP_200_OK)
            response["Idempotency-Replayed"] = "true"
            return response

        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        data["idempotency_key"] = key
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            payment = serializer.save()
            if not payment.number:
                payment.number = allocate_document_number(
                    payment.tenant_id, scope="payment", prefix="PMT",
                    on_date=payment.payment_date, collision_model=Payment,
                )
                payment.save(update_fields=["number", "updated_at"])

        headers = self.get_success_headers(serializer.data)
        return Response(
            self.get_serializer(payment).data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    # -- allocation ---------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="apply")
    def apply(self, request, pk=None):
        """``POST /payments/{id}/apply`` — allocate this receipt to invoices.

        The whole allocation commits or none of it does. A cheque split across
        six invoices that stops after the third leaves the receipt half
        applied and the customer's statement wrong in a way nobody can see
        from either side.

        Each invoice's ``amount_paid``/``amount_due``/``status`` is then
        **recomputed** by ``apps.sales.services.invoice_workflow.apply_payment``
        from the ``PaymentApplication`` rows rather than incremented here.
        Incrementing is the read-modify-write that loses a concurrent
        allocation, and it drifts silently because there is no second source to
        check it against.
        """
        payment = self.get_object()
        body = ApplyPaymentSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        allocations = body.validated_data["allocations"]
        applied_on = body.validated_data.get("applied_on") or timezone.localdate()

        if payment.status not in APPLICABLE_STATUSES:
            raise DomainError(
                f"Payment {payment.number or payment.id} is "
                f"{payment.get_status_display().lower()}. Only captured or "
                f"later receipts represent money that has moved; applying an "
                f"authorisation would settle an invoice against a hold that "
                f"can still expire."
            )

        from apps.sales.services.invoice_workflow import apply_payment

        def run(_key: Optional[str]) -> Payment:
            locked = (
                Payment.all_tenants.select_for_update()
                .filter(pk=payment.pk, tenant_id=payment.tenant_id)
                .first()
            )
            if locked is None:  # pragma: no cover - get_object already checked
                raise NotFound("Payment not found in this tenant.")

            requested = sum((row["amount"] for row in allocations), ZERO)
            requested = quantize_currency(requested, locked.currency)
            if requested > locked.unapplied_amount:
                raise DomainError(
                    f"Allocating {requested} exceeds the {locked.unapplied_amount} "
                    f"still unapplied on payment {locked.number or locked.id}. "
                    f"The surplus belongs on the payment as credit on account, "
                    f"not on an invoice."
                )

            rows: list[PaymentApplication] = []
            for row in allocations:
                invoice = Invoice.objects.filter(pk=row["invoice"]).first()
                if invoice is None:
                    raise NotFound(f"Invoice {row['invoice']} not found in this tenant.")
                if invoice.customer_id != locked.customer_id:
                    raise DomainError(
                        f"Invoice {invoice.number or invoice.id} belongs to a "
                        f"different customer. Applying one customer's receipt to "
                        f"another's invoice makes both statements wrong."
                    )
                if invoice.currency != locked.currency:
                    # Expressible in the schema (exchange_rate_used,
                    # fx_gain_loss_amount) but not yet posted to the ledger.
                    # Refusing beats writing an allocation whose realised FX
                    # difference never reaches the GL.
                    raise DomainError(
                        f"Invoice {invoice.number or invoice.id} is in "
                        f"{invoice.currency} and the receipt is in "
                        f"{locked.currency}. Cross-currency allocation realises "
                        f"an FX gain or loss that must be posted, and that "
                        f"posting is not implemented — refusing rather than "
                        f"leaving the difference off the ledger."
                    )
                amount = quantize_currency(row["amount"], invoice.currency)
                rows.append(
                    PaymentApplication(
                        tenant_id=locked.tenant_id,
                        payment=locked,
                        invoice=invoice,
                        amount=amount,
                        applied_on=applied_on,
                        exchange_rate_used=Decimal("1"),
                        fx_gain_loss_amount=ZERO,
                        created_by_id=getattr(request.user, "id", None),
                    )
                )

            PaymentApplication.objects.bulk_create(rows)

            for row in rows:
                # Recompute, never increment. See the docstring.
                apply_payment(
                    row.invoice_id,
                    tenant_id=locked.tenant_id,
                    user_id=getattr(request.user, "id", None),
                )

            total_applied = (
                PaymentApplication.all_tenants.filter(
                    tenant_id=locked.tenant_id, payment_id=locked.pk, is_reversed=False
                ).aggregate(total=Sum("amount"))["total"]
                or ZERO
            )
            locked.unapplied_amount = quantize_currency(
                locked.amount - total_applied, locked.currency
            )
            locked.updated_by_id = getattr(request.user, "id", None)
            locked.save(update_fields=["unapplied_amount", "updated_by", "updated_at"])
            return locked

        return self.run_idempotent(request, transition="apply", run=run)

    @action(detail=True, methods=["get"], url_path="applications")
    def applications(self, request, pk=None):
        """What this receipt has been allocated to, and what is left over.

        A nested route rather than a filter on a top-level collection: the
        payment must be visible to the caller first, so the ABAC scope of the
        parent is always in play.
        """
        payment = self.get_object()
        rows = (
            PaymentApplication.objects.filter(payment=payment)
            .select_related("invoice")
            .order_by("applied_on", "invoice__number")
        )
        return Response(
            {
                "payment": str(payment.id),
                "amount": f"{payment.amount:f}",
                "unapplied_amount": f"{payment.unapplied_amount:f}",
                "applications": PaymentApplicationSerializer(
                    rows, many=True, context=self.get_serializer_context()
                ).data,
            }
        )

    # -- refunds ------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="refund")
    def refund(self, request, pk=None):
        """``POST /payments/{id}/refund`` — return money to the customer.

        Creates a :class:`~apps.payments.models.Refund` in ``PENDING``. It is
        deliberately *not* marked succeeded here: for a gateway payment the
        provider decides, asynchronously, and its webhook is what moves the row
        to ``SUCCEEDED`` or ``FAILED``. Optimistically marking it succeeded
        would tell the customer their money is on the way before anyone has
        agreed to send it, and would leave a ``FAILED`` refund looking settled.

        ``SUM(refunds.amount) <= payment.amount`` cannot be a check constraint
        (it spans rows), so it is enforced here under a row lock on the parent
        payment — a bare SELECT would let two concurrent refunds each see the
        other's absence and together over-refund.
        """
        payment = self.get_object()
        body = RefundRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        if payment.status not in APPLICABLE_STATUSES:
            raise DomainError(
                f"Payment {payment.number or payment.id} is "
                f"{payment.get_status_display().lower()}; there is nothing to "
                f"refund until money has actually been captured."
            )

        def run(key: Optional[str]) -> Refund:
            locked = (
                Payment.all_tenants.select_for_update()
                .filter(pk=payment.pk, tenant_id=payment.tenant_id)
                .first()
            )
            already = (
                Refund.all_tenants.filter(
                    tenant_id=locked.tenant_id,
                    payment_id=locked.pk,
                    status__in=LIVE_REFUND_STATUSES,
                ).aggregate(total=Sum("amount"))["total"]
                or ZERO
            )
            refundable = quantize_currency(locked.amount - already, locked.currency)
            requested = body.validated_data.get("amount")
            amount = quantize_currency(
                requested if requested is not None else refundable, locked.currency
            )
            if amount <= ZERO:
                raise DomainError(
                    f"Payment {locked.number or locked.id} has already been "
                    f"refunded in full."
                )
            if amount > refundable:
                raise DomainError(
                    f"Refunding {amount} would exceed the {refundable} still "
                    f"refundable on payment {locked.number or locked.id} "
                    f"({already} has already been returned)."
                )

            refund_date = body.validated_data.get("refund_date") or timezone.localdate()
            refund = Refund(
                tenant_id=locked.tenant_id,
                payment=locked,
                refund_date=refund_date,
                currency=locked.currency,
                amount=amount,
                reason=(body.validated_data.get("reason") or "")[:255],
                fee_refunded_amount=body.validated_data.get("fee_refunded_amount")
                or ZERO,
                status=Refund.Status.PENDING,
                idempotency_key=key or "",
                created_by_id=getattr(request.user, "id", None),
                updated_by_id=getattr(request.user, "id", None),
            )
            refund.save()
            refund.number = allocate_document_number(
                locked.tenant_id, scope="refund", prefix="REF",
                on_date=refund_date, collision_model=Refund,
            )
            refund.save(update_fields=["number", "updated_at"])
            return refund

        return self.run_idempotent(
            request,
            transition="refund",
            run=run,
            serializer_for=lambda obj: RefundSerializer(
                obj, context=self.get_serializer_context()
            ),
        )


# ---------------------------------------------------------------------------
# Refunds and webhook events
# ---------------------------------------------------------------------------

class RefundViewSet(ReadOnlyTenantViewSet):
    """Refunds are read-only here; they are created through their payment.

    ``POST /refunds/`` would let a refund be raised with no parent lock and no
    ``SUM(refunds) <= payment.amount`` check, which is exactly the race the
    sub-resource exists to close. The list endpoint stays because "what have we
    refunded this month" is a real question with no other home.
    """

    permission_domain = "banking"
    resource = "refund"
    queryset = Refund.objects.all()
    serializer_class = RefundSerializer
    select_related = ("payment", "payment__customer", "journal_entry")
    filterset_fields = ("status", "payment", "currency", "refund_date")
    search_fields = ("number", "reason", "gateway_refund_id")
    ordering_fields = ("refund_date", "amount", "created_at")
    ordering = ("-refund_date", "-created_at")


class WebhookEventViewSet(RbacOnlyQuerysetMixin, ReadOnlyTenantViewSet):
    """The provider events we received, in the order we received them.

    Stored before they are processed: a crash between "acted on" and
    "recorded" is how a payment gets applied twice, so the row is written
    first and processing is a separate, retryable step.
    """

    permission_domain = "banking"
    resource = "webhook_event"
    queryset = WebhookEvent.objects.all()
    serializer_class = WebhookEventSerializer
    select_related = ("gateway", "payment", "refund")
    filterset_fields = ("status", "event_type", "gateway", "signature_verified")
    search_fields = ("provider_event_id", "event_type", "last_error")
    ordering_fields = ("received_at", "processed_at", "attempts")
    ordering = ("-received_at",)
    extra_permissions = {"replay": ["banking.webhook_event.replay"]}

    @action(detail=True, methods=["post"], url_path="replay")
    def replay(self, request, pk=None):
        """``POST /webhook-events/{id}/replay`` — **not implemented**.

        Mounted so the contract exists. Replaying safely needs the event
        processor to be idempotent on ``provider_event_id`` first; without
        that, replaying a ``charge.succeeded`` records the receipt a second
        time, which is the precise failure the stored-event design is meant to
        prevent.
        """
        self.get_object()
        raise NotImplementedYet(
            "Webhook replay is not implemented yet. It must run through an "
            "event processor that is idempotent on provider_event_id, or a "
            "replay duplicates the money movement the event describes."
        )


__all__ = [
    "PaymentViewSet",
    "RefundViewSet",
    "WebhookEventViewSet",
    "PaymentGatewayConfigViewSet",
    "MissingIdempotencyKey",
]
