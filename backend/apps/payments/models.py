"""
Cash in: receipts, their gateway plumbing, and how they attach to invoices.

Three separate concerns live here, and keeping them separate is what makes
reconciliation possible:

1. **The money event** (:class:`Payment`) — "we received 1,000 USD from ACME
   on 3 March by card". This is true regardless of which invoices it settles,
   and it is what the bank statement will show.
2. **The allocation** (:class:`PaymentApplication`) — "of that 1,000, apply
   600 to INV-0041 and 400 to INV-0043". Allocation is an accounting decision
   that can change (a misapplied receipt is re-applied) without the money
   event changing.
3. **The gateway conversation** (:class:`PaymentGatewayConfig`,
   :class:`WebhookEvent`, :class:`Refund`) — an unreliable, retrying, at-least-
   once channel that must never be allowed to create money twice.

Collapsing (1) and (2) into a single "payment has an invoice FK" is the most
common design error in this domain. It makes partial payments, batch receipts
covering ten invoices, and unapplied credit on account all unrepresentable,
and every system that starts there eventually grows this table anyway — after
a migration nobody enjoys.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.core.fields import MoneyField, RateField
from apps.core.models import (
    Currency,
    ImmutableFinancialModel,
    StatusTransitionMixin,
    TenantScopedModel,
)


# ---------------------------------------------------------------------------
# Gateway configuration
# ---------------------------------------------------------------------------

class GatewayProvider(models.TextChoices):
    STRIPE = "stripe", "Stripe"
    PAYPAL = "paypal", "PayPal"
    BRAINTREE = "braintree", "Braintree"
    AUTHORIZE_NET = "authorize_net", "Authorize.Net"
    #: Not a gateway at all — the "we took a cheque" path. Modelled as a
    #: provider so that every Payment has a config to hang a clearing account
    #: off, and so reporting does not need a NULL branch everywhere.
    MANUAL = "manual", "Manual / offline"


class PaymentGatewayConfig(TenantScopedModel):
    """One tenant's connection to one payment provider.

    Per-tenant, not per-installation: each customer organisation collects into
    *their* merchant account. A shared platform-level key would pool every
    tenant's funds into ours, which is a licensing problem long before it is
    an engineering one.

    Accounts
    --------
    ``clearing_account`` is the undeposited-funds / gateway-clearing account.
    Money authorised by a gateway is **not** in the bank yet: Stripe pays out
    on a rolling delay, net of fees, in a batch that bundles dozens of
    charges. Posting a card receipt straight to the bank account guarantees
    the bank reconciliation never balances. Instead:

        capture:  Dr Gateway clearing / Cr Accounts receivable
        payout:   Dr Bank / Dr Fee expense / Cr Gateway clearing

    The clearing account's balance is then exactly "authorised but not yet
    deposited", which is a number the finance team can verify against the
    provider's dashboard.

    ``fee_account`` receives the processor's cut, which must be recognised as
    an expense gross rather than netted off revenue — netting understates both
    revenue and cost, and misstates the VAT base in jurisdictions where the
    fee is separately taxable.
    """

    provider = models.CharField(
        max_length=16, choices=GatewayProvider.choices, db_index=True
    )
    display_name = models.CharField(
        max_length=100, help_text="Shown on the checkout page and in reports."
    )
    is_active = models.BooleanField(default=True)
    #: The config offered by default on new payment links. Exactly one per
    #: tenant, enforced by a partial unique index below.
    is_default = models.BooleanField(default=False)
    #: Sandbox credentials must never post real money into the real ledger;
    #: the posting service refuses a test-mode payment against a live tenant.
    is_test_mode = models.BooleanField(default=False)

    credentials = models.JSONField(
        default=dict,
        blank=True,
        help_text="ENCRYPTED AT REST — see the class docstring before reading.",
    )
    """
    ``credentials`` holds API keys, merchant ids and OAuth refresh tokens.

    It is a plain ``JSONField`` at the Django level but the column is wrapped
    by a field-level KMS envelope: values are encrypted with a per-tenant data
    key, the data key is itself encrypted by the KMS master key, and only the
    ciphertext plus key id reaches PostgreSQL. Consequences that matter to
    anyone touching this model:

    * You cannot filter or index on the contents. ``credentials__api_key`` in
      a queryset returns nothing useful — the stored bytes are ciphertext.
    * A database dump, a replica, and a PITR backup all contain only
      ciphertext, so a stolen backup is not a stolen merchant account.
    * Rotation is a re-encrypt of the data key, not a re-entry of the
      credentials by the tenant.
    * Never log this field. Never put it in a serializer. Never include it in
      an error message or a Sentry breadcrumb.
    """

    #: A *reference* to the webhook signing secret in the secret manager
    #: (e.g. "kms://tenants/{id}/stripe/whsec"), never the secret itself.
    #: Signature verification fetches it at request time so a rotation takes
    #: effect immediately without rewriting rows.
    webhook_secret_ref = models.CharField(max_length=255, blank=True)
    webhook_endpoint_id = models.CharField(max_length=255, blank=True)

    #: ISO-4217 alpha-3. An array rather than a join table because the list is
    #: short, always read whole, and never joined against.
    supported_currencies = ArrayField(
        base_field=models.CharField(max_length=3, choices=Currency.choices),
        default=list,
        blank=True,
    )

    clearing_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="gateway_clearing_configs",
        help_text="Undeposited funds / gateway clearing (an asset).",
    )
    fee_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="gateway_fee_configs",
        help_text="Processing fee expense.",
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "payments_gateway_config"
        ordering = ["provider", "display_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "provider", "display_name"],
                name="uq_gateway_config_name",
            ),
            # "Only one default" as a partial unique index. Doing this in
            # application code means two concurrent "make this the default"
            # clicks leave a tenant with two defaults and a checkout page that
            # picks one at random.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_default=True),
                name="uq_gateway_config_single_default",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "provider", "is_active"], name="ix_gateway_active"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.get_provider_display()} — {self.display_name}"


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

class Payment(StatusTransitionMixin, ImmutableFinancialModel):
    """Money received from a customer, independent of what it settles.

    Lifecycle
    ---------
    ``PENDING -> AUTHORIZED -> CAPTURED -> SETTLED`` on the happy card path,
    with ``FAILED``, ``REFUNDED`` / ``PARTIALLY_REFUNDED`` and ``DISPUTED`` as
    the ways it goes wrong. Offline methods (cash, cheque, transfer) skip
    straight to ``SETTLED`` — there is no authorisation hold on a banknote.

    The distinction that earns its keep is **AUTHORIZED vs CAPTURED**: an
    authorisation is a promise (the customer's limit is reduced, no money has
    moved, it expires in ~7 days) and must NOT hit the ledger. Only capture
    creates a receivable-reducing entry. Systems that post on authorisation
    show revenue for orders that were never charged.

    ``SETTLED`` means the funds reached the tenant's bank in a payout. It is
    tracked separately from ``CAPTURED`` because the gap between them is
    exactly the balance of the gateway clearing account.

    ``DISPUTED`` is not terminal and not a status the tenant chooses — the
    gateway asserts it via webhook. It can resolve back to ``CAPTURED``
    (dispute won) or forward to ``REFUNDED`` (lost).

    Money invariants
    ----------------
    * ``unapplied_amount`` is the part of this receipt not yet allocated to an
      invoice. It equals ``amount - SUM(applications.amount)`` and is stored so
      that "customer credit on account" is a single indexed read.
    * ``fee_amount`` is what the processor kept. Gross ``amount`` is what the
      customer paid and what settles the invoice; the fee is an expense, never
      a reduction of the receipt.

    Idempotency
    -----------
    ``idempotency_key`` is the single defence against charging a customer
    twice. It is required (enforced by a check constraint) for every payment
    that came through a gateway, because that is the path where retries
    happen: a browser double-submit, a client-side retry after a timeout, or a
    webhook redelivery all arrive with the same key and the second one loses
    the race against the unique index instead of creating a second charge.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        AUTHORIZED = "authorized", "Authorized"
        CAPTURED = "captured", "Captured"
        SETTLED = "settled", "Settled"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially refunded"
        DISPUTED = "disputed", "Disputed"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        # Offline receipts are created straight into SETTLED; the gateway path
        # walks the chain. PENDING -> CAPTURED covers providers that authorise
        # and capture in one call (PayPal express, wallets).
        Status.PENDING: {Status.AUTHORIZED, Status.CAPTURED, Status.FAILED},
        # An expired or cancelled authorisation FAILS; it never "settles".
        Status.AUTHORIZED: {Status.CAPTURED, Status.FAILED},
        Status.CAPTURED: {
            Status.SETTLED,
            Status.REFUNDED,
            Status.PARTIALLY_REFUNDED,
            Status.DISPUTED,
            # Rare, but real: a capture reversed by the acquirer before payout.
            Status.FAILED,
        },
        Status.SETTLED: {
            Status.REFUNDED,
            Status.PARTIALLY_REFUNDED,
            Status.DISPUTED,
        },
        Status.PARTIALLY_REFUNDED: {Status.REFUNDED, Status.DISPUTED},
        # A dispute resolves either way; the gateway tells us which.
        Status.DISPUTED: {Status.CAPTURED, Status.SETTLED, Status.REFUNDED},
        Status.FAILED: set(),
        Status.REFUNDED: set(),
    }

    class Method(models.TextChoices):
        CARD = "card", "Card"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        CASH = "cash", "Cash"
        CHEQUE = "cheque", "Cheque"
        WALLET = "wallet", "Digital wallet"
        GATEWAY = "gateway", "Gateway (other)"

    #: Gapless per-tenant receipt number, allocated when the payment is
    #: recorded (unlike an invoice number, there is no draft stage to abandon).
    number = models.CharField(max_length=32, blank=True)
    customer = models.ForeignKey(
        "sales.Customer", on_delete=models.PROTECT, related_name="payments"
    )

    payment_date = models.DateField(db_index=True)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    amount = MoneyField(help_text="Gross amount received from the customer.")
    #: amount - SUM(applications.amount). Positive = credit on account.
    unapplied_amount = MoneyField()
    fee_amount = MoneyField(help_text="Processor fee, expensed gross.")

    method = models.CharField(max_length=16, choices=Method.choices, db_index=True)
    gateway = models.ForeignKey(
        PaymentGatewayConfig,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payments",
        help_text="NULL for offline receipts.",
    )
    #: The provider's own id (Stripe ``pi_...`` / ``ch_...``). Not unique in
    #: the schema across providers, but unique per gateway config below — it is
    #: how a webhook finds the payment it is talking about.
    gateway_transaction_id = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    #: Client-supplied, replayed on retry. NOT NULL (non-blank) for gateway
    #: payments — see the class docstring.
    idempotency_key = models.CharField(max_length=128, blank=True)

    journal_entry = models.OneToOneField(
        "accounting.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payment",
    )
    #: Where the money landed: a bank/cash account for offline receipts, the
    #: gateway clearing account for card receipts. PROTECT because the posted
    #: entry references it and history must stay explicable.
    deposit_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="deposited_payments",
    )

    reference = models.CharField(
        max_length=100, blank=True, help_text="Cheque number, wire reference."
    )
    #: Machine-readable decline reason (``card_declined``, ``insufficient_funds``)
    #: kept apart from the human message so dunning logic can branch on it
    #: without string-matching a localised sentence.
    failure_code = models.CharField(max_length=64, blank=True)
    failure_message = models.TextField(blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "payments_payment"
        ordering = ["-payment_date", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"],
                condition=~models.Q(number=""),
                name="uq_payment_number",
            ),
            # The anti-double-charge index. Everything else about idempotency
            # is convenience; this is the guarantee.
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uq_payment_idempotency",
            ),
            models.UniqueConstraint(
                fields=["gateway", "gateway_transaction_id"],
                condition=~models.Q(gateway_transaction_id=""),
                name="uq_payment_gateway_txn",
            ),
            # Gateway payments MUST carry a key; offline receipts need not.
            models.CheckConstraint(
                condition=models.Q(gateway__isnull=True)
                | ~models.Q(idempotency_key=""),
                name="ck_payment_gateway_has_idempotency_key",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0)
                & models.Q(fee_amount__gte=0)
                & models.Q(unapplied_amount__gte=0),
                name="ck_payment_amounts_valid",
            ),
            # Cannot have allocated more than was received.
            models.CheckConstraint(
                condition=models.Q(unapplied_amount__lte=models.F("amount")),
                name="ck_payment_unapplied_within_amount",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="captured")
                | models.Q(captured_at__isnull=False),
                name="ck_payment_captured_has_timestamp",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="failed") | ~models.Q(failure_code=""),
                name="ck_payment_failed_has_code",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status", "payment_date"], name="ix_payment_status"),
            models.Index(fields=["tenant", "customer"], name="ix_payment_customer"),
            models.Index(fields=["tenant", "method"], name="ix_payment_method"),
            models.Index(fields=["tenant", "gateway"], name="ix_payment_gateway"),
            models.Index(
                fields=["gateway_transaction_id"], name="ix_payment_gateway_txn_lookup"
            ),
            # "Credit on account" report and the auto-application job.
            models.Index(
                fields=["tenant", "customer"],
                condition=models.Q(unapplied_amount__gt=0),
                name="ix_payment_unapplied",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.number or f"payment {self.id}"

    @property
    def applied_amount(self):
        return self.amount - self.unapplied_amount

    @property
    def reduces_receivables(self) -> bool:
        """Only captured-or-later money settles an invoice. Authorisations do
        not: no funds have moved and the hold may simply expire."""
        return self.status in {
            self.Status.CAPTURED,
            self.Status.SETTLED,
            self.Status.PARTIALLY_REFUNDED,
            self.Status.DISPUTED,
        }


class PaymentApplication(ImmutableFinancialModel):
    """The allocation of part of a payment to one invoice.

    This is the many-to-many that makes real-world receipts representable:
    one cheque covering six invoices, and one invoice paid in three
    instalments, are both just several rows here.

    ``UNIQUE (payment, invoice)`` rather than allowing several rows for the
    same pair: two partial applications of the same payment to the same
    invoice carry no information a single summed row does not, and permitting
    them turns "how much of payment X went to invoice Y?" into an aggregate
    that every caller would have to remember to write.

    ``amount > 0`` strictly. A zero application is noise; a negative one is
    someone trying to express an un-application, which must be a deletion of
    this row plus a recomputation, not a compensating entry that leaves the
    sum right and the audit trail wrong.

    Note this is an ``ImmutableFinancialModel``: un-applying is done by the
    reversal path in ``payments.services``, which reverses the GL entry and
    recomputes both sides, never by ``delete()``.
    """

    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, related_name="applications"
    )
    invoice = models.ForeignKey(
        "sales.Invoice", on_delete=models.PROTECT, related_name="payment_applications"
    )
    #: In the *invoice's* currency. A cross-currency application also records
    #: the rate used, because the FX gain/loss it creates is a posting of its
    #: own and must be reproducible from these two columns alone.
    amount = MoneyField()
    applied_on = models.DateField(db_index=True)
    exchange_rate_used = RateField(
        default=1,
        help_text="Payment currency -> invoice currency at application time.",
    )
    #: Realised FX difference, posted to the gain/loss account.
    fx_gain_loss_amount = MoneyField()
    is_reversed = models.BooleanField(default=False)
    reversed_at = models.DateTimeField(null=True, blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "payments_payment_application"
        ordering = ["-applied_on"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "invoice"], name="uq_payment_application_pair"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="ck_payment_application_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(exchange_rate_used__gt=0),
                name="ck_payment_application_fx_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(is_reversed=False)
                | models.Q(reversed_at__isnull=False),
                name="ck_payment_application_reversal_timestamp",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["invoice"], name="ix_payment_app_invoice"),
            models.Index(fields=["payment"], name="ix_payment_app_payment"),
            models.Index(fields=["tenant", "applied_on"], name="ix_payment_app_date"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.amount} of {self.payment_id} -> {self.invoice_id}"


