# Phase 5 — Make the multi-tenant guarantee enforceable

*The "one database, one schema, multi-tenant" model rests on RLS being on, the
app role being unprivileged, and the ledger triggers being installed. `docs/`
and `apps/core/rls.py` both promised boot-time checks for this. None existed —
`apps/tenancy/checks.py`, `apps/accounting/checks.py` and the `check_rls`
command the Makefile calls were all missing. This phase makes the guarantee
machine-checked instead of asserted in prose.*

## What changed

### Real Django system checks (`deploy=True`)
- **`apps/tenancy/checks.py`**
  - `check_app_role_is_not_privileged` — the runtime role must not be a
    superuser and must not hold `BYPASSRLS` (both skip RLS unconditionally).
  - `check_rls_forced_on_tenant_tables` — every tenant table must have
    `relrowsecurity AND relforcerowsecurity`.
  - `check_rls_policies_present` — warns on a forced table with no policy.
- **`apps/accounting/checks.py`**
  - `check_ledger_triggers_installed` — the five PL/pgSQL guards
    (`trg_entry_balanced`, `trg_journal_entry_immutable`, `trg_journal_entry_no_delete`,
    `trg_journal_line_immutable`, `trg_period_locked`) must exist.

Registered `deploy=True` so they run on `manage.py check --deploy`, in CI, and at
deploy — **but not during `migrate`**, which runs the non-deploy checks as the
owner role, where "not a superuser" would rightly fail. Wired via
`TenancyConfig.ready()` / `AccountingConfig.ready()` importing the modules.

### The tenant-table set is derived, not hand-listed
`tenant_scoped_tables()` returns every table with a `tenant_id` column, straight
from the model registry. On the live schema this yields **exactly** the 99
force-RLS tables — and it includes the seven iam/tenancy tables
(`iam_role`, `iam_api_key`, `iam_tenant_membership`, `tenancy_audit_log`, …) that
carry `tenant_id` without subclassing `TenantScopedModel` and that a
subclass-based derivation would miss. A new tenant table is now covered
automatically; forgetting its RLS fails the check instead of leaking silently.

### `manage.py check_rls` — the command the Makefile already invoked
`apps/tenancy/management/commands/check_rls.py` runs the deploy checks and exits
non-zero on any error (`--strict` also fails on warnings). `make rls-verify` now
works (it was a phantom command, like `seed_system_roles`).

## Verification (PostgreSQL 18)

| Check | Result |
|---|---|
| `check_rls` as **erp_app** | exit 0 — "app role cannot bypass, every tenant table forces RLS, ledger triggers installed" |
| `check_rls` as **postgres** (superuser) | **exit 1** — flags `tenancy.E001` (superuser) + `tenancy.E002` (BYPASSRLS) |
| `tenant_scoped_tables()` vs DB | 99 == 99, zero missing, zero extra |
| `pytest` | **303 passed** (298 + 5 new in `tests/test_rls_checks.py`) |
| `manage.py check` (non-deploy, migrate path) | 0 issues — deploy checks correctly skip |
| OpenAPI schema vs baseline | byte-identical (D2) |

## Note for Phase 6
`config/settings/prod.py` still declares the dead `STARTUP_CHECKS` list naming
`apps.tenancy.checks.assert_*` (nothing reads it). Now that the real checks exist
as `@register(deploy=True)`, Phase 6 removes `STARTUP_CHECKS`.
