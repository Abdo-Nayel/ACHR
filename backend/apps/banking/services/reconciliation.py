"""
The bank reconciliation matching engine.

The job: given one line the bank reported, find the ledger line(s) that
represent the same money, and rank the candidates by how confident we are.

Design position — this module *suggests*, a human *decides*
----------------------------------------------------------
Auto-matching is the feature everybody asks for and the one that does the
most damage when it is too eager, so the threshold is deliberately high and
configurable (:data:`AUTO_APPLY_THRESHOLD`).

The asymmetry is the whole argument. An unmatched transaction is *visible*:
it sits at the top of the reconciliation screen, the session refuses to
close while the difference is non-zero, and somebody deals with it today. A
wrongly auto-matched transaction is *invisible*: the difference nets to zero,
the screen is empty, the session closes, and the error surfaces months later
as "the bank says we were paid but the customer's statement still shows the
invoice open" — by which time the customer has been chased for money they
already sent, a credit control relationship is damaged, and unwinding the
match means touching a closed period.

So: leaving a transaction unmatched costs a few minutes of clerical work.
Auto-applying a wrong match costs a reconciliation nobody can trust, and it
destroys the *reason* the control exists. When in doubt, do not match.

Scoring
-------
Scores are Decimal in [0, 1] and additive-free — each rule returns a fixed
score rather than summing bonuses, because a sum of small signals reaches
0.9 without any single strong signal, which is exactly the confident-and-wrong
case above.

======  ================================================================
Score   Rule
======  ================================================================
1.00    Exact amount, date within ``EXACT_DATE_WINDOW`` days
0.90    Exact amount, reference/document number found in the description
0.80    Exact amount, date within ``NEAR_DATE_WINDOW`` days
0.75    Exact amount, strong payee-name similarity
0.60    Amount within ``AMOUNT_TOLERANCE``, payee-name similarity
0.40    Amount within tolerance, date close, nothing else agrees
======  ================================================================
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Optional, Sequence

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.accounting.models import JournalEntry, JournalLine
from apps.banking.models import (
    BankAccount,
    BankTransaction,
    ReconciliationMatch,
    ReconciliationSession,
)
from apps.core.fields import ZERO, to_money

__all__ = [
    "Candidate",
    "suggest_matches",
    "confirm_match",
    "unmatch",
    "auto_reconcile",
    "close_session",
    "UnbalancedSession",
]


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Only matches at or above this confidence are applied without a human.
#: 0.95 means "exact amount and a date that lines up, or exact amount and the
#: invoice number literally printed in the bank narrative". Everything else
#: waits for a person. Deliberately not exposed in the UI as a slider: the
#: person who would lower it is precisely the person under time pressure at
#: month end.
AUTO_APPLY_THRESHOLD: Decimal = Decimal("0.95")

#: Days between the ledger date and the bank date for a "same day-ish" match.
EXACT_DATE_WINDOW: int = 3
NEAR_DATE_WINDOW: int = 15
#: How far back to look for candidates at all. Beyond this the noise (an old
#: invoice for a coincidentally identical amount) outweighs the signal.
SEARCH_WINDOW_DAYS: int = 120

#: Absolute tolerance for a "nearly equal" amount. Small and absolute rather
#: than a percentage: the real causes of a small difference (a fixed transfer
#: fee, a rounding of the last minor unit) do not scale with the amount, and
#: a percentage tolerance on a 500,000 transfer would swallow a 400 error.
AMOUNT_TOLERANCE: Decimal = Decimal("1.000000")

#: Below this, two names are not the same counterparty.
NAME_SIMILARITY_FLOOR: float = 0.72

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_NOISE_TOKENS = frozenset(
    {
        "ltd", "llc", "inc", "gmbh", "sarl", "sa", "plc", "co", "company",
        "the", "and", "for", "payment", "pmt", "transfer", "trf", "ref",
        "invoice", "inv", "bill", "from", "to", "via", "bank",
    }
)


class UnbalancedSession(ValidationError):
    """Raised when someone tries to close a session that does not balance."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One possible explanation of a bank line, with why we believe it."""

    journal_line_id: uuid.UUID
    entry_id: uuid.UUID
    entry_number: str
    entry_date: date
    amount: Decimal
    description: str
    confidence: Decimal
    match_type: str
    reasons: tuple[str, ...]

    @property
    def is_auto_applicable(self) -> bool:
        return self.confidence >= AUTO_APPLY_THRESHOLD


