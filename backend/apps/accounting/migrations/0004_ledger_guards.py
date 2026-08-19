"""
Ledger guards: PostgreSQL triggers that enforce the accounting invariants
*below* the ORM.

Why triggers at all
===================
``apps.accounting.services.posting.post_entry()`` already checks all of this
in Python, and it is a good check: it produces an error message an accountant
can read ("entry is out of balance by 0.020000 EGP on line 3"). But a Python
check is a *convention*, and conventions in a codebase with a dozen modules
and a five-year lifetime are eventually bypassed:

    JournalLine.objects.bulk_create(...)      # skips save() and signals
    JournalEntry.objects.filter(...).update() # skips save() entirely
    cursor.execute("UPDATE accounting_journal_line SET debit = ...")
    a data migration written at 23:00 during an incident
    a psql session

Every one of those reaches the table without passing through the service. The
triggers below are the layer that no code path can skip, because it lives in
the same place the rows do.

Division of labour, stated once:
    Python check   -> a good error message, fast feedback, testable.
    DB trigger     -> the actual guarantee.
Neither replaces the other.

The five guards
===============

``trg_journal_entry_immutable`` (BEFORE UPDATE)
    A posted entry is history. Editing the amount, date, account or period of
    a posted entry silently changes a report that has already been filed with
    a tax authority — and leaves no trace, because the row simply holds
    different numbers than it did yesterday. Only a small set of columns may
    change after posting: the status (to void/reverse it), the void reason,
    the reversal pointer, the posting stamp, the allocated number, and the
    audit stamp. Everything else is frozen. Corrections are made by posting a
    reversing entry, which is visible.

``trg_journal_entry_no_delete`` (BEFORE DELETE)
    Deleting a posted entry destroys the audit trail *and* leaves a gap in
    the number sequence, which auditors read as evidence of a suppressed
    transaction. Drafts may be deleted: they were never numbered and never
    appeared in a report.

``trg_journal_line_immutable`` (BEFORE UPDATE OR DELETE)
    The entry-level guard is not enough on its own: an attacker or a buggy
    job can leave ``accounting_journal_entry`` untouched and rewrite the
    lines underneath it, or delete one side of a balanced pair. The only
    column that may move on a posted line is ``reconciled_at`` (bank
    reconciliation matches an existing line to a statement; it does not
    change the accounting).

``trg_entry_balanced`` (CONSTRAINT TRIGGER, DEFERRABLE INITIALLY DEFERRED)
    The real double-entry guarantee. See the long note below.

``trg_period_locked`` (BEFORE INSERT)
    Nothing may be created in a closed period. Once a period is closed the
    figures have been reported; a late entry retroactively changes a number
    somebody has already signed.

Why ``trg_entry_balanced`` MUST be deferred
===========================================
Journal lines are inserted one at a time. A two-line entry (Dr 100 / Cr 100)
passes through this state:

    after line 1:  SUM(debit) = 100, SUM(credit) = 0     <- unbalanced
    after line 2:  SUM(debit) = 100, SUM(credit) = 100   <- balanced

An ordinary ``AFTER INSERT`` trigger fires immediately after line 1 and
raises. Every posting in the system would fail, and the workaround people
reach for — disabling the trigger during posting — removes the guarantee
entirely.

``CREATE CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`` moves the
check to ``COMMIT``. The intermediate, transiently-unbalanced states are
invisible to it; it sees only the final state of the transaction. That is
precisely the semantics double-entry bookkeeping needs: the *transaction* must
balance, not each statement within it.

Two consequences worth knowing:
  * the error surfaces at COMMIT, not at the offending INSERT, so the Python
    check earns its keep by pointing at the actual line;
  * ``SET CONSTRAINTS ALL IMMEDIATE`` can be used in tests to get the error
    at the statement instead.

A ``CHECK`` constraint could not do this job: CHECK is per-row and cannot run
an aggregate over sibling rows. ``ck_entry_balanced`` on the entry's
materialised ``total_debit``/``total_credit`` columns (see models.py) is the
complementary half — it proves the totals agree with each other; this trigger
proves the totals agree with the *lines*.
"""

