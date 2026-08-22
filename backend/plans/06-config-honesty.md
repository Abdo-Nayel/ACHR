# Phase 6 — Make the configuration honest

*Config that names modules, tasks and functions which do not exist. Each was a
latent failure: a beat tick that raises, a Sentry event that vanishes, a boot
that crashes. This phase makes every dotted path in `config/` resolve to real
code, guarded by a standing test.*

## What changed

### B3 — the beat scheduler fired six tasks that did not exist
`config/celery.py` scheduled seven periodic tasks; only one
(`nightly_ledger_integrity_check`) was registered. The other six raised
`NotRegistered` on every tick. Resolved by intent:
- **Wired the three whose implementation already existed but had no `tasks.py`:**
  `apps/hr/tasks.py` (`accrue_leave` → `leave.accrue_monthly`),
  `apps/inventory/tasks.py` (`recompute_stock_levels`),
  `apps/sales/tasks.py` (`refresh_overdue_invoices` → `invoice_workflow.refresh_overdue_status`).
  Each is a thin wrapper over the existing service, fanned out per active tenant
  by a new shared helper `apps/core/tenancy_tasks.for_each_tenant` — which runs
  each tenant under `tenant_context` (so RLS is bound, per phase 1) and isolates
  one tenant's failure from the rest.
- **Deleted the three with no implementation** (`send_payment_reminders`,
  `expire_documents_report`, `retry_failed_webhooks`) from the beat schedule,
  along with `task_routes` prefixes for apps that have no `tasks.py`
  (`payments`, `banking`, `payroll`, `notifications`) and the stray
  `task_queues_declared` attribute (not a real Celery setting).

Every remaining beat task now resolves in the Celery registry.

### prod.py — three references to code that does not exist
- **Sentry `before_send`** imported `apps.core.observability.scrub_event` — a
  missing module, so **every production error event raised `ImportError` and was
  dropped**. Wrote `apps/core/observability.py`: a `scrub_event` that redacts
  values under sensitive keys (password, token, salary, iban, tax_id, …) so
  salaries and bank details never reach Sentry, and never returns `None` (which
  would silently drop the incident).
- **Read-replica router** named `apps.reporting.routers.ReadReplicaRouter`, which
  does not exist — a boot crash the moment `POSTGRES_REPLICA_HOST` was set.
  Removed the `DATABASE_ROUTERS` line; a blanket read-router would break
  read-your-writes anyway, so heavy reports opt into the replica explicitly with
  `.using("replica")`. The replica DB alias stays available.
- **`STARTUP_CHECKS`** listed `apps.tenancy.checks.assert_*` functions that never
  existed and was read by nothing. Removed — Phase 5's `@register(deploy=True)`
  checks are the real, running replacement.

### Dead code
Deleted `apps/hr/services/leave.py::year_end_carry_over` (zero callers, zero
references). `receive_stock`/`return_stock` in `inventory/services/fulfilment.py`
were left: unlike `year_end_carry_over` they are re-exported as a coherent
public service pair, so removing them is a product decision, not a cleanup
(the same reason `accrue_monthly` was *wired* rather than deleted).

### Left as-is (deliberately)
- The `MONEY_*` constants duplicated between `settings/base.py` and
  `core/fields.py` **cannot** be single-sourced: settings load before the app
  registry, so `base.py` cannot import `core.fields`. Style nit, not dishonesty.
- The `report`/`reports` throttle scopes are a documented intentional alias.

## Verification (PostgreSQL 18)

| Check | Result |
|---|---|
| `pytest` | **324 passed** (303 + 21 new across 3 new test files) |
| every beat task resolves in the Celery registry | yes (was 1 of 7) |
| scheduled tasks run end to end against a seeded tenant | yes (`test_scheduled_tasks.py`) |
| `test_config_integrity.py` | imports every dotted path in MIDDLEWARE, DRF, LOGGING, celery beat — all resolve |
| `manage.py check` | 0 issues |
| OpenAPI schema vs baseline | byte-identical (D2) |
