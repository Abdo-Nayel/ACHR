"""
Banking: the outside world's view of our money, and how we tie it to ours.

The bank statement is the only *independent* record in an accounting system.
Everything else — invoices, bills, payroll — is something we wrote down
ourselves, and self-reported figures prove nothing. Reconciliation is the
control that turns "our books say we have 412,000" into "the bank agrees we
have 412,000, and here is the line-by-line explanation of the difference".

Two structural rules shape this module:

**The statement is imported, never edited.** A :class:`BankTransaction` is
what the bank told us. If it is wrong, the bank issues a correction, which
arrives as another transaction. Editing an imported line to make it match
our books is the exact behaviour reconciliation exists to prevent — it turns
the one independent record into a second copy of our own opinion.

**Matching is a relationship, not a field.** A bank line is linked to ledger
lines through :class:`ReconciliationMatch` rows rather than by stamping a FK
on either side. Reality is many-to-many: one transfer pays six invoices, one
invoice is settled by three instalments, and a bank charge is split between
two expense accounts. A nullable ``journal_line_id`` column on the bank
transaction would model none of those, and the workarounds people build on
top of it (duplicating the bank line, or "adjusting" the amount) corrupt the
statement.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.fields import MoneyField, RateField, ZERO
from apps.core.models import Currency, StatusTransitionMixin, TenantScopedModel


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class BankAccount(TenantScopedModel):
    """A real account at a real bank, paired 1:1 with a ledger account.

    The OneToOne to ``accounting.Account`` is what makes reconciliation
    meaningful: the ledger account's balance is what *we* think we have, the
    statement's closing balance is what the *bank* thinks we have, and
    reconciliation explains the difference (uncleared cheques, deposits in
    transit, charges we did not know about). Two bank accounts sharing one
    ledger account would make that comparison meaningless, so the database
    forbids it rather than a code comment discouraging it.

    The linked account must have ``is_reconcilable = True``. That flag is on
    ``Account`` and cannot be enforced by a CHECK from here (a constraint
    cannot join), so :meth:`clean` asserts it and the service layer re-checks.

    Only the last four digits of the account number are stored. The full
    number is not needed to reconcile, it is regulated personal data in most
    jurisdictions, and a breach of a table nobody thought was sensitive is a
    reliably expensive way to learn that. IBAN is stored because payment
    files genuinely require it.

    ``current_balance`` is a denormalised convenience for dashboards,
    maintained from imported statements. It is never the source of a
    financial figure: the ledger account is.
    """

    class FeedProvider(models.TextChoices):
        NONE = "none", "Manual import only"
        PLAID = "plaid", "Plaid"
        SALTEDGE = "saltedge", "Salt Edge"
        TINK = "tink", "Tink"
        YODLEE = "yodlee", "Yodlee"
        DIRECT = "direct", "Direct bank API"

    name = models.CharField(max_length=120)
    bank_name = models.CharField(max_length=120)
    #: Never the full number. See the class docstring.
    account_number_last4 = models.CharField(max_length=4, blank=True)
    iban = models.CharField(max_length=34, blank=True)
    swift = models.CharField(max_length=11, blank=True)
    branch = models.CharField(max_length=120, blank=True)

    currency = models.CharField(max_length=3, choices=Currency.choices)
    ledger_account = models.OneToOneField(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="bank_account",
    )

    opening_balance = MoneyField()
    opening_date = models.DateField(null=True, blank=True)
    #: Last known balance per the bank, not per the ledger. They differ by
    #: exactly the set of unreconciled items, which is the whole point.
    current_balance = MoneyField()
    balance_as_of = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    feed_provider = models.CharField(
        max_length=12, choices=FeedProvider.choices, default=FeedProvider.NONE
    )
    #: The provider's opaque id for this account. Unique per tenant when set:
    #: connecting the same real account twice creates two parallel streams of
    #: the same transactions and doubles every balance on the dashboard.
    feed_external_id = models.CharField(max_length=128, blank=True)
    feed_last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "banking_bank_account"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="uq_bank_account_name"),
            models.UniqueConstraint(
                fields=["tenant", "iban"],
                condition=~models.Q(iban=""),
                name="uq_bank_account_iban",
            ),
            models.UniqueConstraint(
                fields=["tenant", "feed_provider", "feed_external_id"],
                condition=~models.Q(feed_external_id=""),
                name="uq_bank_account_feed_id",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_bank_account_active"),
            models.Index(
                fields=["tenant", "feed_provider"], name="ix_bank_account_feed"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} ({self.bank_name})"

    def clean(self) -> None:
        super().clean()
        if self.ledger_account_id and not self.ledger_account.is_reconcilable:
            raise ValidationError(
                {
                    "ledger_account": (
                        "A bank account must point at a reconcilable ledger "
                        "account. Reconciling against a non-cash account "
                        "produces a balance nobody can explain."
                    )
                }
            )
        if self.ledger_account_id and self.ledger_account.currency not in (
            None, "", self.currency
        ):
            raise ValidationError(
                {"currency": "Bank and ledger account currencies must match."}
            )


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

class BankStatement(TenantScopedModel):
    """One imported statement file or one API sync window.

    ``file_checksum`` is unique per tenant and it is the single most valuable
    column in this module. Importing the same CSV twice — because the first
    attempt appeared to hang, because two people were "helping", because the
    file was re-downloaded with a different name — duplicates every
    transaction on it. The duplicates then get matched to the same invoices,
    the bank balance doubles, and untangling it means deleting rows from a
    period that may already be closed. A unique hash of the file contents
    makes the second import a clean, immediate error instead.

    The checksum is of the *contents*, not the filename, precisely because
    the filename is the thing that changes on a re-download.

    ``opening_balance``/``closing_balance`` come from the statement itself
    and are checked against the sum of its lines during import: a statement
    whose lines do not explain its own balance movement was truncated or
    parsed wrongly, and importing it half-complete is worse than not
    importing it.
    """

    class ImportSource(models.TextChoices):
        CSV = "csv", "CSV"
        OFX = "ofx", "OFX / QFX"
        MT940 = "mt940", "SWIFT MT940"
        CAMT = "camt053", "ISO 20022 CAMT.053"
        API = "api", "Bank API feed"
        MANUAL = "manual", "Manual entry"

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="statements"
    )
    statement_number = models.CharField(max_length=50, blank=True)
    statement_date = models.DateField(db_index=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    currency = models.CharField(max_length=3, choices=Currency.choices)
    opening_balance = MoneyField()
    closing_balance = MoneyField()

    imported_at = models.DateTimeField(default=timezone.now)
    imported_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    import_source = models.CharField(
        max_length=10, choices=ImportSource.choices, default=ImportSource.CSV
    )
    original_filename = models.CharField(max_length=255, blank=True)
    #: SHA-256 of the raw file bytes. See the class docstring.
    file_checksum = models.CharField(max_length=64, blank=True)
    line_count = models.PositiveIntegerField(default=0)

    class Meta(TenantScopedModel.Meta):
        db_table = "banking_bank_statement"
        ordering = ["-statement_date", "-imported_at"]
        constraints = [
            # Per tenant, not global: two tenants banking with the same bank
            # can legitimately hold byte-identical statement files only by
            # coincidence, but a global index would leak that coincidence as
            # a duplicate-key error across the tenant boundary.
            models.UniqueConstraint(
                fields=["tenant", "file_checksum"],
                condition=~models.Q(file_checksum=""),
                name="uq_bank_statement_checksum",
            ),
            models.UniqueConstraint(
                fields=["tenant", "bank_account", "statement_number"],
                condition=~models.Q(statement_number=""),
                name="uq_bank_statement_number",
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__isnull=True)
                | models.Q(period_start__isnull=True)
                | models.Q(period_end__gte=models.F("period_start")),
                name="ck_bank_statement_period_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "bank_account", "statement_date"],
                name="ix_bank_statement_period",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.bank_account_id} {self.statement_date}"


class BankTransaction(StatusTransitionMixin, TenantScopedModel):
    """One line as reported by the bank. Read-only after import.

    Why ``amount`` is a single signed column here, when ``JournalLine`` uses
    separate non-negative debit/credit columns
    -------------------------------------------------------------------------
    The ledger's split exists to make ``SUM(debit) = SUM(credit)`` a database
    constraint — it has two sides that must balance. A bank line has one
    side: money entered the account or left it. The bank itself reports a
    signed amount (or a debit/credit indicator we normalise into one on
    import), and preserving that reported value exactly is the whole point of
    an independent record.

    Sign convention, stated once so no import parser has to guess:
    **credits are positive (money in), debits are negative (money out)**,
    from *our* point of view. Note that this is the opposite of the bank's
    own vocabulary on a printed statement, where a "credit" is a credit to
    *their* liability to us — a genuine and expensive source of sign errors
    in OFX and MT940 parsers, which is why the rule is written here rather
    than inferred per format.

    ``external_id`` is the dedup key for API feeds: providers re-send a
    rolling window of recent transactions on every poll, so without a unique
    key each poll adds the same lines again. It is unique per
    ``(tenant, bank_account)`` and only when present, because CSV and MT940
    files usually carry no stable per-line identifier.
    """

    class Status(models.TextChoices):
        UNMATCHED = "unmatched", "Unmatched"
        SUGGESTED = "suggested", "Match suggested"
        MATCHED = "matched", "Matched"
        IGNORED = "ignored", "Ignored"
        MANUAL = "manual", "Manually posted"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.UNMATCHED: {Status.SUGGESTED, Status.MATCHED, Status.IGNORED, Status.MANUAL},
        Status.SUGGESTED: {Status.MATCHED, Status.UNMATCHED, Status.IGNORED, Status.MANUAL},
        # Un-matching is legal and must stay legal: the commonest correction
        # in reconciliation is undoing a wrong match.
        Status.MATCHED: {Status.UNMATCHED},
        Status.IGNORED: {Status.UNMATCHED},
        Status.MANUAL: {Status.MATCHED, Status.UNMATCHED},
    }

    statement = models.ForeignKey(
        BankStatement, null=True, blank=True,
        on_delete=models.PROTECT, related_name="transactions",
    )
    #: Denormalised from the statement because API feeds deliver
    #: transactions with no statement at all, and because every query in
    #: this module filters by account first.
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="transactions"
    )

    transaction_date = models.DateField(db_index=True)
    #: When the money actually became available. Differs from
    #: ``transaction_date`` for cheques and cross-border payments, and it is
    #: the date interest is calculated on — so both are kept.
    value_date = models.DateField(null=True, blank=True)

    description = models.CharField(max_length=500, blank=True)
    reference = models.CharField(max_length=140, blank=True)
    counterparty_name = models.CharField(max_length=200, blank=True)
    counterparty_account = models.CharField(max_length=64, blank=True)

    currency = models.CharField(max_length=3, choices=Currency.choices)
    #: Signed. Credits (money in) positive, debits (money out) negative.
    amount = MoneyField()
    #: Balance after this line, as reported by the bank. Kept because it lets
    #: an import verify it did not drop a line: consecutive running balances
    #: must differ by exactly the amount between them.
    running_balance = MoneyField(null=True, blank=True)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.UNMATCHED, db_index=True
    )
    matched_at = models.DateTimeField(null=True, blank=True)
    matched_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Provider transaction id. The dedup key for feeds; see the docstring.
    external_id = models.CharField(max_length=128, blank=True)
    raw_payload = models.JSONField(null=True, blank=True)
    notes = models.CharField(max_length=500, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "banking_bank_transaction"
        ordering = ["-transaction_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "bank_account", "external_id"],
                condition=~models.Q(external_id=""),
                name="uq_bank_txn_external_id",
            ),
            # A zero-amount bank line carries no information and cannot be
            # reconciled against anything; it is always a parser artefact.
            models.CheckConstraint(
                condition=~models.Q(amount=0), name="ck_bank_txn_nonzero"
            ),
            models.CheckConstraint(
                condition=~models.Q(status="matched")
                | models.Q(matched_at__isnull=False),
                name="ck_bank_txn_matched_at",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            # The reconciliation worklist: one account, unmatched, by date.
            models.Index(
                fields=["tenant", "bank_account", "status", "transaction_date"],
                name="ix_bank_txn_worklist",
            ),
            models.Index(
                fields=["tenant", "transaction_date"], name="ix_bank_txn_date"
            ),
            # Candidate search by amount is the first filter the matching
            # engine applies, so it must not be a scan.
            models.Index(
                fields=["tenant", "bank_account", "amount"], name="ix_bank_txn_amount"
            ),
            models.Index(fields=["tenant", "statement"], name="ix_bank_txn_statement"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.transaction_date} {self.amount} {self.description[:40]}"

    @property
    def is_credit(self) -> bool:
        """Money in. Named from our perspective, not the bank's."""
        return self.amount > ZERO

    @property
    def matched_total(self) -> Decimal:
        """Sum of confirmed match amounts. See ``ReconciliationMatch``."""
        return self.matches.aggregate(total=models.Sum("matched_amount"))["total"] or ZERO


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

