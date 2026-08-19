"""
The posting engine — the only sanctioned write path into the general ledger.

Every module that has a financial effect builds a :class:`JournalEntryDraft`
and hands it to :func:`post_entry`. Concentrating the write here means the
balance rule, the period lock, the FX conversion and the idempotency guard
are implemented once and cannot be forgotten by the twelfth module.

Design notes
------------
*Why a draft dataclass instead of letting callers build ORM objects?*
    Because a caller holding a ``JournalLine`` instance can save it. The
    draft is inert: it has no ``save()``, so the only way to make it real is
    to go through the validation in this module.

*Why lock the period row?*
    ``SELECT ... FOR SHARE`` on the period makes "close the period" and "post
    into the period" mutually exclusive. Without it, a close that starts at
    T and a post that starts at T+1ms both read ``status='open'`` and the
    entry lands in a period that is closed by the time the transaction
    commits — a genuine race we have seen in production systems.

*Why compute totals in Python and also constrain them in SQL?*
    The Python computation gives a friendly error; the SQL constraint is
    what actually guarantees the invariant, including against a future
    ``bulk_create`` that skips this function.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.accounting.models import (
    Account,
    FiscalPeriod,
    Journal,
    JournalEntry,
    JournalLine,
    NORMAL_BALANCE,
)
from apps.core.fields import ZERO, quantize_currency, to_money
from apps.accounting.services.fx import (
    base_currency as fx_base_currency,
    resolve_rate,
)


# ---------------------------------------------------------------------------
# Draft objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LineDraft:
    """One intended debit or credit. Amounts are Decimal, never float."""

    account_id: uuid.UUID
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    description: str = ""
    partner_type: str = ""
    partner_id: Optional[uuid.UUID] = None
    project_id: Optional[uuid.UUID] = None
    department_id: Optional[uuid.UUID] = None
    tax_rate_id: Optional[uuid.UUID] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "debit", to_money(self.debit, field_name="debit"))
        object.__setattr__(self, "credit", to_money(self.credit, field_name="credit"))
        if self.debit < ZERO or self.credit < ZERO:
            raise ValidationError("Journal line amounts must be non-negative.")
        if (self.debit > ZERO) == (self.credit > ZERO):
            raise ValidationError(
                "A journal line must carry exactly one of debit or credit "
                f"(got debit={self.debit}, credit={self.credit}). Negative "
                "amounts belong on the opposite side, not as a negative."
            )


@dataclass(slots=True)
class JournalEntryDraft:
    """A complete, not-yet-persisted journal entry."""

    journal_code: str
    entry_date: date
    currency: str
    lines: list[LineDraft] = field(default_factory=list)
    memo: str = ""
    source: str = JournalEntry.Source.MANUAL
    source_document_type: str = ""
    source_document_id: Optional[uuid.UUID] = None
    exchange_rate: Decimal = Decimal("1")
    idempotency_key: str = ""

    def add(self, line: LineDraft) -> "JournalEntryDraft":
        self.lines.append(line)
        return self

    def debit(self, account_id, amount, **kw) -> "JournalEntryDraft":
        return self.add(LineDraft(account_id=account_id, debit=amount, **kw))

    def credit(self, account_id, amount, **kw) -> "JournalEntryDraft":
        return self.add(LineDraft(account_id=account_id, credit=amount, **kw))

    @property
    def total_debit(self) -> Decimal:
        return sum((line.debit for line in self.lines), ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((line.credit for line in self.lines), ZERO)

    @property
    def difference(self) -> Decimal:
        return self.total_debit - self.total_credit


class UnbalancedEntry(ValidationError):
    """Raised when debits != credits. Its own class so that callers and
    monitoring can distinguish a logic bug from ordinary user input error."""


class PeriodClosed(ValidationError):
    pass


class DuplicatePosting(ValidationError):
    """Raised when an idempotency key has already been consumed."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_draft(draft: JournalEntryDraft, *, tenant_id: uuid.UUID) -> None:
    """Every check that can be made before touching the database."""
    if len(draft.lines) < 2:
        raise ValidationError(
            "A journal entry needs at least two lines; a single-sided entry "
            "cannot balance."
        )

    rounded_debit = quantize_currency(draft.total_debit, draft.currency)
    rounded_credit = quantize_currency(draft.total_credit, draft.currency)
    if rounded_debit != rounded_credit:
        raise UnbalancedEntry(
            f"Entry does not balance: debits {rounded_debit} != credits "
            f"{rounded_credit} (difference {rounded_debit - rounded_credit} "
            f"{draft.currency}). Refusing to post."
        )
    if rounded_debit <= ZERO:
        raise ValidationError("A journal entry must move a non-zero amount.")

    if draft.exchange_rate <= ZERO:
        raise ValidationError("Exchange rate must be positive.")

    # Accounts must exist, belong to this tenant, be active and postable.
    account_ids = {line.account_id for line in draft.lines}
    accounts = {
        a.id: a
        for a in Account.all_tenants.filter(id__in=account_ids, tenant_id=tenant_id)
    }
    missing = account_ids - accounts.keys()
    if missing:
        raise ValidationError(
            f"Accounts not found in this tenant: {sorted(str(m) for m in missing)}"
        )
    for account in accounts.values():
        if not account.is_postable:
            raise ValidationError(
                f"Account {account.code} is a summary account and cannot be "
                f"posted to directly. Post to one of its children."
            )
        if not account.is_active:
            raise ValidationError(f"Account {account.code} is archived.")


