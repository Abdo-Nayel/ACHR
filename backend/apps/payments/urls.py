"""
Payments URL registration.

``payments`` and ``refunds`` keep their prefixes; ``payment-gateways`` is new,
because :class:`~apps.payments.viewsets.PaymentGatewayConfigViewSet` had no
route and a tenant could therefore not see which providers were configured.

Note the asymmetry: ``POST /payments/`` exists and ``POST /refunds/`` does
not. A refund has to be raised against a locked parent payment so that
``SUM(refunds) <= payment.amount`` holds under concurrency, so it is
``POST /payments/{id}/refund`` and the refund collection is read-only.
"""

from __future__ import annotations

from apps.payments.viewsets import (
    PaymentGatewayConfigViewSet,
    PaymentViewSet,
    RefundViewSet,
    WebhookEventViewSet,
)


def register(router) -> None:
    """Mount this app's routes on the v1 router."""
    router.register(r"payments", PaymentViewSet, basename="payments")
    router.register(r"refunds", RefundViewSet, basename="refunds")
    router.register(r"webhook-events", WebhookEventViewSet, basename="webhook-events")
    router.register(
        r"payment-gateways", PaymentGatewayConfigViewSet, basename="payment-gateways"
    )