from __future__ import annotations

from django.db import migrations

# ---------------------------------------------------------------------------
# 1. Posted entries are frozen
# ---------------------------------------------------------------------------
# Implemented by diffing ``to_jsonb(OLD)`` against ``to_jsonb(NEW)`` with the
# mutable columns removed, rather than by listing 20 equality tests. The diff
# form is self-maintaining: a column added by a future migration is frozen by
# default, which is the safe direction to fail.
ENTRY_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION accounting_journal_entry_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fn$
DECLARE
    -- Columns that may legitimately change after an entry is posted.
    --   status          void / reverse transitions
    --   void_reason     recorded at the moment of voiding
    --   reversal_of     back-pointer written when the mirror entry is created
    --   posted_at/by    stamped by the DRAFT -> POSTED transition itself
    --   number          allocated at posting time, not before
    --   updated_at/by   audit stamp (Django auto_now writes it on every save)
    mutable_cols CONSTANT text[] := ARRAY[
        'status', 'void_reason', 'reversal_of_id',
        'updated_at', 'updated_by_id',
        'posted_at', 'posted_by_id', 'number'
    ];
    frozen_old jsonb;
    frozen_new jsonb;
BEGIN
    -- Drafts are working documents: edit them freely.
    IF OLD.status = 'draft' THEN
        RETURN NEW;
    END IF;

    frozen_old := to_jsonb(OLD) - mutable_cols;
    frozen_new := to_jsonb(NEW) - mutable_cols;

    IF frozen_old IS DISTINCT FROM frozen_new THEN
        RAISE EXCEPTION
            'Journal entry % (status=%) is immutable; only status/number/'
            'posting metadata may change. Post a reversing entry instead.',
            OLD.id, OLD.status
            USING ERRCODE = '23514',
                  HINT = 'Changed columns: ' ||
                         COALESCE((
                             SELECT string_agg(key, ', ' ORDER BY key)
                             FROM jsonb_each(frozen_new)
                             WHERE value IS DISTINCT FROM frozen_old -> key
                         ), '(row shape changed)');
    END IF;

    -- Terminal states are terminal. Without this a voided entry could be
    -- flipped back to 'posted' and re-enter the books.
    IF OLD.status IN ('voided', 'reversed') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'Journal entry % is % and cannot change status.',
            OLD.id, OLD.status
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS trg_journal_entry_immutable ON accounting_journal_entry;
CREATE TRIGGER trg_journal_entry_immutable
    BEFORE UPDATE ON accounting_journal_entry
    FOR EACH ROW
    EXECUTE FUNCTION accounting_journal_entry_immutable();
"""

# ---------------------------------------------------------------------------
# 2. Posted entries are never deleted
# ---------------------------------------------------------------------------
ENTRY_NO_DELETE_FN = """
CREATE OR REPLACE FUNCTION accounting_journal_entry_no_delete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF OLD.status <> 'draft' THEN
        RAISE EXCEPTION
            'Journal entry % (%) cannot be deleted; void or reverse it.',
            COALESCE(NULLIF(OLD.number, ''), OLD.id::text), OLD.status
            USING ERRCODE = '23514',
                  HINT = 'Deleting a posted entry breaks the audit trail and '
                         'leaves a gap in the document sequence.';
    END IF;
    RETURN OLD;
END;
$fn$;

DROP TRIGGER IF EXISTS trg_journal_entry_no_delete ON accounting_journal_entry;
CREATE TRIGGER trg_journal_entry_no_delete
    BEFORE DELETE ON accounting_journal_entry
    FOR EACH ROW
    EXECUTE FUNCTION accounting_journal_entry_no_delete();