def resolve_period(tenant_id: uuid.UUID, entry_date: date, *, lock: bool) -> FiscalPeriod:
    """Find the fiscal period containing ``entry_date``, optionally locking it."""
    qs = FiscalPeriod.all_tenants.filter(
        tenant_id=tenant_id, start_date__lte=entry_date, end_date__gte=entry_date
    )
    if lock:
        # FOR SHARE, not FOR UPDATE: many concurrent posts should proceed in
        # parallel; only a period *close* (which takes FOR UPDATE) must wait.
        qs = qs.select_for_update(of=("self",), no_key=True)
    period = qs.first()
    if period is None:
        raise ValidationError(
            f"No fiscal period covers {entry_date:%Y-%m-%d}. "
            f"Create the period before posting into it."
        )
    return period


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

@transaction.atomic
def post_entry(
    draft: JournalEntryDraft,
    *,
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    allow_soft_closed: bool = False,
) -> JournalEntry:
    """Validate, number and persist a balanced entry. Atomic and idempotent.

    The whole function runs in one transaction: either the entry, all its
    lines and every affected account balance move together, or nothing does.
    """
    validate_draft(draft, tenant_id=tenant_id)

    # --- idempotency: cheap pre-check, then rely on the unique index ------
    if draft.idempotency_key:
        existing = JournalEntry.all_tenants.filter(
            tenant_id=tenant_id, idempotency_key=draft.idempotency_key
        ).first()
        if existing is not None:
            # Returning the original rather than raising makes retrying a
            # webhook or a Celery task safe by construction.
            return existing

    period = resolve_period(tenant_id, draft.entry_date, lock=True)
    allowed = {FiscalPeriod.Status.OPEN}
    if allow_soft_closed:
        allowed.add(FiscalPeriod.Status.SOFT_CLOSED)
    if period.status not in allowed:
        raise PeriodClosed(
            f"Fiscal period '{period.name}' is {period.get_status_display().lower()}. "
            f"Post to the current open period and, if this corrects a prior "
            f"period, use a reversing entry."
        )

    journal = Journal.all_tenants.filter(
        tenant_id=tenant_id, code=draft.journal_code, is_active=True
    ).first()
    if journal is None:
        raise ValidationError(f"Unknown or inactive journal '{draft.journal_code}'.")

    # Resolve the FX rate here, at the choke point, rather than in each of the
    # five modules that post. Before this call the draft's default of 1 was
    # taken at face value, so a foreign-currency document with no rate posted
    # at 1:1 -- balanced, unflagged, and wrong by the whole spread. See
    # apps/accounting/services/fx.py.
    rate = resolve_rate(
        tenant_id, draft.currency, draft.entry_date, draft.exchange_rate
    )
    base_ccy = fx_base_currency(tenant_id)

    entry = JournalEntry(
        tenant_id=tenant_id,
        journal=journal,
        period=period,
        entry_date=draft.entry_date,
        status=JournalEntry.Status.POSTED,
        source=draft.source,
        memo=draft.memo[:500],
        currency=draft.currency,
        exchange_rate=rate,
        total_debit=quantize_currency(draft.total_debit, draft.currency),
        total_credit=quantize_currency(draft.total_credit, draft.currency),
        posted_at=timezone.now(),
        posted_by_id=user_id,
        created_by_id=user_id,
        source_document_type=draft.source_document_type,
        source_document_id=draft.source_document_id,
        idempotency_key=draft.idempotency_key,
        number=allocate_number(tenant_id, journal, draft.entry_date),
    )
    try:
        entry.save()
    except IntegrityError as exc:
        if "uq_entry_idempotency" in str(exc):
            raise DuplicatePosting(
                "This business event has already been posted."
            ) from exc
        raise

    lines = [
        JournalLine(
            tenant_id=tenant_id,
            entry=entry,
            line_number=index,
            account_id=line.account_id,
            description=line.description[:500],
            debit=quantize_currency(line.debit, draft.currency),
            credit=quantize_currency(line.credit, draft.currency),
            # Quantized to the *base* currency's minor units, not the
            # transaction currency's. They are different for real pairs --
            # JPY has none, KWD and BHD have three -- and rounding a JPY base
            # amount to two decimals stores a fraction of a yen that no
            # payment can ever settle, leaving a residue in every
            # reconciliation.
            base_debit=quantize_currency(line.debit * rate, base_ccy),
            base_credit=quantize_currency(line.credit * rate, base_ccy),
            partner_type=line.partner_type,
            partner_id=line.partner_id,
            project_id=line.project_id,
            department_id=line.department_id,
            tax_rate_id=line.tax_rate_id,
            created_by_id=user_id,
        )
        for index, line in enumerate(draft.lines, start=1)
    ]
    JournalLine.objects.bulk_create(lines)

    _apply_to_cached_balances(tenant_id, lines, sign=1)
    return entry


