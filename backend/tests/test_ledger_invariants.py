"""The ledger's invariants, asserted directly.

Everything else in this product is a subsidiary record whose only durable
effect is a journal entry. If any assertion in this file stops holding, every
number the system has ever reported is suspect — so these tests are the ones
that must never be marked ``xfail`` to get a release out.

The invariants, and what each one is really guarding:

======================================  =========================================
Invariant                               The bug it prevents
======================================  =========================================
debits == credits, or nothing is written A missing ``transaction.atomic`` leaving
                                        half an entry behind
one side per line                       A sign error becoming a silently reversed
                                        entry instead of an error
no floats anywhere near an amount       ``0.1 + 0.2 != 0.3`` in a trial balance
closed periods reject postings          A filed period changing after the fact
posted entries cannot be deleted        An audit trail with a hole in it
void unwinds the balance exactly        A cancelled entry that still moves money
reverse mirrors and nets to zero        A correction that does not correct
idempotency keys collapse retries       A retried webhook posting salary twice
allocation never leaks a minor unit     100.00 split three ways becoming 99.99
document numbers are gapless            A sequence gap read as a deleted invoice
======================================  =========================================

Property-based tests use hypothesis for the arithmetic (where the input space
is infinite and hand-picked examples always miss the awkward one) and plain
examples for the workflow (where the interesting inputs are a small, named
set of states).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from hypothesis import HealthCheck, assume, given, settings as hyp_settings
from hypothesis import strategies as st

from apps.accounting.models import (
    Account,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
)
from apps.accounting.models_sequence import DocumentSequence
from apps.accounting.services.posting import (
    JournalEntryDraft,
    LineDraft,
    PeriodClosed,
    UnbalancedEntry,
    allocate_number,
    assert_ledger_balanced,
    post_entry,
    reverse_entry,
    void_entry,
)
from apps.core.fields import ZERO, allocate, minor_units, quantize_currency, to_money
from apps.core.models import Currency
from tests.conftest import TEST_CURRENCY, ledger_row_counts, make_draft

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_balanced_entry_posts_and_moves_cached_balances(
    tenant, chart_of_accounts, open_period, owner_user
):
    """A balanced entry lands, and both accounts' cached balances move by the
    amount *in the direction their type increases*.

    ``Account.cached_balance`` is a denormalisation maintained by the posting
    service inside the same transaction as the lines. Asserting the movement
    here (rather than only asserting the lines) is what catches a future
    refactor that writes lines without updating the cache — a drift nobody
    notices until the dashboard and the trial balance disagree.
    """
    bank = chart_of_accounts["bank_main"]          # asset, increases on debit
    revenue = chart_of_accounts["sales_revenue"]   # income, increases on credit
    amount = Decimal("1250.75")

    before_bank = Account.all_tenants.get(pk=bank.pk).cached_balance
    before_revenue = Account.all_tenants.get(pk=revenue.pk).cached_balance

    entry = post_entry(
        make_draft(debit_account=bank, credit_account=revenue, amount=amount),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )

    assert entry.status == JournalEntry.Status.POSTED
    assert entry.period_id == open_period.id
    assert entry.total_debit == entry.total_credit == quantize_currency(
        amount, TEST_CURRENCY
    )
    assert entry.number, "A posted entry must carry a document number."
    assert entry.posted_at is not None
    assert entry.lines.count() == 2

    assert Account.all_tenants.get(pk=bank.pk).cached_balance == before_bank + amount
    assert (
        Account.all_tenants.get(pk=revenue.pk).cached_balance == before_revenue + amount
    )
    assert_ledger_balanced(tenant.id)


def test_base_amounts_are_converted_at_the_entry_rate(
    tenant, chart_of_accounts, open_period, owner_user
):
    """``base_debit``/``base_credit`` are stored, not derived, so a later
    correction to the FX table cannot restate filed figures.

    Deliberately posted in a currency that is *not* the tenant's base. It used
    to use ``TEST_CURRENCY`` — the base currency — with a rate of 2.5, which
    asserted that the books can be converted against themselves. That is now
    refused by ``fx.resolve_rate``: a rate other than 1 on a base-currency
    entry is not a conversion, it is an error, and the old shape meant this
    test could pass while the actual foreign-currency path was never executed.
    """
    draft = make_draft(
        debit_account=chart_of_accounts["bank_main"],
        credit_account=chart_of_accounts["sales_revenue"],
        amount=Decimal("100.00"),
        currency=Currency.USD,
    )
    draft.exchange_rate = Decimal("2.5")

    entry = post_entry(draft, tenant_id=tenant.id, user_id=owner_user.id)

    debit_line = entry.lines.get(debit__gt=ZERO)
    assert debit_line.debit == Decimal("100.00")
    assert debit_line.base_debit == Decimal("250.00")


# ---------------------------------------------------------------------------
# Refusals — and the "wrote NOTHING" guarantee
# ---------------------------------------------------------------------------

def test_unbalanced_entry_raises_and_writes_nothing(
    tenant, chart_of_accounts, open_period, owner_user
):
    """The single most important negative test in the repository.

    Asserting only that ``UnbalancedEntry`` is raised would still pass if the
    service had already inserted the entry header and then blown up on the
    lines — which is precisely what a missing ``transaction.atomic`` looks
    like. So the row counts are compared before and after, read through
    ``all_tenants`` so that a row written under some *other* tenant would also
    be caught.
    """
    before = ledger_row_counts(tenant.id)

    draft = JournalEntryDraft(
        journal_code="GEN", entry_date=date.today(), currency=TEST_CURRENCY
    )
    draft.debit(chart_of_accounts["bank_main"].id, Decimal("100.00"))
    draft.credit(chart_of_accounts["sales_revenue"].id, Decimal("99.99"))

    with pytest.raises(UnbalancedEntry):
        post_entry(draft, tenant_id=tenant.id, user_id=owner_user.id)

    assert ledger_row_counts(tenant.id) == before, (
        "A rejected posting left rows behind: post_entry is not atomic."
    )


def test_line_with_both_sides_raises_at_construction():
    """``LineDraft`` validates in ``__post_init__`` — before any database
    round trip, and before the caller can hand the draft anywhere else."""
    with pytest.raises(ValidationError) as exc:
        LineDraft(
            account_id=uuid.uuid4(), debit=Decimal("10.00"), credit=Decimal("10.00")
        )
    assert "exactly one of debit or credit" in str(exc.value)


def test_line_with_neither_side_raises_at_construction():
    with pytest.raises(ValidationError):
        LineDraft(account_id=uuid.uuid4(), debit=ZERO, credit=ZERO)


def test_negative_amount_raises_rather_than_flipping_sides():
    """Direction is carried by which column the amount is in, never by a sign.
    A negative debit is a caller who meant a credit and did not say so."""
    with pytest.raises(ValidationError):
        LineDraft(account_id=uuid.uuid4(), debit=Decimal("-10.00"))


def test_single_line_entry_is_refused(tenant, chart_of_accounts, open_period):
    draft = JournalEntryDraft(
        journal_code="GEN", entry_date=date.today(), currency=TEST_CURRENCY
    )
    draft.debit(chart_of_accounts["bank_main"].id, Decimal("10.00"))
    with pytest.raises(ValidationError):
        post_entry(draft, tenant_id=tenant.id)


def test_posting_to_a_summary_account_is_refused(
    tenant, chart_of_accounts, open_period
):
    """Only leaves may be posted to; a balance on a roll-up is ambiguous."""
    header = Account.all_tenants.filter(
        tenant_id=tenant.id, is_postable=False
    ).first()
    assert header is not None, "The seeded chart should contain summary accounts."

    draft = JournalEntryDraft(
        journal_code="GEN", entry_date=date.today(), currency=TEST_CURRENCY
    )
    draft.debit(header.id, Decimal("10.00"))
    draft.credit(chart_of_accounts["sales_revenue"].id, Decimal("10.00"))

    with pytest.raises(ValidationError) as exc:
        post_entry(draft, tenant_id=tenant.id)
    assert "summary account" in str(exc.value)


# ---------------------------------------------------------------------------
# Money typing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_value",
    [0.1, 1.0, -2.5, float("1e10")],
    ids=["tenth", "one", "negative", "large"],
)
def test_to_money_refuses_floats(bad_value):
    """A float reaching the money layer is a *programming* error upstream —
    some JSON was parsed without ``parse_float=Decimal`` — so it raises rather
    than being absorbed."""
    with pytest.raises(ValidationError) as exc:
        to_money(bad_value)
    assert "Floats are forbidden" in str(exc.value)


def test_to_money_refuses_bool_which_is_an_int_in_disguise():
    """``isinstance(True, int)`` is True in Python. Without the explicit bool
    check, ``to_money(True)`` would quietly become 1.000000."""
    with pytest.raises(ValidationError):
        to_money(True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1.5"), Decimal("1.500000")),
        ("2.345678", Decimal("2.345678")),
        (7, Decimal("7.000000")),
    ],
)
def test_to_money_accepts_decimal_str_int(value, expected):
    assert to_money(value) == expected


def test_float_amount_in_a_line_draft_raises():
    """The refusal is enforced at the boundary the ledger actually uses."""
    with pytest.raises(ValidationError):
        LineDraft(account_id=uuid.uuid4(), debit=0.1)


# ---------------------------------------------------------------------------
# Period locks
# ---------------------------------------------------------------------------

def test_posting_into_a_closed_period_raises(
    tenant, chart_of_accounts, open_period, owner_user
):
    FiscalPeriod.all_tenants.filter(pk=open_period.pk).update(
        status=FiscalPeriod.Status.CLOSED
    )
    before = ledger_row_counts(tenant.id)

    with pytest.raises(PeriodClosed):
        post_entry(
            make_draft(
                debit_account=chart_of_accounts["bank_main"],
                credit_account=chart_of_accounts["sales_revenue"],
                amount=Decimal("10.00"),
            ),
            tenant_id=tenant.id,
            user_id=owner_user.id,
        )

    assert ledger_row_counts(tenant.id) == before


def test_soft_closed_period_refuses_by_default_and_admits_with_the_flag(
    tenant, chart_of_accounts, open_period, owner_user
):
    """SOFT_CLOSED is the month-end window: operations stop posting, the
    accountant does not. The flag is the whole difference between the two."""
    FiscalPeriod.all_tenants.filter(pk=open_period.pk).update(
        status=FiscalPeriod.Status.SOFT_CLOSED
    )

    def _draft():
        return make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("10.00"),
        )

    with pytest.raises(PeriodClosed):
        post_entry(_draft(), tenant_id=tenant.id, user_id=owner_user.id)

    entry = post_entry(
        _draft(),
        tenant_id=tenant.id,
        user_id=owner_user.id,
        allow_soft_closed=True,
    )
    assert entry.status == JournalEntry.Status.POSTED


def test_posting_outside_every_period_names_the_missing_period(
    tenant, chart_of_accounts, open_period
):
    far_future = date(date.today().year + 5, 6, 15)
    with pytest.raises(ValidationError) as exc:
        post_entry(
            make_draft(
                debit_account=chart_of_accounts["bank_main"],
                credit_account=chart_of_accounts["sales_revenue"],
                amount=Decimal("10.00"),
                entry_date=far_future,
            ),
            tenant_id=tenant.id,
        )
    assert "No fiscal period covers" in str(exc.value)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

def test_journal_entry_delete_raises(tenant, chart_of_accounts, open_period, owner_user):
    """Deleting a posted entry destroys the audit trail and silently changes
    reports that have already been filed. ``ImmutableFinancialModel.delete``
    raises; the database trigger in migration 0002 is the backstop."""
    entry = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("42.00"),
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )
    with pytest.raises(PermissionDenied):
        entry.delete()
    assert JournalEntry.all_tenants.filter(pk=entry.pk).exists()


def test_journal_line_delete_raises(tenant, chart_of_accounts, open_period, owner_user):
    entry = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("42.00"),
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )
    line = entry.lines.first()
    with pytest.raises(PermissionDenied):
        line.delete()


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------

def test_void_reverses_the_cached_balance_movement_exactly(
    tenant, chart_of_accounts, open_period, owner_user
):
    """Void keeps the number (no sequence gap) and unwinds the balances to
    the cent — not approximately, exactly."""
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    amount = Decimal("333.33")

    before_bank = Account.all_tenants.get(pk=bank.pk).cached_balance
    before_revenue = Account.all_tenants.get(pk=revenue.pk).cached_balance

    entry = post_entry(
        make_draft(debit_account=bank, credit_account=revenue, amount=amount),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )
    number_before_void = entry.number

    voided = void_entry(entry, reason="keyed twice", user_id=owner_user.id)

    assert voided.status == JournalEntry.Status.VOIDED
    assert voided.number == number_before_void, "Voiding must not release the number."
    assert voided.void_reason == "keyed twice"
    assert Account.all_tenants.get(pk=bank.pk).cached_balance == before_bank
    assert Account.all_tenants.get(pk=revenue.pk).cached_balance == before_revenue


def test_void_requires_a_reason(tenant, chart_of_accounts, open_period, owner_user):
    entry = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("10.00"),
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )
    with pytest.raises(ValidationError):
        void_entry(entry, reason="   ", user_id=owner_user.id)


def test_reverse_produces_a_mirror_that_nets_to_zero(
    tenant, chart_of_accounts, open_period, next_period, owner_user
):
    """A reversal is exactly "swap the sides". The pair must leave every
    account it touched where it found it, and the original must survive
    unchanged — the books show both the error and its fix."""
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    amount = Decimal("987.65")

    before_bank = Account.all_tenants.get(pk=bank.pk).cached_balance

    original = post_entry(
        make_draft(debit_account=bank, credit_account=revenue, amount=amount),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )
    mirror = reverse_entry(
        original,
        reversal_date=next_period.start_date,
        reason="correcting a mis-keyed receipt",
        user_id=owner_user.id,
    )

    original.refresh_from_db()
    assert original.status == JournalEntry.Status.REVERSED
    assert mirror.reversal_of_id == original.id
    assert mirror.period_id == next_period.id

    original_lines = {
        line.account_id: (line.debit, line.credit)
        for line in original.lines.all()
    }
    mirror_lines = {
        line.account_id: (line.debit, line.credit) for line in mirror.lines.all()
    }
    assert original_lines.keys() == mirror_lines.keys()
    for account_id, (debit, credit) in original_lines.items():
        assert mirror_lines[account_id] == (credit, debit), (
            "A reversal line must be the original with its sides swapped."
        )

    # The pair nets to zero on every account.
    assert Account.all_tenants.get(pk=bank.pk).cached_balance == before_bank
    assert_ledger_balanced(tenant.id)


def test_reversing_an_already_reversed_entry_raises(
    tenant, chart_of_accounts, open_period, next_period, owner_user
):
    entry = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("50.00"),
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )
    reverse_entry(entry, reversal_date=next_period.start_date, user_id=owner_user.id)
    entry.refresh_from_db()

    with pytest.raises(ValidationError) as exc:
        reverse_entry(entry, reversal_date=next_period.start_date, user_id=owner_user.id)
    # Either guard is correct: "already reversed", or "only posted entries can
    # be reversed" now that the original has moved to REVERSED.
    assert "revers" in str(exc.value).lower()


def test_reversing_a_draft_or_voided_entry_raises(
    tenant, chart_of_accounts, open_period, owner_user
):
    entry = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("50.00"),
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )
    void_entry(entry, reason="wrong customer", user_id=owner_user.id)
    entry.refresh_from_db()

    with pytest.raises(ValidationError):
        reverse_entry(entry, user_id=owner_user.id)


def test_illegal_status_transitions_are_refused(
    tenant, chart_of_accounts, open_period, owner_user
):
    """``ALLOWED_TRANSITIONS`` is the contract; VOIDED and REVERSED are
    terminal."""
    entry = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("50.00"),
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )
    void_entry(entry, reason="keyed twice", user_id=owner_user.id)
    entry.refresh_from_db()

    for target in (
        JournalEntry.Status.POSTED,
        JournalEntry.Status.DRAFT,
        JournalEntry.Status.REVERSED,
    ):
        with pytest.raises(ValueError):
            entry.assert_can_transition(target)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_same_idempotency_key_returns_the_same_entry_and_one_row(
    tenant, chart_of_accounts, open_period, owner_user
):
    """A retried Celery task, a double-clicked button and a redelivered
    webhook must all be no-ops. ``post_entry`` returns the *original* rather
    than raising, so the caller's happy path stays a happy path."""
    key = "invoice:issue:c0ffee"
    bank = chart_of_accounts["bank_main"]
    revenue = chart_of_accounts["sales_revenue"]
    amount = Decimal("777.00")

    first = post_entry(
        make_draft(
            debit_account=bank, credit_account=revenue, amount=amount,
            idempotency_key=key,
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )
    balance_after_first = Account.all_tenants.get(pk=bank.pk).cached_balance

    second = post_entry(
        make_draft(
            debit_account=bank, credit_account=revenue, amount=amount,
            idempotency_key=key,
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )

    assert second.id == first.id
    assert (
        JournalEntry.all_tenants.filter(
            tenant_id=tenant.id, idempotency_key=key
        ).count()
        == 1
    )
    assert JournalLine.all_tenants.filter(tenant_id=tenant.id, entry=first).count() == 2
    assert Account.all_tenants.get(pk=bank.pk).cached_balance == balance_after_first, (
        "A replayed posting moved the balance a second time."
    )


def test_distinct_idempotency_keys_post_separately(
    tenant, chart_of_accounts, open_period, owner_user
):
    first = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("10.00"),
            idempotency_key="a",
        ),
        tenant_id=tenant.id,
    )
    second = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("10.00"),
            idempotency_key="b",
        ),
        tenant_id=tenant.id,
    )
    assert first.id != second.id


