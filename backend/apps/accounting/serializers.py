"""
Serializers for the general ledger.

Three rules shape every class in this module, and all three exist because the
ledger is the one place in the system where a wrong number is not a bug report
but a restatement:

1. **Nothing that a service owns is writable.** ``status``, ``number``,
   ``total_debit``, ``total_credit``, ``posted_at`` and ``period`` are
   established by :mod:`apps.accounting.services.posting`. A writable
   ``status`` would let a caller mark an entry POSTED without allocating a
   number, without checking the period lock and without the balance check —
   the row would say "posted" and the books would disagree. See
   :mod:`apps.core.viewsets` for the sub-resource pattern that replaces it.

2. **Money is a string on the wire.** Every amount uses the core
   :class:`~apps.core.serializers.MoneyField`, which refuses JSON floats.

3. **A posted entry is frozen.** Once ``status == POSTED`` the lines, the
   totals and the account of each line become read-only at the serializer
   layer, mirroring the database trigger installed in migration
   ``0004_ledger_guards``. Two layers is not redundancy: the serializer gives
   a field error the user can act on, the trigger is what makes the invariant
   true.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from rest_framework import serializers

from apps.accounting.models import (
    Account,
    AccountType,
    ExchangeRate,
    FiscalPeriod,
    FiscalYear,
    Journal,
    JournalEntry,
    JournalLine,
    NormalBalance,
    TaxRate,
)
from apps.core.fields import ZERO
from apps.core.serializers import (
    MoneyField,
    RateField,
    ReadOnlyModelSerializer,
    TenantScopedSerializer,
)

#: Maximum depth the chart-of-accounts tree serialiser will walk.
#: A chart deeper than this is either a data error or a cycle that slipped past
#: ``ck_account_no_self_parent`` (which only forbids *direct* self-parenting);
#: recursing without a bound turns a bad row into a RecursionError 500 on every
#: page load instead of one visibly truncated branch.
MAX_TREE_DEPTH = 12


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------

class AccountSerializer(TenantScopedSerializer):
    """One node of the chart of accounts.

    ``cached_balance`` is exposed but never accepted. It is a denormalisation
    maintained by the posting service inside the same transaction as the
    journal lines; a client that could write it could make the dashboard
    disagree with the ledger with a single PATCH, and nothing would ever
    reconcile the two.

    ``is_active`` is likewise read-only: deactivating an account is
    ``POST /accounts/{id}/archive``, which refuses to archive an account that
    still carries a balance. A writable boolean would skip that check.
    """

    server_owned_fields = ("cached_balance", "cached_balance_as_of", "is_active")

    cached_balance = MoneyField(read_only=True)
    #: The effective normal side (override, else the type default) — read-only.
    normal_balance = serializers.CharField(read_only=True)
    #: The chosen side, written on create; blank = derive from the section.
    normal_balance_override = serializers.ChoiceField(
        choices=NormalBalance.choices, required=False, allow_blank=True, write_only=True
    )
    parent_code = serializers.CharField(source="parent.code", read_only=True, default=None)

    class Meta:
        model = Account
        fields = (
            "id",
            "code",
            "level",
            "full_code",
            "name",
            "type",
            "parent",
            "parent_code",
            "description",
            "currency",
            "is_postable",
            "is_active",
            "system_key",
            "is_reconcilable",
            "requires_party",
            "income_category",
            "cached_balance",
            "cached_balance_as_of",
            "normal_balance",
            "normal_balance_override",
            "created_at",
            "updated_at",
        )
        # The code is allocated by the server (see create); the section, level
        # and postability are derived from the account's place in the tree.
        read_only_fields = (
            "system_key", "code", "level", "full_code", "type", "is_postable",
            "income_category",
        )

    def create(self, validated_data: dict) -> Account:
        """Create an account with a **server-allocated** segmented code.

        The client names an account under a parent and picks its side; the code,
        level, full code, section and postability come from its place in the
        tree (see ``apps.accounting.services.coding.allocate_account``). A caller
        never invents a number, so two accountants adding accounts at once cannot
        collide and the chart stays a clean positional tree.
        """
        from apps.accounting.services.coding import allocate_account

        tenant_id = self.get_tenant_id()
        account = allocate_account(
            tenant_id,
            parent=validated_data.get("parent"),
            name=validated_data.get("name", ""),
            normal_balance=validated_data.get("normal_balance_override", ""),
            requires_party=validated_data.get("requires_party", False),
            currency=validated_data.get("currency"),
            is_reconcilable=validated_data.get("is_reconcilable", False),
            user_id=self.get_actor_id(),
        )
        description = validated_data.get("description")
        if description:
            account.description = description
            account.save(update_fields=["description", "updated_at"])
        return account

    def update(self, instance: Account, validated_data: dict) -> Account:
        # A coded account's place in the tree is its identity: re-parenting would
        # change its full code and orphan every posting that resolved it. Edits
        # are name/description/currency/side only.
        validated_data.pop("parent", None)
        return super().update(instance, validated_data)

    def validate_parent(self, parent: Optional[Account]) -> Optional[Account]:
        """Reject a parent that would create a cycle.

        ``ck_account_no_self_parent`` only catches ``parent_id = id``. A
        two-node cycle (A -> B -> A) satisfies that constraint and then makes
        every tree walk, every roll-up report and every balance aggregation
        loop forever.
        """
        if parent is None:
            return parent
        instance = getattr(self, "instance", None)
        if instance is not None:
            node: Optional[Account] = parent
            seen = 0
            while node is not None and seen < MAX_TREE_DEPTH:
                if node.pk == instance.pk:
                    raise serializers.ValidationError(
                        f"Account {instance.code} cannot be placed under "
                        f"{parent.code}: that would make the chart of accounts "
                        f"cyclic."
                    )
                node = node.parent
                seen += 1
        return parent

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        parent = attrs.get("parent", getattr(instance, "parent", None))
        is_postable = attrs.get(
            "is_postable", getattr(instance, "is_postable", True)
        )
        # A parent that is itself postable makes its balance ambiguous — is it
        # its own postings or the roll-up of its children? See the model
        # docstring; this is the check that keeps the answer "the roll-up".
        if parent is not None and parent.is_postable and is_postable:
            raise serializers.ValidationError(
                {
                    "parent": (
                        f"Account {parent.code} is a postable (leaf) account and "
                        f"cannot have children. Mark it is_postable=false first, "
                        f"which is only allowed while it has no journal lines."
                    )
                }
            )
        return attrs


class AccountTreeSerializer(AccountSerializer):
    """The chart of accounts as a nested tree, for the account picker.

    Serialised recursively from a prefetched queryset rather than by querying
    per node: a 400-account chart rendered one query per node is 400 round
    trips on every page load of every user.
    """

    children = serializers.SerializerMethodField()

    class Meta(AccountSerializer.Meta):
        fields = AccountSerializer.Meta.fields + ("children",)

    def get_children(self, obj: Account) -> list[dict]:
        depth = self.context.get("_tree_depth", 0)
        if depth >= MAX_TREE_DEPTH:
            return []
        child_context = dict(self.context)
        child_context["_tree_depth"] = depth + 1
        # ``obj.children.all()`` hits the prefetch cache when the viewset
        # prefetched it, and falls back to one query per node when it did not.
        children = sorted(obj.children.all(), key=lambda a: a.code)
        return AccountTreeSerializer(children, many=True, context=child_context).data


class TaxRateSerializer(TenantScopedSerializer):
    """A VAT / sales-tax definition and the accounts it posts to.

    ``rate`` is a *fraction* (0.140000 for 14%), matching the DB constraint
    ``ck_tax_rate_fraction``. Accepting a percentage here and dividing by 100
    somewhere later is how a 14% rate becomes 1400%.
    """

    rate = RateField(min_value=Decimal("0"), max_value=Decimal("1"))

    class Meta:
        model = TaxRate
        fields = (
            "id",
            "name",
            "code",
            "rate",
            "is_compound",
            "is_recoverable",
            "collected_account",
            "paid_account",
            "is_active",
            "effective_from",
            "effective_to",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        start = attrs.get("effective_from", getattr(instance, "effective_from", None))
        end = attrs.get("effective_to", getattr(instance, "effective_to", None))
        if start and end and end < start:
            raise serializers.ValidationError(
                {"effective_to": "A tax rate cannot stop applying before it starts."}
            )
        recoverable = attrs.get(
            "is_recoverable", getattr(instance, "is_recoverable", True)
        )
        paid_account = attrs.get("paid_account", getattr(instance, "paid_account", None))
        if recoverable and paid_account is None:
            raise serializers.ValidationError(
                {
                    "paid_account": (
                        "A recoverable tax needs an input-VAT account to debit. "
                        "Without one the reclaimable tax is silently expensed and "
                        "the tenant loses the reclaim."
                    )
                }
            )
        return attrs


# ---------------------------------------------------------------------------
# Fiscal calendar
# ---------------------------------------------------------------------------

class FiscalYearSerializer(TenantScopedSerializer):
    """A financial year. ``status`` moves only through the period actions."""

    server_owned_fields = ("status", "closed_at")

    class Meta:
        model = FiscalYear
        fields = (
            "id",
            "name",
            "start_date",
            "end_date",
            "status",
            "closed_at",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        start = attrs.get("start_date", getattr(instance, "start_date", None))
        end = attrs.get("end_date", getattr(instance, "end_date", None))
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"end_date": "A fiscal year must end after it starts."}
            )
        return attrs


class FiscalPeriodSerializer(TenantScopedSerializer):
    """A month (usually) — the unit at which the books are locked.

    ``status`` is read-only. Closing is ``POST /fiscal-periods/{id}/close``,
    which takes a row lock that a concurrent posting must wait behind. A
    PATCH on this field would race with an in-flight ``post_entry`` and the
    entry would land in a period that is closed by the time it commits.
    """

    server_owned_fields = ("status", "closed_at", "closed_by")

    accepts_postings = serializers.BooleanField(read_only=True)
    fiscal_year_name = serializers.CharField(
        source="fiscal_year.name", read_only=True, default=None
    )

    class Meta:
        model = FiscalPeriod
        fields = (
            "id",
            "fiscal_year",
            "fiscal_year_name",
            "name",
            "start_date",
            "end_date",
            "status",
            "accepts_postings",
            "closed_at",
            "closed_by",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        start = attrs.get("start_date", getattr(instance, "start_date", None))
        end = attrs.get("end_date", getattr(instance, "end_date", None))
        year = attrs.get("fiscal_year", getattr(instance, "fiscal_year", None))
        if start and end and end < start:
            raise serializers.ValidationError(
                {"end_date": "A fiscal period must end on or after it starts."}
            )
        if year is not None and start and end:
            if start < year.start_date or end > year.end_date:
                raise serializers.ValidationError(
                    {
                        "start_date": (
                            f"The period {start}..{end} falls outside fiscal year "
                            f"'{year.name}' ({year.start_date}..{year.end_date}). A "
                            f"period that straddles two years makes every year-end "
                            f"figure ambiguous."
                        )
                    }
                )
        return attrs


class JournalSerializer(TenantScopedSerializer):
    """A book of original entry. ``sequence_prefix`` drives document numbers.

    Changing ``sequence_prefix`` on a journal that has already posted entries
    is refused: the prefix is part of the allocated number, and changing it
    mid-year produces two number series in one journal, which reads to an
    auditor exactly like a deleted range.
    """

    class Meta:
        model = Journal
        fields = (
            "id",
            "code",
            "name",
            "kind",
            "default_account",
            "sequence_prefix",
            "is_active",
            "created_at",
            "updated_at",
        )

    def validate_sequence_prefix(self, value: str) -> str:
        instance = getattr(self, "instance", None)
        if instance is None or value == instance.sequence_prefix:
            return value
        if instance.entries.exclude(number="").exists():
            raise serializers.ValidationError(
                f"Journal {instance.code} has already issued numbers with prefix "
                f"'{instance.sequence_prefix}'. Changing it now splits the "
                f"sequence in two, which is indistinguishable from a deleted "
                f"range. Create a new journal instead."
            )
        return value


# ---------------------------------------------------------------------------
# Journal entries
# ---------------------------------------------------------------------------

class JournalLineSerializer(TenantScopedSerializer):
    """One debit or credit.

    ``base_debit`` / ``base_credit`` are read-only: they are the amount
    converted at the entry's frozen ``exchange_rate`` and are what every
    report aggregates. Letting a client supply them would let the reporting
    currency disagree with the transaction currency by an arbitrary amount.
    """

    server_owned_fields = ("base_debit", "base_credit", "reconciled_at")

    debit = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)
    credit = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)
    base_debit = MoneyField(read_only=True)
    base_credit = MoneyField(read_only=True)
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True)

    class Meta:
        model = JournalLine
        fields = (
            "id",
            "line_number",
            "account",
            "account_code",
            "account_name",
            "description",
            "debit",
            "credit",
            "base_debit",
            "base_credit",
            "partner_type",
            "partner_id",
            "project",
            "department",
            "tax_rate",
            "reconciled_at",
        )
        # Assigned by the parent serializer from list position, so that
        # re-ordering lines in the UI cannot collide with
        # ``uq_line_number_per_entry``.
        read_only_fields = ("line_number",)

    def validate(self, attrs: dict) -> dict:
        debit = attrs.get("debit", ZERO) or ZERO
        credit = attrs.get("credit", ZERO) or ZERO
        # Mirrors ``ck_line_single_sided``. Caught here so the caller gets a
        # field error instead of a 409 from a constraint name.
        if (debit > ZERO) == (credit > ZERO):
            raise serializers.ValidationError(
                {
                    "debit": (
                        f"A journal line carries exactly one of debit or credit "
                        f"(got debit={debit}, credit={credit}). A negative amount "
                        f"belongs on the opposite side, not as a negative."
                    )
                }
            )
        return attrs


class LedgerLineSerializer(ReadOnlyModelSerializer):
    """A journal line as it appears in an account ledger, with its context.

    ``running_balance`` is injected by :meth:`AccountViewSet.ledger` rather
    than computed here: a running balance is a property of a *sequence* of
    rows, and a serializer that computed it per row would either re-query the
    whole history for every row or silently restart from zero on page two.
    """

    entry_number = serializers.CharField(source="entry.number", read_only=True)
    entry_date = serializers.DateField(source="entry.entry_date", read_only=True)
    entry_memo = serializers.CharField(source="entry.memo", read_only=True)
    journal_code = serializers.CharField(source="entry.journal.code", read_only=True)
    debit = MoneyField(read_only=True)
    credit = MoneyField(read_only=True)
    running_balance = serializers.SerializerMethodField()

    class Meta:
        model = JournalLine
        fields = (
            "id",
            "entry",
            "entry_number",
            "entry_date",
            "entry_memo",
            "journal_code",
            "line_number",
            "account",
            "description",
            "debit",
            "credit",
            "partner_type",
            "partner_id",
            "reconciled_at",
            "running_balance",
        )

    def get_running_balance(self, obj: JournalLine) -> Optional[str]:
        balances = self.context.get("running_balances") or {}
        value = balances.get(obj.pk)
        return None if value is None else f"{value:f}"


class JournalEntrySerializer(TenantScopedSerializer):
    """A journal entry with its lines, writable as one document while DRAFT.

    Why the lines are nested and written together
    ---------------------------------------------
    An entry and its lines are one fact, not two. Exposing lines as a separate
    collection means a client can PUT a header, fail to PUT the lines, and
    leave a half-written entry that satisfies no invariant. Writing them in
    one request inside one transaction means the entry either exists complete
    or does not exist.

    What is never writable
    ----------------------
    ``status``, ``number``, ``total_debit``, ``total_credit``, ``posted_at``,
    ``posted_by``, ``reversal_of`` and ``period``:

    * The **totals** are control figures. If a client could send them, it
      could send ``total_debit == total_credit`` for lines that do not
      balance, and ``ck_entry_balanced`` — which checks the *columns*, not the
      lines — would pass. The materialised totals only mean anything if the
      server is the only thing that writes them.
    * The **period** is derived from ``entry_date`` by
      :func:`~apps.accounting.services.posting.resolve_period`. A
      client-chosen period lets an entry dated in March be filed in April,
      which is the whole of period-shifting fraud.
    * The **status** is moved by ``POST /journal-entries/{id}/post``, ``/void``
      and ``/reverse``.

    Once ``status == POSTED`` the lines become read-only as well, matching the
    database trigger. Corrections to a posted entry are void (same period) or
    reverse (any later period), never an edit.
    """

    server_owned_fields = (
        "status",
        "number",
        "total_debit",
        "total_credit",
        "posted_at",
        "posted_by",
        "reversal_of",
        "void_reason",
        "period",
        "idempotency_key",
        "source_document_type",
        "source_document_id",
    )

    lines = JournalLineSerializer(many=True)
    total_debit = MoneyField(read_only=True)
    total_credit = MoneyField(read_only=True)
    exchange_rate = RateField(min_value=Decimal("0.000001"), required=False)
    journal_code = serializers.CharField(source="journal.code", read_only=True)
    period_name = serializers.CharField(source="period.name", read_only=True, default=None)
    is_posted = serializers.BooleanField(read_only=True)

    class Meta:
        model = JournalEntry
        fields = (
            "id",
            "journal",
            "journal_code",
            "period",
            "period_name",
            "number",
            "entry_date",
            "status",
            "is_posted",
            "source",
            "memo",
            "currency",
            "exchange_rate",
            "total_debit",
            "total_credit",
            "posted_at",
            "posted_by",
            "reversal_of",
            "void_reason",
            "source_document_type",
            "source_document_id",
            "lines",
            "created_at",
            "created_by",
            "updated_at",
        )

    # -- field shaping ------------------------------------------------------

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        instance = getattr(self, "instance", None)
        if instance is not None and getattr(instance, "status", None) != JournalEntry.Status.DRAFT:
            # Everything about a posted (or voided, or reversed) entry is
            # history. The trigger in migration 0004 refuses the UPDATE anyway;
            # this turns a 409 from a constraint name into a field error.
            for name in ("lines", "journal", "entry_date", "currency", "exchange_rate", "memo"):
                field = fields.get(name)
                if field is not None:
                    field.read_only = True
                    field.required = False
        return fields

    # -- validation ---------------------------------------------------------

    def validate_lines(self, lines: list[dict]) -> list[dict]:
        if not lines:
            raise serializers.ValidationError(
                "A journal entry needs lines. An entry with none cannot balance "
                "and cannot be posted."
            )
        return lines

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        if instance is not None and instance.status != JournalEntry.Status.DRAFT:
            raise serializers.ValidationError(
                f"Journal entry {instance.number or instance.id} is "
                f"{instance.get_status_display().lower()} and is part of the "
                f"permanent record. Void it (same period) or reverse it (any "
                f"later period) — it cannot be edited."
            )
        return attrs

    # -- write paths --------------------------------------------------------

    def create(self, validated_data: dict) -> JournalEntry:
        from django.db import transaction

        lines = validated_data.pop("lines", [])
        # A draft is always created as a DRAFT. Posting is a separate call so
        # that the balance check, the period lock and the number allocation all
        # happen in the posting service and nowhere else.
        validated_data["status"] = JournalEntry.Status.DRAFT
        validated_data["period"] = self._resolve_period(validated_data["entry_date"])

        with transaction.atomic():
            entry = super().create(validated_data)
            self._write_lines(entry, lines)
            self._refresh_totals(entry)
        entry.refresh_from_db()
        return entry

    def update(self, instance: JournalEntry, validated_data: dict) -> JournalEntry:
        from django.db import transaction

        lines = validated_data.pop("lines", None)
        entry_date = validated_data.get("entry_date")
        if entry_date is not None:
            validated_data["period"] = self._resolve_period(entry_date)

        with transaction.atomic():
            entry = super().update(instance, validated_data)
            if lines is not None:
                # Replace wholesale rather than diffing: line identity has no
                # business meaning on a draft, and a partial diff is how a
                # "removed" line survives and quietly unbalances the entry.
                entry.lines.all().delete()
                self._write_lines(entry, lines)
            self._refresh_totals(entry)
        entry.refresh_from_db()
        return entry

    # -- helpers ------------------------------------------------------------

    def _resolve_period(self, entry_date):
        from apps.accounting.services.posting import resolve_period

        tenant_id = self.get_tenant_id()
        try:
            # lock=False: this is a draft, nothing is being posted yet, and
            # taking a share lock here would make period close wait on people
            # merely typing.
            return resolve_period(tenant_id, entry_date, lock=False)
        except Exception as exc:  # noqa: BLE001 - re-raised as a field error
            raise serializers.ValidationError({"entry_date": str(exc)}) from exc

    def _write_lines(self, entry: JournalEntry, lines: list[dict]) -> None:
        rate = entry.exchange_rate or Decimal("1")
        rows = []
        for index, line in enumerate(lines, start=1):
            debit = line.get("debit") or ZERO
            credit = line.get("credit") or ZERO
            rows.append(
                JournalLine(
                    tenant_id=entry.tenant_id,
                    entry=entry,
                    line_number=index,
                    account=line["account"],
                    description=(line.get("description") or "")[:500],
                    debit=debit,
                    credit=credit,
                    base_debit=debit * rate,
                    base_credit=credit * rate,
                    partner_type=line.get("partner_type", ""),
                    partner_id=line.get("partner_id"),
                    project=line.get("project"),
                    department=line.get("department"),
                    tax_rate=line.get("tax_rate"),
                    created_by_id=self.get_actor_id(),
                )
            )
        JournalLine.objects.bulk_create(rows)

    @staticmethod
    def _refresh_totals(entry: JournalEntry) -> None:
        """Materialise the control totals from the lines that actually exist.

        Recomputed from the rows rather than from the submitted payload: those
        are the only two things that can disagree, and the rows are the ones
        the trial balance will read.
        """
        from django.db.models import Sum

        totals = entry.lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        JournalEntry.all_tenants.filter(pk=entry.pk).update(
            total_debit=totals["debit"] or ZERO,
            total_credit=totals["credit"] or ZERO,
        )


# ---------------------------------------------------------------------------
# Posting payload
# ---------------------------------------------------------------------------

class JournalEntryDraftLineInputSerializer(serializers.Serializer):
    """One line of a posting payload. Not a model serializer on purpose.

    :class:`~apps.accounting.services.posting.LineDraft` is inert — it has no
    ``save()`` — and that is the property we want to preserve all the way from
    the request body to ``post_entry``. Validating into a ModelSerializer
    would put a saveable ``JournalLine`` in the hands of the caller.
    """

    account = serializers.UUIDField()
    debit = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)
    credit = MoneyField(min_value=Decimal("0"), required=False, default=ZERO)
    description = serializers.CharField(
        required=False, allow_blank=True, max_length=500, default=""
    )
    partner_type = serializers.ChoiceField(
        choices=("customer", "vendor", "employee"), required=False, allow_blank=True,
        default="",
    )
    partner_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    project = serializers.UUIDField(required=False, allow_null=True, default=None)
    department = serializers.UUIDField(required=False, allow_null=True, default=None)
    tax_rate = serializers.UUIDField(required=False, allow_null=True, default=None)

    def validate(self, attrs: dict) -> dict:
        debit = attrs.get("debit") or ZERO
        credit = attrs.get("credit") or ZERO
        if (debit > ZERO) == (credit > ZERO):
            raise serializers.ValidationError(
                {
                    "debit": (
                        f"Exactly one of debit or credit must be non-zero "
                        f"(got debit={debit}, credit={credit})."
                    )
                }
            )
        return attrs


class JournalEntryDraftInputSerializer(serializers.Serializer):
    """The payload for posting a manual journal entry.

    Everything the database and the posting service will check is checked here
    first, because the error a user can act on is "debits exceed credits by
    12.40 EGP", not ``IntegrityError: ck_entry_balanced``.

    The three rules, and why each is worth its own message:

    * **At least two lines.** A one-sided entry cannot balance by
      construction. Reporting it as an imbalance would be technically true and
      unhelpful.
    * **Exactly one of debit or credit per line.** Both-sided or neither-sided
      lines are the usual shape of a copy-paste error, and a line carrying
      both is ambiguous about which side the user meant.
    * **Debits equal credits.** The message names the *difference* and its
      currency. "Does not balance" makes the user re-add the column by hand;
      "debits exceed credits by 12.40 EGP" points straight at the typo.

    ``to_draft()`` converts the validated payload into an inert
    :class:`~apps.accounting.services.posting.JournalEntryDraft`, which is the
    only thing ``post_entry`` accepts.
    """

    journal_code = serializers.CharField(max_length=20)
    entry_date = serializers.DateField()
    currency = serializers.CharField(min_length=3, max_length=3)
    memo = serializers.CharField(
        required=False, allow_blank=True, max_length=500, default=""
    )
    exchange_rate = RateField(
        required=False, min_value=Decimal("0.000001"), default=Decimal("1")
    )
    source = serializers.CharField(required=False, allow_blank=True, default="")
    lines = JournalEntryDraftLineInputSerializer(many=True)
    idempotency_key = serializers.CharField(
        required=False, allow_blank=True, max_length=128, default=""
    )

    def validate_currency(self, value: str) -> str:
        return value.upper()

    def validate_lines(self, lines: list[dict]) -> list[dict]:
        if len(lines) < 2:
            raise serializers.ValidationError(
                "A journal entry needs at least two lines: a single-sided entry "
                "cannot balance."
            )
        return lines

    def validate(self, attrs: dict) -> dict:
        from apps.core.fields import quantize_currency

        lines = attrs["lines"]
        currency = attrs["currency"]

        total_debit = sum((line.get("debit") or ZERO for line in lines), ZERO)
        total_credit = sum((line.get("credit") or ZERO for line in lines), ZERO)

        # Compare at the currency's own precision, exactly as
        # ``validate_draft`` does. Comparing at full numeric(19,6) precision
        # would reject entries that the posting service would then accept,
        # which is worse than either behaviour on its own.
        rounded_debit = quantize_currency(total_debit, currency)
        rounded_credit = quantize_currency(total_credit, currency)
        difference = rounded_debit - rounded_credit

        if difference != ZERO:
            side = "exceed" if difference > ZERO else "fall short of"
            raise serializers.ValidationError(
                {
                    "lines": (
                        f"Entry does not balance: debits ({rounded_debit} "
                        f"{currency}) {side} credits ({rounded_credit} "
                        f"{currency}) by {abs(difference)} {currency}."
                    )
                }
            )
        if rounded_debit <= ZERO:
            raise serializers.ValidationError(
                {"lines": "A journal entry must move a non-zero amount."}
            )
        return attrs

    def to_draft(self):
        """Build the inert draft the posting service consumes."""
        from apps.accounting.services.posting import JournalEntryDraft, LineDraft

        data = self.validated_data
        draft = JournalEntryDraft(
            journal_code=data["journal_code"],
            entry_date=data["entry_date"],
            currency=data["currency"],
            memo=data.get("memo", ""),
            source=data.get("source") or JournalEntry.Source.MANUAL,
            exchange_rate=data.get("exchange_rate") or Decimal("1"),
            idempotency_key=data.get("idempotency_key", ""),
        )
        for line in data["lines"]:
            draft.add(
                LineDraft(
                    account_id=line["account"],
                    debit=line.get("debit") or ZERO,
                    credit=line.get("credit") or ZERO,
                    description=line.get("description", ""),
                    partner_type=line.get("partner_type", ""),
                    partner_id=line.get("partner_id"),
                    project_id=line.get("project"),
                    department_id=line.get("department"),
                    tax_rate_id=line.get("tax_rate"),
                )
            )
        return draft


# ---------------------------------------------------------------------------
# FX
# ---------------------------------------------------------------------------

class ExchangeRateSerializer(TenantScopedSerializer):
    """One day's rate between two currencies.

    Rates are per tenant because a group may run a corporate rate table that
    differs from the central bank's, and a shared table would silently restate
    one tenant's reports when another corrected a rate.
    """

    rate = RateField(min_value=Decimal("0.000001"))

    class Meta:
        model = ExchangeRate
        fields = (
            "id",
            "from_currency",
            "to_currency",
            "rate",
            "rate_date",
            "source",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)
        source = attrs.get("from_currency", getattr(instance, "from_currency", None))
        target = attrs.get("to_currency", getattr(instance, "to_currency", None))
        if source and target and source == target:
            raise serializers.ValidationError(
                {
                    "to_currency": (
                        "A currency's rate against itself is always 1 and is "
                        "never stored — storing it invites a row that says "
                        "otherwise."
                    )
                }
            )
        return attrs


__all__ = [
    "AccountSerializer",
    "AccountTreeSerializer",
    "AccountType",
    "TaxRateSerializer",
    "FiscalYearSerializer",
    "FiscalPeriodSerializer",
    "JournalSerializer",
    "JournalLineSerializer",
    "LedgerLineSerializer",
    "JournalEntrySerializer",
    "JournalEntryDraftLineInputSerializer",
    "JournalEntryDraftInputSerializer",
    "ExchangeRateSerializer",
]