class ReconciliationMatch(TenantScopedModel):
    """A link asserting "this bank line and this ledger line are the same money".

    Why ``matched_amount`` exists instead of assuming the full amount
    -------------------------------------------------------------------------
    Because partial and split matching is the normal case, not an edge case:

    * One 15,000 transfer settles three invoices of 5,000. Three matches,
      each for part of the bank line.
    * One 12,000 invoice is paid in two instalments. Two matches, each for
      part of the ledger line.
    * A 9,970 receipt against a 10,000 invoice, with 30 deducted as a bank
      charge. Two matches: 9,970 to the invoice's AR line, 30 to bank
      charges.

    If the model assumed "one match = the whole amount", every one of those
    would be forced into a workaround — splitting the imported bank line
    (which corrupts the independent record) or writing an adjusting journal
    entry per instalment (which buries the real transaction). Storing the
    matched amount per link makes the arithmetic explicit: a bank line is
    fully reconciled when its matches sum to its amount, and the residual is
    visible and actionable until then.

    ``UNIQUE (bank_transaction, journal_line)`` prevents the same pair being
    linked twice, which would double-count the settlement while looking
    entirely reasonable in the UI.
    """

    class MatchType(models.TextChoices):
        AUTO_EXACT = "auto_exact", "Automatic — exact"
        AUTO_FUZZY = "auto_fuzzy", "Automatic — fuzzy"
        MANUAL = "manual", "Manual"
        SPLIT = "split", "Split / partial"
        RULE = "rule", "Rule-based"

    bank_transaction = models.ForeignKey(
        BankTransaction, on_delete=models.PROTECT, related_name="matches"
    )
    journal_line = models.ForeignKey(
        "accounting.JournalLine", on_delete=models.PROTECT, related_name="bank_matches"
    )
    session = models.ForeignKey(
        "banking.ReconciliationSession", null=True, blank=True,
        on_delete=models.PROTECT, related_name="matches",
    )

    currency = models.CharField(max_length=3, choices=Currency.choices)
    #: Always positive: the *magnitude* of money attributed to this link.
    #: Direction is already unambiguous from the bank line's sign and the
    #: ledger line's side, and allowing a signed value here would let a
    #: negative "match" silently cancel a real one.
    matched_amount = MoneyField()
    match_type = models.CharField(
        max_length=12, choices=MatchType.choices, default=MatchType.MANUAL
    )
    #: 0..1. Stored even for manual matches (as 1.0) so that a later audit
    #: can ask "which of last year's matches were machine-guessed?".
    confidence_score = RateField(default=1)
    notes = models.CharField(max_length=500, blank=True)
    #: Kept separate from ``created_by``: an auto-match is created by a
    #: background job but *confirmed* by a person, and the control depends on
    #: knowing which.
    confirmed_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "banking_reconciliation_match"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["bank_transaction", "journal_line"],
                name="uq_reconciliation_match_pair",
            ),
            models.CheckConstraint(
                condition=models.Q(matched_amount__gt=0),
                name="ck_reconciliation_match_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(confidence_score__gte=0)
                & models.Q(confidence_score__lte=1),
                name="ck_reconciliation_confidence_range",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "bank_transaction"], name="ix_recon_match_txn"
            ),
            models.Index(
                fields=["tenant", "journal_line"], name="ix_recon_match_line"
            ),
            models.Index(fields=["tenant", "session"], name="ix_recon_match_session"),
            models.Index(
                fields=["tenant", "match_type"], name="ix_recon_match_type"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.bank_transaction_id} <-> {self.journal_line_id}"