# ---------------------------------------------------------------------------
# Suggestion
# ---------------------------------------------------------------------------

def suggest_matches(
    bank_transaction: BankTransaction,
    *,
    limit: int = 10,
    search_window_days: int = SEARCH_WINDOW_DAYS,
) -> list[Candidate]:
    """Rank the ledger lines that could explain ``bank_transaction``.

    Candidate set: unreconciled lines on the bank account's own ledger
    account, in posted entries, within the search window. Restricting to the
    *bank's* ledger account is not an optimisation — a payment that never
    touched the cash account cannot be what cleared the bank, and offering it
    invites a match that balances the screen while leaving the books wrong.
    """
    tenant_id = bank_transaction.tenant_id
    account_id = bank_transaction.bank_account.ledger_account_id
    txn_date = bank_transaction.transaction_date
    amount = bank_transaction.amount

    # A bank credit (money in) clears a ledger *debit* to the cash account,
    # and vice versa. Filtering on the correct side halves the candidate set
    # and, more importantly, makes a sign-confused suggestion impossible.
    side_filter = Q(debit__gt=0) if amount > ZERO else Q(credit__gt=0)
    magnitude = abs(amount)

    lines = (
        JournalLine.all_tenants.filter(
            tenant_id=tenant_id,
            account_id=account_id,
            reconciled_at__isnull=True,
            entry__status=JournalEntry.Status.POSTED,
            entry__entry_date__gte=txn_date - timedelta(days=search_window_days),
            entry__entry_date__lte=txn_date + timedelta(days=search_window_days),
        )
        .filter(side_filter)
        .select_related("entry")
    )

    # Already partially matched lines are still candidates, but only for
    # their residual — this is what makes instalment payments work.
    consumed = _consumed_amounts(tenant_id, [line.id for line in lines])

    haystack = _normalise(
        " ".join(
            filter(
                None,
                [
                    bank_transaction.description,
                    bank_transaction.reference,
                    bank_transaction.counterparty_name,
                ],
            )
        )
    )
    payee = _normalise(bank_transaction.counterparty_name or bank_transaction.description)

    candidates: list[Candidate] = []
    for line in lines:
        line_amount = line.debit if line.debit > ZERO else line.credit
        residual = line_amount - consumed.get(line.id, ZERO)
        if residual <= ZERO:
            continue

        scored = _score(
            residual=residual,
            magnitude=magnitude,
            line=line,
            txn_date=txn_date,
            haystack=haystack,
            payee=payee,
        )
        if scored is None:
            continue
        confidence, match_type, reasons = scored
        candidates.append(
            Candidate(
                journal_line_id=line.id,
                entry_id=line.entry_id,
                entry_number=line.entry.number,
                entry_date=line.entry.entry_date,
                amount=residual,
                description=line.description,
                confidence=confidence,
                match_type=match_type,
                reasons=reasons,
            )
        )

    # Highest confidence first; ties broken by date proximity, because when
    # two identical amounts are equally plausible the nearer one almost
    # always is the one.
    candidates.sort(
        key=lambda c: (-c.confidence, abs((c.entry_date - txn_date).days))
    )
    return candidates[:limit]


