# 08 — Testing Strategy

This document says what must be tested before code ships, why the pyramid is
shaped the way it is for an accounting system, and which file guards which
invariant. It is a contract, not advice: the rules in §2 are the ones a
reviewer is expected to enforce.

---

## 1. The pyramid, and why it leans

```
                     ┌───────────────┐
                     │  E2E journeys │      ~1%   (Playwright, planned)
                     ├───────────────┤
                     │  Integration  │      ~25%  payroll → ledger,
                     │  (cross-app)  │           sales → ledger, RLS
                     ├───────────────┤
                     │   Service /   │      ~50%  post_entry, invoice
                     │  state machine│           workflow, stock service
                     ├───────────────┤
                     │  Unit + prop  │      ~24%  money arithmetic,
                     └───────────────┘           allocation, tax slabs
```

Two departures from the usual advice:

**The integration band is unusually wide.** In most products a unit test of a
service is a good proxy for the behaviour users get. Here it is not: the
things that actually break a ledger — a missing `transaction.atomic`, a
constraint that only fires at COMMIT, a period lock that races a close, an
RLS policy that was never applied — are all invisible to a test that mocks the
database. Every test in this suite that touches money runs against a real
PostgreSQL instance with the real migrations, including the triggers in
`accounting/0002_ledger_guards` and the policies in
`tenancy/0002_row_level_security`.

**Mocks are rare, and never used for money.** A mocked `post_entry` proves
nothing: the whole value of that function is the invariant it enforces. The
only sanctioned mocks are external systems (payment gateways, object storage,
email) and, at present, one internal gap — see §6.

---

## 2. What MUST have a test before it ships

Non-negotiable. A pull request touching any of the following without a test is
incomplete, whatever the coverage percentage says.

| Change | Required test |
|---|---|
| **Anything that writes to the ledger** | A balanced-posting test *and* a failure test asserting the row count is unchanged (proves atomicity, not just that an exception was raised) |
| **Any state transition** | One test per legal edge, and one per illegal edge that a user could plausibly attempt. `ALLOWED_TRANSITIONS` is the contract; if it is not asserted, it is decoration |
| **Any permission check** | A positive case, a negative case, and — for anything row-scoped — a case where the actor holds the RBAC permission but the ABAC scope excludes the row |
| **Any new `system_key`** | A seeding test proving `seed_chart_of_accounts` creates it, because the service that resolves it will raise in production if it is absent |
| **Any monetary arithmetic** | A property test, not examples. See §3 |
| **Any new tenant-scoped model** | An isolation test: tenant A must not see tenant B's rows, through the ORM *and* through raw SQL |
| **Any idempotency key** | A replay test: call it twice, assert one row and the same return value |
| **Any bulk `UPDATE`/`DELETE`** | A test that the guard rails (`TenantQuerySet.delete`, `ImmutableFinancialModel.delete`) still hold for everything the statement did not intend to touch |

Two more rules that apply to every test in the repository:

* **Decimal only.** A float literal in test data is a bug in the test. `to_money`
  refuses floats at runtime; a test that passes one is asserting the error path
  by accident.
* **No tolerances on money.** `assert net == gross - deductions`, never
  `assert abs(net - expected) < 0.01`. The database's balance constraint has no
  tolerance, so a test with one is weaker than the system it is testing — and a
  systematic one-cent error is exactly what a tolerance hides until the run is
  fifty thousand payslips long.

---

## 3. Why property-based testing for money

Hand-picked examples test the cases the author thought of. Money arithmetic
fails on the cases nobody thinks of: `100.00` split three ways, `0.01` split
two ways, a currency with zero minor units (JPY) or three (KWD), a weight
vector whose ratios do not divide evenly into cents.

The canonical example is allocation. The naive implementation

```python
[total * w / sum(weights) for w in weights]     # rounds each share independently
```

is correct for almost every input a developer types by hand and loses a cent
on `100.00 / 3`. That cent is not a display nuisance: allocated tax and applied
payments both flow into journal lines, so the missing unit becomes
`sum(debits) != sum(credits)` and `post_entry` refuses the whole transaction —
at the worst possible moment, usually month-end, usually on the largest batch.

