"""
Banking serializers.

The recurring theme here is that a *match* is an assertion, not a document:
it links a bank line to a ledger line and it can be undone cheaply. Everything
else — balances, statuses, matched totals — is derived by
``apps.banking.services.reconciliation`` and is read-only at the API edge.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.banking.models import (
    BankAccount,
    BankStatement,
    BankTransaction,
    ReconciliationMatch,
    ReconciliationSession,
)
from apps.core.serializers import MoneyField, RateField, TenantScopedSerializer


class BankAccountSerializer(TenantScopedSerializer):
    """A real bank account and the GL account that mirrors it.

    ``current_balance`` is the *bank's* balance as last imported or synced;
    the ledger's view of the same account lives on ``ledger_account``. Keeping
    both and never conflating them is what makes a reconciliation difference a
    number you can look at rather than an argument.
    """

    opening_balance = MoneyField(required=False)
    current_balance = MoneyField(read_only=True)
    ledger_account_code = serializers.CharField(
        source="ledger_account.code", read_only=True
    )

    server_owned_fields = ("current_balance", "balance_as_of", "feed_last_synced_at")

    class Meta:
        model = BankAccount
        fields = (
            "id", "name", "bank_name", "account_number_last4", "iban", "swift",
            "branch", "currency", "ledger_account", "ledger_account_code",
            "opening_balance", "opening_date", "current_balance",
            "balance_as_of", "is_active", "feed_provider", "feed_external_id",
            "feed_last_synced_at", "created_at", "updated_at",
        )


class BankStatementSerializer(TenantScopedSerializer):
    """One imported statement file.

    ``file_checksum`` is the dedupe key (``uq_bank_statement_checksum``): the
    same file imported twice is the same statement, not a second one.
    """

    opening_balance = MoneyField(required=False)
    closing_balance = MoneyField(required=False)
    bank_account_name = serializers.CharField(source="bank_account.name", read_only=True)

    server_owned_fields = ("imported_at", "imported_by", "line_count")

    class Meta:
        model = BankStatement
        fields = (
            "id", "bank_account", "bank_account_name", "statement_number",
            "statement_date", "period_start", "period_end", "currency",
            "opening_balance", "closing_balance", "imported_at", "imported_by",
            "import_source", "original_filename", "file_checksum",
            "line_count", "created_at", "updated_at",
        )


class StatementImportSerializer(serializers.Serializer):
    """Body for ``POST /bank-statements/import/``.

    Upload metadata only: the file itself is streamed to object storage by the
    client against a signed URL, and this endpoint records the statement header
    plus the checksum of what was uploaded. The checksum is computed by the
    client over the *raw bytes* and re-verified by the parser worker — an
    import that claims a checksum it does not match never produces
    transactions.
    """

    bank_account = serializers.UUIDField()
    statement_date = serializers.DateField()
    file_checksum = serializers.CharField(max_length=64, min_length=32)
    import_source = serializers.ChoiceField(
        choices=BankStatement.ImportSource.choices,
        default=BankStatement.ImportSource.CSV,
    )
    original_filename = serializers.CharField(max_length=255, required=False,
                                              allow_blank=True)
    statement_number = serializers.CharField(max_length=50, required=False,
                                             allow_blank=True)
    period_start = serializers.DateField(required=False, allow_null=True)
    period_end = serializers.DateField(required=False, allow_null=True)
    currency = serializers.CharField(max_length=3, required=False, allow_blank=True)
    opening_balance = MoneyField(required=False)
    closing_balance = MoneyField(required=False)
    line_count = serializers.IntegerField(required=False, min_value=0)

    def validate(self, attrs: dict) -> dict:
        start, end = attrs.get("period_start"), attrs.get("period_end")
        if start and end and end < start:
            raise serializers.ValidationError(
                {"period_end": "The statement period ends before it starts."}
            )
        return attrs


class BankTransactionSerializer(TenantScopedSerializer):
    """One line off a bank statement.

    ``status`` is read-only. Matching, unmatching and ignoring are POST
    sub-resources that run the reconciliation service, which locks the row,
    recomputes the residual and refuses to over-allocate. Writing the column
    directly would mark a line matched with no ``ReconciliationMatch`` behind
    it — it disappears from the worklist and stays wrong in the ledger.
    """

    amount = MoneyField(required=False)
    running_balance = MoneyField(required=False, allow_null=True)
    matched_total = MoneyField(read_only=True)
    bank_account_name = serializers.CharField(source="bank_account.name", read_only=True)
    is_credit = serializers.BooleanField(read_only=True)

    server_owned_fields = ("status", "matched_at", "matched_by")

    class Meta:
        model = BankTransaction
        fields = (
            "id", "statement", "bank_account", "bank_account_name",
            "transaction_date", "value_date", "description", "reference",
            "counterparty_name", "counterparty_account", "currency", "amount",
            "running_balance", "status", "matched_at", "matched_by",
            "matched_total", "is_credit", "external_id", "notes",
            "created_at", "updated_at",
        )


class ReconciliationMatchSerializer(TenantScopedSerializer):
    """The link between one bank line and one ledger line.

    Created through ``POST /bank-transactions/{id}/match`` rather than by
    POSTing here directly, because the amount defaulting ("the smaller of the
    two residuals") and the over-allocation guard live in the service.
    """

    matched_amount = MoneyField(required=False)
    confidence_score = RateField(required=False)
    entry_number = serializers.CharField(
        source="journal_line.entry.number", read_only=True
    )

    server_owned_fields = ("confirmed_by", "confirmed_at")

    class Meta:
        model = ReconciliationMatch
        fields = (
            "id", "bank_transaction", "journal_line", "entry_number", "session",
            "currency", "matched_amount", "match_type", "confidence_score",
            "notes", "confirmed_by", "confirmed_at", "created_at",
        )


class ReconciliationSessionSerializer(TenantScopedSerializer):
    """A period's reconciliation of one bank account.

    ``difference`` is maintained by ``refresh_session`` and can only be closed
    at zero — ``ck_recon_session_closed_balanced`` enforces the same rule in
    the database, so no code path can sign off an unbalanced session.
    """

    statement_closing_balance = MoneyField(required=False)
    ledger_closing_balance = MoneyField(read_only=True)
    difference = MoneyField(read_only=True)
    bank_account_name = serializers.CharField(source="bank_account.name", read_only=True)
    is_balanced = serializers.BooleanField(read_only=True)

    server_owned_fields = (
        "status", "ledger_closing_balance", "difference", "matched_count",
        "unmatched_count", "closed_at", "closed_by",
    )

    class Meta:
        model = ReconciliationSession
        fields = (
            "id", "bank_account", "bank_account_name", "period_start",
            "period_end", "currency", "statement_closing_balance",
            "ledger_closing_balance", "difference", "status", "is_balanced",
            "matched_count", "unmatched_count", "notes", "closed_at",
            "closed_by", "created_at", "updated_at",
        )


class MatchRequestSerializer(serializers.Serializer):
    """Body for ``POST /bank-transactions/{id}/match``."""

    journal_line = serializers.UUIDField()
    matched_amount = MoneyField(required=False, allow_null=True)
    session = serializers.UUIDField(required=False, allow_null=True)
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True)


class UnmatchRequestSerializer(serializers.Serializer):
    """Body for ``POST /bank-transactions/{id}/unmatch``.

    ``match`` is optional: omitting it releases every match on the line, which
    is what a reviewer means by "this one is wrong, start again".
    """

    match = serializers.UUIDField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class AutoReconcileSerializer(serializers.Serializer):
    """Body for ``POST /reconciliation-sessions/{id}/auto-reconcile``."""

    threshold = RateField(required=False, allow_null=True)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=5000)


class CandidateSerializer(serializers.Serializer):
    """A ranked suggestion from ``suggest_matches``. Read-only projection.

    Not a model serializer: a candidate is a computed dataclass, not a row.
    ``confidence`` is a string for the same reason every other decimal is —
    a client that parsed it as a float would render 0.8500000000000001.
    """

    journal_line_id = serializers.UUIDField(read_only=True)
    entry_id = serializers.UUIDField(read_only=True)
    entry_number = serializers.CharField(read_only=True)
    entry_date = serializers.DateField(read_only=True)
    amount = MoneyField(read_only=True)
    description = serializers.CharField(read_only=True)
    confidence = RateField(read_only=True)
    match_type = serializers.CharField(read_only=True)
    reasons = serializers.ListField(child=serializers.CharField(), read_only=True)