def _score(
    *,
    residual: Decimal,
    magnitude: Decimal,
    line: JournalLine,
    txn_date: date,
    haystack: str,
    payee: str,
) -> Optional[tuple[Decimal, str, tuple[str, ...]]]:
    """Return ``(confidence, match_type, reasons)`` or None for no match.

    Each rule returns a *fixed* score rather than accumulating bonuses.
    Summing weak signals is how a matcher reaches 0.9 on three coincidences
    and auto-applies something no human would have accepted.
    """
    day_gap = abs((line.entry.entry_date - txn_date).days)
    exact_amount = residual == magnitude
    near_amount = abs(residual - magnitude) <= AMOUNT_TOLERANCE

    reference_hit = _reference_hit(line, haystack)
    name_score = _name_similarity(payee, _normalise(line.description))

    if exact_amount and day_gap <= EXACT_DATE_WINDOW:
        return (
            Decimal("1.000000"),
            ReconciliationMatch.MatchType.AUTO_EXACT,
            (f"amount equal ({residual})", f"dates {day_gap}d apart"),
        )
    if exact_amount and reference_hit:
        return (
            Decimal("0.900000"),
            ReconciliationMatch.MatchType.AUTO_EXACT,
            (f"amount equal ({residual})", f"reference '{reference_hit}' in narrative"),
        )
    if exact_amount and day_gap <= NEAR_DATE_WINDOW:
        return (
            Decimal("0.800000"),
            ReconciliationMatch.MatchType.AUTO_FUZZY,
            (f"amount equal ({residual})", f"dates {day_gap}d apart"),
        )
    if exact_amount and name_score >= NAME_SIMILARITY_FLOOR:
        return (
            Decimal("0.750000"),
            ReconciliationMatch.MatchType.AUTO_FUZZY,
            (f"amount equal ({residual})", f"payee similarity {name_score:.2f}"),
        )
    if near_amount and name_score >= NAME_SIMILARITY_FLOOR:
        return (
            Decimal("0.600000"),
            ReconciliationMatch.MatchType.AUTO_FUZZY,
            (
                f"amount within tolerance (Δ {abs(residual - magnitude)})",
                f"payee similarity {name_score:.2f}",
            ),
        )
    if near_amount and day_gap <= EXACT_DATE_WINDOW:
        return (
            Decimal("0.400000"),
            ReconciliationMatch.MatchType.AUTO_FUZZY,
            (
                f"amount within tolerance (Δ {abs(residual - magnitude)})",
                f"dates {day_gap}d apart",
            ),
        )
    if exact_amount:
        # Right amount, nothing else agrees. Worth showing to a human,
        # nowhere near worth applying automatically.
        return (
            Decimal("0.300000"),
            ReconciliationMatch.MatchType.AUTO_FUZZY,
            (f"amount equal ({residual}) but {day_gap}d apart",),
        )
    return None


# ---------------------------------------------------------------------------
# Applying and undoing matches
# ---------------------------------------------------------------------------

@transaction.atomic
def confirm_match(
    *,
    bank_transaction: BankTransaction,
    journal_line: JournalLine | uuid.UUID,
    matched_amount: Decimal | str | int | None = None,
    match_type: str = ReconciliationMatch.MatchType.MANUAL,
    confidence: Decimal | str | int = 1,
    session: Optional[ReconciliationSession] = None,
    user_id: Optional[uuid.UUID] = None,
    notes: str = "",
) -> ReconciliationMatch:
    """Create one confirmed link between a bank line and a ledger line.

    Locks the bank transaction for the duration. Two reviewers working the
    same list at month end will otherwise both see it unmatched, both match
    it to different invoices, and the bank line ends up over-allocated —
    which nets to zero on the summary screen and is therefore never noticed.

    ``matched_amount`` defaults to the smaller of the two residuals, which is
    the only allocation that can never over-apply either side.
    """
    tenant_id = bank_transaction.tenant_id
    # Lock first, then re-read the residuals under the lock.
    locked = (
        BankTransaction.all_tenants.filter(pk=bank_transaction.pk)
        .select_for_update()
        .get()
    )
    line = _resolve_line(journal_line, tenant_id)

    txn_residual = _bank_residual(locked)
    line_residual = _line_residual(line, tenant_id)
    if txn_residual <= ZERO:
        raise ValidationError(
            f"Bank transaction {locked.pk} is already fully matched."
        )
    if line_residual <= ZERO:
        raise ValidationError(
            f"Journal line {line.pk} is already fully reconciled."
        )

    amount = (
        min(txn_residual, line_residual)
        if matched_amount is None
        else to_money(matched_amount, field_name="matched_amount")
    )
    if amount <= ZERO:
        raise ValidationError({"matched_amount": "A match must move a positive amount."})
    if amount > txn_residual:
        raise ValidationError(
            {
                "matched_amount": (
                    f"Cannot allocate {amount}: only {txn_residual} of the bank "
                    f"line is unmatched."
                )
            }
        )
    if amount > line_residual:
        raise ValidationError(
            {
                "matched_amount": (
                    f"Cannot allocate {amount}: only {line_residual} of the "
                    f"ledger line is unreconciled."
                )
            }
        )

    now = timezone.now()
    match = ReconciliationMatch.objects.create(
        tenant_id=tenant_id,
        bank_transaction=locked,
        journal_line=line,
        session=session,
        currency=locked.currency,
        matched_amount=amount,
        match_type=match_type,
        confidence_score=to_money(confidence, field_name="confidence"),
        notes=notes[:500],
        confirmed_by_id=user_id,
        confirmed_at=now,
        created_by_id=user_id,
    )

    # The bank line is MATCHED only when nothing of it is left over. A
    # partially allocated line must stay on the worklist, or the residual
    # silently becomes a permanent unexplained difference.
    if amount >= txn_residual:
        locked.status = BankTransaction.Status.MATCHED
        locked.matched_at = now
        locked.matched_by_id = user_id
        locked.save(
            update_fields=["status", "matched_at", "matched_by", "updated_at"]
        )

    if amount >= line_residual:
        # Stamped on the ledger line so the unreconciled-items report is an
        # index scan rather than a NOT EXISTS over the match table.
        JournalLine.all_tenants.filter(pk=line.pk).update(reconciled_at=now)

    return match


