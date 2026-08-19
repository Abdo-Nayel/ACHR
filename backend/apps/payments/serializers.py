"""
Serializers for money received, allocated and returned.

Three things are deliberately not writable, and each one is a control rather
than a style choice:

* **``status``.** A payment moves ``PENDING -> AUTHORIZED -> CAPTURED ->
  SETTLED`` and only ``CAPTURED`` or later reduces a receivable. A writable
  status column lets a client mark an authorisation "settled", which settles
  invoices against money that never moved and that the gateway will happily
  let expire. Movement is a POST sub-resource; see ``apps.core.viewsets``.
* **``unapplied_amount``.** It is ``amount - SUM(applications.amount)`` and is
  maintained by ``POST /payments/{id}/apply``. Accepting it from the body lets
  a caller claim credit on account that no receipt backs.
* **``journal_entry``.** The ledger link is written by the posting service, in
  the same transaction as the entry. A client-supplied value would let a
  payment point at somebody else's entry.

Every monetary field uses :class:`apps.core.serializers.MoneyField` and
therefore crosses the wire as a JSON *string*.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import serializers

from apps.core.fields import ZERO
from apps.core.serializers import (
    MoneyField,
    RateField,
    ReadOnlyModelSerializer,
    TenantScopedSerializer,
    TransitionSerializer,
)
from apps.payments.models import (
    Payment,
    PaymentApplication,
    PaymentGatewayConfig,
    Refund,
    WebhookEvent,
)


# ---------------------------------------------------------------------------
# Gateway configuration
# ---------------------------------------------------------------------------

class PaymentGatewayConfigSerializer(TenantScopedSerializer):
    """A configured payment provider.

    ``credentials`` is **absent from ``fields`` entirely**, not merely
    read-only. A read-only secret is still a secret that every GET of the
    list endpoint puts into a browser cache, a proxy log and a support
    screenshot. Rotating a key is ``banking.gateway_config.rotate_secret``,
    which is a different authority from reading the config.
    """

    class Meta:
        model = PaymentGatewayConfig
        fields = (
            "id", "provider", "display_name", "is_active", "is_default",
            "is_test_mode", "webhook_endpoint_id", "clearing_account",
            "fee_account", "created_at", "updated_at",
        )


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

class PaymentApplicationSerializer(ReadOnlyModelSerializer):
    """One allocation of a receipt to one invoice. Immutable by construction.

    Un-applying is not an edit of this row: it is a reversal plus a
    recomputation of both sides, because the invoice's ``amount_paid`` is a
    cache of these rows and must be re-derived, never adjusted.
    """

    amount = MoneyField(read_only=True)
    exchange_rate_used = RateField(read_only=True)
    fx_gain_loss_amount = MoneyField(read_only=True)
    invoice_number = serializers.CharField(source="invoice.number", read_only=True)

    class Meta:
        model = PaymentApplication
        fields = (
            "id", "payment", "invoice", "invoice_number", "amount",
            "applied_on", "exchange_rate_used", "fx_gain_loss_amount",
            "is_reversed", "reversed_at", "created_at",
        )


class PaymentSerializer(TenantScopedSerializer):
    """Money received from a customer, independent of what it settles.

    ``amount`` is the gross the customer paid; ``fee_amount`` is what the
    processor kept and is expensed separately. Netting the fee off the receipt
    would settle the invoice short and leave a permanent unexplained residue
    on the AR control account.
    """

    server_owned_fields = (
        "status", "number", "unapplied_amount", "journal_entry",
        "captured_at", "settled_at", "failure_code", "failure_message",
        "gateway_transaction_id",
    )

    amount = MoneyField(min_value=Decimal("0.000001"))
    fee_amount = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)
    unapplied_amount = MoneyField(read_only=True)
    applied_amount = MoneyField(read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    applications = PaymentApplicationSerializer(many=True, read_only=True)

    class Meta:
        model = Payment
        fields = (
            "id", "number", "customer", "customer_name", "payment_date",
            "currency", "amount", "fee_amount", "unapplied_amount",
            "applied_amount", "method", "gateway", "gateway_transaction_id",
            "status", "idempotency_key", "journal_entry", "deposit_account",
            "reference", "failure_code", "failure_message", "captured_at",
            "settled_at", "notes", "applications", "created_at", "created_by",
            "updated_at",
        )
        # Client-supplied and unique per tenant: it is the anti-double-charge
        # key, so it is writable on create and never afterwards.
        extra_kwargs = {"idempotency_key": {"required": False, "allow_blank": True}}

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        if instance is not None:
            raise serializers.ValidationError(
                f"Payment {instance.number or instance.id} has been recorded and "
                f"cannot be edited. A receipt that turns out to be wrong is "
                f"refunded or reversed, never rewritten — the customer's bank "
                f"statement is the other half of this record."
            )

        customer = attrs.get("customer")
        currency = attrs.get("currency")
        if customer is not None and currency and customer.currency != currency:
            raise serializers.ValidationError(
                {"currency": (
                    f"Customer {customer.code} trades in {customer.currency}; "
                    f"recording a {currency} receipt against them creates an FX "
                    f"exposure nobody chose."
                )}
            )

        gateway = attrs.get("gateway")
        key = (attrs.get("idempotency_key") or "").strip()
        if gateway is not None and not key:
            # Mirrors ck_payment_gateway_has_idempotency_key. Failing here names
            # the cause instead of surfacing an opaque IntegrityError.
            raise serializers.ValidationError(
                {"idempotency_key": (
                    "A gateway payment must carry an idempotency key: that is "
                    "the only thing standing between a retried request and a "
                    "customer charged twice."
                )}
            )

        account = attrs.get("deposit_account")
        if account is not None and not getattr(account, "is_postable", True):
            raise serializers.ValidationError(
                {"deposit_account": (
                    f"Account {account.code} is a heading, not a postable "
                    f"account. Money cannot land on a total."
                )}
            )
        return attrs

    def create(self, validated_data: dict) -> Payment:
        from django.utils import timezone

        # A new receipt is entirely unallocated until POST {id}/apply says
        # otherwise. Deriving it here rather than accepting it keeps
        # ck_payment_unapplied_within_amount true by construction.
        validated_data["unapplied_amount"] = validated_data["amount"]

        # Offline receipts land SETTLED; gateway receipts start PENDING and
        # walk the authorise/capture/settle chain under the provider's
        # webhooks. This is the model's own rule (``Payment`` docstring:
        # "there is no authorisation hold on a banknote") and it has to be
        # applied *here*, at the only place an offline receipt is created —
        # leaving cash in PENDING would mean it never reduces a receivable,
        # because ``reduces_receivables`` is false for PENDING and
        # ``POST {id}/apply`` refuses it. The money is in the till and the
        # invoice would stay open, with nothing anywhere reporting a problem.
        if validated_data.get("gateway") is None:
            validated_data["status"] = Payment.Status.SETTLED
            validated_data["settled_at"] = timezone.now()
        else:
            validated_data["status"] = Payment.Status.PENDING
        return super().create(validated_data)


class PaymentAllocationSerializer(serializers.Serializer):
    """One ``{invoice, amount}`` pair inside an apply request."""

    invoice = serializers.UUIDField()
    amount = MoneyField(min_value=Decimal("0.000001"))


class ApplyPaymentSerializer(TransitionSerializer):
    """Body for ``POST /payments/{id}/apply``.

    A list, not a single invoice: one cheque covering six invoices is the
    normal case, and allocating them one request at a time means six chances
    to stop half-way and leave the receipt partly applied.
    """

    allocations = PaymentAllocationSerializer(many=True)
    applied_on = serializers.DateField(required=False, allow_null=True)

    def validate_allocations(self, value: list[dict]) -> list[dict]:
        if not value:
            raise serializers.ValidationError(
                "Nothing to apply. Send at least one {invoice, amount} pair."
            )
        seen: set[Any] = set()
        for row in value:
            if row["invoice"] in seen:
                raise serializers.ValidationError(
                    f"Invoice {row['invoice']} appears twice. "
                    f"uq_payment_application_pair permits one application per "
                    f"(payment, invoice); send the combined amount instead."
                )
            seen.add(row["invoice"])
        return value


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

class RefundSerializer(TenantScopedSerializer):
    """Money returned to the customer against an earlier payment.

    Its own document, never a negative payment: negative amounts would break
    every non-negative constraint and every SUM in a cash report, and the
    providers themselves model a refund as a separate object with its own id
    and its own webhooks.
    """

    server_owned_fields = (
        "status", "number", "journal_entry", "gateway_refund_id",
        "failure_code", "failure_message",
    )

    amount = MoneyField(min_value=Decimal("0.000001"))
    fee_refunded_amount = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)
    payment_number = serializers.CharField(source="payment.number", read_only=True)

    class Meta:
        model = Refund
        fields = (
            "id", "number", "payment", "payment_number", "refund_date",
            "currency", "amount", "fee_refunded_amount", "reason",
            "gateway_refund_id", "status", "journal_entry", "idempotency_key",
            "failure_code", "failure_message", "created_at", "created_by",
            "updated_at",
        )


class RefundRequestSerializer(TransitionSerializer):
    """Body for ``POST /payments/{id}/refund``.

    ``amount`` is optional and defaults to the payment's full remaining
    refundable amount, because "refund this payment" is the common request and
    making the client compute the remainder is how a partial refund ends up
    over-refunding a payment that was already partly returned.
    """

    amount = MoneyField(min_value=Decimal("0.000001"), required=False, allow_null=True)
    refund_date = serializers.DateField(required=False, allow_null=True)
    fee_refunded_amount = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

class WebhookEventSerializer(ReadOnlyModelSerializer):
    """A raw provider event, stored before it is acted on.

    Read-only over the API: the tenant did not create these rows and must not
    be able to edit them, because they are the evidence that the gateway said
    what we acted on. Re-processing one is ``POST {id}/replay``.
    """

    class Meta:
        model = WebhookEvent
        fields = (
            "id", "gateway", "provider_event_id", "event_type",
            "signature_verified", "status", "attempts", "last_error",
            "received_at", "processed_at", "payment", "refund", "created_at",
        )


__all__ = [
    "PaymentGatewayConfigSerializer",
    "PaymentApplicationSerializer",
    "PaymentSerializer",
    "PaymentAllocationSerializer",
    "ApplyPaymentSerializer",
    "RefundSerializer",
    "RefundRequestSerializer",
    "WebhookEventSerializer",
]