# ---------------------------------------------------------------------------
# Document numbering
# ---------------------------------------------------------------------------

def test_document_numbers_are_gapless_and_unique(
    tenant, chart_of_accounts, open_period, journals, owner_user
):
    """Twenty entries produce ``…-000001`` through ``…-000020`` with nothing
    missing. A gap in a document sequence is read by most tax authorities as
    evidence of a deleted document, which is why the counter is a locked row
    and not a PostgreSQL ``SEQUENCE`` (which is non-transactional and burns a
    number on rollback)."""
    numbers = [
        post_entry(
            make_draft(
                debit_account=chart_of_accounts["bank_main"],
                credit_account=chart_of_accounts["sales_revenue"],
                amount=Decimal("1.00"),
                memo=f"entry {index}",
            ),
            tenant_id=tenant.id,
            user_id=owner_user.id,
        ).number
        for index in range(20)
    ]

    assert len(set(numbers)) == len(numbers), "Duplicate document number."
    suffixes = sorted(int(number.rsplit("-", 1)[1]) for number in numbers)
    assert suffixes == list(range(1, 21)), f"Sequence has a gap: {suffixes}"


def test_number_allocation_under_simulated_concurrency_never_repeats(
    tenant, chart_of_accounts, open_period, journals
):
    """Simulates the interleaving that ``MAX(number) + 1`` gets wrong.

    Real thread-level concurrency against a test transaction is not reliably
    reproducible (pytest-django wraps the test in a transaction the other
    connection cannot see), so this drives the allocator directly, many times,
    interleaved with a second scope. What it proves is the property that
    matters: allocation is a monotonic, gapless, non-repeating counter per
    ``(tenant, scope, year)`` — the invariant the row lock exists to preserve.
    """
    general = journals["GEN"]
    sales = journals["SAL"]
    today = date.today()

    issued: list[str] = []
    for _ in range(25):
        issued.append(allocate_number(tenant.id, general, today))
        # Interleave a different scope: the two counters must not interfere.
        allocate_number(tenant.id, sales, today)

    assert len(set(issued)) == 25
    values = [int(number.rsplit("-", 1)[1]) for number in issued]
    assert values == list(range(1, 26))

    sequence = DocumentSequence.all_tenants.get(
        tenant_id=tenant.id, scope=f"journal:{general.code}", year=today.year
    )
    assert sequence.next_value == 26

    # And a second tenant's counter is untouched by ours: sequences are
    # per (tenant, scope, year), never global.
    assert (
        DocumentSequence.all_tenants.filter(
            scope=f"journal:{general.code}", year=today.year
        ).count()
        == 1
    )