So `apps.core.fields.allocate` uses the largest-remainder method, and the test
that guards it is a hypothesis property:

```python
assert sum(allocate(total, weights, currency)) == total
```

asserted over hundreds of generated totals and weight vectors, plus named
parametrised cases for the awkward currencies. The property is the
specification; the examples are documentation of the cases a reviewer will ask
about.

The same reasoning applies to `post_entry` itself (any set of debits with a
matching credit side must post and leave the ledger balanced) and to the
progressive tax scale (a raise must never reduce take-home pay — a monotonicity
property that catches "top rate applied to the whole income" without needing to
know the correct answer).

Where property testing is *not* used: workflows. The interesting inputs to a
state machine are a small named set of states, and generating them randomly
produces slower tests that are harder to read and no more thorough.

---

## 4. How to run the suite

```bash
cd backend

pytest                                  # everything
pytest tests/test_ledger_invariants.py  # one file
pytest -k allocate                      # one concern
pytest -m "not slow"                    # the pre-commit subset
pytest -m rls                           # only the database-policy assertions
pytest -p no:randomly                   # reproduce a failure in declared order
pytest -n auto                          # parallel; each worker gets its own DB
```

With coverage, as CI runs it:

```bash
make test        # pytest -q --cov=backend/apps --cov-fail-under=85 -n auto
make coverage    # HTML report
```

**pytest must be invoked from `backend/`.** The configuration lives in
`backend/setup.cfg`, and pytest resolves its rootdir from the invocation
directory; a run started at the repository root will not find it and will fail
to load the Django settings.

### Prerequisites

* A PostgreSQL instance (`make up` brings one up in docker-compose).
* Migrations are applied by pytest-django into a `test_erp` database.
* **Connect as the non-superuser application role.** `config/settings/dev.py`
  uses `erp_app` deliberately. A superuser bypasses Row-Level Security, so
  every isolation test would pass on a developer's machine and the leak would
  be found in production. If the RLS-marked tests skip, check this first.

### Reading the output

* `SKIPPED [n] ... RLS assertions require PostgreSQL` — the suite ran without
  the database-level guarantees. Acceptable locally, never in CI.
* `pytest-randomly` reorders tests each run. A failure that appears only under
  one seed is almost always leaked state — most often a tenant left bound in a
  `ContextVar` — and the seed is printed at the top of the output so it can be
  reproduced with `-p randomly --randomly-seed=<n>`.

---

## 5. Invariants by file

| File | Invariants it guards | Fails if… |
|---|---|---|
| `tests/conftest.py` | Every test runs inside `tenant_context` **and** with `app.current_tenant` bound; RLS-marked tests skip cleanly off PostgreSQL | Fixtures silently return no rows, or writes fail the RLS `WITH CHECK` |
| `tests/test_ledger_invariants.py` | `debits == credits`; a rejected posting writes **nothing**; one side per line; no floats; period locks (OPEN / SOFT_CLOSED / CLOSED); posted entries are undeletable; void unwinds the cached balance exactly; reverse mirrors and nets to zero; idempotency keys collapse retries; document numbers are gapless and unique; `allocate` never leaks a minor unit | `post_entry` loses its `transaction.atomic`; a rounding change re-introduces cent leakage; the numbering counter is swapped for a PostgreSQL `SEQUENCE` |
| `tests/test_tenant_isolation.py` | Tenant A never sees tenant B's rows through the ORM or through raw SQL; unscoped saves raise; bulk delete raises; `FORCE ROW LEVEL SECURITY` is on; every cache key contains the tenant id | A policy is dropped by a migration; a cache key is "optimised" down to the user id; tests are run as the database owner |
| `tests/test_payroll_integration.py` | `net == gross - deductions` on every payslip and on the run totals; exactly one balanced entry per run; debits == gross + employer contributions; credits == net + tax + insurance + other; exactly-once posting; segregation of duties; marginal (not flat) income tax across bracket boundaries | A deduction is added without being wired into the posting; someone "simplifies" the approver check; the tax loop stops being marginal |
| `tests/test_invoice_workflow.py` | `DRAFT → SENT → PARTIALLY_PAID → PAID` with `amount_due` asserted at each step; illegal transitions refused; the AR debit equals the invoice total; overpayment refused; voiding a paid invoice refused; `refresh_overdue_status` flips only unpaid, past-due invoices and is reversible | A "mark as unpaid" verb appears; `amount_paid` becomes an increment instead of a recomputation; the overdue sweep becomes one-way |

