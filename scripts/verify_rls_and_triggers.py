"""
Live verification of Row-Level Security and the deferred ledger triggers.

Must connect as a NON-SUPERUSER role: PostgreSQL superusers bypass RLS
unconditionally, so running this as `postgres` reports a false pass.

Runs against a REAL PostgreSQL database and asserts the invariants the whole
system rests on. Unlike the pytest suite this is a single self-contained
script, so it can be pointed at a staging database after a deploy:

    DJANGO_SETTINGS_MODULE=config.settings.dev python scripts/verify_rls_and_triggers.py

It is destructive (it creates tenants and posts entries). Never run it against
production.
"""
import os, django, datetime as dt
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
django.setup()
from django.conf import settings
settings.DATABASES['app'] = dict(settings.DATABASES['default'],
                                 USER='erp_app', PASSWORD='erp')
from django.db import connections
from apps.tenancy.models import Tenant
from apps.accounting.models import Account, JournalEntry

t1, t2 = list(Tenant.objects.order_by('created_at')[:2])
conn = connections['app']
PASS, FAIL = [], []

def check(name, fn):
    try:
        fn(); PASS.append(name); print(f"  PASS  {name}")
    except Exception as e:
        FAIL.append((name, e)); print(f"  FAIL  {name}: {type(e).__name__}: {e}")

print("=== ROW-LEVEL SECURITY (as non-superuser 'erp_app') ===")

def rls_read():
    with conn.cursor() as c:
        c.execute("SELECT set_config('app.current_tenant', %s, false)", [str(t1.id)])
        c.execute("SELECT set_config('app.rls_bypass', 'off', false)")
        c.execute("SELECT count(*) FROM accounting_account WHERE tenant_id = %s", [str(t2.id)])
        leaked = c.fetchone()[0]
        assert leaked == 0, f"leaked {leaked} rows of tenant B"
        c.execute("SELECT count(DISTINCT tenant_id) FROM accounting_account")
        assert c.fetchone()[0] == 1, "more than one tenant visible"
check("cross-tenant SELECT returns zero rows", rls_read)

def rls_no_context():
    with conn.cursor() as c:
        c.execute("SELECT set_config('app.current_tenant', '', false)")
        c.execute("SELECT set_config('app.rls_bypass', 'off', false)")
        c.execute("SELECT count(*) FROM accounting_account")
        n = c.fetchone()[0]
        assert n == 0, f"unset tenant exposed {n} rows (fail-open!)"
check("unset tenant setting exposes nothing (fail-closed)", rls_no_context)

def rls_write_check():
    with conn.cursor() as c:
        c.execute("SELECT set_config('app.current_tenant', %s, false)", [str(t1.id)])
        c.execute("SELECT set_config('app.rls_bypass', 'off', false)")
        try:
            c.execute(
                "INSERT INTO accounting_account (id,tenant_id,code,name,type,is_postable,"
                "is_active,system_key,is_reconcilable,cached_balance,created_at,updated_at,description)"
                " VALUES (gen_random_uuid(),%s,'666','Injected','asset',true,true,'',false,0,now(),now(),'')",
                [str(t2.id)])
        except Exception:
            conn.rollback(); return
        conn.rollback()
        raise AssertionError("WITH CHECK did not block the cross-tenant INSERT")
check("WITH CHECK blocks INSERT into another tenant", rls_write_check)

def rls_bypass_works():
    with conn.cursor() as c:
        c.execute("SELECT set_config('app.current_tenant', '', false)")
        c.execute("SELECT set_config('app.rls_bypass', 'on', false)")
        c.execute("SELECT count(DISTINCT tenant_id) FROM accounting_account")
        assert c.fetchone()[0] >= 2, "platform-admin bypass did not widen visibility"
        c.execute("SELECT set_config('app.rls_bypass', 'off', false)")
check("explicit platform-admin bypass still works", rls_bypass_works)

print("\n=== DEFERRED BALANCE TRIGGER (fires at COMMIT) ===")
def deferred_balance():
    entry = JournalEntry.all_tenants.filter(status='posted', tenant_id=t1.id).first()
    acct = Account.all_tenants.filter(tenant_id=t1.id, is_postable=True).first()
    raw = connections['default']
    raw.set_autocommit(False)
    try:
        with raw.cursor() as c:
            # Slip in a line that unbalances a posted entry. The statement
            # itself must succeed (the trigger is DEFERRED); COMMIT must fail.
            c.execute(
                "INSERT INTO accounting_journal_line "
                "(id,tenant_id,entry_id,line_number,account_id,description,debit,credit,"
                " base_debit,base_credit,partner_type,created_at,updated_at) "
                "VALUES (gen_random_uuid(),%s,%s,99,%s,'sneak',5,0,5,0,'',now(),now())",
                [str(t1.id), str(entry.id), str(acct.id)])
        try:
            raw.connection.commit()
        except Exception:
            raw.connection.rollback(); return          # trigger fired at COMMIT
        raw.connection.rollback()
        raise AssertionError("COMMIT succeeded — the ledger was left unbalanced")
    finally:
        raw.set_autocommit(True)
check("COMMIT rejected when a line unbalances a posted entry", deferred_balance)

print("\n" + "="*58)
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
for n, e in FAIL: print(f"  FAILED: {n}\n          {type(e).__name__}: {e}")
raise SystemExit(1 if FAIL else 0)
