# Phase 0 — Environment, baseline, tooling

## Machine (probed 2026-08-22)

| Thing | State |
|---|---|
| Python | 3.11.9 |
| System PostgreSQL | 18.4 on `127.0.0.1:5432`, `scram-sha-256` auth (credentials not held) |
| Docker | daemon not running — the `make up` path is unavailable |
| Global Python env | Django 5.0.2 (**too old**), no `psycopg`/`celery`/`drf-spectacular`/`decouple`/`structlog`, no venv |

## What Phase 0 set up

- **`.venv`** at repo root, `pip install -r backend/requirements/dev.txt` → Django
  **5.2.17**, psycopg **3.3.4**, DRF, Celery, drf-spectacular, pytest, ruff, mypy.
- **An isolated throwaway PostgreSQL 18 cluster** at `C:\Users\Admin\.achr-localdb`,
  port **5433**, `trust` auth, created with `initdb`. It is deliberately separate from
  the system server on 5432 so nothing here disturbs any other project. Start it with:
  ```
  & "C:\Program Files\PostgreSQL\18\bin\pg_ctl" -D C:\Users\Admin\.achr-localdb -o "-p 5433" start
  ```
- **`backend/.env`** (gitignored) pointing at that cluster. `erp_app` is the runtime
  role; `postgres` owns and migrates.
- `manage.py provision_db_roles` → `erp_app` created, **verified NOSUPERUSER +
  NOBYPASSRLS**.
- `manage.py migrate` (as owner) → all 23 app migrations + Django/token_blacklist apply
  cleanly on PG18.

### Schema shape on PG18 (matches the design)
| Object | Count |
|---|---|
| Tables (public) | 121 |
| Tables with `FORCE ROW LEVEL SECURITY` | 99 |
| RLS policies | 99 |
| Ledger guard triggers | 5 (`trg_entry_balanced`, `trg_journal_entry_immutable`, `trg_journal_entry_no_delete`, `trg_journal_line_immutable`, `trg_period_locked`) |

> PostgreSQL 18 vs the documented 16: no divergence observed. RLS, `FORCE`, the
> deferred balance trigger and `set_config(..., is_local)` all behave as designed.

## Test baseline — **285 passed in 3:00**, run as `erp_app`

`cd backend && pytest -p no:randomly -q` → **285 passed**, 0 failed. The suite genuinely
exercises RLS: `tests/conftest.py` `bind_tenant` calls `bind_database_session()` (not
just `tenant_context`), so it runs under the non-owner role with policies live. This is
the safety net every later phase is measured against.

## Findings recorded during setup (pre-existing, on `main`)

1. **`CreditNoteSerializer` was broken (fixed here).** `apps/sales/serializers.py`
   listed `order_number`, `subject`, `salesperson` in `Meta.fields` and declared a
   `salesperson_name` field — **none of which exist on the `CreditNote` model** (they
   were copy-pasted from `InvoiceSerializer`). Any request to the credit-note endpoint
   raised `ImproperlyConfigured` → **HTTP 500**, and `manage.py spectacular` could not
   generate the schema at all. No test exercised it, so the suite was green despite it.
   Fixed by removing the four phantom field references. This is the one deliberate
   application-source change in Phase 0, justified because the D2 baseline schema cannot
   be captured otherwise and the endpoint had no working behaviour to preserve.

2. **Bug B2 confirmed live.** `python scripts/verify_core_invariants.py` **fails as
   `erp_app`** with `new row violates row-level security policy for table
   "accounting_account"` — because the script relies on `tenant_context()`, which sets
   the ContextVar but never calls `bind_database_session()`, so `app.current_tenant` is
   unset on the session and RLS refuses the insert. It originally "passed" only because
   it was run as a superuser, which bypasses RLS and masks the bug. Phase 1 fixes this;
   after Phase 1 the script should run clean as `erp_app`.

3. **`make schema` (`--fail-on-warn`) is not clean.** Plain generation succeeds (281
   paths captured to `baseline-openapi.json`), but drf-spectacular emits 557 warnings /
   120 guess-errors (health `APIView`s without a `serializer_class`, enum-name
   collisions). Not blocking the baseline; noted for a later pass.

## Tooling added
- `backend/pyproject.toml` — ruff (line-length 100, per CONVENTIONS §9) + mypy +
  django-stubs. No such config existed before, so `make lint`/`make typecheck` ran
  unconfigured.
- `Makefile.mk` → renamed to `Makefile`; fixed line 81 (`run:` recipe was
  space-indented, so `make run` failed with "missing separator").

## Baseline artefacts
- `backend/plans/baseline-openapi.json` — the D2 API-compatibility reference.
- Suite result above — the behavioural reference.

## Lint baseline

`ruff check backend` → **54 issues** (31 unsorted-imports, 14 unused-import, 5
ambiguous-variable-name, 4 module-import-not-at-top). Pre-existing; cleaned
incrementally as each module is refactored, not in one sweep. The CreditNote fix
introduced none.