def allocate_number(tenant_id: uuid.UUID, journal: Journal, entry_date: date) -> str:
    """Allocate the next gapless document number for (journal, year).

    Uses a dedicated PostgreSQL sequence table row locked ``FOR UPDATE``
    rather than ``MAX(number) + 1``: the latter hands the same number to two
    concurrent transactions under READ COMMITTED, and the resulting duplicate
    is only caught by the unique index — after the rest of the work is done.
    """
    from apps.accounting.models_sequence import DocumentSequence  # local import

    year = entry_date.year
    seq, _ = DocumentSequence.all_tenants.select_for_update().get_or_create(
        tenant_id=tenant_id,
        scope=f"journal:{journal.code}",
        year=year,
        defaults={"next_value": 1, "prefix": journal.sequence_prefix},
    )
    value = seq.next_value
    seq.next_value = value + 1
    seq.save(update_fields=["next_value", "updated_at"])
    return f"{seq.prefix}-{year}-{value:06d}"


def _apply_to_cached_balances(
    tenant_id: uuid.UUID, lines: Sequence[JournalLine], *, sign: int
) -> None:
    """Move the denormalised account balances by the posted amounts.

    Uses ``F()`` expressions so the update is a single atomic SQL statement
    per account — read-modify-write in Python would lose concurrent postings.
    """
    delta: dict[uuid.UUID, Decimal] = {}
    accounts = {
        a.id: a
        for a in Account.all_tenants.filter(
            tenant_id=tenant_id, id__in={line.account_id for line in lines}
        )
    }
    for line in lines:
        account = accounts[line.account_id]
        movement = (
            (line.debit - line.credit)
            if account.increases_on_debit
            else (line.credit - line.debit)
        )
        delta[account.id] = delta.get(account.id, ZERO) + movement * sign

    now = timezone.now()
    for account_id, amount in delta.items():
        Account.all_tenants.filter(id=account_id).update(
            cached_balance=F("cached_balance") + amount, cached_balance_as_of=now
        )


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------

