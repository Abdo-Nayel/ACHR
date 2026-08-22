# Phase 1 — The core kernel

*One home for each cross-cutting rule. Highest-leverage phase; everything else
builds on it. Two live bugs fixed, three duplications collapsed, API unchanged.*

## What changed

### 1. `tenant_context()` now binds both tenancy layers — **fixes B2**
`apps/core/tenancy_context.py`: `tenant_context()` opens a transaction and calls
`bind_database_session()` inside it, so it binds *both* the ORM `ContextVar` and
the PostgreSQL session variable `app.current_tenant` that RLS reads. Previously it
bound only the first, so any non-request code (Celery tasks, the reporting
integrity job) read **zero rows** under the non-owner `erp_app` role — silently.
`platform_admin_context()` likewise now actually pushes `app.rls_bypass=on` to the
session (it previously set a `ContextVar` nothing read — a no-op security control).
`config/celery.py` gains a `before_task_publish` hook that stamps the caller's
`tenant_id`/`user_id` onto the message (nothing was writing the header the worker
already read), and the worker hook now restores both ContextVars.

The 8 hand-written `tenant_context(...) + bind_database_session(...)` pairs in
`iam/services/{invitations,signup}.py` and the three seed commands are gone — the
merge made them redundant.

### 2. One exception for illegal transitions — **fixes B1**
`apps/core/exceptions.py`: `IllegalTransitionError` is now
`(DomainError, ValueError)`. An illegal transition *is* a value error (so the
existing `except ValueError` guards still catch it) **and** an HTTP 409 (so it no
longer falls through DRF's handler to a 500). Before, the 18 hand-written guards
raised a bare `ValueError` that mapped to nothing → 500 for a plain user mistake.

### 3. One status state machine — collapses F2, F3, F4
`apps/core/models.py` gains `StatusTransitionMixin` (`ALLOWED_TRANSITIONS`,
`assert_can_transition`, `transition`). Adopted by every non-GL document model
(sales, payments, expenses, hr, payroll, inventory, banking, projects — 18 models).
Deleted: 3 local copies of the mixin (inventory/banking/projects), 12 hand-written
`assert_can_transition` methods, and the `_move`/`_set_status` shape they fed.
**`apps/accounting` is untouched** — `JournalEntry`/`FiscalPeriod` keep their own
methods until Phase 8 (D4), and their tests still pass because
`IllegalTransitionError` is a `ValueError`.

### 4. Smaller dedups
- `_actor_id()` moved to the core viewset base; 4 identical copies deleted
  (expenses ×2, hr, payroll).
- `apps/core/fields.py`: the 3 byte-identical `DecimalField` subclasses collapse
  onto one `_FixedDecimalField` base. `deconstruct()` still pops precision, so the
  migrations are **unchanged** — verified by `makemigrations --check` (no drift).

## Verification (all green, PostgreSQL 18, as `erp_app`)

| Check | Result |
|---|---|
| `pytest` | **291 passed** (285 existing + 6 new `test_status_transitions.py`) |
| `manage.py check` | 0 issues |
| `makemigrations --check --dry-run` | No changes — schema frozen (D1) |
| `scripts/verify_core_invariants.py` | **29/29** (was failing on `main` as `erp_app` — B2) |
| `scripts/verify_rls_and_triggers.py` | **5/5** |
| OpenAPI schema vs baseline | **paths + components byte-identical** (D2) |
| `seed_demo_tenant` end-to-end as `erp_app` | tenant + chart + invoice + payment + expense + payroll all created |

New test `tests/test_status_transitions.py` pins B1: an illegal transition is a
409, is still a `ValueError`, and renders as the `illegal_transition` envelope —
never a 500. B2 is proven by `verify_core_invariants.py` running every DB
operation through `tenant_context` as `erp_app` and passing.

## Notes for later phases
- `scripts/verify_rls_and_triggers.py` needs a seeded DB (it reads existing
  tenants); its trigger/read checks were made robust to session state here.
- The `Makefile` `seed` target calls `seed_system_roles`, which **does not exist**
  (roles are seeded by `seed_permissions`) — a phantom command for Phase 6.
- `apps/iam` transition code (`Invitation.transition`, the god module) is left for
  Phase 4; the GL for Phase 8.