### Fixtures worth knowing about

| Fixture | What it gives you |
|---|---|
| `tenant`, `other_tenant` | Two tenants; the second exists only to prove its rows never leak |
| `bind_tenant` (autouse) | Wraps the test in `tenant_context` and binds `app.current_tenant`. Inert for tests that do not ask for a `tenant` |
| `chart_of_accounts` | Runs the real `seed_chart_of_accounts`, returns `{system_key: Account}`. Look accounts up by role, never by code |
| `open_period`, `next_period` | The fiscal period containing today, and the one after it (where reversals land) |
| `owner_user`, `accountant_user`, `employee_user` | Three actors with different roles — enough to test segregation of duties |
| `db_no_rls` | Runs a block with `app.rls_bypass = on`, so a test can plant another tenant's row and then prove it is invisible |
| `iam_permission_stub` | Supplies `apps.iam.services.permissions.assert_permission`, which the payroll engine imports lazily and which does not yet exist (§6) |

---

## 6. Known gaps

These are gaps in the code under test, recorded here so a reader does not
mistake a workaround in the suite for sloppiness.

| Gap | Effect on the suite |
|---|---|
| `apps.iam.services.permissions.assert_permission` does not exist; `payroll.services.engine.approve_run` imports it lazily | The `iam_permission_stub` fixture supplies a permissive stub so the segregation-of-duties assertion is reachable. Delete the fixture once the real module lands |
| `apps.inventory.services.stock.issue_stock` does not exist; `sales.services.invoice_workflow._release_stock_for_invoice` imports it lazily | Invoice tests and the demo seed use free-text lines (`item=None`). The stock leg of invoice issue is therefore **untested**, and is the first test to add when that function is written |
| `apps.expenses` has no service layer | The demo tenant records an approved expense with no journal entry rather than inventing a posting path. Expense → GL is untested |
| `payroll.PayrollRun` has no "submit for approval" service | `tests/test_payroll_integration.py` applies `CALCULATED → PENDING_APPROVAL` directly, after validating it against the model's own transition map |
| `docs/03-data-model.md` names five payroll accounts without the `payroll_` prefix that `engine.py` actually resolves | `seed_chart_of_accounts` creates one account per role using the literal the engine passes, and re-keys any alias it finds. See `SYSTEM_KEY_ALIASES` in that command |
| `make test` invokes pytest from the repository root | The configuration is at `backend/setup.cfg`; run the suite from `backend/`, or move the target to `cd backend && pytest` |

---

## 7. What a good test looks like here

```python
def test_unbalanced_entry_raises_and_writes_nothing(tenant, chart_of_accounts, open_period):
    before = ledger_row_counts(tenant.id)          # 1. observe the world

    draft = JournalEntryDraft(journal_code="GEN", entry_date=date.today(), currency="EGP")
    draft.debit(bank.id, Decimal("100.00"))        # 2. Decimal, always
    draft.credit(revenue.id, Decimal("99.99"))     # 3. one cent off, deliberately

    with pytest.raises(UnbalancedEntry):           # 4. the specific class,
        post_entry(draft, tenant_id=tenant.id)     #    not bare Exception

    assert ledger_row_counts(tenant.id) == before  # 5. and NOTHING was written
```

Step 5 is the one people leave out. Without it the test passes on an
implementation that inserts the entry header, fails on the lines, and leaves a
posted-looking row with no lines behind — which is what a missing
`transaction.atomic` looks like from the outside, and what a trial balance
looks like the next morning.