class ReconciliationSession(StatusTransitionMixin, TenantScopedModel):
    """One period's reconciliation of one bank account, and its outcome.

    The session is the artefact an auditor asks for: it records that on a
    given date, for a given period, someone compared the bank's closing
    balance to the ledger's and accounted for every unit of difference.

    ``difference = statement_closing_balance - ledger_closing_balance``
    (adjusted for items in transit). A session may be closed **only** when
    that difference is zero — ``ck_recon_session_closed_balanced`` enforces
    it in SQL, not in the service, because "close anyway, we'll fix it next
    month" is the single most common way a small discrepancy becomes a
    permanent, un-investigable one. By the time anyone looks, the evidence
    (which cheque, which fee, which duplicate) is gone.

    ``BALANCED`` is distinct from ``CLOSED``: the difference reached zero,
    but a human has not yet signed off. Collapsing the two would remove the
    review step that the whole control depends on.
    """

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        BALANCED = "balanced", "Balanced, awaiting sign-off"
        CLOSED = "closed", "Closed"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.IN_PROGRESS: {Status.BALANCED},
        Status.BALANCED: {Status.CLOSED, Status.IN_PROGRESS},
        # Terminal. Reopening a closed reconciliation would let a signed-off
        # period change after the fact; corrections go in the next period.
        Status.CLOSED: set(),
    }

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="reconciliations"
    )
    period_start = models.DateField()
    period_end = models.DateField()

    currency = models.CharField(max_length=3, choices=Currency.choices)
    statement_closing_balance = MoneyField()
    ledger_closing_balance = MoneyField()
    #: Materialised rather than computed on read so the closing check is a
    #: plain column comparison the database can constrain.
    difference = MoneyField()

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.IN_PROGRESS, db_index=True
    )
    matched_count = models.PositiveIntegerField(default=0)
    unmatched_count = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)

    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "banking_reconciliation_session"
        ordering = ["-period_end"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "bank_account", "period_start", "period_end"],
                name="uq_recon_session_period",
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="ck_recon_session_date_order",
            ),
            # The control. A closed reconciliation that does not balance is
            # not a reconciliation.
            models.CheckConstraint(
                condition=~models.Q(status="closed") | models.Q(difference=0),
                name="ck_recon_session_closed_balanced",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="closed")
                | (
                    models.Q(closed_at__isnull=False)
                    & models.Q(closed_by__isnull=False)
                ),
                name="ck_recon_session_closed_signoff",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    difference=models.F("statement_closing_balance")
                    - models.F("ledger_closing_balance")
                ),
                name="ck_recon_session_difference",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "bank_account", "status"],
                name="ix_recon_session_account",
            ),
            models.Index(
                fields=["tenant", "period_end"], name="ix_recon_session_period"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.bank_account_id} {self.period_start}..{self.period_end}"

    @property
    def is_balanced(self) -> bool:
        return self.difference == ZERO
