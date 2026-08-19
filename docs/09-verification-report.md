# Verification report — Phase 1

Everything below was **executed**, not reviewed by eye. The commands are
reproducible from a clean checkout.

Environment: Python 3.11.15, Django 5.2.17, PostgreSQL 16.13.

---

## 1. Static verification

| Check | Command | Result |
|---|---|---|
| Every Python file parses | `find . -name '*.py' \| xargs python -m py_compile` | **PASS** — 0 errors |
| No floating-point money | `grep -rn 'FloatField' --include=*.py` | **PASS** — 0 occurrences |
| Django 5.1+ constraint syntax | `grep -rn 'CheckConstraint(check='` | **PASS** — 0 occurrences (all use `condition=`) |
| Permission catalogue is valid JSON | `python -m json.tool config/permissions.json` | **PASS** |
| Permission references resolve | `python scripts/validate_permissions.py` | **PASS** — 187 permissions, 7 roles, 632 role→permission references, 59 scope rules, all resolve |
| `docker-compose.yml` parses | `yaml.safe_load(...)` | **PASS** — 8 services |
| Mermaid diagrams parse | mermaid 11 parser | **PASS** — 20/20 blocks |

## 2. Django system check

```
$ python manage.py check
System check identified no issues (0 silenced).
```

All 13 apps load, every cross-app foreign key resolves, and no reverse
accessor collides.

> **One real defect was caught here.** `sales.InvoiceLine.timesheet_entry` and
> `projects.TimesheetEntry.invoice_line` both modelled the time-to-invoice
> link, from opposite ends. Two columns for one relationship is two sources of
> truth, and the day they disagree an hour gets billed twice. Resolved by
> deleting the `sales` side; the relationship now lives solely on
> `TimesheetEntry`, where the status coupling that must stay consistent with
> it also lives. `line.timesheet_entry` still reads naturally as the reverse
> accessor, so no calling code changed.

## 3. Migrations against a live PostgreSQL 16

```
$ python manage.py migrate
... 23 migrations applied ... OK
```

Resulting physical schema:

| Object | Count |
|---|---|
| Tables | 99 |
| Foreign keys | 467 |
| CHECK constraints | 257 |
| UNIQUE constraints / indexes | 227 |
| Indexes (total) | 1 194 |
| Tables with `FORCE ROW LEVEL SECURITY` | 86 |
| RLS policies | 86 |
| Ledger guard triggers | 5 |

Triggers present: `trg_entry_balanced`, `trg_journal_entry_immutable`,
`trg_journal_entry_no_delete`, `trg_journal_line_immutable`,
`trg_period_locked`.

## 4. Runtime invariant tests

`python scripts/verify_core_invariants.py` — **26/26 passed**

### Double-entry
- Balanced entry posts, allocates a document number, moves the cached balance
- Unbalanced entry raises `UnbalancedEntry` **and writes nothing** (row count
  unchanged — this is what catches a missing `transaction.atomic`)
- A line carrying both debit and credit is rejected
- A line carrying neither is rejected
- A `float` amount is rejected outright by `to_money`
- A single-line entry is rejected (it cannot balance)
- Posting to a non-postable summary account is rejected
- Posting into a `CLOSED` period raises `PeriodClosed`
- Posting to another tenant's account is rejected

### Idempotency and numbering
- The same `idempotency_key` posted twice returns the *same* entry and creates
  no second row
- Document numbers are sequential and gapless

### Immutability
- `JournalEntry.delete()` raises `PermissionDenied`
- Bulk queryset `.delete()` raises
- Raw SQL `DELETE` of a posted entry is blocked **by the database trigger**
- Raw SQL `UPDATE` of a posted line is blocked **by the database trigger**

### Corrections
- `reverse_entry` produces a mirror with the sides swapped, and the pair nets
  the account balance back to where it started
- Reversing an already-reversed entry is rejected
- `void_entry` unwinds the cached balance exactly
- Voiding without a reason is rejected

### Tenant isolation (ORM layer)
- A queryset under tenant A never returns tenant B's rows
- With **no** tenant context a queryset returns nothing — fail-closed, not
  fail-open
- Saving without a tenant context raises

### Money arithmetic
- `allocate()` splits with zero cent leakage across several
  non-evenly-divisible cases (100.00 ÷ 3, 0.05 ÷ 7, 1234.57 by weights 3/5/7/11)
- Thirty separate 0.10 postings leave the ledger **exactly** balanced — the
  test that would fail immediately under floats

### Whole-ledger integrity
- `assert_ledger_balanced()` passes for both tenants

## 5. Database-enforced guarantees

`python scripts/verify_rls_and_triggers.py` — **5/5 passed**

Run as the non-superuser role `erp_app`, because **PostgreSQL superusers
bypass RLS unconditionally**. Testing isolation as `postgres` reports a false
pass, which is precisely how teams ship a system whose isolation has never
actually been exercised. (Our first run did exactly this and appeared to
"leak" — the finding was the test, not the policy.)

- Cross-tenant `SELECT` in raw SQL returns zero rows
- An **unset** `app.current_tenant` exposes nothing (fail-closed)
- `WITH CHECK` blocks writing a row *into* another tenant — the hole that
  exists when a policy specifies only `USING`
- The explicit platform-admin bypass still widens visibility when set
- A `COMMIT` is rejected when an injected line unbalances a posted entry.
  The `INSERT` itself succeeds; the `DEFERRABLE INITIALLY DEFERRED` constraint
  trigger fires at commit, which is why it must be deferred at all — lines are
  inserted one at a time and a non-deferred check would fail on the first one.

---

## Known gaps (Phase 2 work, not defects)

| Gap | Impact |
|---|---|
| No DRF serializers/viewsets yet | The API contract in `06-api-contract.md` is a design, not running code |
| `apps/*/urls.py` and the middleware modules referenced by settings are unwritten | `config/urls.py` will not import until they exist |
| `apps.expenses` has no service layer | Expenses are recorded but do not yet post to the GL |
| Payment gateway network calls are `NotImplementedError` stubs | Deliberate — the interface and webhook idempotency are what Phase 1 owed |
| pytest suite is written but unexecuted | It needs the Phase 2 fixtures and a CI database |
