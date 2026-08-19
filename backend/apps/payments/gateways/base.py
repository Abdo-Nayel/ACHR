"""
The payment-gateway boundary.

Everything above this module speaks ``Decimal`` and ISO-4217 currency codes.
Everything below it speaks whatever the provider's SDK speaks — for most
card processors, an *integer number of minor units* (``1050`` for
``USD 10.50``). This module is the membrane between the two, and the single
rule it exists to enforce is:

    **Amounts are converted to integer minor units exactly once, here, at the
    boundary — never earlier, never twice.**

Why that rule, spelled out
--------------------------
``int(amount * 100)`` scattered through call sites fails in three ways at
once:

* It is wrong for currencies that do not have two minor units. JPY has none
  (``¥1050`` is ``1050``, not ``105000``) and KWD has three. Hard-coding 100
  overcharges Kuwaiti customers by 10x and undercharges Japanese ones by 100x.
  :func:`apps.core.fields.minor_units` knows the real exponent.
* It truncates instead of rounding. ``int(Decimal("10.499999") * 100)`` is
  ``1049``; the customer is charged one cent less than the invoice says, the
  invoice never reaches ``PAID``, and a dunning email goes out for 0.01.
  :func:`apps.core.fields.quantize_currency` rounds half-up, which is what tax
  authorities expect.
* Converting twice is silent and catastrophic. If a service layer already
  produced minor units and the gateway adapter converts again, the customer is
  charged 100x. Because the *type* is ``int`` on both sides, nothing catches
  it. Keeping the conversion in one function — :func:`to_minor_units` — makes
  a double conversion a code-review-visible act rather than an arithmetic
  accident.

The inverse conversion (:func:`from_minor_units`) is equally single-sited: an
amount coming *back* from a webhook is an integer that must become a Decimal
before it touches anything financial. Never ``float(cents) / 100``.

Adapter contract
----------------
Concrete gateways subclass :class:`PaymentGateway`, register themselves with
``@register_gateway("<provider>")`` and are constructed via
:func:`get_gateway`. They must:

* never raise a provider-specific exception past their own boundary — wrap it
  in a :class:`GatewayResult` with ``success=False`` and a stable
  ``error_code``, so the caller's error handling does not depend on which
  processor a tenant happens to use;
* be stateless per call and safe to retry, deriving idempotency from the
  caller-supplied :attr:`PaymentIntent.idempotency_key`;
* do no database work. They talk to the provider and return data. Persisting
  the result — and the decision about whether a journal entry may be posted —
  belongs to ``apps.payments.services``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Type

from apps.core.fields import ZERO, minor_units, quantize_currency, to_money


# ---------------------------------------------------------------------------
# Minor-unit conversion — the boundary, and the only place it happens
# ---------------------------------------------------------------------------

def to_minor_units(amount: Decimal, currency: str) -> int:
    """Decimal -> the provider's integer representation. Call once, here.

    Rounds to the currency's own precision first (half-up), then shifts. The
    two steps are separate on purpose: shifting an unrounded value and casting
    to ``int`` truncates, and a truncated charge is an invoice that can never
    be fully settled.
    """
    rounded = quantize_currency(to_money(amount), currency)
    return int(rounded.scaleb(minor_units(currency)))


def from_minor_units(value: int, currency: str) -> Decimal:
    """Provider integer -> Decimal. The inverse, and equally single-sited.

    ``value`` must be an ``int``. A float here means someone parsed the
    provider's JSON without ``parse_float=Decimal``, which is the bug this
    function refuses to launder.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"Minor units must be an int, got {type(value).__name__}. "
            f"Parse gateway JSON with parse_float=Decimal."
        )
    return to_money(Decimal(value).scaleb(-minor_units(currency)))