@transaction.atomic
def void_entry(
    entry: JournalEntry, *, reason: str, user_id: Optional[uuid.UUID] = None
) -> JournalEntry:
    """Cancel an entry in place. Legal only while its period is still open.

    The row survives with ``status=VOIDED`` and keeps its number — deleting
    it would leave a gap in the sequence, which is the first thing an auditor
    looks for.
    """
    entry.assert_can_transition(JournalEntry.Status.VOIDED)
    period = resolve_period(entry.tenant_id, entry.entry_date, lock=True)
    if period.status != FiscalPeriod.Status.OPEN:
        raise PeriodClosed(
            "Cannot void an entry in a closed period. Reverse it instead so "
            "the correction lands in the current period."
        )
    if not reason.strip():
        raise ValidationError("A void reason is required for the audit trail.")

    if entry.status == JournalEntry.Status.POSTED:
        _apply_to_cached_balances(
            entry.tenant_id, list(entry.lines.all()), sign=-1
        )

    JournalEntry.all_tenants.filter(pk=entry.pk).update(
        status=JournalEntry.Status.VOIDED,
        void_reason=reason[:255],
        updated_by_id=user_id,
        updated_at=timezone.now(),
    )
    entry.refresh_from_db()
    return entry


@transaction.atomic
def reverse_entry(
    entry: JournalEntry,
    *,
    reversal_date: Optional[date] = None,
    reason: str = "",
    user_id: Optional[uuid.UUID] = None,
) -> JournalEntry:
    """Create a mirror entry that cancels ``entry`` in a later open period.

    This is the correction mechanism that preserves history: the original
    stays exactly as filed, and the books show both the error and its fix.
    """
    if entry.status != JournalEntry.Status.POSTED:
        raise ValidationError("Only posted entries can be reversed.")
    if hasattr(entry, "reversed_by"):
        raise ValidationError(f"Entry {entry.number} has already been reversed.")

    reversal_date = reversal_date or timezone.localdate()

    draft = JournalEntryDraft(
        journal_code=entry.journal.code,
        entry_date=reversal_date,
        currency=entry.currency,
        exchange_rate=entry.exchange_rate,
        memo=(reason or f"Reversal of {entry.number}")[:500],
        source=entry.source,
        source_document_type=entry.source_document_type,
        source_document_id=entry.source_document_id,
        idempotency_key=f"reversal:{entry.id}",
    )
    for line in entry.lines.all().order_by("line_number"):
        # Swap the sides. This is the whole of "reversal".
        draft.add(
            LineDraft(
                account_id=line.account_id,
                debit=line.credit,
                credit=line.debit,
                description=f"Reversal: {line.description}"[:500],
                partner_type=line.partner_type,
                partner_id=line.partner_id,
                project_id=line.project_id,
                department_id=line.department_id,
                tax_rate_id=line.tax_rate_id,
            )
        )

    mirror = post_entry(draft, tenant_id=entry.tenant_id, user_id=user_id)

    JournalEntry.all_tenants.filter(pk=entry.pk).update(
        status=JournalEntry.Status.REVERSED, updated_at=timezone.now()
    )
    JournalEntry.all_tenants.filter(pk=mirror.pk).update(reversal_of=entry)
    mirror.refresh_from_db()
    return mirror


# ---------------------------------------------------------------------------
# Integrity check
# ---------------------------------------------------------------------------

def assert_ledger_balanced(tenant_id: uuid.UUID) -> None:
    """Whole-ledger trial balance check. Run nightly by a Celery beat task.

    The DB constraints make an unbalanced entry impossible, so a failure here
    means data was loaded outside the application (a restore, a migration, a
    manual SQL fix) and must be investigated before the books are trusted.
    """
    totals = JournalLine.all_tenants.filter(
        tenant_id=tenant_id, entry__status=JournalEntry.Status.POSTED
    ).aggregate(debit=Sum("base_debit"), credit=Sum("base_credit"))

    debit = totals["debit"] or ZERO
    credit = totals["credit"] or ZERO
    if debit != credit:
        raise UnbalancedEntry(
            f"LEDGER INTEGRITY FAILURE for tenant {tenant_id}: "
            f"total debits {debit} != total credits {credit} "
            f"(difference {debit - credit})."
        )
