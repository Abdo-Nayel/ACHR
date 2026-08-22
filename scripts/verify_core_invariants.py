"""
Live verification of the double-entry, idempotency and isolation invariants.

Runs against a REAL PostgreSQL database and asserts the invariants the whole
system rests on. Unlike the pytest suite this is a single self-contained
script, so it can be pointed at a staging database after a deploy:

    DJANGO_SETTINGS_MODULE=config.settings.dev python scripts/verify_core_invariants.py

It is destructive (it creates tenants and posts entries). Never run it against
production.
"""
import os, uuid, django
from decimal import Decimal
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
django.setup()

from django.db import connection, transaction, IntegrityError, InternalError, ProgrammingError
from django.core.exceptions import PermissionDenied, ValidationError
from apps.core.tenancy_context import bind_database_session, tenant_context
from apps.tenancy.models import Tenant
from apps.accounting.models import Account, AccountType, FiscalYear, FiscalPeriod, Journal, JournalEntry, JournalLine
from apps.accounting.services.posting import (
    JournalEntryDraft, LineDraft, post_entry, void_entry, reverse_entry,
    UnbalancedEntry, PeriodClosed, assert_ledger_balanced,
)
from apps.core.fields import to_money, allocate
import datetime as dt

PASS, FAIL = [], []
def check(name, fn):
    try:
        fn(); PASS.append(name); print(f"  PASS  {name}")
    except Exception as e:
        FAIL.append((name, e)); print(f"  FAIL  {name}: {type(e).__name__}: {e}")

def expect_raise(exc, fn):
    try:
        fn()
    except exc:
        return
    except Exception as e:
        raise AssertionError(f"expected {exc.__name__}, got {type(e).__name__}: {e}")
    raise AssertionError(f"expected {exc.__name__}, nothing raised")

# ---- fixtures -------------------------------------------------------------
t1 = Tenant.objects.create(name="Acme", slug="acme", country="EG")
t2 = Tenant.objects.create(name="Globex", slug="globex", country="EG")

def chart(t):
    with tenant_context(t.id):
        cash = Account.objects.create(tenant=t, code="1000", name="Cash", type=AccountType.ASSET, system_key="cash")
        ar   = Account.objects.create(tenant=t, code="1100", name="AR", type=AccountType.ASSET, system_key="ar_control")
        rev  = Account.objects.create(tenant=t, code="4000", name="Revenue", type=AccountType.INCOME, system_key="sales_revenue")
        hdr  = Account.objects.create(tenant=t, code="9999", name="Header", type=AccountType.ASSET, is_postable=False)
        j    = Journal.objects.create(tenant=t, code="GEN", name="General", kind=Journal.Kind.GENERAL, sequence_prefix="JE")
        fy   = FiscalYear.objects.create(tenant=t, name="2026", start_date=dt.date(2026,1,1), end_date=dt.date(2026,12,31))
        p_open = FiscalPeriod.objects.create(tenant=t, fiscal_year=fy, name="2026-08",
                     start_date=dt.date(2026,8,1), end_date=dt.date(2026,8,31))
        p_closed = FiscalPeriod.objects.create(tenant=t, fiscal_year=fy, name="2026-01",
                     start_date=dt.date(2026,1,1), end_date=dt.date(2026,1,31),
                     status=FiscalPeriod.Status.CLOSED)
        return dict(cash=cash, ar=ar, rev=rev, hdr=hdr, j=j, open=p_open, closed=p_closed)

A = chart(t1); B = chart(t2)
D = dt.date(2026, 8, 15)

def draft(amount="1000.00", date=D, key=""):
    d = JournalEntryDraft(journal_code="GEN", entry_date=date, currency="EGP", idempotency_key=key)
    d.debit(A["ar"].id, amount); d.credit(A["rev"].id, amount)
    return d