# ---------------------------------------------------------------------------
# Value objects crossing the boundary
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PaymentIntent:
    """What the application wants the gateway to do, in application terms.

    Amounts are ``Decimal`` and currency is ISO-4217 — deliberately *not*
    minor units. The adapter converts at the moment it builds the SDK call,
    so an intent can be logged, retried or replayed without anyone having to
    know whether it has already been converted.

    ``idempotency_key`` is mandatory and mirrors
    ``payments.Payment.idempotency_key``. Passing the same key to the same
    provider twice must yield the same charge, not a second one; that is the
    contract every major processor offers and the reason a retried webhook or
    a double-clicked button does not bill a customer twice.
    """

    amount: Decimal
    currency: str
    idempotency_key: str
    #: Provider-side customer/token handle, never a raw PAN. Card data must
    #: not enter this process: it drags the whole application into PCI-DSS
    #: scope. The browser tokenises directly with the provider.
    payment_method_token: str = ""
    customer_reference: str = ""
    description: str = ""
    #: True for auth-then-capture flows (goods shipped later). False captures
    #: immediately. Defaults to immediate capture because an uncaptured
    #: authorisation expires silently and the money never arrives.
    capture_immediately: bool = True
    return_url: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", to_money(self.amount, field_name="amount"))
        if self.amount <= ZERO:
            raise ValueError("A payment intent must be for a positive amount.")
        if len(self.currency or "") != 3:
            raise ValueError(f"Currency must be an ISO-4217 alpha-3 code, got {self.currency!r}.")
        if not self.idempotency_key:
            raise ValueError(
                "idempotency_key is required. Without it a retry — from the "
                "browser, the task queue, or the gateway itself — charges the "
                "customer a second time."
            )

    def minor_amount(self) -> int:
        """The amount as the provider wants it. THE conversion point."""
        return to_minor_units(self.amount, self.currency)


@dataclass(frozen=True, slots=True)
class GatewayResult:
    """What the gateway said, translated back into application terms.

    Uniform across providers so that ``apps.payments.services`` never branches
    on which processor a tenant uses. ``status`` is a
    ``payments.Payment.Status`` value, mapped by the adapter — a provider's
    ``"requires_capture"`` becomes ``"authorized"`` inside the adapter, not in
    a giant ``if provider == ...`` in the service layer.

    A failure is a *returned value*, not an exception: declines are ordinary
    business outcomes (roughly 5-10% of card traffic) and modelling them as
    exceptions makes every call site wrap everything in ``try``.
    """

    success: bool
    #: Payment.Status value.
    status: str = ""
    transaction_id: str = ""
    #: Amount actually moved, as a Decimal — already converted back from the
    #: provider's minor units by the adapter.
    amount: Decimal = ZERO
    currency: str = ""
    fee_amount: Decimal = ZERO
    #: Stable, provider-independent code (``card_declined``, ``expired_card``,
    #: ``insufficient_funds``, ``gateway_unavailable``). Dunning and retry
    #: logic branch on this; never on the human message.
    error_code: str = ""
    error_message: str = ""
    #: For 3-D Secure / redirect flows.
    requires_action: bool = False
    action_url: str = ""
    raw_response: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_retryable(self) -> bool:
        """Whether re-sending the same intent could plausibly succeed.

        A declined card will decline again; a timed-out network call may not.
        Retrying the former annoys the issuer's fraud system, and retrying the
        latter is safe only because the idempotency key makes it so.
        """
        return self.error_code in {
            "gateway_unavailable",
            "gateway_timeout",
            "rate_limited",
            "processing_error",
        }


@dataclass(frozen=True, slots=True)
class WebhookEnvelope:
    """A provider event, normalised. Produced by ``parse_webhook_event``.

    ``provider_event_id`` becomes ``payments.WebhookEvent.provider_event_id``
    and is the deduplication key that makes redelivery a no-op.
    """

    provider_event_id: str
    event_type: str
    #: Normalised to a Payment.Status where the event implies one, else "".
    mapped_status: str = ""
    transaction_id: str = ""
    amount: Optional[Decimal] = None
    currency: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