"""

# ---------------------------------------------------------------------------
# 3. Lines of a posted entry are frozen
# ---------------------------------------------------------------------------
LINE_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION accounting_journal_line_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fn$
DECLARE
    -- reconciled_at is the one genuinely post-hoc fact about a line: matching
    -- it to a bank statement records *when we saw the money*, and changes no
    -- accounting figure. updated_at/updated_by_id ride along because Django's
    -- auto_now stamps them on any save; excluding them would make it
    -- impossible to set reconciled_at through the ORM at all.
    mutable_cols CONSTANT text[] := ARRAY[
        'reconciled_at', 'updated_at', 'updated_by_id'
    ];
    v_entry_id uuid;
    v_status text;
    frozen_old jsonb;
    frozen_new jsonb;
BEGIN
    v_entry_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.entry_id ELSE NEW.entry_id END;

    SELECT status INTO v_status
    FROM accounting_journal_entry
    WHERE id = v_entry_id;

    -- Parent already gone: this is the child half of a cascade from a draft
    -- entry's delete, which the entry-level trigger has already authorised.
    IF NOT FOUND THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;

    IF v_status = 'draft' THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'Line % of journal entry % cannot be deleted: the entry is %.',
            OLD.line_number, v_entry_id, v_status
            USING ERRCODE = '23514',
                  HINT = 'Deleting one side of a posted entry unbalances the '
                         'ledger. Reverse the entry instead.';
    END IF;

    frozen_old := to_jsonb(OLD) - mutable_cols;
    frozen_new := to_jsonb(NEW) - mutable_cols;

    IF frozen_old IS DISTINCT FROM frozen_new THEN
        RAISE EXCEPTION
            'Line % of journal entry % is immutable (entry is %); only '
            'reconciled_at may change.',
            OLD.line_number, v_entry_id, v_status
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS trg_journal_line_immutable ON accounting_journal_line;
CREATE TRIGGER trg_journal_line_immutable
    BEFORE UPDATE OR DELETE ON accounting_journal_line
    FOR EACH ROW
    EXECUTE FUNCTION accounting_journal_line_immutable();
"""

# ---------------------------------------------------------------------------
# 4. Debits must equal credits — checked at COMMIT
# ---------------------------------------------------------------------------
ENTRY_BALANCED_FN = """
CREATE OR REPLACE FUNCTION accounting_entry_balanced()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_entry     accounting_journal_entry%ROWTYPE;
    v_debit     numeric(19, 6);
    v_credit    numeric(19, 6);
    v_lines     integer;
BEGIN
    SELECT * INTO v_entry
    FROM accounting_journal_entry
    WHERE id = NEW.entry_id;

    -- Entry deleted later in the same transaction (draft cleanup): nothing
    -- left to balance.
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    -- A draft is allowed to be unbalanced; it is a work in progress and is
    -- excluded from every report. The check bites the moment it is posted,
    -- because the UPDATE that sets status='posted' is itself a statement in
    -- this transaction and the deferred trigger reads the *final* status.
    IF v_entry.status <> 'posted' THEN
        RETURN NULL;
    END IF;

    SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0), COUNT(*)
    INTO v_debit, v_credit, v_lines
    FROM accounting_journal_line
    WHERE entry_id = NEW.entry_id;

    IF v_lines < 2 THEN
        RAISE EXCEPTION
            'Posted journal entry % has % line(s); double entry needs at least 2.',
            COALESCE(NULLIF(v_entry.number, ''), v_entry.id::text), v_lines
            USING ERRCODE = '23514';
    END IF;

    IF v_debit <> v_credit THEN
        RAISE EXCEPTION
            'Posted journal entry % is out of balance: debits %, credits %, '
            'difference %.',
            COALESCE(NULLIF(v_entry.number, ''), v_entry.id::text),
            v_debit, v_credit, (v_debit - v_credit)
            USING ERRCODE = '23514',
                  HINT = 'Every transaction must have equal debits and credits.';
    END IF;

    IF v_debit <= 0 THEN
        RAISE EXCEPTION 'Posted journal entry % has zero value.',
            COALESCE(NULLIF(v_entry.number, ''), v_entry.id::text)
            USING ERRCODE = '23514';
    END IF;

    -- The entry's materialised control totals must agree with its lines.
    -- Without this, a report that trusts total_debit (as the dashboard does)
    -- can disagree with a report that aggregates lines (as the trial balance
    -- does), and the two numbers are impossible to reconcile after the fact.
    IF v_entry.total_debit <> v_debit OR v_entry.total_credit <> v_credit THEN
        RAISE EXCEPTION
            'Journal entry % control totals (% / %) disagree with its lines (% / %).',
            COALESCE(NULLIF(v_entry.number, ''), v_entry.id::text),
            v_entry.total_debit, v_entry.total_credit, v_debit, v_credit
            USING ERRCODE = '23514';
    END IF;

    RETURN NULL;  -- AFTER trigger: return value is ignored
END;
$fn$;

DROP TRIGGER IF EXISTS trg_entry_balanced ON accounting_journal_line;
-- DEFERRABLE INITIALLY DEFERRED: fires once at COMMIT, after every line of
-- the entry exists. A non-deferred AFTER INSERT trigger would fire after the
-- first line and fail every posting in the system.
CREATE CONSTRAINT TRIGGER trg_entry_balanced
    AFTER INSERT OR UPDATE ON accounting_journal_line
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION accounting_entry_balanced();
"""

