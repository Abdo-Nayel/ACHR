"""
Non-viewset payment routes — mounted at ``/api/v1/payments/``.

    POST /payments/webhooks/<provider>/   gateway callback (no JWT, signed)

Why this prefix is outside the authenticated router
---------------------------------------------------
``config/urls.py`` mounts it separately and says why: a webhook is not a user
request. It carries no JWT, it has no tenant header, and it is authenticated by
the *provider's* signature over the raw body. Keeping it on a distinct prefix
means the CSRF exemption and the missing ``IsAuthenticated`` are one auditable
decision in one place rather than a permission class somebody has to notice on
a viewset.

The contract this endpoint must honour when it is implemented
-------------------------------------------------------------
1. **Verify the signature against the raw bytes, before parsing.**
   ``PaymentGateway.verify_webhook_signature(raw_body, headers)`` takes bytes
   for a reason: re-serialising the parsed JSON changes key order and
   whitespace, and the HMAC no longer matches. Parsing first also means
   untrusted input reaches the JSON decoder before anything has been
   authenticated.
2. **Store, then process.** Write the :class:`~apps.payments.models.WebhookEvent`
   row and return 200 immediately; act on it in a separate, retryable step. A
   crash between "acted on" and "recorded" is how a payment is applied twice,
   and providers retry aggressively — a slow handler earns duplicate
   deliveries, not patience.
3. **Deduplicate on ``provider_event_id``.** ``uq_webhook_event_provider_id``
   is the guarantee; the application check is only the friendly message.
4. **Resolve the tenant from the endpoint, never from the payload.** A body
   field naming the tenant is attacker-controlled. The gateway configuration
   the endpoint id belongs to is not.

Mounted and answering 501 rather than absent: the route, its shape and its
schema entry are what the provider console and the frontend are configured
against, and a 404 during integration reads as a routing bug that costs
somebody an afternoon. It deliberately does not accept-and-drop — a 200 that
silently discards a ``charge.succeeded`` loses a customer's money with no
error anywhere.
"""

from __future__ import annotations

from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.accounting.viewsets import NotImplementedYet
from apps.core.throttling import BurstThrottle


@method_decorator(csrf_exempt, name="dispatch")
class GatewayWebhookView(APIView):
    """``POST /payments/webhooks/<provider>/`` — **not implemented**.

    Unauthenticated by design (see the module docstring) and throttled, so an
    unimplemented public endpoint cannot be used as an amplification target
    while the real handler is being written.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [BurstThrottle]
    throttle_scope = "webhook"

    @extend_schema(request=None, responses={501: None})
    def post(self, request, provider: str):
        raise NotImplementedYet(
            f"The {provider} webhook receiver is not implemented yet. It must "
            f"verify the provider signature over the raw request body before "
            f"parsing, store the event and return 200, then process it "
            f"separately and idempotently on provider_event_id. Returning 200 "
            f"and discarding the event instead would lose money silently."
        )


urlpatterns = [
    path("webhooks/<str:provider>/", GatewayWebhookView.as_view(), name="webhook"),
]