class GatewayError(Exception):
    """Configuration or programming error — NOT a decline.

    Reserved for "this gateway is not usable at all": missing credentials, an
    unsupported currency, an unknown provider. Declines travel as
    ``GatewayResult(success=False)`` instead, because they are expected.
    """


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class PaymentGateway(abc.ABC):
    """Abstract adapter for one payment provider.

    Constructed with the tenant's :class:`~apps.payments.models.PaymentGatewayConfig`
    row, from which it reads credentials (decrypted by the KMS field wrapper)
    and the webhook secret reference. One instance per operation; hold no
    mutable state between calls, because instances are created inside Celery
    tasks that may run concurrently for the same tenant.
    """

    #: Set by @register_gateway.
    provider: str = ""

    def __init__(self, config) -> None:
        self.config = config

    # -- capability checks --------------------------------------------------

    def assert_supports(self, currency: str) -> None:
        """Fail before the network call rather than after a rejected charge."""
        supported = getattr(self.config, "supported_currencies", None) or []
        if supported and currency.upper() not in {c.upper() for c in supported}:
            raise GatewayError(
                f"{self.provider} config '{self.config.display_name}' does not "
                f"support {currency}. Configure it or route to another gateway."
            )

    # -- money movement -----------------------------------------------------

    @abc.abstractmethod
    def create_charge(self, intent: PaymentIntent) -> GatewayResult:
        """Authorise (and optionally capture) ``intent``.

        Must pass ``intent.idempotency_key`` to the provider's own idempotency
        mechanism, and must convert the amount with ``intent.minor_amount()``
        — not with any local arithmetic.
        """

    @abc.abstractmethod
    def capture(self, transaction_id: str, amount: Decimal, currency: str) -> GatewayResult:
        """Capture a previously authorised charge, in full or in part.

        Separate from :meth:`create_charge` because an authorisation is not
        money: it must not be posted to the ledger, and it expires. Capture is
        the event that creates a receipt.
        """

    @abc.abstractmethod
    def refund(
        self,
        transaction_id: str,
        amount: Decimal,
        currency: str,
        *,
        idempotency_key: str,
        reason: str = "",
    ) -> GatewayResult:
        """Return funds against a captured charge.

        ``idempotency_key`` is explicit and required for the same reason as on
        the charge path: a retried refund task must not refund twice.
        """

    # -- inbound events -----------------------------------------------------

    @abc.abstractmethod
    def verify_webhook_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        """HMAC-verify the *raw* request body.

        ``raw_body`` is bytes straight off the socket, before any JSON parse.
        Re-serialising the parsed body changes key order and whitespace, the
        digest no longer matches, and the usual "fix" is to stop verifying —
        at which point anyone who knows the endpoint URL can mark invoices as
        paid.

        Implementations must use a constant-time comparison and must reject
        signatures whose timestamp is outside a short tolerance, otherwise a
        captured-and-replayed request stays valid forever.
        """

    @abc.abstractmethod
    def parse_webhook_event(self, payload: Mapping[str, Any]) -> WebhookEnvelope:
        """Normalise a provider payload into a :class:`WebhookEnvelope`.

        Pure and side-effect-free: no database writes, no state changes. The
        caller persists the envelope first and processes it asynchronously
        (see :class:`~apps.payments.models.WebhookEvent`).
        """


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_GATEWAY_REGISTRY: dict[str, Type[PaymentGateway]] = {}


def register_gateway(provider: str) -> Callable[[Type[PaymentGateway]], Type[PaymentGateway]]:
    """Class decorator binding an adapter to a ``GatewayProvider`` value.

    A registry rather than an ``if/elif`` factory so that adding a provider is
    one new module plus one import, with no edit to shared code — and so that
    a deployment can ship without an adapter it does not use.
    """

    def decorator(cls: Type[PaymentGateway]) -> Type[PaymentGateway]:
        key = provider.lower()
        existing = _GATEWAY_REGISTRY.get(key)
        # Re-registering the same class is harmless (module reimport under the
        # autoreloader); a *different* class silently shadowing another is not.
        if existing is not None and existing is not cls:
            raise GatewayError(
                f"Gateway '{key}' is already registered to {existing.__name__}."
            )
        cls.provider = key
        _GATEWAY_REGISTRY[key] = cls
        return cls

    return decorator


def get_gateway(provider: str, config) -> PaymentGateway:
    """Build the adapter for ``provider``, bound to a tenant's config row.

    Raises :class:`GatewayError` rather than returning ``None``: a missing
    adapter is a deployment fault, and returning ``None`` only moves the
    ``AttributeError`` to a less informative place.
    """
    key = (provider or "").lower()
    cls = _GATEWAY_REGISTRY.get(key)
    if cls is None:
        raise GatewayError(
            f"No payment gateway adapter registered for '{provider}'. "
            f"Registered: {sorted(_GATEWAY_REGISTRY)}."
        )
    return cls(config)


def registered_providers() -> list[str]:
    return sorted(_GATEWAY_REGISTRY)


# ---------------------------------------------------------------------------
# Reference implementation — SKELETON, not wired to the network
# ---------------------------------------------------------------------------