# ---------------------------------------------------------------------------
# 5. Closed periods reject new entries
# ---------------------------------------------------------------------------
PERIOD_LOCKED_FN = """
CREATE OR REPLACE FUNCTION accounting_period_locked()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_period_status text;
    v_period_name   text;
    v_year_status   text;
BEGIN
    SELECT p.status, p.name, y.status
    INTO v_period_status, v_period_name, v_year_status
    FROM accounting_fiscal_period p
    JOIN accounting_fiscal_year y ON y.id = p.fiscal_year_id
    WHERE p.id = NEW.period_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Journal entry references a non-existent fiscal period.'
            USING ERRCODE = '23503';
    END IF;

    -- Hard close: no new rows at all, draft or posted. A draft created in a
    -- closed period is a trap — it looks harmless until somebody posts it and
    -- a filed period silently changes.
    IF v_period_status = 'closed' OR v_year_status = 'closed' THEN
        RAISE EXCEPTION
            'Fiscal period % is closed; nothing may be posted into it.',
            v_period_name
            USING ERRCODE = '23514',
                  HINT = 'Post the correction into the current open period, '
                         'dated today, referencing the original entry.';
    END IF;

    -- SOFT_CLOSED is deliberately NOT rejected here. It is a permission
    -- question ("may this user still post while the accountant finishes
    -- month-end?"), answered by accounting.period.post_to_soft_closed in
    -- apps.iam, not a data-integrity question the database can decide.
    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS trg_period_locked ON accounting_journal_entry;
CREATE TRIGGER trg_period_locked
    BEFORE INSERT ON accounting_journal_entry
    FOR EACH ROW
    EXECUTE FUNCTION accounting_period_locked();
"""

FORWARD_SQL = "\n".join([
    ENTRY_IMMUTABLE_FN,
    ENTRY_NO_DELETE_FN,
    LINE_IMMUTABLE_FN,
    ENTRY_BALANCED_FN,
    PERIOD_LOCKED_FN,
])

# Drop triggers before functions: a function cannot be dropped while a trigger
# depends on it (without CASCADE, which would silently drop things we did not
# name).
REVERSE_SQL = """
DROP TRIGGER IF EXISTS trg_period_locked ON accounting_journal_entry;
DROP TRIGGER IF EXISTS trg_entry_balanced ON accounting_journal_line;
DROP TRIGGER IF EXISTS trg_journal_line_immutable ON accounting_journal_line;
DROP TRIGGER IF EXISTS trg_journal_entry_no_delete ON accounting_journal_entry;
DROP TRIGGER IF EXISTS trg_journal_entry_immutable ON accounting_journal_entry;

DROP FUNCTION IF EXISTS accounting_period_locked();
DROP FUNCTION IF EXISTS accounting_entry_balanced();
DROP FUNCTION IF EXISTS accounting_journal_line_immutable();
DROP FUNCTION IF EXISTS accounting_journal_entry_no_delete();
DROP FUNCTION IF EXISTS accounting_journal_entry_immutable();
"""


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ("accounting", "0003_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