@transaction.atomic
def unmatch(
    match: ReconciliationMatch, *, reason: str = "", user_id: Optional[uuid.UUID] = None
) -> None:
    """Undo a match and put both sides back on the worklist.

    Deleting the link is correct here and is not in tension with the
    append-only rule elsewhere: a match is an *assertion about* two records,
    not a financial record itself. Nothing in the ledger changes — no entry
    is created, amended or removed — so there is no audit trail to destroy
    beyond the assertion, and keeping tombstone rows would only complicate
    every residual calculation. Undoing a wrong match must stay cheap, or
    reviewers work around it by posting adjusting entries, which *does*
    corrupt the ledger.
    """
    tenant_id = match.tenant_id
    bank_transaction = (
        BankTransaction.all_tenants.filter(pk=match.bank_transaction_id)
        .select_for_update()
        .get()
    )
    line_id = match.journal_line_id
    if match.session_id:
        session = ReconciliationSession.all_tenants.filter(
            pk=match.session_id
        ).first()
        if session is not None and session.status == ReconciliationSession.Status.CLOSED:
            raise ValidationError(
                "This match belongs to a closed reconciliation. Reopen the "
                "period or post a correcting entry instead."
            )

    # Instance delete, not queryset delete: ``TenantQuerySet.delete()`` is
    # disabled precisely so nobody bulk-deletes tenant data by accident.
    match.delete()

    bank_transaction.status = BankTransaction.Status.UNMATCHED
    bank_transaction.matched_at = None
    bank_transaction.matched_by = None
    bank_transaction.notes = (
        f"{bank_transaction.notes} | unmatched: {reason}".strip(" |")[:500]
    )
    bank_transaction.updated_by_id = user_id
    bank_transaction.save(
        update_fields=[
            "status", "matched_at", "matched_by", "notes", "updated_by", "updated_at"
        ]
    )

    # The ledger line is only unreconciled again if nothing else still claims it.
    if not ReconciliationMatch.all_tenants.filter(
        tenant_id=tenant_id, journal_line_id=line_id
    ).exists():
        JournalLine.all_tenants.filter(pk=line_id).update(reconciled_at=None)


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