@register_gateway("stripe")
class StripeGateway(PaymentGateway):
    """Shape of a real adapter. **No network calls are implemented here.**

    This class exists to show where each responsibility goes, and to keep the
    registry non-empty in tests. Every method that would talk to Stripe raises
    ``NotImplementedError`` with a note naming the SDK call that belongs
    there. Do not "make it work" by adding requests calls inline — the real
    adapter lives in ``apps/payments/gateways/stripe.py``, is constructed with
    an injected SDK client so it can be faked in tests, and is the only file
    that ever imports the ``stripe`` package.

    Note what is *already correct* here even though the calls are stubs: the
    amount conversion happens exactly once, via ``intent.minor_amount()`` and
    :func:`from_minor_units`, and never inside a method body as
    ``amount * 100``.
    """

    #: Stripe's vocabulary -> ours. Kept as data so the mapping is auditable
    #: in one place instead of being spread across branches.
    STATUS_MAP: Mapping[str, str] = {
        "requires_payment_method": "pending",
        "requires_confirmation": "pending",
        "requires_action": "pending",
        "processing": "pending",
        "requires_capture": "authorized",
        "succeeded": "captured",
        "canceled": "failed",
    }

    EVENT_STATUS_MAP: Mapping[str, str] = {
        "payment_intent.succeeded": "captured",
        "payment_intent.payment_failed": "failed",
        "payment_intent.amount_capturable_updated": "authorized",
        "charge.refunded": "refunded",
        "charge.dispute.created": "disputed",
        "payout.paid": "settled",
    }

    def create_charge(self, intent: PaymentIntent) -> GatewayResult:
        self.assert_supports(intent.currency)
        # Boundary conversion — the ONLY place Decimal becomes an int here.
        amount_minor = intent.minor_amount()  # noqa: F841  (used by the real call)
        raise NotImplementedError(
            "Real implementation: stripe.PaymentIntent.create("
            "amount=amount_minor, currency=intent.currency.lower(), "
            "payment_method=intent.payment_method_token, "
            "capture_method='automatic' if intent.capture_immediately else 'manual', "
            "confirm=True, metadata=dict(intent.metadata), "
            "idempotency_key=intent.idempotency_key) — then map "
            "response.status through STATUS_MAP and convert response.amount "
            "back with from_minor_units()."
        )

    def capture(self, transaction_id: str, amount: Decimal, currency: str) -> GatewayResult:
        self.assert_supports(currency)
        amount_minor = to_minor_units(amount, currency)  # noqa: F841
        raise NotImplementedError(
            "Real implementation: stripe.PaymentIntent.capture("
            "transaction_id, amount_to_capture=amount_minor)."
        )

    def refund(
        self,
        transaction_id: str,
        amount: Decimal,
        currency: str,
        *,
        idempotency_key: str,
        reason: str = "",
    ) -> GatewayResult:
        self.assert_supports(currency)
        if not idempotency_key:
            raise GatewayError("Refunds require an idempotency key.")
        amount_minor = to_minor_units(amount, currency)  # noqa: F841
        raise NotImplementedError(
            "Real implementation: stripe.Refund.create(payment_intent=transaction_id, "
            "amount=amount_minor, reason=reason or None, "
            "idempotency_key=idempotency_key)."
        )

    def verify_webhook_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        raise NotImplementedError(
            "Real implementation: stripe.Webhook.construct_event(raw_body, "
            "headers['Stripe-Signature'], secret) where `secret` is fetched "
            "from the secret manager using config.webhook_secret_ref. Pass "
            "raw_body unmodified — Django's request.body, not request.POST — "
            "and let the SDK do the constant-time compare and the timestamp "
            "tolerance check."
        )

    def parse_webhook_event(self, payload: Mapping[str, Any]) -> WebhookEnvelope:
        """Pure mapping. Safe to implement fully: it touches no network.

        Included complete (unlike the methods above) to show the intended
        shape: read the provider's structure, convert money back from minor
        units exactly once, and hand a normalised envelope to the caller —
        which then *stores* it and returns 200 before any processing.
        """
        obj = (payload.get("data") or {}).get("object") or {}
        currency = str(obj.get("currency") or "").upper()

        amount: Optional[Decimal] = None
        raw_amount = obj.get("amount_received", obj.get("amount"))
        if isinstance(raw_amount, int) and not isinstance(raw_amount, bool) and currency:
            amount = from_minor_units(raw_amount, currency)

        event_type = str(payload.get("type") or "")
        return WebhookEnvelope(
            provider_event_id=str(payload.get("id") or ""),
            event_type=event_type,
            mapped_status=self.EVENT_STATUS_MAP.get(event_type, ""),
            transaction_id=str(obj.get("id") or ""),
            amount=amount,
            currency=currency,
            payload=payload,
        )