print("\n=== DOUBLE-ENTRY INVARIANTS ===")
with tenant_context(t1.id):
    def t_balanced():
        e = post_entry(draft("1000.00"), tenant_id=t1.id)
        assert e.status == "posted" and e.total_debit == e.total_credit
        assert e.number.startswith("JE-2026-"), e.number
        A["ar"].refresh_from_db(); assert A["ar"].cached_balance == Decimal("1000.000000"), A["ar"].cached_balance
    check("balanced entry posts, numbers, updates cached balance", t_balanced)

    def t_unbalanced():
        d = JournalEntryDraft(journal_code="GEN", entry_date=D, currency="EGP")
        d.debit(A["ar"].id, "100.00"); d.credit(A["rev"].id, "99.00")
        before = JournalEntry.all_tenants.count()
        expect_raise(UnbalancedEntry, lambda: post_entry(d, tenant_id=t1.id))
        assert JournalEntry.all_tenants.count() == before, "partial write leaked!"
    check("unbalanced entry rejected, writes nothing", t_unbalanced)

    check("line with both debit and credit rejected",
          lambda: expect_raise(ValidationError, lambda: LineDraft(account_id=A["ar"].id, debit="5", credit="5")))
    check("line with neither side rejected",
          lambda: expect_raise(ValidationError, lambda: LineDraft(account_id=A["ar"].id)))
    check("float amount rejected outright",
          lambda: expect_raise(ValidationError, lambda: to_money(0.1 + 0.2)))
    check("single-line entry rejected", lambda: expect_raise(ValidationError, lambda: post_entry(
        JournalEntryDraft(journal_code="GEN", entry_date=D, currency="EGP",
                          lines=[LineDraft(account_id=A["ar"].id, debit="10")]), tenant_id=t1.id)))
    check("posting to a non-postable header account rejected", lambda: expect_raise(ValidationError, lambda: post_entry(
        (lambda d: (d.debit(A["hdr"].id, "5"), d.credit(A["rev"].id, "5"), d)[-1])(
            JournalEntryDraft(journal_code="GEN", entry_date=D, currency="EGP")), tenant_id=t1.id)))
    check("posting into a CLOSED period rejected",
          lambda: expect_raise(PeriodClosed, lambda: post_entry(draft("50.00", dt.date(2026,1,15)), tenant_id=t1.id)))
    check("posting to another tenant's account rejected", lambda: expect_raise(ValidationError, lambda: post_entry(
        (lambda d: (d.debit(B["ar"].id, "5"), d.credit(B["rev"].id, "5"), d)[-1])(
            JournalEntryDraft(journal_code="GEN", entry_date=D, currency="EGP")), tenant_id=t1.id)))

print("\n=== IDEMPOTENCY & NUMBERING ===")
with tenant_context(t1.id):
    def t_idem():
        e1 = post_entry(draft("77.00", key="evt-abc"), tenant_id=t1.id)
        n = JournalEntry.all_tenants.count()
        e2 = post_entry(draft("77.00", key="evt-abc"), tenant_id=t1.id)
        assert e1.id == e2.id, "idempotency returned a different entry"
        assert JournalEntry.all_tenants.count() == n, "duplicate row created"
    check("same idempotency key returns same entry, no duplicate", t_idem)

    def t_gapless():
        nums = [post_entry(draft("1.00"), tenant_id=t1.id).number for _ in range(5)]
        seq = [int(x.split("-")[-1]) for x in nums]
        assert seq == list(range(seq[0], seq[0] + 5)), seq
        assert len(set(nums)) == 5
    check("document numbers are sequential and gapless", t_gapless)

print("\n=== IMMUTABILITY (ORM + DATABASE TRIGGERS) ===")
with tenant_context(t1.id):
    e = post_entry(draft("500.00"), tenant_id=t1.id)
    check("ORM: delete() on posted entry raises",
          lambda: expect_raise(PermissionDenied, lambda: e.delete()))
    check("ORM: queryset bulk delete raises",
          lambda: expect_raise(PermissionDenied, lambda: JournalEntry.objects.all().delete()))
entry_id = e.id


def _raw_in_own_txn(sql, params):
    """Run one raw statement in its **own** top-level transaction, tenant bound.

    The trigger checks cannot share ``tenant_context``'s transaction: the
    balance trigger is ``DEFERRABLE INITIALLY DEFERRED`` and fires at COMMIT,
    and a savepoint RELEASE (which is all a nested ``atomic`` does) is not a
    commit — so a nested block would never trip it. A fresh transaction per
    check also means an immediate BEFORE-trigger abort rolls back cleanly here
    instead of poisoning the rest of the run. The tenant is rebound inside so
    RLS still admits the row.
    """
    with transaction.atomic():
        bind_database_session(t1.id)
        with connection.cursor() as c:
            c.execute(sql, params)


check("DB TRIGGER: raw SQL DELETE of posted entry blocked",
      lambda: expect_raise(Exception, lambda: _raw_in_own_txn(
          "DELETE FROM accounting_journal_entry WHERE id = %s", [str(entry_id)])))

check("DB TRIGGER: raw SQL UPDATE of posted line blocked",
      lambda: expect_raise(Exception, lambda: _raw_in_own_txn(
          "UPDATE accounting_journal_line SET debit = 999999 WHERE entry_id = %s",
          [str(entry_id)])))

check("DB TRIGGER: injecting an unbalancing line blocked",
      lambda: expect_raise(Exception, lambda: _raw_in_own_txn(
          "INSERT INTO accounting_journal_line "
          "(id,tenant_id,entry_id,line_number,account_id,description,debit,credit,"
          " base_debit,base_credit,partner_type,created_at,updated_at) "
          "VALUES (gen_random_uuid(),%s,%s,99,%s,'sneak',5,0,5,0,'',now(),now())",
          [str(t1.id), str(entry_id), str(A["ar"].id)])))