class Refund(StatusTransitionMixin, ImmutableFinancialModel):
    """Money returned to the customer against an earlier payment.

    A refund is its own document, not a negative payment. Negative amounts on
    the payment table would break every non-negative constraint, every SUM in
    a cash report, and the gateway's own model — providers issue a distinct
    refund object with its own id and its own webhooks.

    ``amount`` is constrained positive and the service layer enforces
    ``SUM(refunds.amount) <= payment.amount``; that cannot be a check
    constraint because it spans rows, so it is a ``SELECT ... FOR UPDATE`` on
    the parent payment plus a recompute.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.PENDING: {Status.SUCCEEDED, Status.FAILED, Status.CANCELLED},
        Status.SUCCEEDED: set(),
        Status.FAILED: set(),
        Status.CANCELLED: set(),
    }

    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, related_name="refunds"
    )
    number = models.CharField(max_length=32, blank=True)
    refund_date = models.DateField(db_index=True)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    amount = MoneyField()
    reason = models.CharField(max_length=255, blank=True)
    gateway_refund_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    #: Refunding does not always return the processing fee; when it does not,
    #: the fee stays an expense and the tenant is out of pocket. Recorded so
    #: the margin report tells the truth.
    fee_refunded_amount = MoneyField()
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="refund",
    )
    idempotency_key = models.CharField(max_length=128, blank=True)
    failure_code = models.CharField(max_length=64, blank=True)
    failure_message = models.TextField(blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "payments_refund"
        ordering = ["-refund_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"],
                condition=~models.Q(number=""),
                name="uq_refund_number",
            ),
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uq_refund_idempotency",
            ),
            models.UniqueConstraint(
                fields=["tenant", "gateway_refund_id"],
                condition=~models.Q(gateway_refund_id=""),
                name="uq_refund_gateway_id",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0)
                & models.Q(fee_refunded_amount__gte=0),
                name="ck_refund_amounts_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status", "refund_date"], name="ix_refund_status"),
            models.Index(fields=["payment"], name="ix_refund_payment"),
        ]


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

class WebhookEvent(StatusTransitionMixin, TenantScopedModel):
    """A raw event delivered by a payment provider, stored before it is acted on.

    The store-then-process pattern
    ------------------------------
    The endpoint does exactly four things, in this order, and nothing else:

    1. **Verify the signature** against the secret at ``webhook_secret_ref``.
       An unverified body is attacker-controlled input claiming a customer
       paid. Verify *before* parsing, on the raw bytes — re-serialising the
       JSON first changes the bytes and the HMAC no longer matches.
    2. **Persist the raw payload** in this table, with ``provider_event_id``
       as the deduplication key.
    3. **Return 200 immediately.** The provider is waiting, with a timeout
       measured in seconds.
    4. **Enqueue** an asynchronous task that does the real work.

    Why processing inline is a bug, not a style preference
    -----------------------------------------------------
    Every gateway retries on timeout or non-2xx, and every gateway delivers
    *at least once*, never exactly once. If the handler captures a charge,
    posts a journal entry and emails a receipt inline, it will sometimes take
    longer than the provider's timeout — a slow GL posting, a lock wait, a
    cold cache. The provider then gives up waiting and **redelivers the same
    event**. The first attempt is still running and will commit. The result is
    two captures, two journal entries, two receipts, and a customer charged
    twice for one order. The failure is invisible in testing (where everything
    is fast) and appears in production exactly when the system is busiest.

    Returning 200 within milliseconds removes the retry trigger, and the
    unique index on ``(tenant, gateway, provider_event_id)`` makes the
    redelivery that happens anyway a no-op INSERT instead of a second charge.
    That index is the actual guarantee; the fast return is what keeps it from
    being exercised constantly.

    ``raw_payload`` is retained verbatim because it is the only evidence of
    what the provider actually said during a chargeback investigation, and
    because a parser bug found next month can be fixed by replaying these rows.
    """

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"
        #: Known event type we deliberately do not act on. Distinct from
        #: PROCESSED so that "events we handle" stays a measurable number and
        #: an unhandled-but-important type is visible in the queue.
        IGNORED = "ignored", "Ignored"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.RECEIVED: {Status.PROCESSING, Status.IGNORED},
        # FAILED -> PROCESSING is the retry path; failures are replayable.
        Status.PROCESSING: {Status.PROCESSED, Status.FAILED, Status.IGNORED},
        Status.FAILED: {Status.PROCESSING, Status.IGNORED},
        Status.PROCESSED: set(),
        Status.IGNORED: set(),
    }

    gateway = models.ForeignKey(
        PaymentGatewayConfig, on_delete=models.PROTECT, related_name="webhook_events"
    )
    #: The provider's event id (``evt_...``). THE deduplication key.
    provider_event_id = models.CharField(max_length=255)
    event_type = models.CharField(
        max_length=100, db_index=True, help_text="e.g. payment_intent.succeeded"
    )
    raw_payload = models.JSONField()
    #: False means the body failed HMAC verification. Such rows are stored for
    #: forensics and never processed — an attacker probing the endpoint is
    #: something we want a record of, not something we want to silently drop.
    signature_verified = models.BooleanField(default=False)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.RECEIVED, db_index=True
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    received_at = models.DateTimeField(db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    #: Resolved links, filled in by the processor so support can jump from an
    #: event to the money it moved. Nullable: many events touch nothing yet.
    payment = models.ForeignKey(
        Payment,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="webhook_events",
    )
    refund = models.ForeignKey(
        Refund,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="webhook_events",
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "payments_webhook_event"
        ordering = ["-received_at"]
        constraints = [
            # Scoped to (tenant, gateway) because provider event ids are only
            # unique within a merchant account, and two tenants on the same
            # provider can legitimately see the same id shape.
            models.UniqueConstraint(
                fields=["tenant", "gateway", "provider_event_id"],
                name="uq_webhook_event_provider_id",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="processed")
                | models.Q(processed_at__isnull=False),
                name="ck_webhook_processed_has_timestamp",
            ),
            # An unverified event must never reach PROCESSED.
            models.CheckConstraint(
                condition=~models.Q(status="processed")
                | models.Q(signature_verified=True),
                name="ck_webhook_processed_was_verified",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status", "received_at"], name="ix_webhook_status"),
            models.Index(fields=["tenant", "event_type"], name="ix_webhook_event_type"),
            models.Index(fields=["gateway", "provider_event_id"], name="ix_webhook_lookup"),
            # The retry sweeper: unprocessed work, oldest first.
            models.Index(
                fields=["received_at"],
                condition=models.Q(status__in=["received", "processing", "failed"]),
                name="ix_webhook_pending",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.event_type} {self.provider_event_id}"