def test_a_rolled_back_posting_returns_its_number(
    tenant, chart_of_accounts, open_period, owner_user
):
    """The gaplessness guarantee, stated as the reason a SEQUENCE was rejected.

    The counter is incremented inside the caller's transaction, so a failure
    after allocation gives the number back. This is the behaviour a raw
    PostgreSQL ``SEQUENCE`` cannot provide.
    """
    first = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("5.00"),
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )

    with pytest.raises(UnbalancedEntry):
        with transaction.atomic():
            bad = JournalEntryDraft(
                journal_code="GEN", entry_date=date.today(), currency=TEST_CURRENCY
            )
            bad.debit(chart_of_accounts["bank_main"].id, Decimal("5.00"))
            bad.credit(chart_of_accounts["sales_revenue"].id, Decimal("4.00"))
            post_entry(bad, tenant_id=tenant.id, user_id=owner_user.id)

    second = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("5.00"),
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )

    first_value = int(first.number.rsplit("-", 1)[1])
    second_value = int(second.number.rsplit("-", 1)[1])
    assert second_value == first_value + 1, "The failed posting burned a number."


# ---------------------------------------------------------------------------
# Property-based: posting
# ---------------------------------------------------------------------------

#: Amounts in the range a real ledger sees, at cent precision. Bounded away
#: from zero (minimum 1.00) for two reasons: a zero line is refused by
#: ``LineDraft`` and is tested by name above, and a total small enough to
#: allocate a zero share to one of six accounts would be filtered out by
#: ``assume`` on most examples, which hypothesis rightly complains about.
cent_amounts = st.integers(min_value=100, max_value=10_000_00).map(
    lambda cents: (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))
)