print("\n=== CORRECTIONS ===")
with tenant_context(t1.id):
    def t_reverse():
        orig = post_entry(draft("250.00"), tenant_id=t1.id)
        bal_before = Account.all_tenants.get(pk=A["ar"].pk).cached_balance
        mirror = reverse_entry(orig, reversal_date=D)
        orig.refresh_from_db()
        assert orig.status == "reversed", orig.status
        ol = list(orig.lines.order_by("line_number")); ml = list(mirror.lines.order_by("line_number"))
        assert [(l.debit, l.credit) for l in ol] == [(l.credit, l.debit) for l in ml], "sides not swapped"
        bal_after = Account.all_tenants.get(pk=A["ar"].pk).cached_balance
        assert bal_after == bal_before - Decimal("250.000000"), (bal_before, bal_after)
    check("reverse_entry mirrors sides and nets balance to zero", t_reverse)

    def t_double_reverse():
        o = post_entry(draft("60.00"), tenant_id=t1.id)
        reverse_entry(o, reversal_date=D)
        o.refresh_from_db()
        expect_raise(ValidationError, lambda: reverse_entry(o, reversal_date=D))
    check("reversing an already-reversed entry rejected", t_double_reverse)

    def t_void():
        o = post_entry(draft("40.00"), tenant_id=t1.id)
        b = Account.all_tenants.get(pk=A["ar"].pk).cached_balance
        void_entry(o, reason="keyed twice")
        o.refresh_from_db(); assert o.status == "voided"
        assert Account.all_tenants.get(pk=A["ar"].pk).cached_balance == b - Decimal("40.000000")
    check("void_entry unwinds the cached balance exactly", t_void)
    check("void without a reason rejected", lambda: expect_raise(ValidationError,
          lambda: void_entry(post_entry(draft("11.00"), tenant_id=t1.id), reason="  ")))

print("\n=== TENANT ISOLATION ===")
def t_orm_isolation():
    with tenant_context(t1.id):
        ids1 = set(Account.objects.values_list("id", flat=True))
    with tenant_context(t2.id):
        ids2 = set(Account.objects.values_list("id", flat=True))
    assert ids1 and ids2 and not (ids1 & ids2), "tenants see each other's accounts"
check("ORM manager isolates tenants", t_orm_isolation)

def t_no_context():
    assert Account.objects.count() == 0, "unscoped read returned rows (fail-open!)"
check("no tenant context -> queryset returns nothing (fail-closed)", t_no_context)

def t_no_context_save():
    expect_raise(PermissionDenied, lambda: Account.objects.create(
        code="X", name="X", type=AccountType.ASSET))
check("saving without tenant context raises", t_no_context_save)

def t_rls():
    # Simulate the app role: RLS is FORCEd, so even the table owner is subject.
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("SELECT set_config('app.current_tenant', %s, true)", [str(t1.id)])
            c.execute("SELECT set_config('app.rls_bypass', 'off', true)")
            c.execute("SELECT count(*) FROM accounting_account WHERE tenant_id = %s", [str(t2.id)])
            leaked = c.fetchone()[0]
            assert leaked == 0, f"RLS leaked {leaked} rows from another tenant"
            c.execute("SELECT count(*) FROM accounting_account")
            assert c.fetchone()[0] == len(B and [1]) * 0 + 4, "unexpected visible row count"
check("PostgreSQL RLS blocks cross-tenant reads in raw SQL", t_rls)

def t_rls_insert():
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("SELECT set_config('app.current_tenant', %s, true)", [str(t1.id)])
            c.execute("SELECT set_config('app.rls_bypass', 'off', true)")
            expect_raise(Exception, lambda: c.execute(
                "INSERT INTO accounting_account (id,tenant_id,code,name,type,is_postable,"
                "is_active,system_key,is_reconcilable,cached_balance,created_at,updated_at,description)"
                " VALUES (gen_random_uuid(),%s,'666','Injected','asset',true,true,'',false,0,now(),now(),'')",
                [str(t2.id)]))
check("RLS WITH CHECK blocks writing INTO another tenant", t_rls_insert)

print("\n=== MONEY ARITHMETIC ===")
def t_allocate():
    for total, weights in [("100.00", ["1","1","1"]), ("0.05", ["1","1","1","1","1","1","1"]),
                           ("1234.57", ["3","5","7","11"]), ("10.00", ["1","2"])]:
        parts = allocate(Decimal(total), [Decimal(w) for w in weights], "EGP")
        assert sum(parts) == Decimal(total).quantize(Decimal("0.01")), (total, parts, sum(parts))
check("allocate() splits with zero cent leakage", t_allocate)

def t_no_float_drift():
    with tenant_context(t1.id):
        for _ in range(30):
            post_entry(draft("0.10"), tenant_id=t1.id)
        assert_ledger_balanced(t1.id)
check("30x 0.10 postings leave the ledger exactly balanced", t_no_float_drift)

print("\n=== WHOLE-LEDGER INTEGRITY ===")
check("assert_ledger_balanced(t1) passes", lambda: assert_ledger_balanced(t1.id))
check("assert_ledger_balanced(t2) passes", lambda: assert_ledger_balanced(t2.id))

print("\n" + "="*60)
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
for n, e in FAIL: print(f"  FAILED: {n}\n          {type(e).__name__}: {e}")
raise SystemExit(1 if FAIL else 0)