def auto_reconcile(
    *,
    tenant_id: uuid.UUID,
    bank_account: BankAccount | uuid.UUID,
    session: Optional[ReconciliationSession] = None,
    threshold: Decimal = AUTO_APPLY_THRESHOLD,
    limit: int = 500,
    user_id: Optional[uuid.UUID] = None,
) -> dict[str, int]:
    """Apply only the matches we are sure about; flag the rest for a human.

    Anything below ``threshold`` is recorded as a *suggestion*
    (``status = SUGGESTED``) and left for review. See the module docstring
    for why the bar is high: an unmatched line is a visible five-minute task,
    a wrongly matched line is an invisible error that closes the period and
    surfaces months later against a customer who already paid.

    Each transaction is matched in its own transaction so that one bad line
    cannot roll back an hour of correct work.
    """
    account = (
        bank_account
        if isinstance(bank_account, BankAccount)
        else BankAccount.all_tenants.filter(tenant_id=tenant_id, pk=bank_account).get()
    )
    stats = {"examined": 0, "matched": 0, "suggested": 0, "skipped": 0}

    pending = BankTransaction.all_tenants.filter(
        tenant_id=tenant_id,
        bank_account=account,
        status__in=[BankTransaction.Status.UNMATCHED, BankTransaction.Status.SUGGESTED],
    ).order_by("transaction_date")[:limit]

    for txn in pending:
        stats["examined"] += 1
        candidates = suggest_matches(txn)
        if not candidates:
            stats["skipped"] += 1
            continue

        best = candidates[0]
        # An ambiguous winner is not a winner. Two candidates at the same
        # confidence means the machine cannot tell them apart, and picking
        # one at random is the failure this whole module is built to avoid.
        runner_up = candidates[1] if len(candidates) > 1 else None
        ambiguous = runner_up is not None and runner_up.confidence >= best.confidence

        if best.confidence >= threshold and not ambiguous:
            try:
                with transaction.atomic():
                    confirm_match(
                        bank_transaction=txn,
                        journal_line=best.journal_line_id,
                        match_type=best.match_type,
                        confidence=best.confidence,
                        session=session,
                        user_id=user_id,
                        notes="auto: " + "; ".join(best.reasons),
                    )
                stats["matched"] += 1
            except ValidationError:
                # Another worker took it, or the residual moved underneath
                # us. Leaving it for the next run is always safe.
                stats["skipped"] += 1
            continue

        if txn.status != BankTransaction.Status.SUGGESTED:
            txn.transition(
                BankTransaction.Status.SUGGESTED, user_id=user_id
            )
        stats["suggested"] += 1

    return stats


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@transaction.atomic
def refresh_session(
    session: ReconciliationSession, *, user_id: Optional[uuid.UUID] = None
) -> ReconciliationSession:
    """Recompute the session's ledger balance, difference and counters."""
    tenant_id = session.tenant_id
    account_id = session.bank_account.ledger_account_id

    totals = JournalLine.all_tenants.filter(
        tenant_id=tenant_id,
        account_id=account_id,
        entry__status=JournalEntry.Status.POSTED,
        entry__entry_date__lte=session.period_end,
    ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    ledger_balance = (totals["debit"] or ZERO) - (totals["credit"] or ZERO)

    txns = BankTransaction.all_tenants.filter(
        tenant_id=tenant_id,
        bank_account=session.bank_account,
        transaction_date__gte=session.period_start,
        transaction_date__lte=session.period_end,
    )
    session.ledger_closing_balance = ledger_balance
    session.difference = session.statement_closing_balance - ledger_balance
    session.matched_count = txns.filter(
        status=BankTransaction.Status.MATCHED
    ).count()
    session.unmatched_count = txns.exclude(
        status__in=[BankTransaction.Status.MATCHED, BankTransaction.Status.IGNORED]
    ).count()
    session.updated_by_id = user_id
    session.save(
        update_fields=[
            "ledger_closing_balance",
            "difference",
            "matched_count",
            "unmatched_count",
            "updated_by",
            "updated_at",
        ]
    )
    if session.difference == ZERO and (
        session.status == ReconciliationSession.Status.IN_PROGRESS
    ):
        session.transition(ReconciliationSession.Status.BALANCED, user_id=user_id)
    return session


@transaction.atomic
def close_session(
    session: ReconciliationSession,
    *,
    user_id: uuid.UUID,
    notes: str = "",
) -> ReconciliationSession:
    """Sign off a reconciliation. Refuses outright when it does not balance.

    There is no ``force=True``. "Close it anyway, we'll find the 12.40 next
    month" is how a trivial discrepancy becomes a permanent one: next month
    the difference has merged with three others, the statement PDF has been
    archived, the person who keyed the payment has moved on, and the only
    remaining option is to write the difference off — which means the books
    now contain a number nobody can explain, and the reconciliation control
    has been converted into a ritual.

    The database enforces the same rule (``ck_recon_session_closed_balanced``)
    so that no future code path, admin action or data fix can close an
    unbalanced session behind this function's back.
    """
    session = (
        ReconciliationSession.all_tenants.filter(pk=session.pk)
        .select_for_update()
        .get()
    )
    refresh_session(session, user_id=user_id)

    if session.difference != ZERO:
        outstanding = session.unmatched_count
        raise UnbalancedSession(
            f"Cannot close reconciliation for {session.bank_account_id}: the "
            f"statement and the ledger differ by {session.difference} "
            f"{session.currency}, with {outstanding} transaction(s) still "
            f"unexplained. Every unit of the difference must be matched, "
            f"posted or explicitly written off *before* closing."
        )

    session.assert_can_transition(ReconciliationSession.Status.CLOSED)
    session.status = ReconciliationSession.Status.CLOSED
    session.closed_at = timezone.now()
    session.closed_by_id = user_id
    if notes:
        session.notes = f"{session.notes}\n{notes}".strip()
    session.updated_by_id = user_id
    session.save(
        update_fields=[
            "status", "closed_at", "closed_by", "notes", "updated_by", "updated_at"
        ]
    )
    return session


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _resolve_line(value: JournalLine | uuid.UUID, tenant_id: uuid.UUID) -> JournalLine:
    if isinstance(value, JournalLine):
        if value.tenant_id != tenant_id:
            raise ValidationError("Journal line belongs to another tenant.")
        return value
    line = JournalLine.all_tenants.filter(tenant_id=tenant_id, pk=value).first()
    if line is None:
        raise ValidationError(f"Journal line {value} not found in this tenant.")
    return line


def _consumed_amounts(
    tenant_id: uuid.UUID, line_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """How much of each ledger line has already been claimed by a match."""
    if not line_ids:
        return {}
    rows = (
        ReconciliationMatch.all_tenants.filter(
            tenant_id=tenant_id, journal_line_id__in=list(line_ids)
        )
        .values("journal_line_id")
        .annotate(total=Sum("matched_amount"))
    )
    return {row["journal_line_id"]: row["total"] or ZERO for row in rows}


def _bank_residual(txn: BankTransaction) -> Decimal:
    claimed = ReconciliationMatch.all_tenants.filter(
        tenant_id=txn.tenant_id, bank_transaction=txn
    ).aggregate(total=Sum("matched_amount"))["total"] or ZERO
    return abs(txn.amount) - claimed


def _line_residual(line: JournalLine, tenant_id: uuid.UUID) -> Decimal:
    gross = line.debit if line.debit > ZERO else line.credit
    claimed = ReconciliationMatch.all_tenants.filter(
        tenant_id=tenant_id, journal_line=line
    ).aggregate(total=Sum("matched_amount"))["total"] or ZERO
    return gross - claimed


def _reference_hit(line: JournalLine, haystack: str) -> str:
    """Find a document number from the ledger side inside the bank narrative.

    Only tokens of four characters or more count. Shorter ones ("12", "AB")
    match by accident constantly, and an accidental reference hit is scored
    at 0.90 — high enough to be auto-applied, which is exactly the mistake we
    are trying not to make.
    """
    for source in (getattr(line.entry, "number", ""), line.description):
        for token in _TOKEN_RE.findall((source or "").lower()):
            if len(token) >= 4 and token not in _NOISE_TOKENS and token in haystack:
                return token
    return ""


def _normalise(text: str) -> str:
    """Fold case, strip accents and punctuation, drop boilerplate tokens.

    Bank narratives are shouted, truncated and transliterated
    ("PMT/ACME TRADING LTD/INV 4471"). Comparing them raw finds nothing;
    comparing the normalised token stream finds the counterparty.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    tokens = [
        token
        for token in _TOKEN_RE.findall(ascii_text.lower())
        if token not in _NOISE_TOKENS
    ]
    return " ".join(tokens)


def _name_similarity(left: str, right: str) -> float:
    """Token-aware similarity in [0, 1].

    ``SequenceMatcher`` alone punishes reordering ("ACME TRADING" vs
    "TRADING, ACME") which banks do constantly, so the token overlap is
    taken as a floor. This is the one place a float is acceptable: it is a
    heuristic score, never a monetary amount, and it is quantised to a
    Decimal before it is ever stored on a match.
    """
    if not left or not right:
        return 0.0
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))
    ratio = SequenceMatcher(None, left, right).ratio()
    return max(overlap, ratio)