@hyp_settings(
    max_examples=25,
    deadline=None,
    # Each example writes to the database; the function-scoped fixtures are
    # reused across examples on purpose, and the ledger stays balanced
    # cumulatively, which is exactly the property under test.
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    debit_amounts=st.lists(cent_amounts, min_size=1, max_size=6),
    split=st.integers(min_value=1, max_value=6),
)
def test_any_balanced_multi_line_entry_posts_and_keeps_the_ledger_balanced(
    tenant, chart_of_accounts, open_period, owner_user, debit_amounts, split
):
    """For any set of debits, a credit side constructed to match must post.

    The credit side is built by allocating the debit total across ``split``
    accounts with :func:`apps.core.fields.allocate`, which is the same routine
    production uses — so this test simultaneously asserts that allocation
    cannot leak a minor unit into an unbalanced entry.
    """
    postable = [
        chart_of_accounts[key]
        for key in ("bank_main", "ar_control", "inventory_asset", "cash_on_hand")
    ]
    credit_accounts = [
        chart_of_accounts[key]
        for key in ("sales_revenue", "service_revenue", "ap_control",
                    "output_vat", "payroll_salaries_payable", "share_capital")
    ][:split]
    assume(credit_accounts)

    total = sum(debit_amounts, ZERO)
    weights = [Decimal(1)] * len(credit_accounts)
    credit_amounts = allocate(total, weights, TEST_CURRENCY)
    assume(all(amount > ZERO for amount in credit_amounts))

    draft = JournalEntryDraft(
        journal_code="GEN", entry_date=date.today(), currency=TEST_CURRENCY,
        memo="property test",
    )
    for index, amount in enumerate(debit_amounts):
        draft.debit(postable[index % len(postable)].id, amount)
    for account, amount in zip(credit_accounts, credit_amounts):
        draft.credit(account.id, amount)

    entry = post_entry(draft, tenant_id=tenant.id, user_id=owner_user.id)

    assert entry.status == JournalEntry.Status.POSTED
    assert entry.total_debit == entry.total_credit
    assert entry.total_debit == quantize_currency(total, TEST_CURRENCY)
    assert_ledger_balanced(tenant.id)


# ---------------------------------------------------------------------------
# Property-based: allocation (the cent-leakage guard)
# ---------------------------------------------------------------------------

@given(
    total_cents=st.integers(min_value=1, max_value=1_000_000_00),
    weights=st.lists(
        st.integers(min_value=1, max_value=1000), min_size=1, max_size=12
    ),
)
@hyp_settings(
    max_examples=400,
    deadline=None,
    # This test needs no database at all, but the module-level
    # ``django_db`` mark pulls in the function-scoped ``db`` fixture. The
    # suppression says "yes, we know" rather than letting a health check
    # failure hide the property being asserted.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_allocate_never_leaks_a_minor_unit(total_cents, weights):
    """``sum(allocate(total, weights, ccy)) == total``. Exactly. Always.

    Naive proportional splitting loses or invents minor units — 100.00 across
    three ways becomes 33.33 * 3 = 99.99 — and in a tax allocation or a
    payment application that single cent is an unbalanced journal entry the
    posting service will refuse, at the worst possible moment. The
    largest-remainder method in ``allocate`` exists to make that impossible,
    and this is the test that says so for inputs nobody would think to write
    by hand.
    """
    total = (Decimal(total_cents) / Decimal(100)).quantize(Decimal("0.01"))
    decimal_weights = [Decimal(w) for w in weights]

    parts = allocate(total, decimal_weights, TEST_CURRENCY)

    assert len(parts) == len(decimal_weights)
    assert sum(parts, ZERO) == total, (
        f"Cent leakage: {total} split into {parts} sums to {sum(parts, ZERO)}"
    )
    assert all(part >= ZERO for part in parts), "Allocation produced a negative share."


@pytest.mark.parametrize(
    ("total", "weights", "currency"),
    [
        (Decimal("100.00"), [Decimal(1), Decimal(1), Decimal(1)], "EGP"),
        (Decimal("0.01"), [Decimal(1), Decimal(1)], "EGP"),
        (Decimal("10.00"), [Decimal(1), Decimal(2), Decimal(7)], "EGP"),
        (Decimal("1000"), [Decimal(1), Decimal(1), Decimal(1)], "JPY"),
        (Decimal("100.000"), [Decimal(1), Decimal(1), Decimal(1)], "KWD"),
    ],
    ids=["thirds", "one-cent-two-ways", "uneven", "zero-minor-units", "three-minor-units"],
)
def test_allocate_named_awkward_cases(total, weights, currency):
    """The cases a reviewer will ask about, pinned by name.

    ``JPY`` has no minor unit and ``KWD`` has three; an allocator that assumes
    two would round 1000 JPY into 333.33 and produce an amount the currency
    cannot express.
    """
    parts = allocate(total, weights, currency)
    assert sum(parts, ZERO) == total
    exponent = Decimal(1).scaleb(-minor_units(currency))
    for part in parts:
        assert part == part.quantize(exponent), (
            f"{part} has more precision than {currency} can express."
        )


def test_allocate_across_zero_weight_raises():
    with pytest.raises(ValidationError):
        allocate(Decimal("10.00"), [ZERO, ZERO], TEST_CURRENCY)


# ---------------------------------------------------------------------------
# Whole-ledger integrity
# ---------------------------------------------------------------------------

def test_assert_ledger_balanced_passes_on_an_empty_ledger(tenant, chart_of_accounts):
    """No postings is a balanced ledger, not a missing one. The nightly task
    must not page anybody about a tenant that has not started trading."""
    assert_ledger_balanced(tenant.id)


def test_assert_ledger_balanced_ignores_voided_entries(
    tenant, chart_of_accounts, open_period, owner_user
):
    """It aggregates POSTED lines only; a voided entry's lines survive as
    history and must not be counted."""
    entry = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("15.00"),
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )
    void_entry(entry, reason="duplicate", user_id=owner_user.id)
    assert_ledger_balanced(tenant.id)


@pytest.mark.rls
def test_balance_trigger_rejects_an_unbalanced_entry_written_around_the_service(
    tenant, chart_of_accounts, open_period, owner_user, db_no_rls
):
    """The database is the authority, not ``post_entry``.

    ``migrations/0002_ledger_guards`` installs a DEFERRABLE constraint trigger
    that re-checks ``SUM(debit) == SUM(credit)`` at COMMIT. This test writes
    lines the way a future ``bulk_create`` or a hand-written SQL fix would —
    bypassing the service entirely — and asserts the database still refuses.
    """
    from django.db.utils import InternalError, IntegrityError, ProgrammingError

    entry = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("20.00"),
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )

    with pytest.raises((IntegrityError, InternalError, ProgrammingError)):
        with transaction.atomic():
            JournalLine.all_tenants.create(
                tenant_id=tenant.id,
                entry=entry,
                line_number=99,
                account=chart_of_accounts["bank_main"],
                debit=Decimal("1.00"),
                credit=ZERO,
                base_debit=Decimal("1.00"),
                base_credit=ZERO,
            )
            # The guard is DEFERRABLE INITIALLY DEFERRED, so in production it
            # fires at COMMIT -- deliberately, so that a multi-statement entry
            # is judged once it is whole rather than after its first line.
            #
            # There is no COMMIT to reach here: pytest-django runs each test
            # inside a transaction it rolls back, which makes the `atomic()`
            # above a SAVEPOINT. Without this line the write appears to
            # succeed, the assertion below fails, and the violation instead
            # surfaces as an error during fixture teardown -- attributed to
            # whichever test happens to tear down first.
            #
            # SET CONSTRAINTS ... IMMEDIATE runs the pending check now, which
            # is exactly what COMMIT would do.
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
